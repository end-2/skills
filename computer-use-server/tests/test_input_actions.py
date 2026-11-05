"""Validate native input boundaries and the shared broker's replay guarantees."""
import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from backend_common import BackendError
from input_actions import INPUT_KINDS, validate_input_action
from mock_backend import MockBackend
from server import APIError, ComputerServer


class InputValidationTests(unittest.TestCase):
    def setUp(self):
        self.state = MockBackend().snapshot()

    def validate(self, kind, **payload):
        return validate_input_action({"kind": kind, "pid": 1, **payload}, self.state)

    def test_optional_defaults_do_not_change_the_original_request(self):
        action = {"kind": "mouse_click", "pid": 1, "x": 25, "y": 30}
        saved = copy.deepcopy(action)
        normalized = validate_input_action(action, self.state)
        self.assertEqual(action, saved)
        self.assertEqual(normalized, dict(action, button="left", count=1, modifiers=[]))
        drag = self.validate("mouse_drag", from_x=5, from_y=10, to_x=30, to_y=40)
        self.assertEqual(drag["duration_ms"], 400)

    def test_negative_display_coordinates_are_observed_not_assumed(self):
        self.state["input"]["displays"].append({"x": -800, "y": -200, "width": 800, "height": 600})
        self.validate("mouse_move", x=-800, y=-200)
        for x, y in [(-801, 0), (1024, 0), (0, 768), (-1, 600)]:
            with self.subTest(x=x, y=y), self.assertRaises(BackendError):
                self.validate("mouse_move", x=x, y=y)

    def test_invalid_numeric_and_modifier_inputs_fail_before_dispatch(self):
        actions = [
            {"kind": "mouse_move", "pid": 1, "x": x, "y": 10}
            for x in (True, float("nan"), float("inf"), 10 ** 1000, "10", None)
        ] + [
            {"kind": "key_press", "pid": 1, "key": "a", "modifiers": modifiers}
            for modifiers in ("ctrl", ["ctrl", "ctrl"], ["super"], [[]], [True])
        ] + [
            {"kind": "mouse_click", "pid": 1, "x": 1, "y": 1, "count": True},
            {"kind": "mouse_drag", "pid": 1, "from_x": 1, "from_y": 1, "to_x": 2, "to_y": 2, "duration_ms": 2001},
            {"kind": "scroll", "pid": 1, "x": 1, "y": 1, "delta_x": 0, "delta_y": 0},
            {"kind": "scroll", "pid": 1, "x": 1, "y": 1, "delta_x": False, "delta_y": 1},
            {"kind": []},
        ]
        for action in actions:
            with self.subTest(kind=str(action.get("kind"))), self.assertRaises(BackendError) as error:
                validate_input_action(action, self.state)
            self.assertEqual(error.exception.code, "invalid_action")

    def test_text_and_keys_have_distinct_contracts(self):
        self.assertEqual(self.validate("type_text", text="한글 😀 e\u0301")['text'], "한글 😀 e\u0301")
        for text in ("", "x" * 4097, "\ud800", "a\0b", 1):
            with self.assertRaises(BackendError):
                self.validate("type_text", text=text)
        for key in ("enter", "f12", "a", "space", "`", "\\"):
            self.validate("key_press", key=key, modifiers=["ctrl", "shift"])
        for key in ("A", "ctrl+a", "😀", "f13", "", []):
            with self.assertRaises(BackendError):
                self.validate("key_press", key=key)

    def test_unadvertised_input_and_wrong_foreground_are_rejected(self):
        self.state["capabilities"]["type_text"] = False
        with self.assertRaises(BackendError) as error:
            self.validate("type_text", text="hello")
        self.assertEqual(error.exception.code, "unsupported")
        with self.assertRaises(BackendError) as error:
            self.validate("key_press", pid=2, key="enter")
        self.assertEqual(error.exception.code, "element_unavailable")
        with self.assertRaises(BackendError):
            self.validate("key_press", pid=True, key="enter")

    def test_missing_geometry_disables_coordinates_not_keyboard(self):
        self.state.pop("input")
        self.validate("key_press", key="tab")
        with self.assertRaises(BackendError):
            self.validate("mouse_move", x=10, y=10)

    def test_unknown_fields_and_missing_required_fields_are_rejected(self):
        with self.assertRaises(BackendError):
            self.validate("type_text", text="hello", node_id="input")
        with self.assertRaises(BackendError):
            self.validate("scroll", x=10, y=10, delta_y=1)


