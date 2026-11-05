"""Windows SendInput adapter; native DLLs load only when instantiated.

UI Automation and this adapter run on the same worker thread. INPUT uses fixed
Windows integer widths even when these structures are tested on another OS.
SendInput acceptance is not application completion; callers must observe again.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes as C
import math
import time

from backend_common import BackendError
from input_actions import INPUT_KINDS, validate_input_action


DWORD, WORD, LONG, ULONG_PTR = C.c_uint32, C.c_uint16, C.c_int32, C.c_size_t


class MOUSEINPUT(C.Structure):
    _fields_ = [("dx", LONG), ("dy", LONG), ("mouseData", DWORD),
                ("dwFlags", DWORD), ("time", DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(C.Structure):
    _fields_ = [("wVk", WORD), ("wScan", WORD), ("dwFlags", DWORD),
                ("time", DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(C.Structure):
    _fields_ = [("uMsg", DWORD), ("wParamL", WORD), ("wParamH", WORD)]


class INPUT_UNION(C.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(C.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", DWORD), ("data", INPUT_UNION)]


class POINT(C.Structure):
    _fields_ = [("x", LONG), ("y", LONG)]


class RECT(C.Structure):
    _fields_ = [("left", LONG), ("top", LONG), ("right", LONG), ("bottom", LONG)]


KEYUP, UNICODE, EXTENDED = 0x0002, 0x0004, 0x0001
MOVE, ABSOLUTE, VIRTUALDESK = 0x0001, 0x8000, 0x4000
WHEEL, HWHEEL = 0x0800, 0x1000
BUTTONS = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}
MODIFIER_VKS = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10, "meta": 0x5B}
KEY_VKS = {**{c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz0123456789"},
           **{f"f{i}": 0x6F + i for i in range(1, 13)},
           "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC,
           ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF, "`": 0xC0,
           "space": 0x20, "enter": 0x0D, "tab": 0x09, "escape": 0x1B,
           "backspace": 0x08, "delete": 0x2E, "insert": 0x2D, "home": 0x24,
           "end": 0x23, "page_up": 0x21, "page_down": 0x22,
           "left": 0x25, "right": 0x27, "up": 0x26, "down": 0x28}
EXTENDED_VKS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B}


def keyboard(vk=0, scan=0, flags=0):
    event = INPUT(type=1)
    event.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    return event


def key(vk, up=False):
    return keyboard(vk, flags=(EXTENDED if vk in EXTENDED_VKS else 0) | (KEYUP if up else 0))


def mouse(flags, x=0, y=0, data=0):
    event = INPUT(type=0)
    event.mi = MOUSEINPUT(x, y, data & 0xFFFFFFFF, flags, 0, 0)
    return event


class Win32API:
    """Small ctypes boundary. No COM initialization or UI thread changes."""

    def __init__(self):
        if not hasattr(C, "WinDLL"):
            raise BackendError("unsupported", "Windows SendInput is unavailable on this host.")
        self.user = C.WinDLL("user32", use_last_error=True)
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self.advapi = C.WinDLL("advapi32", use_last_error=True)
        self.monitor_callback = C.WINFUNCTYPE(C.c_int, C.c_void_p, C.c_void_p,
                                            C.POINTER(RECT), C.c_ssize_t)
        signatures = [
            (self.user, "SendInput", [C.c_uint, C.POINTER(INPUT), C.c_int], C.c_uint),
            (self.user, "GetForegroundWindow", [], C.c_void_p),
            (self.user, "GetWindowThreadProcessId", [C.c_void_p, C.POINTER(DWORD)], DWORD),
            (self.user, "GetAsyncKeyState", [C.c_int], C.c_int16),
            (self.user, "GetPhysicalCursorPos", [C.POINTER(POINT)], C.c_int),
            (self.user, "GetSystemMetrics", [C.c_int], C.c_int),
            (self.user, "EnumDisplayMonitors", [C.c_void_p, C.POINTER(RECT), self.monitor_callback, C.c_ssize_t], C.c_int),
            (self.user, "OpenInputDesktop", [DWORD, C.c_int, DWORD], C.c_void_p),
            (self.user, "CloseDesktop", [C.c_void_p], C.c_int),
            (self.kernel, "OpenProcess", [DWORD, C.c_int, DWORD], C.c_void_p),
            (self.kernel, "GetCurrentProcess", [], C.c_void_p),
            (self.kernel, "CloseHandle", [C.c_void_p], C.c_int),
            (self.advapi, "OpenProcessToken", [C.c_void_p, DWORD, C.POINTER(C.c_void_p)], C.c_int),
            (self.advapi, "GetTokenInformation", [C.c_void_p, C.c_int, C.c_void_p, DWORD, C.POINTER(DWORD)], C.c_int),
            (self.advapi, "GetSidSubAuthorityCount", [C.c_void_p], C.POINTER(C.c_ubyte)),
            (self.advapi, "GetSidSubAuthority", [C.c_void_p, DWORD], C.POINTER(DWORD)),
        ]
        for dll, name, args, result in signatures:
            fn = getattr(dll, name)
            fn.argtypes, fn.restype = args, result
        self._dpi = getattr(self.user, "SetThreadDpiAwarenessContext", None)
        if self._dpi is not None:
            self._dpi.argtypes, self._dpi.restype = [C.c_void_p], C.c_void_p

    @contextmanager
    def physical_coordinates(self):
        previous = self._dpi(C.c_void_p(-4)) if self._dpi else None
        if not previous and self._dpi:
            previous = self._dpi(C.c_void_p(-3))
        if not previous:
            raise BackendError("unsupported", "Physical screen coordinates require per-monitor DPI awareness.")
        try:
            yield
        finally:
            self._dpi(previous)

    def foreground_pid(self):
        pid = DWORD()
        hwnd = self.user.GetForegroundWindow()
        if hwnd:
            self.user.GetWindowThreadProcessId(hwnd, C.byref(pid))
        return pid.value

    def pressed(self, vk):
        return bool(self.user.GetAsyncKeyState(vk) & 0x8000)

    def _token(self, process):
        token = C.c_void_p()
        if not self.advapi.OpenProcessToken(process, 0x0008, C.byref(token)):
            raise OSError("Cannot query process token")
        try:
            size = DWORD()
            self.advapi.GetTokenInformation(token, 25, None, 0, C.byref(size))
            if not 0 < size.value <= 65536:
                raise OSError("Cannot query token integrity")
            buffer = C.create_string_buffer(size.value)
            if not self.advapi.GetTokenInformation(token, 25, buffer, size.value, C.byref(size)):
                raise OSError("Cannot query token integrity")
            sid = C.cast(buffer, C.POINTER(C.c_void_p))[0]
            count = self.advapi.GetSidSubAuthorityCount(sid)
            if not count or not count[0]:
                raise OSError("Invalid token integrity SID")
            level = self.advapi.GetSidSubAuthority(sid, count[0] - 1)[0]
            ui_access = DWORD()
            if not self.advapi.GetTokenInformation(token, 26, C.byref(ui_access), C.sizeof(ui_access), C.byref(size)):
                raise OSError("Cannot query token UI access")
            return level, bool(ui_access.value)
        finally:
            self.kernel.CloseHandle(token)

    def permission_reason(self, pid):
        desktop = self.user.OpenInputDesktop(0, False, 0x0081)  # READOBJECTS | WRITEOBJECTS
        if not desktop:
            return "input_desktop_unavailable"
        self.user.CloseDesktop(desktop)
        process = self.kernel.OpenProcess(0x1000, False, pid) if pid else None
        if not process:
            return "foreground_process_unavailable"
        try:
            own_level, own_ui_access = self._token(self.kernel.GetCurrentProcess())
            target_level, _ = self._token(process)
            if target_level > own_level and not own_ui_access:
                return "foreground_integrity_level_blocks_input"
        except OSError:
            return "foreground_input_permission_unknown"
        finally:
            self.kernel.CloseHandle(process)
        return None

    def geometry(self):
        displays = []
        with self.physical_coordinates():
            @self.monitor_callback
            def collect(_monitor, _dc, rect, _data):
                r = rect.contents
                if r.right > r.left and r.bottom > r.top:
                    displays.append({"x": r.left, "y": r.top, "width": r.right-r.left, "height": r.bottom-r.top})
                return 1
            if not self.user.EnumDisplayMonitors(None, None, collect, 0):
                raise BackendError("unsupported", "Windows display geometry is unavailable.")
        point = POINT()
        pointer = {"x": point.x, "y": point.y} if self.user.GetPhysicalCursorPos(C.byref(point)) else None
        return sorted(displays, key=lambda d: (d["x"], d["y"], d["width"], d["height"])), pointer

    def virtual_screen(self):
        with self.physical_coordinates():
            return tuple(self.user.GetSystemMetrics(index) for index in (76, 77, 78, 79))

    def send(self, events):
        array = (INPUT * len(events))(*events)
        C.set_last_error(0)
        inserted = int(self.user.SendInput(len(array), array, C.sizeof(INPUT)))
        return inserted, C.get_last_error()


class WindowsInput:
    """Input sequencing with injected native API for tests on non-Windows hosts."""

    def __init__(self, *, _api=None, _sleep=time.sleep):
        self.api = None
        self.reason = None
        self.sleep = _sleep
        try:
            self.api = _api if _api is not None else Win32API()
        except (BackendError, OSError, AttributeError):
            self.reason = "windows_input_api_unavailable"

    def snapshot(self, pid):
        info = {"coordinate_space": "screen", "units": "pixels", "displays": [],
                "pointer": None, "backend": "Windows SendInput"}
        caps = dict.fromkeys(INPUT_KINDS, False)
        reason = self.reason
        if self.api is not None:
            reason = self.api.permission_reason(pid)
            if reason is None:
                caps.update(type_text=True, key_press=True)
            try:
                info["displays"], info["pointer"] = self.api.geometry()
                if reason is None and info["displays"]:
                    caps.update(mouse_move=True, mouse_click=True, mouse_drag=True, scroll=True)
                elif not info["displays"]:
                    reason = reason or "physical_display_geometry_unavailable"
            except (BackendError, OSError):
                reason = reason or "physical_display_geometry_unavailable"
        if reason:
            info["unavailable_reason"] = reason
        return info, caps

    def act(self, action, state):
        action = validate_input_action(action, state)
        if self.api is None:
            raise BackendError("unsupported", "Windows native input is unavailable.")
        pid, kind = action["pid"], action["kind"]
        reason = self.api.permission_reason(pid)
        if reason:
            raise BackendError("unsupported", "Windows input is unavailable for this foreground app.", {"reason": reason})
        if self.api.foreground_pid() != pid:
            raise BackendError("element_unavailable", "The foreground app changed before input dispatch.")
        # Do not release keys or buttons a person was already holding.
        guarded = {0x01, 0x02, 0x04, 0x10, 0x11, 0x12, 0x5B, 0x5C}
        if kind == "key_press":
            guarded.add(KEY_VKS[action["key"]])
        if any(self.api.pressed(vk) for vk in guarded):
            raise BackendError("unsupported", "A key or mouse button is already held; observe again after it is released.")
        held = {}
        dispatched = False
        failure = None

        def release_for(event):
            if event.type == 1:
                k = event.ki
                token = (1, k.wVk, k.wScan, k.dwFlags & ~KEYUP)
                return token, keyboard(k.wVk, k.wScan, k.dwFlags | KEYUP), bool(k.dwFlags & KEYUP)
            for down, up in BUTTONS.values():
                if event.mi.dwFlags & (down | up):
                    return (0, down), mouse(up), bool(event.mi.dwFlags & up)
            return None

        def send(events):
            nonlocal dispatched
            if self.api.foreground_pid() != pid:
                raise BackendError("element_unavailable", "The foreground app changed during input dispatch.",
                                   {"action_outcome_unknown": dispatched})
            # Keep all potentially pressed inputs until the whole batch succeeds;
            # an exception or partial native insertion still reaches cleanup.
            for event in events:
                item = release_for(event)
                if item and not item[2]:
                    held[item[0]] = item[1]
            dispatched = True
            inserted, error = self.api.send(events)
            if inserted != len(events):
                raise BackendError("unsupported" if inserted == 0 else "backend_unavailable",
                                   "Windows did not insert every input event; UIPI or desktop restrictions may block input.",
                                   {"inserted": inserted, "requested": len(events), "winerror": error,
                                    "action_outcome_unknown": True})
            for event in events:
                item = release_for(event)
                if item and item[2]:
                    held.pop(item[0], None)

        def move(x, y):
            left, top, width, height = self.api.virtual_screen()
            if width <= 0 or height <= 0:
                raise BackendError("unsupported", "Virtual desktop geometry is unavailable.")
            # SendInput maps 0 and 65535 to the first and last virtual pixels.
            x, y = math.floor(x), math.floor(y)
            if not left <= x < left + width or not top <= y < top + height:
                raise BackendError("element_unavailable", "The display geometry changed before input dispatch.")
            nx = round((x-left) * 65535 / max(1, width-1))
            ny = round((y-top) * 65535 / max(1, height-1))
            return mouse(MOVE | ABSOLUTE | VIRTUALDESK, nx, ny)

        modifiers = [MODIFIER_VKS[m] for m in action.get("modifiers", [])]
        try:
            if kind == "type_text":
                encoded = action["text"].encode("utf-16-le")
                events = []
                for index in range(0, len(encoded), 2):
                    unit = int.from_bytes(encoded[index:index+2], "little")
                    events.extend((keyboard(scan=unit, flags=UNICODE), keyboard(scan=unit, flags=UNICODE | KEYUP)))
                send(events)
            elif kind == "key_press":
                vk = KEY_VKS[action["key"]]
                send([key(m) for m in modifiers] + [key(vk), key(vk, True)] +
                     [key(m, True) for m in reversed(modifiers)])
            elif kind == "mouse_move":
                send([move(action["x"], action["y"])])
            elif kind == "mouse_click":
                down, up = BUTTONS[action["button"]]
                events = [key(m) for m in modifiers] + [move(action["x"], action["y"])]
                for _ in range(action["count"]):
                    events.extend((mouse(down), mouse(up)))
                send(events + [key(m, True) for m in reversed(modifiers)])
            elif kind == "scroll":
                events = [move(action["x"], action["y"])]
                if action["delta_y"]:
                    events.append(mouse(WHEEL, data=-120 * action["delta_y"]))
                if action["delta_x"]:
                    events.append(mouse(HWHEEL, data=120 * action["delta_x"]))
                send(events)
            else:
                down, up = BUTTONS[action["button"]]
                send([key(m) for m in modifiers] + [move(action["from_x"], action["from_y"]), mouse(down)])
                steps = max(2, action["duration_ms"] // 20)
                for index in range(1, steps + 1):
                    self.sleep(action["duration_ms"] / 1000 / steps)
                    fraction = index / steps
                    send([move(action["from_x"] + (action["to_x"]-action["from_x"]) * fraction,
                               action["from_y"] + (action["to_y"]-action["from_y"]) * fraction)])
                send([mouse(up)] + [key(m, True) for m in reversed(modifiers)])
        except Exception as exc:
            failure = exc if isinstance(exc, BackendError) else BackendError(
                "backend_unavailable", "Windows input dispatch failed.", {"action_outcome_unknown": dispatched})
        finally:
            if held:
                releases = list(reversed(list(held.values())))
                try:
                    released, _ = self.api.send(releases)  # Release even if foreground changed.
                    if released != len(releases):
                        raise OSError("Release insertion was incomplete")
                except Exception:
                    failure = BackendError("backend_unavailable", "Windows could not confirm releasing held input.",
                                           {"action_outcome_unknown": True, "release_unconfirmed": True})
        if failure:
            raise failure
        return {"accepted": True, "kind": kind, "pid": pid}
