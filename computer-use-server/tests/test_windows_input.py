"""SendInput contract tests with injected native APIs; no Windows desktop used."""
from contextlib import nullcontext
import ctypes as C
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from backend_common import BackendError
from input_actions import INPUT_KINDS, KEYS
from windows_backend import WindowsBackend
from windows_input import (ABSOLUTE, BUTTONS, EXTENDED, HARDWAREINPUT, HWHEEL, INPUT,
                           INPUT_UNION, KEYBDINPUT, KEYUP, KEY_VKS, MOUSEINPUT,
                           MOVE, RECT, UNICODE, VIRTUALDESK, WHEEL, Win32API,
                           WindowsInput, keyboard)
from test_windows_backend import FakeAutomation, FakeControl


def event_data(event):
    if event.type == 1:
        return ("key", event.ki.wVk, event.ki.wScan, event.ki.dwFlags)
    return ("mouse", event.mi.dx, event.mi.dy, event.mi.mouseData, event.mi.dwFlags)


class FakeAPI:
    def __init__(self):
        self.pid = 11
        self.reason = None
        self.displays = [{"x": -1920, "y": 0, "width": 1920, "height": 1080},
                         {"x": 0, "y": 0, "width": 1920, "height": 1080}]
        self.pointer = {"x": 100, "y": 200}
        self.virtual = (-1920, 0, 3840, 1080)
        self.calls = []
        self.responses = []
        self.after_send = None
        self.held = set()
        self.geometry_error = False

    def permission_reason(self, pid):
        return self.reason

    def geometry(self):
        if self.geometry_error:
            raise BackendError("unsupported", "No geometry")
        return self.displays, self.pointer

    def foreground_pid(self):
        return self.pid

    def virtual_screen(self):
        return self.virtual

    def pressed(self, vk):
        return vk in self.held

    def send(self, events):
        self.calls.append([event_data(e) for e in events])
        if self.after_send:
            self.after_send()
        result = self.responses.pop(0) if self.responses else (len(events), 0)
        if isinstance(result, Exception):
            raise result
        return result


class WindowsInputTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.sleeps = []
        self.input = WindowsInput(_api=self.api, _sleep=self.sleeps.append)
        info, caps = self.input.snapshot(11)
        self.state = {"frontmost_app": {"pid": 11}, "input": info, "capabilities": caps}

    def action(self, kind, **fields):
        return self.input.act({"kind": kind, "pid": 11, **fields}, self.state)

    def error(self, code, function, *args):
        with self.assertRaises(BackendError) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_input_abi_uses_windows_widths_and_pointer_aligned_union(self):
        pointer_size = C.sizeof(C.c_void_p)
        self.assertEqual(C.sizeof(HARDWAREINPUT), 8)
        self.assertEqual(C.sizeof(KEYBDINPUT), 24 if pointer_size == 8 else 16)
        self.assertEqual(C.sizeof(MOUSEINPUT), 32 if pointer_size == 8 else 24)
        self.assertEqual(C.sizeof(INPUT_UNION), C.sizeof(MOUSEINPUT))
        self.assertEqual(INPUT.data.offset, 8 if pointer_size == 8 else 4)
        self.assertEqual(C.sizeof(INPUT), 40 if pointer_size == 8 else 28)

    def test_native_send_passes_array_count_and_full_union_size(self):
        native = Win32API.__new__(Win32API)
        observed = []
        def send(count, array, size):
            observed.append((count, size, [event_data(array[i]) for i in range(count)]))
            return count - 1
        native.user = SimpleNamespace(SendInput=send)
        with patch("windows_input.C.set_last_error", create=True), \
             patch("windows_input.C.get_last_error", return_value=5, create=True):
            self.assertEqual(native.send([keyboard(65), keyboard(65, flags=KEYUP)]), (1, 5))
        self.assertEqual(observed, [(2, C.sizeof(INPUT), [("key", 65, 0, 0), ("key", 65, 0, KEYUP)])])

    def test_snapshot_advertises_physical_displays_and_supported_capabilities(self):
        self.assertEqual(self.state["input"]["units"], "pixels")
        self.assertEqual(self.state["input"]["displays"][0]["x"], -1920)
        self.assertTrue(all(self.state["capabilities"][kind] for kind in INPUT_KINDS))
        self.assertEqual(self.state["input"]["pointer"], {"x": 100, "y": 200})

    def test_permission_restrictions_disable_input_but_geometry_remains_visible(self):
        self.api.reason = "foreground_integrity_level_blocks_input"
        info, caps = self.input.snapshot(11)
        self.assertFalse(any(caps.values()))
        self.assertEqual(info["unavailable_reason"], self.api.reason)
        self.assertEqual(len(info["displays"]), 2)
        with self.assertRaises(BackendError) as caught:
            self.action("key_press", key="a")
        self.assertEqual(caught.exception.details["reason"], self.api.reason)
        self.assertEqual(self.api.calls, [])

    def test_geometry_failure_disables_only_pointer_actions(self):
        self.api.geometry_error = True
        info, caps = self.input.snapshot(11)
        self.assertTrue(caps["type_text"] and caps["key_press"])
        self.assertFalse(caps["mouse_click"] or caps["scroll"])
        self.assertEqual(info["displays"], [])
        self.assertIn("unavailable_reason", info)

    def test_native_import_failure_is_explicit_without_loading_optional_packages(self):
        with patch("windows_input.Win32API", side_effect=OSError("missing")):
            unavailable = WindowsInput()
        info, caps = unavailable.snapshot(11)
        self.assertFalse(any(caps.values()))
        self.assertEqual(info["unavailable_reason"], "windows_input_api_unavailable")

    def test_unicode_uses_utf16_packets_including_surrogate_pair(self):
        self.action("type_text", text="한글 ✅😀")
        units = list("한글 ✅😀".encode("utf-16-le"))
        expected = []
        for i in range(0, len(units), 2):
            unit = units[i] | units[i+1] << 8
            expected.extend([("key", 0, unit, UNICODE), ("key", 0, unit, UNICODE | KEYUP)])
        self.assertEqual(self.api.calls, [expected])

    def test_keymap_covers_contract_and_chords_release_in_reverse_order(self):
        self.assertEqual(set(KEY_VKS), KEYS)
        self.action("key_press", key="delete", modifiers=["ctrl", "shift"])
        self.assertEqual(self.api.calls, [[("key", 0x11, 0, 0), ("key", 0x10, 0, 0),
                                          ("key", 0x2E, 0, EXTENDED), ("key", 0x2E, 0, EXTENDED | KEYUP),
                                          ("key", 0x10, 0, KEYUP), ("key", 0x11, 0, KEYUP)]])

    def test_mouse_absolute_coordinates_include_negative_origin_and_last_pixel(self):
        self.action("mouse_move", x=-1920, y=0)
        self.action("mouse_move", x=1919, y=1079)
        flags = MOVE | ABSOLUTE | VIRTUALDESK
        self.assertEqual(self.api.calls, [[("mouse", 0, 0, 0, flags)], [("mouse", 65535, 65535, 0, flags)]])

    def test_click_count_buttons_and_modifiers_share_one_native_batch(self):
        self.action("mouse_click", x=100, y=200, button="right", count=3, modifiers=["alt"])
        events = self.api.calls[0]
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual(events[0], ("key", 0x12, 0, 0))
        self.assertEqual([e[-1] for e in events[2:-1]], [0x08, 0x10] * 3)
        self.assertEqual(events[-1], ("key", 0x12, 0, KEYUP))

    def test_scroll_signs_match_contract_and_axes_are_separate_events(self):
        self.action("scroll", x=100, y=200, delta_x=2, delta_y=3)
        events = self.api.calls[0]
        self.assertEqual(events[1], ("mouse", 0, 0, (-360) & 0xFFFFFFFF, WHEEL))
        self.assertEqual(events[2], ("mouse", 0, 0, 240, HWHEEL))

    def test_drag_interpolates_for_duration_then_releases_button_and_modifiers(self):
        self.action("mouse_drag", from_x=0, from_y=0, to_x=100, to_y=200,
                    duration_ms=100, modifiers=["shift"])
        self.assertAlmostEqual(sum(self.sleeps), 0.1)
        self.assertEqual(len(self.api.calls), 7)
        self.assertEqual(self.api.calls[0][0], ("key", 0x10, 0, 0))
        self.assertEqual(self.api.calls[0][-1][-1], BUTTONS["left"][0])
        self.assertEqual(self.api.calls[-1], [("mouse", 0, 0, 0, BUTTONS["left"][1]), ("key", 0x10, 0, KEYUP)])

    def test_partial_chord_insertion_releases_potentially_held_inputs(self):
        self.api.responses = [(2, 5)]
        with self.assertRaises(BackendError) as caught:
            self.action("key_press", key="a", modifiers=["ctrl"])
        self.assertEqual(caught.exception.code, "backend_unavailable")
        self.assertTrue(caught.exception.details["action_outcome_unknown"])
        self.assertEqual(caught.exception.details["inserted"], 2)
        self.assertEqual(self.api.calls[-1], [("key", 65, 0, KEYUP), ("key", 0x11, 0, KEYUP)])

    def test_zero_insertion_reports_restriction_and_attempts_release(self):
        self.api.responses = [(0, 0)]
        with self.assertRaises(BackendError) as caught:
            self.action("key_press", key="a")
        self.assertEqual(caught.exception.code, "unsupported")
        self.assertEqual(self.api.calls[-1], [("key", 65, 0, KEYUP)])
        self.assertTrue(caught.exception.details["action_outcome_unknown"])

    def test_native_exception_still_releases_unicode_packet(self):
        self.api.responses = [OSError("native failure")]
        with self.assertRaises(BackendError):
            self.action("type_text", text="한")
        self.assertEqual(self.api.calls[-1], [("key", 0, ord("한"), UNICODE | KEYUP)])

    def test_drag_foreground_switch_releases_without_sending_further_moves(self):
        self.api.after_send = lambda: setattr(self.api, "pid", 22)
        with self.assertRaises(BackendError) as caught:
            self.action("mouse_drag", from_x=0, from_y=0, to_x=100, to_y=200, modifiers=["ctrl"])
        self.assertEqual(caught.exception.code, "element_unavailable")
        self.assertTrue(caught.exception.details["action_outcome_unknown"])
        self.assertEqual(len(self.api.calls), 2)
        self.assertEqual(self.api.calls[-1], [("mouse", 0, 0, 0, 0x04), ("key", 0x11, 0, KEYUP)])

    def test_failed_cleanup_does_not_claim_release_success(self):
        self.api.responses = [(1, 5), (0, 5)]
        with self.assertRaises(BackendError) as caught:
            self.action("key_press", key="a")
        self.assertTrue(caught.exception.details["release_unconfirmed"])
        self.assertTrue(caught.exception.details["action_outcome_unknown"])

    def test_preexisting_human_key_or_button_blocks_injection(self):
        for held in (0x11, 0x01, ord("A")):
            self.api.held = {held}
            with self.assertRaises(BackendError) as caught:
                self.action("key_press", key="a")
            self.assertEqual(caught.exception.code, "unsupported")
        self.assertEqual(self.api.calls, [])

    def test_invalid_or_unobserved_target_never_dispatches(self):
        cases = [({"kind": "mouse_click", "pid": 11, "x": 1920, "y": 200}, "invalid_action"),
                 ({"kind": "type_text", "pid": 22, "text": "hello"}, "element_unavailable"),
                 ({"kind": "type_text", "pid": 11, "text": "\ud800"}, "invalid_action")]
        for action, code in cases:
            self.error(code, self.input.act, action, self.state)
        self.api.pid = 22
        self.error("element_unavailable", self.input.act, {"kind": "key_press", "pid": 11, "key": "a"}, self.state)
        self.assertEqual(self.api.calls, [])

    def test_native_monitor_and_pointer_output_is_read_after_api_mutates_structs(self):
        native = Win32API.__new__(Win32API)
        native.physical_coordinates = nullcontext
        native.monitor_callback = lambda fn: fn
        def monitors(_dc, _clip, callback, _data):
            rect = RECT(-1920, -100, 0, 980)
            callback(None, None, C.pointer(rect), 0)
            return 1
        def cursor(pointer):
            pointer._obj.x, pointer._obj.y = -300, 17
            return 1
        native.user = SimpleNamespace(EnumDisplayMonitors=monitors, GetPhysicalCursorPos=cursor)
        self.assertEqual(native.geometry(), ([{"x": -1920, "y": -100, "width": 1920, "height": 1080}], {"x": -300, "y": 17}))

    def test_dpi_context_is_restored_even_if_coordinate_query_fails(self):
        native = Win32API.__new__(Win32API)
        calls = []
        def dpi(context):
            calls.append(context.value if isinstance(context, C.c_void_p) else context)
            return 123
        native._dpi = dpi
        with self.assertRaises(OSError):
            with native.physical_coordinates():
                raise OSError("monitor query failed")
        self.assertEqual(calls, [C.c_void_p(-4).value, 123])
        native._dpi = None
        with self.assertRaises(BackendError) as caught:
            with native.physical_coordinates():
                self.fail("Unavailable DPI context must not proceed")
        self.assertEqual(caught.exception.code, "unsupported")

    def test_native_privilege_check_disables_elevated_target_and_closes_handles(self):
        native = Win32API.__new__(Win32API)
        closed_desktops, closed_processes = [], []
        native.user = SimpleNamespace(OpenInputDesktop=lambda *args: 10, CloseDesktop=closed_desktops.append)
        native.kernel = SimpleNamespace(OpenProcess=lambda *args: 20, GetCurrentProcess=lambda: -1,
                                        CloseHandle=closed_processes.append)
        native._token = lambda process: (0x2000, False) if process == -1 else (0x3000, False)
        self.assertEqual(native.permission_reason(11), "foreground_integrity_level_blocks_input")
        self.assertEqual(closed_desktops, [10])
        self.assertEqual(closed_processes, [20])
        native._token = lambda process: (0x2000, True) if process == -1 else (0x3000, False)
        self.assertIsNone(native.permission_reason(11))
        native.user.OpenInputDesktop = lambda *args: None
        self.assertEqual(native.permission_reason(11), "input_desktop_unavailable")

    def test_privilege_query_failure_is_explicit_and_closes_process_handle(self):
        native = Win32API.__new__(Win32API)
        closed = []
        native.user = SimpleNamespace(OpenInputDesktop=lambda *args: 10, CloseDesktop=lambda handle: None)
        native.kernel = SimpleNamespace(OpenProcess=lambda *args: 20, GetCurrentProcess=lambda: -1,
                                        CloseHandle=closed.append)
        def token(_process):
            raise OSError("access denied")
        native._token = token
        self.assertEqual(native.permission_reason(11), "foreground_input_permission_unknown")
        self.assertEqual(closed, [20])


