"""Concurrency and retry invariants; no desktop permissions or live UI required."""

import copy
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import server
from backend_common import BackendError


class FakeClock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class FakeBackend:
    def __init__(self):
        self.state = {
            "applications": [{"pid": 42, "name": "Test App"}],
            "nodes": [
                {"id": "button", "enabled": True, "actions": ["AXPress"], "settable": []},
                {"id": "input", "enabled": True, "actions": [], "settable": ["value", "focused"]},
            ],
            "value": 0,
        }
        self.actions = []
        self.fail_next = False
        self.no_effect = False
        self.on_snapshot = None
        self.delay = 0
        self.active = 0
        self.max_active = 0
        self.guard = threading.Lock()

    def _enter(self):
        with self.guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        if self.delay:
            time.sleep(self.delay)

    def _leave(self):
        with self.guard:
            self.active -= 1

    def snapshot(self):
        self._enter()
        try:
            if self.on_snapshot:
                self.on_snapshot()
            return copy.deepcopy(self.state)
        finally:
            self._leave()

    def act(self, action):
        self._enter()
        try:
            self.actions.append(copy.deepcopy(action))
            if not self.no_effect:
                self.state["value"] += 1
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("backend failed after changing the desktop")
            return {"value": self.state["value"]}
        finally:
            self._leave()

    def close(self):
        pass


class ComputerServerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.backend = FakeBackend()
        self.core = server.ComputerServer(
            self.backend, clock=self.clock, lease_ttl=30, snapshot_ttl=30
        )
        self.a = self.core.create_session("agent-a")
        self.b = self.core.create_session("agent-b")

    def call(self, session, operation, **payload):
        return self.core.call(
            session["session_id"], session["session_token"], operation, payload
        )

    def action(self, snapshot, lease, request_id="request-1", **overrides):
        payload = {
            "snapshot_id": snapshot["snapshot_id"],
            "lease_token": lease["lease_token"],
            "request_id": request_id,
            "action": {"kind": "perform", "node_id": "button", "action": "AXPress"},
        }
        payload.update(overrides)
        return payload

    def assert_rejected(self, session, operation, expected_code=None, **payload):
        with self.assertRaises(server.APIError) as caught:
            self.call(session, operation, **payload)
        if expected_code is not None:
            self.assertEqual(caught.exception.code, expected_code)
        return caught.exception

    def test_status_reads_do_not_change_backend_or_require_a_lease(self):
        initial = copy.deepcopy(self.backend.state)
        first = self.call(self.a, "status")
        second = self.call(self.b, "status")
        self.assertEqual(first["state"], initial)
        self.assertEqual(second["state"], initial)
        self.assertEqual(self.backend.state, initial)
        self.assertEqual(self.backend.actions, [])
        self.assertFalse(first["lease"]["held"])

    def test_concurrent_acquire_has_exactly_one_winner(self):
        barrier = threading.Barrier(2)

        def acquire(session):
            barrier.wait(timeout=2)
            try:
                return session, self.call(session, "acquire")
            except server.APIError as error:
                self.assertEqual(error.code, "lease_busy")
                return session, None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(acquire, (self.a, self.b)))
        winners = [(session, lease) for session, lease in results if lease]
        self.assertEqual(len(winners), 1)
        winner, lease = winners[0]
        loser = self.b if winner is self.a else self.a
        self.assert_rejected(loser, "release", expected_code="lease_required", lease_token=lease["lease_token"])
        self.assert_rejected(loser, "renew", expected_code="lease_required", lease_token=lease["lease_token"])
        self.call(winner, "release", lease_token=lease["lease_token"])
        self.assertIn("lease_token", self.call(loser, "acquire"))

    def test_expired_lease_cannot_act_renew_or_release_a_new_lease(self):
        old_lease = self.call(self.a, "acquire")
        self.clock.advance(31)
        new_lease = self.call(self.b, "acquire")
        self.assertGreater(new_lease["generation"], old_lease["generation"])
        self.assertNotEqual(new_lease["lease_token"], old_lease["lease_token"])
        current = self.call(self.a, "status")
        self.assert_rejected(self.a, "act", expected_code="lease_required", **self.action(current, old_lease))
        self.assert_rejected(self.a, "renew", expected_code="lease_required", lease_token=old_lease["lease_token"])
        self.assert_rejected(self.a, "release", expected_code="lease_required", lease_token=old_lease["lease_token"])
        current_b = self.call(self.b, "status")
        result = self.call(self.b, "act", **self.action(current_b, new_lease))
        self.assertTrue(result["accepted"])
        self.assertEqual(len(self.backend.actions), 1)

    def test_renew_extends_lease_and_release_allows_another_session(self):
        lease = self.call(self.a, "acquire")
        self.clock.advance(20)
        self.call(self.a, "renew", lease_token=lease["lease_token"])
        self.clock.advance(15)
        self.assert_rejected(self.b, "acquire", expected_code="lease_busy")
        self.call(self.a, "release", lease_token=lease["lease_token"])
        self.assertIn("lease_token", self.call(self.b, "acquire"))

    def test_external_desktop_change_invalidates_snapshot_before_action(self):
        lease = self.call(self.a, "acquire")
        old = self.call(self.a, "status")
        self.backend.state["value"] = 99
        self.assert_rejected(self.a, "act", expected_code="stale_snapshot", **self.action(old, lease))
        self.assertEqual(self.backend.actions, [])
        fresh = self.call(self.a, "status")
        result = self.call(
            self.a, "act", **self.action(fresh, lease, request_id="request-2")
        )
        self.assertTrue(result["accepted"])

    def test_expired_snapshot_is_rejected_even_when_state_is_unchanged(self):
        old = self.call(self.a, "status")
        self.clock.advance(31)
        lease = self.call(self.a, "acquire")
        self.assert_rejected(self.a, "act", expected_code="stale_snapshot", **self.action(old, lease))
        self.assertEqual(self.backend.actions, [])

    def test_successful_action_invalidates_other_pre_action_snapshots(self):
        lease = self.call(self.a, "acquire")
        first = self.call(self.a, "status")
        second = self.call(self.a, "status")
        result = self.call(self.a, "act", **self.action(first, lease))
        self.assertTrue(result["accepted"])
        self.assertGreater(result["revision"], first["revision"])
        self.assert_rejected(
            self.a, "act", expected_code="stale_snapshot", **self.action(second, lease, request_id="request-2")
        )
        self.assertEqual(len(self.backend.actions), 1)

    def test_other_session_cannot_use_snapshot_even_with_its_own_lease(self):
        snapshot_a = self.call(self.a, "status")
        lease_b = self.call(self.b, "acquire")
        self.assert_rejected(self.b, "act", expected_code="stale_snapshot", **self.action(snapshot_a, lease_b))
        self.assertEqual(self.backend.actions, [])

    def test_other_session_cannot_use_lease_even_with_its_own_snapshot(self):
        lease_a = self.call(self.a, "acquire")
        snapshot_b = self.call(self.b, "status")
        self.assert_rejected(self.b, "act", expected_code="lease_required", **self.action(snapshot_b, lease_a))
        self.assertEqual(self.backend.actions, [])

    def test_success_retry_returns_original_result_without_reexecution(self):
        lease = self.call(self.a, "acquire")
        snapshot = self.call(self.a, "status")
        payload = self.action(snapshot, lease)
        first = self.call(self.a, "act", **payload)
        second = self.call(self.a, "act", **payload)
        self.assertEqual(second, first)
        self.assertEqual(len(self.backend.actions), 1)
        self.clock.advance(31)
        self.assertEqual(self.call(self.a, "act", **payload), first)
        changed = copy.deepcopy(payload)
        changed["action"]["node_id"] = "another-button"
        self.assert_rejected(self.a, "act", expected_code="request_conflict", **changed)
        self.assertEqual(len(self.backend.actions), 1)

    def test_uncertain_backend_failure_is_cached_and_not_reexecuted(self):
        lease = self.call(self.a, "acquire")
        snapshot = self.call(self.a, "status")
        payload = self.action(snapshot, lease)
        self.backend.fail_next = True
        first_error = self.assert_rejected(self.a, "act", expected_code="action_outcome_unknown", **payload)
        second_error = self.assert_rejected(self.a, "act", expected_code="action_outcome_unknown", **payload)
        self.assertEqual(type(first_error), type(second_error))
        self.assertEqual(str(first_error), str(second_error))
        self.assertTrue(first_error.details["outcome_unknown"])
        self.assertEqual(len(self.backend.actions), 1)
        self.assertEqual(self.backend.state["value"], 1)

    def test_cached_errors_store_json_data_and_replays_create_fresh_exceptions(self):
        lease = self.call(self.a, "acquire")
        snapshot = self.call(self.a, "status")
        payload = self.action(snapshot, lease)
        self.backend.fail_next = True
        first = self.assert_rejected(self.a, "act", expected_code="action_outcome_unknown", **payload)
        records = self.core.sessions[self.a["session_id"]]["requests"]
        saved = records[payload["request_id"]]["error"]
        self.assertIsInstance(saved, dict)
        self.assertEqual(json.loads(json.dumps(saved)), saved)
        self.assertEqual(saved["status"], first.status)
        self.assertTrue(saved["details"]["request_recorded"])
        self.assertEqual(saved["details"]["request_id"], payload["request_id"])
        second = self.assert_rejected(self.a, "act", expected_code="action_outcome_unknown", **payload)
        self.assertIsNot(first, second)
        self.assertEqual(first.document(), second.document())
        second.details["caller_mutation"] = True
        third = self.assert_rejected(self.a, "act", expected_code="action_outcome_unknown", **payload)
        self.assertIsNot(second, third)
        self.assertNotIn("caller_mutation", third.details)
        self.assertEqual(len(self.backend.actions), 1)

    def test_preflight_backend_error_is_recorded_and_recovery_allows_new_request(self):
        lease = self.call(self.a, "acquire")
        snapshot = self.call(self.a, "status")
        payload = self.action(snapshot, lease)
        attempts = []

        def fail_preflight():
            attempts.append(True)
            raise BackendError("backend_unavailable", "Transient observation failure", {"retryable": True})

        self.backend.on_snapshot = fail_preflight
        first = self.assert_rejected(self.a, "act", expected_code="backend_unavailable", **payload)
        second = self.assert_rejected(self.a, "act", expected_code="backend_unavailable", **payload)
        self.assertEqual(first.status, 503)
        self.assertEqual(first.document(), second.document())
        self.assertTrue(first.details["request_recorded"])
        self.assertTrue(first.details["retryable"])
        self.assertEqual(first.details["request_id"], payload["request_id"])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(self.backend.actions, [])
        record = self.core.sessions[self.a["session_id"]]["requests"][payload["request_id"]]
        self.assertIsInstance(record["error"], dict)
        json.dumps(record)
        self.backend.on_snapshot = None
        recovered = self.call(self.a, "status")
        result = self.call(self.a, "act", **self.action(recovered, lease, request_id="after-recovery"))
        self.assertTrue(result["accepted"])
        self.assertEqual(len(self.backend.actions), 1)

    def test_closing_session_releases_its_lease_and_revokes_credentials(self):
        self.call(self.a, "acquire")
        self.call(self.a, "close")
        self.assert_rejected(self.a, "status", expected_code="invalid_session")
        self.assertIn("lease_token", self.call(self.b, "acquire"))

    def test_bad_session_token_cannot_read_or_release_lease(self):
        lease = self.call(self.a, "acquire")
        impostor = dict(self.a, session_token="invalid-token")
        self.assert_rejected(impostor, "status", expected_code="invalid_session")
        self.assert_rejected(impostor, "release", expected_code="invalid_session", lease_token=lease["lease_token"])
        self.assert_rejected(self.b, "acquire", expected_code="lease_busy")

    def test_invalid_operation_ttl_and_action_payload_are_rejected(self):
        self.assert_rejected(self.a, "not-an-operation", expected_code="not_found")
        for ttl in (0, -1, "30", True, float("nan"), float("inf")):
            with self.subTest(ttl=ttl):
                self.assert_rejected(self.a, "acquire", expected_code="invalid_request", ttl=ttl)
        lease = self.call(self.a, "acquire")
        snapshot = self.call(self.a, "status")
        for index, field in enumerate(("snapshot_id", "lease_token", "request_id", "action")):
            with self.subTest(missing_field=field):
                payload = self.action(snapshot, lease, request_id=f"invalid-{index}")
                del payload[field]
                self.assert_rejected(self.a, "act", expected_code="invalid_request", **payload)
        self.assertEqual(self.backend.actions, [])

    def test_no_visible_change_still_invalidates_old_snapshots(self):
        lease = self.call(self.a, "acquire")
        snapshot = self.call(self.a, "status")
        self.backend.no_effect = True
        self.call(self.a, "act", **self.action(snapshot, lease))
        self.assertEqual(self.backend.state["value"], 0)
        self.assert_rejected(
            self.a, "act", expected_code="stale_snapshot",
            **self.action(snapshot, lease, request_id="request-2")
        )
        self.assertEqual(len(self.backend.actions), 1)

    def test_lease_expiring_during_preflight_observation_prevents_dispatch(self):
        lease = self.call(self.a, "acquire")
        snapshot = self.call(self.a, "status")
        self.backend.on_snapshot = lambda: self.clock.advance(31)
        self.assert_rejected(
            self.a, "act", expected_code="lease_required", **self.action(snapshot, lease)
        )
        self.assertEqual(self.backend.actions, [])

    def test_snapshot_expiring_during_preflight_observation_prevents_dispatch(self):
        lease = self.call(self.a, "acquire", ttl=120)
        snapshot = self.call(self.a, "status")
        self.backend.on_snapshot = lambda: self.clock.advance(31)
        self.assert_rejected(
            self.a, "act", expected_code="stale_snapshot", **self.action(snapshot, lease)
        )
        self.assertEqual(self.backend.actions, [])

    def test_unadvertised_or_unobserved_actions_never_reach_backend(self):
        lease = self.call(self.a, "acquire")
        snapshot = self.call(self.a, "status")
        invalid_actions = [
            ({"kind": "perform", "node_id": "button", "action": "AXDelete"}, "unsupported"),
            ({"kind": "perform", "node_id": "missing", "action": "AXPress"}, "element_unavailable"),
            ({"kind": "set_value", "node_id": "button", "value": "text"}, "unsupported"),
            ({"kind": "focus", "node_id": "button"}, "unsupported"),
            ({"kind": "activate_app", "pid": 999}, "element_unavailable"),
            ({"kind": "activate_app", "pid": True}, "invalid_action"),
            ({"kind": "run_shell", "command": "echo wrong"}, "invalid_action"),
        ]
        for index, (action, error_code) in enumerate(invalid_actions):
            with self.subTest(action=action):
                self.assert_rejected(
                    self.a, "act", expected_code=error_code,
                    **self.action(snapshot, lease, request_id=f"invalid-action-{index}", action=action)
                )
        self.assertEqual(self.backend.actions, [])

    def test_simultaneous_status_reads_serialize_backend_access(self):
        self.backend.delay = 0.005
        barrier = threading.Barrier(4)

        def read_repeatedly(index):
            session = self.a if index % 2 else self.b
            barrier.wait(timeout=2)
            for _ in range(5):
                self.call(session, "status")

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(read_repeatedly, range(4)))
        self.assertEqual(self.backend.max_active, 1)
        self.assertEqual(self.backend.actions, [])


class HTTPTransportTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.core = server.ComputerServer(self.backend)
        self.token = "test-bearer-token-for-local-transport"
        self.httpd = server.LocalHTTPServer(("127.0.0.1", 0), self.core, self.token)
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())

    def request(self, method, path, payload=None, *, headers=None, raw_body=None):
        request_headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        if headers:
            request_headers.update(headers)
        body = raw_body if raw_body is not None else (
            json.dumps(payload).encode() if payload is not None else None
        )
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=2)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read()), dict(response.getheaders())
        finally:
            connection.close()

    def session_call(self, session, operation, payload=None):
        return self.request(
            "POST", f"/v1/sessions/{session['session_id']}/{operation}",
            payload if payload is not None else {},
            headers={"X-Session-Token": session["session_token"]},
        )

    def test_health_requires_bearer_and_returns_no_store(self):
        status, body, _ = self.request("GET", "/v1/health", headers={"Authorization": ""})
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")
        status, body, headers = self.request("GET", "/v1/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["result"]["ok"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("token", json.dumps(body))

    def test_browser_origin_and_unexpected_host_are_rejected(self):
        for headers in (
            {"Origin": "https://example.invalid"},
            {"Host": f"example.invalid:{self.httpd.server_port}"},
        ):
            with self.subTest(headers=headers):
                status, body, _ = self.request(
                    "POST", "/v1/sessions", {"agent_id": "untrusted"}, headers=headers
                )
                self.assertEqual(status, 403)
                self.assertEqual(body["error"]["code"], "forbidden")
        self.assertEqual(self.core.sessions, {})

    def test_invalid_json_nonfinite_json_and_nonobject_bodies_are_rejected(self):
        for raw in (b"{", b'{"agent_id":NaN}', b"[]", b"null", b"{}trailing"):
            with self.subTest(raw=raw):
                status, body, _ = self.request("POST", "/v1/sessions", raw_body=raw)
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], "invalid_request")
        self.assertEqual(self.core.sessions, {})

    def test_session_credentials_are_required_after_server_authentication(self):
        _, created, _ = self.request("POST", "/v1/sessions", {"agent_id": "http-agent"})
        session = created["result"]
        status, body, _ = self.request(
            "POST", f"/v1/sessions/{session['session_id']}/status", {}
        )
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "invalid_session")

    def test_full_session_action_retry_and_close_over_http(self):
        status, created, _ = self.request("POST", "/v1/sessions", {"agent_id": "http-agent"})
        self.assertEqual(status, 200)
        session = created["result"]
        _, observed, _ = self.session_call(session, "status")
        _, acquired, _ = self.session_call(session, "acquire")
        payload = {
            "snapshot_id": observed["result"]["snapshot_id"],
            "lease_token": acquired["result"]["lease_token"],
            "request_id": "http-action-1",
            "action": {"kind": "perform", "node_id": "button", "action": "AXPress"},
        }
        status, first, _ = self.session_call(session, "act", payload)
        self.assertEqual(status, 200)
        self.assertTrue(first["result"]["accepted"])
        status, retry, _ = self.session_call(session, "act", payload)
        self.assertEqual(status, 200)
        self.assertEqual(first, retry)
        self.assertEqual(len(self.backend.actions), 1)
        status, closed, _ = self.session_call(session, "close")
        self.assertEqual(status, 200)
        self.assertTrue(closed["result"]["closed"])
        self.assertIsNone(self.core.lease)

    def test_binding_nonloopback_address_is_rejected(self):
        with self.assertRaises(ValueError):
            server.LocalHTTPServer(("0.0.0.0", 0), self.core, self.token)


if __name__ == "__main__":
    unittest.main()
