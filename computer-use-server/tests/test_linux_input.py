"""X11 input contracts with fake Registry/Xlib; no native imports or UI events."""
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from backend_common import BackendError
from input_actions import INPUT_KINDS
from linux_input import LinuxInput, X11Observation, rectangle


class FakeX11:
    def __init__(self):
        self.displays = [{"x": -100, "y": 0, "width": 500, "height": 300}]
        self.pointer = {"x": 1, "y": 2}
        self.locked = 0
        self.keys = []
        self.closed = False
        self.horizontal_available = True
        self.horizontal_events = []
        self.horizontal_held = set()
        self.horizontal_failure = False

    def observe(self):
        return self.displays, self.pointer

    def keycode(self, key):
        self.keys.append(key)
        return 38

    def modifier_mask(self, modifiers):
        return sum({"ctrl": 4, "shift": 1, "alt": 8, "meta": 64}[name] for name in modifiers)

    def locked_modifiers(self):
        return self.locked

    def horizontal_button(self, button, pressed, x=None, y=None):
        self.horizontal_events.append((button, pressed, x, y))
        if pressed:
            self.horizontal_held.add(button)
            if self.horizontal_failure:
                raise BackendError("unsupported", "XTEST dispatch failed")
        else:
            self.horizontal_held.discard(button)

    def close(self):
        self.closed = True


class FakeRegistry:
    def __init__(self):
        self.events = []
        self.failure = lambda event: False
        self.held_keys, self.held_buttons = set(), set()
        self.locked = 0
        self.pointer = (100, 100)
        self.click_positions = []

    def generateKeyboardEvent(self, code, text, kind):
        event = ("key", code, text, kind)
        self.events.append(event)
        if kind == "press":
            self.held_keys.add(code)
        elif kind == "release":
            self.held_keys.discard(code)
        elif kind == "lock":
            self.locked |= code
        elif kind == "unlock":
            self.locked &= ~code
        if self.failure(event):
            raise RuntimeError("Injected failure after a possibly delivered event")
        # Real pyatspi Registry wrappers return None, not Atspi's bool.
        return None

    def generateMouseEvent(self, x, y, name):
        if name.startswith("b") and name[1] not in "12345":
            raise AssertionError("Upstream AT-SPI X11 does not support this button")
        event = ("mouse", x, y, name)
        self.events.append(event)
        if name == "abs" or (name.startswith("b") and x != -1 and y != -1):
            self.pointer = (x, y)
        if name.endswith("p"):
            self.held_buttons.add(name[:2])
            self.click_positions.append(self.pointer)
        elif name.endswith("r"):
            self.held_buttons.discard(name[:2])
        if self.failure(event):
            raise RuntimeError("Injected pointer dispatch failure")
        return None


