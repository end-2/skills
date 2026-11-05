"""AT-SPI adapter contract tests. All accessibles are fakes; no Linux imports/UI."""
import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from backend_common import BackendError
from linux_backend import LinuxBackend


class State:
    def __init__(self, values):
        self.values = values

    def contains(self, value):
        return value in self.values


class Accessible:
    def __init__(self, role, name="", *, pid=42, states=(), children=(), text=None, actions=(), component=False, editable=False):
        self.role, self.name, self.pid = role, name, pid
        self.description = ""
        self.states = set(states)
        self.children = list(children)
        self.text = text
        self.actions = list(actions)
        self.component, self.editable = component, editable
        self.calls = []
        self.fail = False
        self.result = True
        self.cache_clears = 0
        self.text_reads = 0
        self.numeric = None
        self.extents = None

    def clearCache(self):
        self.cache_clears += 1

    def getState(self):
        if self.fail:
            raise RuntimeError("disappeared")
        return State(self.states)

    def getRole(self):
        return self.role

    def getRoleName(self):
        return self.role.lower()

    def get_process_id(self):
        return self.pid

    @property
    def childCount(self):
        return len(self.children)

    def getChildAtIndex(self, index):
        return self.children[index]

    def queryComponent(self):
        if not self.component:
            raise NotImplementedError
        return self

    def grabFocus(self):
        self.calls.append(("focus",))
        return self.result

    def getExtents(self, coordinates):
        if self.extents is None:
            raise NotImplementedError
        return self.extents

    def queryAction(self):
        if not self.actions:
            raise NotImplementedError
        return self

    @property
    def nActions(self):
        return len(self.actions)

    def getName(self, index):
        return self.actions[index]

    def doAction(self, index):
        self.calls.append(("perform", self.actions[index]))
        return self.result

    def queryText(self):
        if self.text is None:
            raise NotImplementedError
        return self

    def getText(self, start, end):
        self.text_reads += 1
        return self.text

    def queryEditableText(self):
        if not self.editable:
            raise NotImplementedError
        return self

    def queryValue(self):
        if self.numeric is None:
            raise NotImplementedError
        return types.SimpleNamespace(currentValue=self.numeric)

    def setTextContents(self, value):
        self.calls.append(("set_value", value))
        if self.result:
            self.text = value
        return self.result