class WindowsInputIntegrationTests(unittest.TestCase):
    def test_uia_snapshot_exposes_frames_and_native_input_on_same_adapter(self):
        control = FakeControl(11, 101)
        control.BoundingRectangle = SimpleNamespace(left=-100, top=20, right=200, bottom=60)
        native = FakeAutomation([control])
        api = FakeAPI()
        with patch("windows_backend.platform.system", return_value="Windows"):
            backend = WindowsBackend(_automation=native, _name_lookup=lambda pid: "fixture",
                                     _input=WindowsInput(_api=api))
        try:
            state = backend.snapshot()
            self.assertEqual(state["nodes"][1]["frame"], {"x": -100, "y": 20, "width": 300, "height": 40})
            self.assertIsNone(state["nodes"][0]["frame"])
            self.assertTrue(state["capabilities"]["type_text"])
            self.assertTrue(backend.act({"kind": "type_text", "pid": 11, "text": "한글"})["accepted"])
            self.assertEqual(len(api.calls), 1)
            backend.snapshot()
            native.foreground = native.other
            with self.assertRaises(BackendError):
                backend.act({"kind": "mouse_move", "pid": 11, "x": 10, "y": 10})
            self.assertEqual(len(api.calls), 1)
        finally:
            backend.close()
        self.assertEqual(native.initialized, 1)
        self.assertEqual(native.uninitialized, 1)


if __name__ == "__main__":
    unittest.main()