class LinuxInputTests(unittest.TestCase):
    def setUp(self):
        self.registry, self.x11 = FakeRegistry(), FakeX11()
        self.api = types.SimpleNamespace(Registry=self.registry, KEY_STRING="string", KEY_PRESS="press",
            KEY_RELEASE="release", KEY_LOCKMODIFIERS="lock", KEY_UNLOCKMODIFIERS="unlock")
        self.sleeps = []
        self.driver = LinuxInput(self.api, x11=self.x11, environment={"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"}, sleep=self.sleeps.append)

    def state(self):
        caps, info = self.driver.status(True)
        return {"frontmost_app": {"pid": 42}, "capabilities": caps, "input": info}

    def act(self, kind, **fields):
        return self.driver.act({"kind": kind, "pid": 42, **fields}, self.state(), lambda: None)

    def error(self, code, fn):
        with self.assertRaises(BackendError) as raised:
            fn()
        self.assertEqual(raised.exception.code, code)

    def test_wayland_never_loads_x11_or_advertises_global_input(self):
        with patch("linux_input.X11Observation") as native:
            for environment in ({"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"},
                                {"DISPLAY": ":0", "XDG_SESSION_TYPE": "wayland"}):
                driver = LinuxInput(self.api, environment=environment)
                caps, info = driver.status(True)
                self.assertEqual(caps, dict.fromkeys(INPUT_KINDS, False))
                self.assertIn("Wayland", info["unavailable_reason"])
                self.assertEqual(info["displays"], [])
                self.assertIsNone(info["pointer"])
            native.assert_not_called()

    def test_missing_bounds_disables_only_pointer_capabilities(self):
        self.x11.displays = []
        caps, info = self.driver.status(True)
        self.assertTrue(caps["key_press"] and caps["type_text"])
        self.assertFalse(any(caps[kind] for kind in ("mouse_move", "mouse_click", "mouse_drag", "scroll")))
        self.assertEqual(info["displays"], [])
        self.assertIn("bounds", info["unavailable_reason"])
        self.error("unsupported", lambda: self.act("mouse_move", x=0, y=0))
        self.assertEqual(self.registry.events, [])

    def test_no_foreground_or_registry_constants_disables_all_input(self):
        caps, _ = self.driver.status(False)
        self.assertFalse(any(caps.values()))
        del self.api.KEY_RELEASE
        with patch("linux_input.X11Observation") as native:
            driver = LinuxInput(self.api, environment={"DISPLAY": ":0"})
            self.assertFalse(any(driver.status(True)[0].values()))
            native.assert_not_called()

    def test_unicode_string_uses_registry_string_synthesis_without_clipboard(self):
        value = "한글 🙂 é\n"
        self.assertTrue(self.act("type_text", text=value)["accepted"])
        self.assertEqual(self.registry.events, [("key", 0, value, "string")])

    def test_chord_uses_mapped_hardware_key_and_current_modifier_masks(self):
        self.act("key_press", key="enter", modifiers=["ctrl", "meta"])
        self.assertEqual(self.x11.keys, ["Return"])
        self.assertEqual(self.registry.events, [("key", 68, None, "lock"), ("key", 38, None, "press"),
                                               ("key", 38, None, "release"), ("key", 68, None, "unlock")])
        self.assertFalse(self.registry.held_keys or self.registry.locked)

    def test_existing_modifier_lock_is_preserved(self):
        self.x11.locked = self.registry.locked = 4
        self.act("key_press", key="/", modifiers=["ctrl", "alt"])
        self.assertEqual(self.x11.keys, ["slash"])
        self.assertEqual(self.registry.events[0], ("key", 8, None, "lock"))
        self.assertEqual(self.registry.locked, 4)

    def test_partial_key_or_modifier_press_failure_still_releases(self):
        for failure_kind in ("press", "lock"):
            with self.subTest(failure_kind=failure_kind):
                self.registry.events.clear()
                self.registry.failure = lambda event: event[-1] == failure_kind
                self.error("action_outcome_unknown", lambda: self.act("key_press", key="a", modifiers=["ctrl"]))
                self.assertFalse(self.registry.held_keys or self.registry.locked)
                self.assertEqual(self.registry.events[-1], ("key", 4, None, "unlock"))

    def test_cleanup_failure_retries_release_then_unlocks_even_if_release_keeps_failing(self):
        self.registry.failure = lambda event: event[-1] == "release"
        self.error("action_outcome_unknown", lambda: self.act("key_press", key="f12", modifiers=["shift"]))
        self.assertEqual(sum(event[-1] == "release" for event in self.registry.events), 2)
        self.assertFalse(self.registry.held_keys or self.registry.locked)

    def test_mouse_click_counts_and_fractional_coordinates_stay_in_bounds(self):
        self.act("mouse_click", x=399.9, y=299.9, button="right", count=3)
        self.assertEqual(self.registry.events, [("mouse", 399, 299, "abs")] + [("mouse", -1, -1, "b3p"), ("mouse", -1, -1, "b3r")] * 3)
        self.assertFalse(self.registry.held_buttons)

    def test_click_coordinate_negative_one_is_not_confused_with_registry_sentinel(self):
        for x in (-1, -0.25):
            with self.subTest(x=x):
                self.registry.pointer = (100, 100)
                self.act("mouse_click", x=x, y=20)
                self.assertEqual(self.registry.click_positions[-1], (-1, 20))

    def test_drag_moves_over_duration_and_releases_at_final_point(self):
        self.act("mouse_drag", from_x=0, from_y=0, to_x=100, to_y=80, duration_ms=100, modifiers=["alt"])
        self.assertAlmostEqual(sum(self.sleeps), 0.1)
        self.assertIn(("mouse", 100, 80, "abs"), self.registry.events)
        self.assertIn(("mouse", -1, -1, "b1r"), self.registry.events)
        self.assertFalse(self.registry.held_buttons or self.registry.locked)

    def test_drag_dispatch_failure_still_releases_button_and_modifiers(self):
        self.registry.failure = lambda event: event[0] == "mouse" and event[-1] == "abs" and event[1] > 0
        self.error("action_outcome_unknown", lambda: self.act("mouse_drag", from_x=0, from_y=0,
            to_x=100, to_y=80, duration_ms=100, modifiers=["ctrl"]))
        self.assertFalse(self.registry.held_buttons or self.registry.locked)
        self.assertEqual(self.registry.events[-1], ("key", 4, None, "unlock"))

    def test_foreground_change_during_drag_stops_movement_but_releases(self):
        foreground = [42]
        self.driver.sleep = lambda _delay: foreground.__setitem__(0, 99)
        def verify():
            if foreground[0] != 42:
                raise BackendError("element_unavailable", "Foreground changed")
        self.error("action_outcome_unknown", lambda: self.driver.act({"kind": "mouse_drag", "pid": 42,
            "from_x": 0, "from_y": 0, "to_x": 100, "to_y": 80, "duration_ms": 100,
            "modifiers": ["ctrl"]}, self.state(), verify))
        self.assertEqual([event[-1] for event in self.registry.events], ["lock", "abs", "b1p", "b1r", "unlock"])
        self.assertEqual(self.registry.events[-2], ("mouse", -1, -1, "b1r"))
        self.assertFalse(self.registry.held_buttons or self.registry.locked)

    def test_foreground_is_rechecked_between_repeated_clicks(self):
        def verify():
            if any(event[-1] == "b1r" for event in self.registry.events):
                raise BackendError("element_unavailable", "Foreground changed")
        self.error("action_outcome_unknown", lambda: self.driver.act({"kind": "mouse_click", "pid": 42,
            "x": 10, "y": 20, "count": 3}, self.state(), verify))
        self.assertEqual([event[-1] for event in self.registry.events], ["abs", "b1p", "b1r"])

    def test_scroll_wheel_directions_and_steps(self):
        self.act("scroll", x=10, y=20, delta_x=0, delta_y=-2)
        self.assertEqual([event[-1] for event in self.registry.events], ["abs", "b4p", "b4r", "b4p", "b4r"])
        self.assertFalse(self.registry.held_buttons)

    def test_unsupported_horizontal_scroll_cannot_silently_succeed_or_partially_scroll(self):
        self.x11.horizontal_available = False
        self.error("unsupported", lambda: self.act("scroll", x=10, y=20, delta_x=2, delta_y=1))
        self.assertEqual(self.registry.events, [])
        self.assertEqual(self.state()["input"]["scroll_axes"], ["vertical"])

    def test_horizontal_scroll_uses_xtest_buttons_and_releases(self):
        self.act("scroll", x=10, y=20, delta_x=-2, delta_y=1)
        self.assertEqual(self.x11.horizontal_events, [(6, True, 10, 20), (6, False, None, None)] * 2)
        self.assertEqual([event[-1] for event in self.registry.events], ["abs", "b5p", "b5r"])
        self.assertFalse(self.x11.horizontal_held)
        self.assertEqual(self.state()["input"]["scroll_axes"], ["vertical", "horizontal"])
        self.assertIn("XTEST", self.state()["input"]["backend"])

    def test_xtest_partial_failure_still_releases_horizontal_button(self):
        self.x11.horizontal_failure = True
        self.error("action_outcome_unknown", lambda: self.act("scroll", x=10, y=20, delta_x=1, delta_y=0))
        self.assertEqual(self.x11.horizontal_events, [(7, True, 10, 20), (7, False, None, None)])
        self.assertFalse(self.x11.horizontal_held)

    def test_xtest_native_boundary_positions_press_and_syncs_without_moving_release(self):
        observer = X11Observation.__new__(X11Observation)
        observer.display, observer.horizontal_available = "test-display", True
        events = []
        observer.lib = types.SimpleNamespace(XDefaultScreen=lambda display: 0,
            XSync=lambda display, discard: events.append(("sync", discard)))
        observer.xtest = types.SimpleNamespace(
            XTestFakeMotionEvent=lambda display, screen, x, y, delay: events.append(("move", x, y)) or 1,
            XTestFakeButtonEvent=lambda display, button, pressed, delay: events.append(("button", button, pressed)) or 1)
        observer.horizontal_button(7, True, 10.8, 20.9)
        observer.horizontal_button(7, False)
        self.assertEqual(events, [("move", 10, 20), ("button", 7, 1), ("sync", False), ("button", 7, 0), ("sync", False)])
        observer.xtest.XTestFakeButtonEvent = lambda *args: 0
        self.error("unsupported", lambda: observer.horizontal_button(7, False))

    def test_bad_target_bounds_and_changed_foreground_never_dispatch(self):
        self.error("element_unavailable", lambda: self.driver.act({"kind": "type_text", "pid": 99, "text": "x"}, self.state(), lambda: None))
        self.error("invalid_action", lambda: self.act("mouse_move", x=400, y=20))
        self.error("invalid_action", lambda: self.act("mouse_move", x=float("nan"), y=20))
        def changed():
            raise BackendError("element_unavailable", "Foreground changed")
        self.error("element_unavailable", lambda: self.driver.act({"kind": "key_press", "pid": 42, "key": "a"}, self.state(), changed))
        self.assertEqual(self.registry.events, [])

    def test_missing_keymap_mapping_fails_before_dispatch(self):
        def missing(name):
            raise BackendError("unsupported", "Key is unmapped")
        self.x11.keycode = missing
        self.error("unsupported", lambda: self.act("key_press", key="a"))
        self.assertEqual(self.registry.events, [])

    def test_rectangle_rejects_missing_and_native_sentinel_extents(self):
        self.assertEqual(rectangle((1, 2, 3, 4)), {"x": 1, "y": 2, "width": 3, "height": 4})
        for value in (None, (0, 0, 0, 10), (-2147483648, 0, 10, 10), (False, 0, 10, 10)):
            self.assertIsNone(rectangle(value))

    def test_x11_symbol_conversion_does_not_pass_keysym_as_hardware_keycode(self):
        observer = X11Observation.__new__(X11Observation)
        calls = []
        observer.display = "test-display"
        observer.lib = types.SimpleNamespace(XStringToKeysym=lambda name: 0xFF0D,
            XKeysymToKeycode=lambda display, symbol: calls.append((display, symbol)) or 36)
        self.assertEqual(observer.keycode("Return"), 36)
        self.assertEqual(calls, [("test-display", 0xFF0D)])

    def test_close_releases_observation_connection(self):
        self.driver.close()
        self.assertTrue(self.x11.closed)


if __name__ == "__main__":
    unittest.main()
