"""AT-SPI Registry input synthesis, restricted to an observable X11 session.

Xlib is loaded lazily for display/keymap queries. Registry synthesis handles most
events; XTEST supplies horizontal wheel buttons omitted by Registry's X11 adapter.
Wayland global coordinates/input are deliberately unsupported.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import ctypes.util
import math
import os
import time

from backend_common import BackendError
from input_actions import INPUT_KINDS, finite_number, validate_input_action


KEY_NAMES = {
    "enter": "Return", "tab": "Tab", "escape": "Escape", "backspace": "BackSpace",
    "delete": "Delete", "insert": "Insert", "home": "Home", "end": "End",
    "page_up": "Prior", "page_down": "Next", "left": "Left", "right": "Right",
    "up": "Up", "down": "Down", "space": "space", "-": "minus", "=": "equal",
    "[": "bracketleft", "]": "bracketright", "\\": "backslash", ";": "semicolon",
    "'": "apostrophe", ",": "comma", ".": "period", "/": "slash", "`": "grave",
    **{f"f{i}": f"F{i}" for i in range(1, 13)},
}
MODIFIER_NAMES = {"ctrl": "Control_L", "alt": "Alt_L", "shift": "Shift_L", "meta": "Super_L"}


def rectangle(value):
    """Reject absent, offscreen sentinel, and nonsensical native extents."""
    try:
        values = [getattr(value, key) for key in ("x", "y", "width", "height")]
    except AttributeError:
        values = value if isinstance(value, (list, tuple)) and len(value) == 4 else []
    if len(values) != 4 or not all(finite_number(item) for item in values) or min(values[2:]) <= 0:
        return None
    return dict(zip(("x", "y", "width", "height"), values))


class _ScreenInfo(ctypes.Structure):
    _fields_ = [("screen_number", ctypes.c_int), ("x_org", ctypes.c_short),
                ("y_org", ctypes.c_short), ("width", ctypes.c_short), ("height", ctypes.c_short)]


class _KeyboardState(ctypes.Structure):
    _fields_ = [("group", ctypes.c_ubyte), ("locked_group", ctypes.c_ubyte),
                ("base_group", ctypes.c_ushort), ("latched_group", ctypes.c_ushort),
                *[(name, ctypes.c_ubyte) for name in ("mods", "base_mods", "latched_mods", "locked_mods",
                  "compat_state", "grab_mods", "compat_grab_mods", "lookup_mods", "compat_lookup_mods")],
                ("ptr_buttons", ctypes.c_ushort)]


class X11Observation:
    """One worker-owned X connection; no keymap changes or shared global handles."""
    def __init__(self, display_name):
        library = ctypes.util.find_library("X11")
        if not library:
            raise BackendError("unsupported", "X11 observation library is unavailable.")
        self.lib = ctypes.CDLL(library)
        self.display = None
        self.xinerama = None
        self.xtest = None
        self.horizontal_available = False
        ptr, integer, ulong = ctypes.c_void_p, ctypes.c_int, ctypes.c_ulong
        bindings = {
            "XOpenDisplay": ([ctypes.c_char_p], ptr), "XCloseDisplay": ([ptr], integer),
            "XDefaultScreen": ([ptr], integer), "XDefaultRootWindow": ([ptr], ulong),
            "XDisplayWidth": ([ptr, integer], integer), "XDisplayHeight": ([ptr, integer], integer),
            "XQueryExtension": ([ptr, ctypes.c_char_p, *[ctypes.POINTER(integer)] * 3], integer),
            "XQueryPointer": ([ptr, ulong, ctypes.POINTER(ulong), ctypes.POINTER(ulong),
                               *[ctypes.POINTER(integer)] * 4, ctypes.POINTER(ctypes.c_uint)], integer),
            "XStringToKeysym": ([ctypes.c_char_p], ulong),
            "XKeysymToKeycode": ([ptr, ulong], ctypes.c_ubyte),
            "XkbKeysymToModifiers": ([ptr, ulong], ctypes.c_uint),
            "XkbGetState": ([ptr, ctypes.c_uint, ctypes.POINTER(_KeyboardState)], integer),
            "XFree": ([ptr], integer),
            "XSync": ([ptr, integer], integer),
        }
        for name, (arguments, result) in bindings.items():
            function = getattr(self.lib, name)
            function.argtypes, function.restype = arguments, result
        self.display = self.lib.XOpenDisplay(display_name.encode())
        if not self.display:
            raise BackendError("unsupported", "Cannot connect to the current X11 display.")
        try:
            for extension in (b"XTEST", b"XKEYBOARD"):
                a, b, c = integer(), integer(), integer()
                if not self.lib.XQueryExtension(self.display, extension, ctypes.byref(a), ctypes.byref(b), ctypes.byref(c)):
                    raise BackendError("unsupported", "X11 input requires XTEST and XKEYBOARD support.")
            library = ctypes.util.find_library("Xinerama")
            if library:
                self.xinerama = ctypes.CDLL(library)
                self.xinerama.XineramaIsActive.argtypes = [ptr]
                self.xinerama.XineramaIsActive.restype = integer
                self.xinerama.XineramaQueryScreens.argtypes = [ptr, ctypes.POINTER(integer)]
                self.xinerama.XineramaQueryScreens.restype = ctypes.POINTER(_ScreenInfo)
            library = ctypes.util.find_library("Xtst")
            if library:
                self.xtest = ctypes.CDLL(library)
                self.xtest.XTestFakeButtonEvent.argtypes = [ptr, ctypes.c_uint, integer, ulong]
                self.xtest.XTestFakeButtonEvent.restype = integer
                self.xtest.XTestFakeMotionEvent.argtypes = [ptr, integer, integer, integer, ulong]
                self.xtest.XTestFakeMotionEvent.restype = integer
                self.horizontal_available = True
        except Exception:
            self.close()
            raise

    def observe(self):
        displays = []
        if self.xinerama and self.xinerama.XineramaIsActive(self.display):
            count = ctypes.c_int()
            screens = self.xinerama.XineramaQueryScreens(self.display, ctypes.byref(count))
            try:
                if screens and 0 < count.value <= 128:
                    for index in range(count.value):
                        screen = screens[index]
                        bounds = rectangle((screen.x_org, screen.y_org, screen.width, screen.height))
                        if bounds and bounds not in displays:
                            displays.append(bounds)
            finally:
                if screens:
                    self.lib.XFree(screens)
        else:
            screen = self.lib.XDefaultScreen(self.display)
            bounds = rectangle((0, 0, self.lib.XDisplayWidth(self.display, screen),
                                self.lib.XDisplayHeight(self.display, screen)))
            if bounds:
                displays.append(bounds)
        root, child = ctypes.c_ulong(), ctypes.c_ulong()
        x, y, local_x, local_y, mask = ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_uint()
        known = self.lib.XQueryPointer(self.display, self.lib.XDefaultRootWindow(self.display),
            ctypes.byref(root), ctypes.byref(child), ctypes.byref(x), ctypes.byref(y),
            ctypes.byref(local_x), ctypes.byref(local_y), ctypes.byref(mask))
        pointer = {"x": x.value, "y": y.value} if known else None
        return displays, pointer

    def keycode(self, name):
        symbol = self.lib.XStringToKeysym(name.encode("ascii"))
        code = self.lib.XKeysymToKeycode(self.display, symbol) if symbol else 0
        if not code:
            raise BackendError("unsupported", "Requested key is not mapped on the current X11 keyboard.")
        return int(code)

    def modifier_mask(self, modifiers):
        mask = 0
        for modifier in modifiers:
            symbol = self.lib.XStringToKeysym(MODIFIER_NAMES[modifier].encode("ascii"))
            current = self.lib.XkbKeysymToModifiers(self.display, symbol) if symbol else 0
            if not current:
                raise BackendError("unsupported", "Requested modifier is not mapped on the current X11 keyboard.")
            mask |= current
        return mask

    def locked_modifiers(self):
        state = _KeyboardState()
        if self.lib.XkbGetState(self.display, 0x100, ctypes.byref(state)) != 0:  # XkbUseCoreKbd
            raise BackendError("unsupported", "Cannot inspect X11 keyboard modifier state.")
        return state.locked_mods

    def horizontal_button(self, button, pressed, x=None, y=None):
        if not self.horizontal_available or button not in (6, 7):
            raise BackendError("unsupported", "XTEST horizontal wheel synthesis is unavailable.")
        # Move and press on the same X connection so server ordering does not
        # depend on Registry's separate connection flushing its motion events.
        if pressed and not self.xtest.XTestFakeMotionEvent(self.display, self.lib.XDefaultScreen(self.display),
                                                          math.floor(x), math.floor(y), 0):
            raise BackendError("unsupported", "XTEST declined positioning the horizontal wheel event.")
        accepted = self.xtest.XTestFakeButtonEvent(self.display, button, int(pressed), 0)
        self.lib.XSync(self.display, False)
        if not accepted:
            raise BackendError("unsupported", "XTEST declined a horizontal wheel event.")

    def close(self):
        if self.display:
            self.lib.XCloseDisplay(self.display)
            self.display = None


class LinuxInput:
    def __init__(self, api, *, x11=None, environment=None, sleep=time.sleep):
        self.api, self.x11, self.sleep = api, x11, sleep
        self.reason = None
        environment = os.environ if environment is None else environment
        self.is_wayland = bool(environment.get("WAYLAND_DISPLAY") or environment.get("XDG_SESSION_TYPE", "").lower() == "wayland")
        if self.is_wayland:
            self.reason = "Generic global input and screen coordinates are unavailable on Wayland."
        elif not environment.get("DISPLAY"):
            self.reason = "An X11 DISPLAY is required for native input."
        elif not all(callable(getattr(api.Registry, name, None)) for name in ("generateKeyboardEvent", "generateMouseEvent")):
            self.reason = "AT-SPI Registry input synthesis is unavailable."
        elif not all(hasattr(api, name) for name in ("KEY_STRING", "KEY_PRESS", "KEY_RELEASE", "KEY_LOCKMODIFIERS", "KEY_UNLOCKMODIFIERS")):
            self.reason = "AT-SPI Registry keyboard synthesis constants are unavailable."
        elif self.x11 is None:
            try:
                self.x11 = X11Observation(environment["DISPLAY"])
            except Exception as error:
                self.reason = str(error) if isinstance(error, BackendError) else "X11 display/keymap observation is unavailable."

    def status(self, identified):
        capabilities = dict.fromkeys(INPUT_KINDS, False)
        info = {"coordinate_space": "screen", "units": "pixels", "displays": [], "pointer": None,
                "backend": "AT-SPI Registry / X11", "scroll_axes": []}
        if not self.reason and self.x11:
            info["scroll_axes"] = ["vertical"]
            if self.x11.horizontal_available:
                info.update(backend="AT-SPI Registry + XTEST / X11", scroll_axes=["vertical", "horizontal"])
        reason = self.reason
        if not reason and self.x11:
            try:
                displays, pointer = self.x11.observe()
                info.update(displays=displays, pointer=pointer)
            except Exception:
                reason = "X11 display geometry is unavailable; pointer input is disabled."
            capabilities.update(type_text=identified, key_press=identified)
            for kind in ("mouse_move", "mouse_click", "mouse_drag", "scroll"):
                capabilities[kind] = identified and bool(info["displays"])
            if not info["displays"] and not reason:
                reason = "X11 display bounds were not observed; pointer input is disabled."
        if not identified:
            reason = reason or "No unique foreground AT-SPI window was identified."
        if reason:
            info["unavailable_reason"] = reason
        return capabilities, info

    def _keyboard(self, code, text, kind):
        # pyatspi versions may discard Atspi's boolean result and return None.
        if self.api.Registry.generateKeyboardEvent(code, text, kind) is False:
            raise BackendError("unsupported", "AT-SPI declined keyboard event synthesis.")

    def _mouse(self, x, y, name):
        # AT-SPI accepts integer screen pixels. Floor keeps a validated fractional
        # point inside its half-open display instead of rounding past the edge.
        if self.api.Registry.generateMouseEvent(math.floor(x), math.floor(y), name) is False:
            raise BackendError("unsupported", "AT-SPI declined pointer event synthesis.")

    @contextmanager
    def _held(self, press, release, before_press=None):
        if before_press:
            before_press()
        try:
            # Register cleanup before pressing: a transport error may follow an
            # already delivered press/lock request.
            press()
            yield
        finally:
            try:
                release()
            except Exception:
                # Releases/unlocks are idempotent; retry only cleanup, never a press.
                release()

    def act(self, action, state, verify_foreground):
        action = validate_input_action(action, state)
        if self.reason or not self.x11:
            raise BackendError("unsupported", self.reason or "X11 input is unavailable.")
        kind = action["kind"]
        # Registry silently ignores buttons 6/7. Reject before a mixed-axis
        # partial dispatch if the explicit XTEST extension is unavailable.
        if kind == "scroll" and action["delta_x"] and not self.x11.horizontal_available:
            raise BackendError("unsupported", "Horizontal wheel input requires the X11 Xtst library.")
        code = self.x11.keycode(KEY_NAMES.get(action["key"], action["key"])) if kind == "key_press" else None
        desired = self.x11.modifier_mask(action.get("modifiers", []))
        # Preserve any modifier locks the user already had before this request.
        mask = desired & ~self.x11.locked_modifiers() if desired else 0
        verify_foreground()

        def move(x, y):
            verify_foreground()
            self._mouse(x, y, "abs")

        try:
            with self._held(lambda: self._keyboard(mask, None, self.api.KEY_LOCKMODIFIERS) if mask else None,
                            lambda: self._keyboard(mask, None, self.api.KEY_UNLOCKMODIFIERS) if mask else None,
                            verify_foreground if mask else None):
                if kind == "type_text":
                    verify_foreground()
                    self._keyboard(0, action["text"], self.api.KEY_STRING)
                elif kind == "key_press":
                    with self._held(lambda: self._keyboard(code, None, self.api.KEY_PRESS),
                                    lambda: self._keyboard(code, None, self.api.KEY_RELEASE), verify_foreground):
                        pass
                elif kind == "mouse_move":
                    move(action["x"], action["y"])
                elif kind == "mouse_click":
                    button = {"left": 1, "middle": 2, "right": 3}[action["button"]]
                    # Registry interprets either coordinate == -1 as "do not
                    # move" for button events. Move separately so observed
                    # negative screen coordinates still select the right point.
                    move(action["x"], action["y"])
                    for _ in range(action["count"]):
                        with self._held(lambda: self._mouse(-1, -1, f"b{button}p"),
                                        lambda: self._mouse(-1, -1, f"b{button}r"), verify_foreground):
                            pass
                elif kind == "mouse_drag":
                    button = {"left": 1, "middle": 2, "right": 3}[action["button"]]
                    x, y = action["from_x"], action["from_y"]
                    move(x, y)
                    with self._held(lambda: self._mouse(x, y, f"b{button}p"),
                                    lambda: self._mouse(-1, -1, f"b{button}r"), verify_foreground):
                        steps = max(2, math.ceil(action["duration_ms"] / 20))
                        for step in range(1, steps + 1):
                            self.sleep(action["duration_ms"] / steps / 1000)
                            x = action["from_x"] + (action["to_x"] - action["from_x"]) * step / steps
                            y = action["from_y"] + (action["to_y"] - action["from_y"]) * step / steps
                            move(x, y)
                else:
                    move(action["x"], action["y"])
                    button = 5 if action["delta_y"] > 0 else 4
                    for _ in range(abs(action["delta_y"])):
                        with self._held(lambda: self._mouse(action["x"], action["y"], f"b{button}p"),
                                        lambda: self._mouse(-1, -1, f"b{button}r"), verify_foreground):
                            pass
                    button = 7 if action["delta_x"] > 0 else 6
                    for _ in range(abs(action["delta_x"])):
                        with self._held(lambda: self.x11.horizontal_button(button, True, action["x"], action["y"]),
                                        lambda: self.x11.horizontal_button(button, False), verify_foreground):
                            pass
        except Exception as error:
            raise BackendError("action_outcome_unknown", "AT-SPI input or cleanup failed; inspect the actual result before retrying.") from error
        return {"accepted": True, "kind": kind}

    def close(self):
        if self.x11:
            self.x11.close()