class BrokerInputTests(unittest.TestCase):
    def setUp(self):
        self.backend = MockBackend()
        self.core = ComputerServer(self.backend)
        self.a = self.core.create_session("native-input-a")
        self.b = self.core.create_session("native-input-b")

    def call(self, session, op, **payload):
        return self.core.call(session["session_id"], session["session_token"], op, payload)

    def payload(self, lease, snapshot, action, request_id="input-request"):
        return {"request_id": request_id, "lease_token": lease["lease_token"],
                "snapshot_id": snapshot["snapshot_id"], "action": action}

    def test_native_text_appends_and_replay_does_not_duplicate_input(self):
        self.backend.value = "before:"
        lease = self.call(self.a, "acquire")
        observed = self.call(self.a, "status")
        request = self.payload(lease, observed, {"kind": "type_text", "pid": 1, "text": "한글 ✅"})
        result = self.call(self.a, "act", **request)
        self.assertEqual(self.call(self.a, "act", **request), result)
        self.assertEqual(self.backend.value, "before:한글 ✅")

    def test_unowned_raw_input_never_reaches_the_backend(self):
        lease = self.call(self.a, "acquire")
        observed = self.call(self.b, "status")
        for kind in INPUT_KINDS:
            with self.subTest(kind=kind), self.assertRaises(APIError) as error:
                self.call(self.b, "act", **self.payload(lease, observed, {"kind": kind, "pid": 1}, kind))
            self.assertEqual(error.exception.code, "lease_required")
        self.assertEqual(self.backend.value, "")
        self.assertEqual(self.backend.pointer, {"x": 0, "y": 0})

    def test_external_pointer_change_rejects_a_snapshot_before_typing(self):
        lease = self.call(self.a, "acquire")
        observed = self.call(self.a, "status")
        self.backend.pointer["x"] = 20
        with self.assertRaises(APIError) as error:
            self.call(self.a, "act", **self.payload(lease, observed, {"kind": "type_text", "pid": 1, "text": "never"}))
        self.assertEqual(error.exception.code, "stale_snapshot")
        self.assertEqual(self.backend.value, "")

    def test_invalid_input_is_cached_and_a_new_decision_can_succeed(self):
        lease = self.call(self.a, "acquire")
        observed = self.call(self.a, "status")
        payload = self.payload(lease, observed, {"kind": "mouse_click", "pid": 1, "x": 1024, "y": 0})
        for _ in range(2):
            with self.assertRaises(APIError) as error:
                self.call(self.a, "act", **payload)
            self.assertEqual(error.exception.status, 400)
            self.assertTrue(error.exception.details["request_recorded"])
        payload = self.payload(lease, self.call(self.a, "status"),
                               {"kind": "mouse_click", "pid": 1, "x": 50, "y": 60}, "new-decision")
        self.call(self.a, "act", **payload)
        self.assertEqual(self.backend.pointer, {"x": 50, "y": 60})

    def test_uncertain_native_input_is_never_replayed_as_new_events(self):
        lease = self.call(self.a, "acquire")
        observed = self.call(self.a, "status")
        original = self.backend.act
        calls = []
        def failing(action):
            calls.append(action)
            original(action)
            raise BackendError("backend_unavailable", "Injected event delivery uncertainty")
        self.backend.act = failing
        payload = self.payload(lease, observed, {"kind": "type_text", "pid": 1, "text": "once"})
        for _ in range(2):
            with self.assertRaises(APIError) as error:
                self.call(self.a, "act", **payload)
            self.assertEqual(error.exception.code, "action_outcome_unknown")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.backend.value, "once")

    def test_four_sessions_race_for_one_input_lease(self):
        sessions = [self.a, self.b, self.core.create_session("c"), self.core.create_session("d")]
        barrier = threading.Barrier(4)
        def acquire(session):
            barrier.wait()
            try:
                return self.call(session, "acquire")
            except APIError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(acquire, sessions))
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(results.count("lease_busy"), 3)


if __name__ == "__main__":
    unittest.main()
