"""Contract tests using fake UIA providers; these do not exercise Windows itself."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from backend_common import BackendError
from windows_backend import WindowsBackend, _native_error, _text


class FakePattern:
    def __init__(self, value="", *, readonly=False, expand=0):
        self._value = value
        self.IsReadOnly = readonly
        self.ExpandCollapseState = expand
        self.IsSelected = False
        self.ToggleState = 0
        self.value_reads = 0
        self.calls = []
        self.accepted = True

    @property
    def Value(self):
        self.value_reads += 1
        return self._value

    def Invoke(self, waitTime=0):
        self.calls.append(("Invoke", waitTime))
        return self.accepted

    def Toggle(self, waitTime=0):
        self.calls.append(("Toggle", waitTime))
        self.ToggleState = 1 - self.ToggleState
        return self.accepted

    def Select(self, waitTime=0):
        self.calls.append(("Select", waitTime))
        self.IsSelected = True
        return self.accepted

    def Expand(self, waitTime=0):
        self.calls.append(("Expand", waitTime))
        self.ExpandCollapseState = 1
        return self.accepted

    def Collapse(self, waitTime=0):
        self.calls.append(("Collapse", waitTime))
        self.ExpandCollapseState = 0
        return self.accepted

    def SetValue(self, value, waitTime=0):
        self.calls.append(("SetValue", value, waitTime))
        self._value = value
        return self.accepted


class FakeControl:
    def __init__(self, pid, rid, *, handle=0, name="control", children=(), patterns=None,
                 password=False, focusable=False, enabled=True):
        self.ProcessId, self.NativeWindowHandle = pid, handle
        self.rid = (42, rid)
        self.Name, self.HelpText, self.ControlTypeName = name, "help", "EditControl"
        self.IsPassword, self.IsKeyboardFocusable = password, focusable
        self.IsEnabled, self.HasKeyboardFocus = enabled, False
        self.children = list(children)
        self.parent = None
        for child in self.children:
            child.parent = self
        self.patterns = patterns or {}
        self.pattern_requests = []
        self.focus_calls = 0

    def GetRuntimeId(self):
        return self.rid

    def GetPattern(self, pattern_id):
        self.pattern_requests.append(pattern_id)
        return self.patterns.get(pattern_id)

    def GetFirstChildControl(self):
        return self.children[0] if self.children else None

    def GetNextSiblingControl(self):
        if self.parent is None:
            return None
        index = self.parent.children.index(self) + 1
        return self.parent.children[index] if index < len(self.parent.children) else None

    def SetFocus(self):
        self.focus_calls += 1
        self.HasKeyboardFocus = True
        return True


class FakeAutomation:
    PatternId = SimpleNamespace(**{name: name for name in (
        "InvokePattern", "TogglePattern", "SelectionItemPattern", "ExpandCollapsePattern", "ValuePattern")})

    def __init__(self, controls=()):
        self.other = FakeControl(22, 22, handle=202, name="other")
        self.window = FakeControl(11, 11, handle=101, name="current", children=controls)
        self.desktop = FakeControl(0, 1, name="desktop", children=[self.other, self.window])
        self.foreground = self.window
        self.initialized = self.uninitialized = 0
        self.activations = []
        self.activation_allowed = True

    def InitializeUIAutomationInCurrentThread(self):
        self.initialized += 1

    def UninitializeUIAutomationInCurrentThread(self):
        self.uninitialized += 1

    def GetForegroundControl(self):
        return self.foreground

    def GetRootControl(self):
        return self.desktop

    def IsWindowVisible(self, handle):
        return True

    def ControlsAreSame(self, left, right):
        return left is right

    def ControlFromHandle(self, handle):
        return next((c for c in self.desktop.children if c.NativeWindowHandle == handle), None)

    def SetForegroundWindow(self, handle):
        self.activations.append(handle)
        if self.activation_allowed:
            self.foreground = self.ControlFromHandle(handle)
        return self.activation_allowed


class WindowsBackendTests(unittest.TestCase):
    def setUp(self):
        self.backends = []

    def tearDown(self):
        for backend in self.backends:
            backend.close()

    def backend(self, native=None, **kwargs):
        native = native or FakeAutomation()
        with patch("windows_backend.platform.system", return_value="Windows"):
            result = WindowsBackend(_automation=native, _name_lookup=lambda pid: f"app-{pid}.exe", **kwargs)
        self.backends.append(result)
        return result

    def assert_error(self, code, function, *args):
        with self.assertRaises(BackendError) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_import_and_platform_do_not_require_windows_dependency(self):
        with patch("windows_backend.platform.system", return_value="Darwin"), \
             patch("windows_backend.importlib.import_module") as importer:
            self.assert_error("backend_unavailable", WindowsBackend)
            importer.assert_not_called()

    def test_missing_optional_dependency_has_install_diagnostic(self):
        with patch("windows_backend.platform.system", return_value="Windows"), \
             patch("windows_backend.importlib.import_module", side_effect=ImportError("missing")):
            error = self.assert_error("backend_unavailable", WindowsBackend)
        self.assertIn("uiautomation==2.0.29", error.details["install"])

    def test_snapshot_is_deterministic_and_limited_to_foreground_window(self):
        child = FakeControl(11, 101, name="entry")
        backend = self.backend(FakeAutomation([child]))
        first = backend.snapshot()
        self.assertEqual(first, backend.snapshot())
        self.assertEqual([app["pid"] for app in first["applications"]], [11, 22])
        self.assertEqual(first["capabilities"]["scope"], "foreground_window")
        self.assertEqual([node["title"] for node in first["nodes"]], ["current", "entry"])
        self.assertFalse(first["truncated"])

    def test_full_text_hash_detects_change_outside_display_prefix(self):
        pattern = FakePattern("a" * 4096 + "x")
        control = FakeControl(11, 101, patterns={"ValuePattern": pattern})
        backend = self.backend(FakeAutomation([control]))
        first = backend.snapshot()
        pattern._value = "a" * 4096 + "y"
        second = backend.snapshot()
        self.assertEqual(first["nodes"][1]["value"], second["nodes"][1]["value"])
        self.assertNotEqual(first["nodes"][1]["value_hash"], second["nodes"][1]["value_hash"])
        self.assertTrue(first["truncated"])
        self.assertTrue(first["nodes"][1]["text_truncated"])
        self.assertEqual(_text("abc")[1], hashlib.sha256(b"abc").hexdigest())

    def test_password_control_never_requests_or_reads_value(self):
        pattern = FakePattern("secret")
        control = FakeControl(11, 101, password=True, patterns={"ValuePattern": pattern})
        backend = self.backend(FakeAutomation([control]))
        state = backend.snapshot()
        node = state["nodes"][1]
        self.assertIsNone(node["value"])
        self.assertIsNone(node["value_hash"])
        self.assertNotIn("value", node["settable"])
        self.assertNotIn("ValuePattern", control.pattern_requests)
        self.assertEqual(pattern.value_reads, 0)
        self.assert_error("unsupported", backend.act, {"kind": "set_value", "node_id": node["id"], "value": "new"})
        self.assertEqual(pattern.calls, [])

    def test_password_transition_after_snapshot_prevents_mutation(self):
        pattern = FakePattern("original")
        control = FakeControl(11, 101, patterns={"ValuePattern": pattern})
        backend = self.backend(FakeAutomation([control]))
        node = backend.snapshot()["nodes"][1]
        control.IsPassword = True
        self.assert_error("unsupported", backend.act, {"kind": "set_value", "node_id": node["id"], "value": "new"})
        self.assertEqual(pattern.calls, [])

    def test_native_patterns_dispatch_without_input_fallback(self):
        pattern = FakePattern()
        control = FakeControl(11, 101, patterns={name: pattern for name in (
            "InvokePattern", "TogglePattern", "SelectionItemPattern", "ExpandCollapsePattern", "ValuePattern")}, focusable=True)
        backend = self.backend(FakeAutomation([control]))
        for action in ("Invoke", "Toggle", "Select", "Expand", "Collapse"):
            node = backend.snapshot()["nodes"][1]
            self.assertIn(action, node["actions"])
            self.assertTrue(backend.act({"kind": "perform", "node_id": node["id"], "action": action})["accepted"])
        node = backend.snapshot()["nodes"][1]
        backend.act({"kind": "set_value", "node_id": node["id"], "value": "updated"})
        backend.act({"kind": "focus", "node_id": node["id"]})
        self.assertEqual(pattern._value, "updated")
        self.assertEqual(control.focus_calls, 1)
        self.assertTrue(all(call[-1] == 0 for call in pattern.calls))

    def test_changed_foreground_and_disabled_control_prevent_actions(self):
        pattern = FakePattern()
        control = FakeControl(11, 101, patterns={"InvokePattern": pattern})
        native = FakeAutomation([control])
        backend = self.backend(native)
        node = backend.snapshot()["nodes"][1]
        native.foreground = native.other
        self.assert_error("element_unavailable", backend.act, {"kind": "perform", "node_id": node["id"], "action": "Invoke"})
        native.foreground = native.window
        node = backend.snapshot()["nodes"][1]
        control.IsEnabled = False
        self.assert_error("invalid_action", backend.act, {"kind": "perform", "node_id": node["id"], "action": "Invoke"})
        self.assertEqual(pattern.calls, [])

    def test_removed_capability_is_not_replaced_with_fallback(self):
        pattern = FakePattern()
        control = FakeControl(11, 101, patterns={"InvokePattern": pattern})
        backend = self.backend(FakeAutomation([control]))
        node = backend.snapshot()["nodes"][1]
        control.patterns.clear()
        self.assert_error("unsupported", backend.act, {"kind": "perform", "node_id": node["id"], "action": "Invoke"})
        self.assertEqual(pattern.calls, [])

    def test_leaf_nodes_and_readonly_values_do_not_advertise_actions(self):
        control = FakeControl(11, 101, patterns={"ExpandCollapsePattern": FakePattern(expand=3),
                                                "ValuePattern": FakePattern(readonly=True)})
        node = self.backend(FakeAutomation([control])).snapshot()["nodes"][1]
        self.assertNotIn("Expand", node["actions"])
        self.assertNotIn("Collapse", node["actions"])
        self.assertNotIn("value", node["settable"])
        self.assertEqual(node["states"]["expand_collapse"], 3)

    def test_false_native_result_is_unknown_not_success(self):
        pattern = FakePattern()
        pattern.accepted = False
        control = FakeControl(11, 101, patterns={"InvokePattern": pattern})
        backend = self.backend(FakeAutomation([control]))
        node = backend.snapshot()["nodes"][1]
        error = self.assert_error("element_unavailable", backend.act,
                                  {"kind": "perform", "node_id": node["id"], "action": "Invoke"})
        self.assertTrue(error.details["action_outcome_unknown"])
        self.assertEqual(len(pattern.calls), 1)

    def test_activation_uses_only_observed_window_and_reports_restrictions(self):
        native = FakeAutomation()
        backend = self.backend(native)
        self.assert_error("element_unavailable", backend.act, {"kind": "activate_app", "pid": 22})
        backend.snapshot()
        native.activation_allowed = False
        error = self.assert_error("unsupported", backend.act, {"kind": "activate_app", "pid": 22})
        self.assertEqual(error.details["reason"], "foreground_activation_denied")
        self.assertEqual(native.activations, [202])
        backend.snapshot()
        native.activation_allowed = True
        self.assertTrue(backend.act({"kind": "activate_app", "pid": 22})["accepted"])

    def test_recycled_window_handle_is_rejected(self):
        native = FakeAutomation()
        backend = self.backend(native)
        backend.snapshot()
        replacement = FakeControl(22, 999, handle=202)
        replacement.parent = native.desktop
        native.desktop.children[0] = replacement
        self.assert_error("element_unavailable", backend.act, {"kind": "activate_app", "pid": 22})
        self.assertEqual(native.activations, [])

    def test_node_and_depth_bounds_are_explicit(self):
        leaf = FakeControl(11, 103)
        branch = FakeControl(11, 102, children=[leaf])
        child = FakeControl(11, 101, children=[branch])
        native = FakeAutomation([child])
        for options in ({"max_nodes": 2}, {"max_depth": 1}):
            state = self.backend(native, **options).snapshot()
            self.assertEqual(len(state["nodes"]), 2)
            self.assertTrue(state["truncated"])
            self.assertEqual(state["nodes"][1]["children"], [])

    def test_com_initialization_cleanup_and_thread_affinity(self):
        native = FakeAutomation()
        old_flags = getattr(sys, "coinit_flags", None)
        backend = self.backend(native)
        self.assertEqual(getattr(sys, "coinit_flags", None), old_flags)
        errors = []

        def other_thread():
            try:
                backend.snapshot()
            except BackendError as error:
                errors.append(error.code)

        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join()
        self.assertEqual(errors, ["backend_unavailable"])
        backend.close()
        backend.close()
        self.assertEqual((native.initialized, native.uninitialized), (1, 1))
        self.assert_error("backend_unavailable", backend.snapshot)

    def test_hresult_classification(self):
        for hresult, expected in ((0x80070005, "permission_denied"), (0x80040201, "element_unavailable"),
                                  (0x80040204, "unsupported"), (0x80070057, "invalid_action")):
            error = RuntimeError("provider detail")
            error.hresult = hresult - (1 << 32)
            self.assertEqual(_native_error(error, "test").code, expected)

    def test_invalid_action_fields_and_boolean_pid(self):
        backend = self.backend()
        backend.snapshot()
        self.assert_error("invalid_action", backend.act, {"kind": "activate_app", "pid": True})
        backend.snapshot()
        self.assert_error("invalid_action", backend.act, {"kind": "focus", "node_id": "11/0", "extra": 1})


if __name__ == "__main__":
    unittest.main()