class LinuxBackendTests(unittest.TestCase):
    def setUp(self):
        self.field = Accessible("TEXT", "Message", text="hello", editable=True, component=True,
                                states={"ENABLED", "EDITABLE", "FOCUSABLE"})
        self.button = Accessible("BUTTON", "Apply", actions=["click"], states={"ENABLED"})
        self.password = Accessible("PASSWORD_TEXT", "Password", text="do-not-read", editable=True,
                                   states={"ENABLED", "EDITABLE"})
        self.frame = Accessible("FRAME", "Editor", children=[self.field, self.button, self.password], component=True,
                                states={"ENABLED", "ACTIVE", "FOCUSABLE"})
        self.app = Accessible("APPLICATION", "Example", children=[self.frame])
        self.desktop = Accessible("DESKTOP", children=[self.app])
        self.api = types.SimpleNamespace(Registry=types.SimpleNamespace(getDesktop=lambda index: self.desktop))
        self.api.DESKTOP_COORDS = 0
        for name in ("DEFUNCT", "ACTIVE", "FOCUSED", "FOCUSABLE", "ENABLED", "EDITABLE", "CHECKED", "SELECTED", "VISIBLE", "SHOWING"):
            setattr(self.api, "STATE_" + name, name)
        for name in ("FRAME", "DIALOG", "WINDOW", "PASSWORD_TEXT"):
            setattr(self.api, "ROLE_" + name, name)
        self.backend = LinuxBackend(api=self.api)

    @staticmethod
    def node(state, title):
        return next(node for node in state["nodes"] if node["title"] == title)

    def assert_error(self, code, fn, *args):
        with self.assertRaises(BackendError) as caught:
            fn(*args)
        self.assertEqual(caught.exception.code, code)

    def test_semantic_snapshot_is_stable_and_does_not_read_passwords(self):
        first = self.backend.snapshot()
        self.assertEqual(first, self.backend.snapshot())
        self.assertEqual(first["platform"], "linux")
        self.assertEqual(first["frontmost_app"]["pid"], 42)
        self.assertFalse(first["capabilities"]["screenshots"])
        self.assertEqual(first["capabilities"]["foreground_status"], "identified")
        self.assertEqual(self.node(first, "Message")["settable"], ["focused", "value"])
        password = self.node(first, "Password")
        self.assertIsNone(password["value"])
        self.assertIsNone(password["value_hash"])
        self.assertNotIn("value", password["settable"])
        self.assertEqual(self.password.text_reads, 0)
        self.assertNotIn("do-not-read", json.dumps(first))
        self.assertGreaterEqual(self.desktop.cache_clears, 2)

    def test_suffix_changes_change_full_text_hash_and_mark_truncation(self):
        self.field.text = "a" * 5000
        first = self.backend.snapshot()
        self.field.text = "a" * 4999 + "b"
        second = self.backend.snapshot()
        before, after = self.node(first, "Message"), self.node(second, "Message")
        self.assertEqual(before["value"], after["value"])
        self.assertEqual(len(after["value"]), 4096)
        self.assertNotEqual(before["value_hash"], after["value_hash"])
        self.assertTrue(after["text_truncated"])
        self.assertTrue(second["truncated"])

    def test_frames_are_observed_only_for_visible_showing_components(self):
        self.field.extents = (10, 20, 200, 30)
        self.assertIsNone(self.node(self.backend.snapshot(), "Message")["frame"])
        self.field.states.update({"VISIBLE", "SHOWING"})
        self.assertEqual(self.node(self.backend.snapshot(), "Message")["frame"],
                         {"x": 10, "y": 20, "width": 200, "height": 30})
        self.field.extents = (-2147483648, -2147483648, 200, 30)
        self.assertIsNone(self.node(self.backend.snapshot(), "Message")["frame"])

    def test_raw_input_rechecks_foreground_before_dispatch(self):
        from linux_input import LinuxInput
        from test_linux_input import FakeRegistry, FakeX11
        registry = FakeRegistry()
        registry.getDesktop = lambda index: self.desktop
        self.api.Registry = registry
        for name, value in {"KEY_STRING": "string", "KEY_PRESS": "press", "KEY_RELEASE": "release",
                            "KEY_LOCKMODIFIERS": "lock", "KEY_UNLOCKMODIFIERS": "unlock"}.items():
            setattr(self.api, name, value)
        driver = LinuxInput(self.api, x11=FakeX11(), environment={"DISPLAY": ":0"})
        backend = LinuxBackend(api=self.api, input_driver=driver)
        state = backend.snapshot()
        self.assertTrue(state["capabilities"]["type_text"])
        backend.act({"kind": "type_text", "pid": 42, "text": "checked"})
        self.assertEqual(registry.events, [("key", 0, "checked", "string")])
        registry.events.clear()
        self.frame.states.remove("ACTIVE")
        self.assert_error("element_unavailable", backend.act, {"kind": "key_press", "pid": 42, "key": "enter"})
        self.assertEqual(registry.events, [])

    def test_nontext_control_values_and_toggle_states_are_observable(self):
        slider = Accessible("SLIDER", "Volume", states={"ENABLED"})
        slider.numeric = 0.5
        self.frame.children.append(slider)
        first = self.backend.snapshot()
        self.assertEqual(self.node(first, "Volume")["value"], 0.5)
        self.assertFalse(self.node(first, "Apply")["checked"])
        self.button.states.add("CHECKED")
        second = self.backend.snapshot()
        self.assertTrue(self.node(second, "Apply")["checked"])
        self.assertNotEqual(first, second)

    def test_semantic_actions_use_exposed_interfaces_and_return_false_as_error(self):
        state = self.backend.snapshot()
        field = self.node(state, "Message")["id"]
        button = self.node(state, "Apply")["id"]
        self.backend.act({"kind": "set_value", "node_id": field, "value": "changed"})
        self.assertEqual(self.field.text, "changed")
        self.backend.act({"kind": "focus", "node_id": field})
        self.backend.act({"kind": "perform", "node_id": button, "action": "click"})
        self.assertEqual(self.button.calls, [("perform", "click")])
        self.assertIn(("focus",), self.field.calls)
        self.button.result = False
        self.assert_error("unsupported", self.backend.act, {"kind": "perform", "node_id": button, "action": "click"})

    def test_action_capabilities_are_revalidated_and_password_mutation_rejected(self):
        state = self.backend.snapshot()
        field = self.node(state, "Message")["id"]
        button = self.node(state, "Apply")["id"]
        password = self.node(state, "Password")["id"]
        self.assert_error("unsupported", self.backend.act, {"kind": "set_value", "node_id": password, "value": "secret"})
        self.button.actions = ["open"]
        self.assert_error("unsupported", self.backend.act, {"kind": "perform", "node_id": button, "action": "click"})
        self.field.states.remove("EDITABLE")
        self.assert_error("unsupported", self.backend.act, {"kind": "set_value", "node_id": field, "value": "bad"})
        self.assertFalse(self.button.calls or self.password.calls or self.field.calls)

    def test_no_active_window_is_explicit_and_does_not_guess_first_app(self):
        self.frame.states.remove("ACTIVE")
        state = self.backend.snapshot()
        self.assertIsNone(state["frontmost_app"])
        self.assertEqual(state["nodes"], [])
        self.assertEqual(state["capabilities"]["foreground_status"], "unavailable")
        self.assertFalse(state["capabilities"]["perform"])
        self.assert_error("unsupported", self.backend.act, {"kind": "activate_app", "pid": 42})

    def test_multiple_active_windows_are_ambiguous(self):
        self.app.children.append(Accessible("DIALOG", "Other", states={"ACTIVE"}))
        state = self.backend.snapshot()
        self.assertIsNone(state["frontmost_app"])
        self.assertEqual(state["capabilities"]["foreground_status"], "ambiguous")
        self.assertEqual(state["nodes"], [])

    def test_foreground_change_prevents_mutation_of_retained_objects(self):
        state = self.backend.snapshot()
        button = self.node(state, "Apply")["id"]
        self.frame.states.remove("ACTIVE")
        self.assert_error("element_unavailable", self.backend.act, {"kind": "perform", "node_id": button, "action": "click"})
        self.assertFalse(self.button.calls)

    def test_frame_activation_requires_unique_observed_focusable_frame(self):
        other_frame = Accessible("FRAME", "Other", pid=99, component=True, states={"ENABLED", "FOCUSABLE"})
        other_app = Accessible("APPLICATION", "Other app", pid=99, children=[other_frame])
        self.desktop.children.append(other_app)
        state = self.backend.snapshot()
        self.assertTrue(next(app for app in state["applications"] if app["pid"] == 99)["can_activate"])
        self.backend.act({"kind": "activate_app", "pid": 99})
        self.assertEqual(other_frame.calls, [("focus",)])
        other_app.children.append(Accessible("FRAME", "Extra", component=True, states={"ENABLED", "FOCUSABLE"}))
        self.assert_error("unsupported", self.backend.act, {"kind": "activate_app", "pid": 99})

    def test_node_limit_disappearing_child_and_cycles_are_bounded(self):
        self.button.fail = True
        self.field.children.append(self.frame)
        backend = LinuxBackend(api=self.api, max_nodes=2)
        state = backend.snapshot()
        self.assertLessEqual(len(state["nodes"]), 2)
        self.assertTrue(state["truncated"])
        state = self.backend.snapshot()
        self.assertTrue(state["truncated"])
        self.assertEqual(len(state["nodes"]), 3)
        self.assertNotIn("Apply", [node["title"] for node in state["nodes"]])

    def test_depth_limit_marks_incomplete_tree(self):
        self.field.children.append(Accessible("LABEL", "Nested"))
        state = LinuxBackend(api=self.api, max_depth=1).snapshot()
        self.assertTrue(state["truncated"])
        self.assertNotIn("Nested", [node["title"] for node in state["nodes"]])

    def test_empty_or_unavailable_registry_reports_setup_error(self):
        self.desktop.children.clear()
        self.assert_error("backend_unavailable", self.backend.snapshot)
        self.api.Registry.getDesktop = lambda index: None
        self.assert_error("backend_unavailable", self.backend.snapshot)

    def test_bad_inputs_and_closed_backend(self):
        state = self.backend.snapshot()
        field = self.node(state, "Message")["id"]
        for action in (None, {"kind": "click", "x": 1}, {"kind": "focus", "node_id": field, "extra": True},
                       {"kind": "set_value", "node_id": field, "value": "nul\0value"}):
            self.assert_error("invalid_action", self.backend.act, action)
        self.backend.close()
        self.assert_error("backend_unavailable", self.backend.snapshot)

    def test_native_import_and_session_failures_are_isolated(self):
        with patch("linux_backend.platform.system", return_value="Darwin"), patch("linux_backend.importlib.import_module") as load:
            self.assert_error("backend_unavailable", LinuxBackend)
            load.assert_not_called()
        with patch("linux_backend.platform.system", return_value="Linux"), patch.dict(os.environ, {}, clear=True):
            self.assert_error("backend_unavailable", LinuxBackend)
        with patch("linux_backend.platform.system", return_value="Linux"), patch.dict(os.environ, {"DISPLAY": ":0"}, clear=True):
            self.assert_error("backend_unavailable", LinuxBackend)
        with patch("linux_backend.platform.system", return_value="Linux"), patch.dict(os.environ, {"DISPLAY": ":0", "DBUS_SESSION_BUS_ADDRESS": "fake"}, clear=True), patch("linux_backend.importlib.import_module", side_effect=ImportError):
            self.assert_error("backend_unavailable", LinuxBackend)


if __name__ == "__main__":
    unittest.main()
