"""Cooperative scheduling invariants and concurrent HTTP form transactions."""
from concurrent.futures import ThreadPoolExecutor
import copy
from pathlib import Path
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import server
from client import request
from mock_backend import MockBackend
from test_server import FakeBackend, FakeClock


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.backend = FakeClock(), FakeBackend()
        self.core = server.ChromeServer(self.backend, clock=self.clock)
        self.a, self.b, self.c = [self.core.create_session(name) for name in ("a", "b", "c")]

    def call(self, session, operation, **payload):
        return self.core.call(session["session_id"], session["session_token"], operation, payload)

    def enqueue(self, session, task_id, **options):
        return self.call(session, "enqueue", task_id=task_id, intent="Complete an isolated work unit", **options)

    def turn(self, session, task_id):
        return self.call(session, "turn", task_id=task_id)

    def finish(self, session, turn, requeue=False):
        return self.call(session, "yield", task_id=turn["task_id"], lease_token=turn["lease_token"], requeue=requeue)

    def reject(self, code, session, operation, **payload):
        with self.assertRaises(server.APIError) as caught:
            self.call(session, operation, **payload)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def press(self, session, turn, snapshot, request_id):
        return self.call(session, "act", request_id=request_id, lease_token=turn["lease_token"],
                         snapshot_id=snapshot["snapshot_id"],
                         action={"kind": "click", "tab_id": "tab-1", "node_id": "button"})

    def test_three_sessions_requeue_at_tail_and_receive_fresh_fenced_tokens(self):
        a1 = self.enqueue(self.a, "a")
        b = self.enqueue(self.b, "b")
        c = self.enqueue(self.c, "c")
        self.assertEqual((a1["state"], b["state"], c["state"]), ("running", "queued", "queued"))
        self.assertEqual((b["queue_position"], c["queue_position"]), (1, 2))
        result = self.finish(self.a, a1, requeue=True)
        self.assertEqual(result["task"]["queue_position"], 2)
        b1 = self.turn(self.b, "b")
        self.assertEqual(b1["state"], "running")
        self.finish(self.b, b1, requeue=True)
        c1 = self.turn(self.c, "c")
        self.assertEqual(c1["state"], "running")
        self.finish(self.c, c1, requeue=True)
        a2 = self.turn(self.a, "a")
        self.assertEqual(a2["state"], "running")
        self.assertEqual([a1["generation"], b1["generation"], c1["generation"], a2["generation"]], [1, 2, 3, 4])
        self.assertNotEqual(a1["lease_token"], a2["lease_token"])
        self.assertEqual(a2["turns"], 2)
        self.reject("lease_required", self.a, "renew", lease_token=a1["lease_token"])

    def test_turn_is_metadata_only_and_does_not_read_backend(self):
        running = self.enqueue(self.a, "a")
        self.enqueue(self.b, "b")
        def unexpected_snapshot():
            self.fail("turn must not request a UI snapshot")
        self.backend.on_snapshot = unexpected_snapshot
        for _ in range(3):
            self.assertEqual(self.turn(self.a, "a")["lease_token"], running["lease_token"])
            self.assertEqual(self.turn(self.b, "b")["state"], "queued")
        self.assertEqual(self.backend.actions, [])

    def test_quantum_is_cooperative_and_renew_does_not_reset_it(self):
        a = self.enqueue(self.a, "a", quantum_ms=100, ttl=10)
        self.enqueue(self.b, "b")
        self.clock.advance(0.15)
        overdue = self.turn(self.a, "a")
        self.assertTrue(overdue["quantum_exhausted"])
        self.assertTrue(overdue["yield_due"])
        self.assertEqual(self.turn(self.b, "b")["state"], "queued")
        self.call(self.a, "renew", lease_token=a["lease_token"], ttl=30)
        renewed = self.turn(self.a, "a")
        self.assertAlmostEqual(renewed["slice_elapsed_ms"], 150)
        self.assertTrue(renewed["yield_due"])
        self.assertEqual(renewed["generation"], a["generation"])
        snapshot = self.call(self.a, "status")
        self.assertTrue(self.press(self.a, a, snapshot, "finish-current-unit")["accepted"])
        self.assertEqual(self.turn(self.b, "b")["state"], "queued")

    def test_expired_quantum_without_waiters_is_not_yield_due(self):
        self.enqueue(self.a, "a", quantum_ms=100)
        self.clock.advance(0.2)
        turn = self.turn(self.a, "a")
        self.assertTrue(turn["quantum_exhausted"])
        self.assertFalse(turn["yield_due"])

    def test_running_backend_action_finishes_before_expired_turn_handoff(self):
        a = self.enqueue(self.a, "a", quantum_ms=100, ttl=1)
        self.enqueue(self.b, "b")
        snapshot = self.call(self.a, "status")
        entered, finish_action, poll_started = threading.Event(), threading.Event(), threading.Event()
        native_act = self.backend.act
        def delayed_act(action):
            entered.set()
            if not finish_action.wait(timeout=3):
                raise RuntimeError("Test action was not released")
            return native_act(action)
        self.backend.act = delayed_act
        def poll_next():
            poll_started.set()
            return self.turn(self.b, "b")
        with ThreadPoolExecutor(max_workers=2) as pool:
            action_future = pool.submit(self.press, self.a, a, snapshot, "running-unit")
            try:
                self.assertTrue(entered.wait(timeout=2))
                self.clock.advance(2)
                turn_future = pool.submit(poll_next)
                self.assertTrue(poll_started.wait(timeout=2))
                time.sleep(0.02)
                self.assertFalse(turn_future.done(), "Next owner was granted while the backend action was still running")
            finally:
                finish_action.set()
            self.assertTrue(action_future.result(timeout=2)["accepted"])
            b = turn_future.result(timeout=2)
        self.assertEqual(b["state"], "running")
        self.assertEqual(self.turn(self.a, "a")["state"], "expired")
        self.assertEqual(len(self.backend.actions), 1)

    def test_yield_retry_cannot_release_a_later_turn_and_changed_options_conflict(self):
        a1 = self.enqueue(self.a, "a")
        self.enqueue(self.b, "b")
        original = self.finish(self.a, a1, requeue=True)
        b1 = self.turn(self.b, "b")
        self.finish(self.b, b1)
        a2 = self.turn(self.a, "a")
        self.assertEqual(self.finish(self.a, a1, requeue=True), original)
        self.assertEqual(self.turn(self.a, "a")["lease_token"], a2["lease_token"])
        self.reject("request_conflict", self.a, "yield", task_id="a", lease_token=a1["lease_token"], requeue=False)
        self.assertEqual(self.turn(self.a, "a")["state"], "running")

    def test_enqueue_is_idempotent_and_terminal_task_never_restarts(self):
        first = self.enqueue(self.a, "a")
        self.assertEqual(self.enqueue(self.a, "a"), first)
        self.reject("request_conflict", self.a, "enqueue", task_id="a", intent="Changed intent")
        completed = self.finish(self.a, first)["task"]
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(self.enqueue(self.a, "a"), completed)
        self.assertNotIn("lease_token", completed)
        self.assertEqual(self.call(self.b, "acquire")["generation"], first["generation"] + 1)

    def test_session_can_have_only_one_active_task_and_task_ids_are_private(self):
        self.enqueue(self.a, "a")
        self.enqueue(self.b, "b")
        for session in (self.a, self.b):
            self.reject("task_busy", session, "enqueue", task_id="another", intent="Other unit")
        self.reject("task_not_found", self.b, "turn", task_id="a")
        self.reject("task_not_found", self.b, "cancel", task_id="a")

    def test_manual_lease_finishes_before_queue_and_manual_acquire_cannot_jump_it(self):
        manual = self.call(self.a, "acquire")
        self.reject("lease_busy", self.a, "enqueue", task_id="a", intent="Own manual lease")
        self.assertEqual(self.enqueue(self.b, "b")["state"], "queued")
        self.reject("lease_busy", self.c, "acquire")
        self.call(self.a, "release", lease_token=manual["lease_token"])
        b = self.turn(self.b, "b")
        self.assertEqual(b["state"], "running")
        self.reject("lease_busy", self.c, "acquire")
        self.finish(self.b, b)
        self.assertIn("lease_token", self.call(self.c, "acquire"))

    def test_manual_release_cannot_end_scheduled_turn(self):
        turn = self.enqueue(self.a, "a")
        self.reject("scheduled_turn", self.a, "release", lease_token=turn["lease_token"])
        self.assertEqual(self.turn(self.a, "a")["state"], "running")

    def test_queue_timeout_expires_waiter_without_blocking_next(self):
        a = self.enqueue(self.a, "a", ttl=120)
        self.enqueue(self.b, "b", queue_timeout=1)
        self.enqueue(self.c, "c", queue_timeout=10)
        self.clock.advance(2)
        self.assertEqual(self.turn(self.b, "b")["state"], "expired")
        self.assertEqual(self.turn(self.c, "c")["queue_position"], 1)
        self.finish(self.a, a)
        self.assertEqual(self.turn(self.c, "c")["state"], "running")
        self.assertEqual(self.enqueue(self.b, "b", queue_timeout=1)["state"], "expired")

    def test_lease_ttl_expiration_grants_next_and_fences_old_owner(self):
        a = self.enqueue(self.a, "a", ttl=1)
        self.enqueue(self.b, "b")
        self.clock.advance(1)
        self.assertEqual(self.turn(self.a, "a")["state"], "expired")
        b = self.turn(self.b, "b")
        self.assertEqual(b["state"], "running")
        self.assertGreater(b["generation"], a["generation"])
        self.reject("lease_required", self.a, "yield", task_id="a", lease_token=a["lease_token"], requeue=False)
        self.reject("lease_required", self.a, "renew", lease_token=a["lease_token"])
        snapshot = self.call(self.a, "status")
        with self.assertRaises(server.APIError) as caught:
            self.press(self.a, a, snapshot, "expired-owner")
        self.assertEqual(caught.exception.code, "lease_required")
        self.assertEqual(self.backend.actions, [])

    def test_every_grant_invalidates_pregrant_and_previous_turn_snapshots(self):
        before = self.call(self.a, "status")
        a1 = self.enqueue(self.a, "a")
        with self.assertRaises(server.APIError) as caught:
            self.press(self.a, a1, before, "pregrant")
        self.assertEqual(caught.exception.code, "stale_snapshot")
        first = self.call(self.a, "status")
        a2 = self.finish(self.a, a1, requeue=True)["task"]
        with self.assertRaises(server.APIError) as caught:
            self.press(self.a, a2, first, "previous-turn")
        self.assertEqual(caught.exception.code, "stale_snapshot")
        fresh = self.call(self.a, "status")
        self.assertGreater(fresh["revision"], first["revision"])
        self.assertTrue(self.press(self.a, a2, fresh, "fresh-turn")["accepted"])

    def test_status_crossing_lease_handoff_returns_usable_snapshot_at_new_revision(self):
        self.call(self.a, "acquire", ttl=1)
        self.assertEqual(self.enqueue(self.b, "b")["state"], "queued")
        self.backend.on_snapshot = lambda: self.clock.advance(1)
        observed = self.call(self.b, "status")
        self.backend.on_snapshot = None
        self.assertEqual(observed["lease"]["owner_session_id"], self.b["session_id"])
        self.assertEqual(observed["lease"]["task_id"], "b")
        turn = self.turn(self.b, "b")
        self.assertEqual(turn["state"], "running")
        unchanged = self.call(self.b, "status")
        self.assertEqual(unchanged["revision"], observed["revision"])
        self.assertTrue(self.press(self.b, turn, observed, "after-status-handoff")["accepted"])
        self.assertEqual(len(self.backend.actions), 1)

    def test_cancel_only_queued_work_and_terminal_cancel_is_idempotent(self):
        a = self.enqueue(self.a, "a")
        self.enqueue(self.b, "b")
        self.enqueue(self.c, "c")
        self.reject("turn_active", self.a, "cancel", task_id="a")
        cancelled = self.call(self.b, "cancel", task_id="b")
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(self.call(self.b, "cancel", task_id="b"), cancelled)
        self.assertEqual(self.turn(self.c, "c")["queue_position"], 1)
        self.finish(self.a, a)
        self.assertEqual(self.turn(self.c, "c")["state"], "running")

    def test_close_removes_waiting_session_and_hands_off_running_lease(self):
        self.enqueue(self.a, "a")
        self.enqueue(self.b, "b")
        self.enqueue(self.c, "c")
        self.call(self.b, "close")
        self.assertEqual(self.turn(self.c, "c")["queue_position"], 1)
        self.call(self.a, "close")
        self.assertEqual(self.turn(self.c, "c")["state"], "running")

    def test_idle_session_expiration_leaves_no_queue_or_lease_obstruction(self):
        self.enqueue(self.a, "a")
        self.enqueue(self.b, "b")
        self.clock.advance(3601)
        new_session = self.core.create_session("new")
        turn = self.enqueue(new_session, "new-task")
        self.assertEqual(turn["state"], "running")
        self.assertEqual(turn["waiting_count"], 0)
        self.reject("invalid_session", self.a, "turn", task_id="a")
        self.reject("invalid_session", self.b, "turn", task_id="b")

    def test_256_turn_limit_rejects_requeue_without_losing_current_lease(self):
        turn = self.enqueue(self.a, "a")
        for _ in range(255):
            turn = self.finish(self.a, turn, requeue=True)["task"]
        self.assertEqual(turn["turns"], 256)
        self.reject("capacity", self.a, "yield", task_id="a", lease_token=turn["lease_token"], requeue=True)
        current = self.turn(self.a, "a")
        self.assertEqual(current["state"], "running")
        self.assertEqual(current["lease_token"], turn["lease_token"])
        self.assertEqual(self.finish(self.a, current)["task"]["state"], "completed")

    def test_task_history_limit_keeps_terminal_ids_idempotent(self):
        for index in range(256):
            turn = self.enqueue(self.a, f"task-{index}")
            self.finish(self.a, turn)
        self.reject("capacity", self.a, "enqueue", task_id="overflow", intent="Complete an isolated work unit")
        self.assertEqual(self.enqueue(self.a, "task-0")["state"], "completed")
        self.assertEqual(self.enqueue(self.b, "separate-session")["state"], "running")

    def test_scheduler_payloads_are_strict_and_validate_finite_ranges(self):
        invalid_options = [
            {"task_id": ""}, {"task_id": "x" * 129}, {"intent": ""}, {"intent": "x" * 513},
            {"quantum_ms": 99}, {"quantum_ms": 10001}, {"quantum_ms": True}, {"quantum_ms": float("nan")},
            {"queue_timeout": 0}, {"queue_timeout": 301}, {"queue_timeout": float("inf")},
            {"ttl": 0}, {"ttl": 121}, {"ttl": "30"}, {"snapshot_id": "queued-ui-target"},
        ]
        for options in invalid_options:
            with self.subTest(options=options):
                payload = dict(task_id="a", intent="Complete unit")
                payload.update(options)
                self.reject("invalid_request", self.a, "enqueue", **payload)
        turn = self.enqueue(self.a, "a")
        for operation in ("turn", "cancel"):
            self.reject("invalid_request", self.a, operation, task_id="a", extra=True)
        for requeue in (None, 0, "true"):
            self.reject("invalid_request", self.a, "yield", task_id="a", lease_token=turn["lease_token"], requeue=requeue)
        self.reject("invalid_request", self.a, "yield", task_id="a", lease_token=turn["lease_token"])

    def test_large_json_integer_options_return_input_errors_without_mutating_scheduler(self):
        for field in ("ttl", "quantum_ms", "queue_timeout"):
            with self.subTest(field=field):
                payload = {"task_id": "a", "intent": "Complete unit", field: 10 ** 1000}
                error = self.reject("invalid_request", self.a, "enqueue", **payload)
                self.assertEqual(error.status, 400)
                self.reject("task_not_found", self.a, "turn", task_id="a")
        acquired = self.call(self.a, "acquire")
        error = self.reject("invalid_request", self.b, "acquire", ttl=10 ** 1000)
        self.assertEqual(error.status, 400)
        error = self.reject("invalid_request", self.a, "renew", lease_token=acquired["lease_token"], ttl=10 ** 1000)
        self.assertEqual(error.status, 400)
        self.call(self.a, "release", lease_token=acquired["lease_token"])
        self.assertEqual(self.enqueue(self.b, "b")["state"], "running")


class RecordingFormBackend(MockBackend):
    def __init__(self):
        super().__init__()
        self.submitted = []

    def act(self, action):
        result = super().act(action)
        if action["kind"] == "click":
            self.submitted.append(self.value)
        return result


class ConcurrentSchedulerHTTPTests(unittest.TestCase):
    def test_three_concurrent_clients_get_fifo_turns_and_preserve_form_transactions(self):
        backend = RecordingFormBackend()
        core = server.ChromeServer(backend)
        token = "scheduler-test-bearer-token"
        httpd = server.LocalHTTPServer(("127.0.0.1", 0), core, token)
        thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        endpoint = f"http://127.0.0.1:{httpd.server_port}"
        def api(session, operation, payload=None):
            reply = request(endpoint, token, f"/v1/sessions/{session['session_id']}/{operation}", payload or {}, session["session_token"])
            self.assertTrue(reply.get("ok"), reply)
            return reply["result"]
        blocker = request(endpoint, token, "/v1/sessions", {"agent_id": "initial-gate"})["result"]
        manual = api(blocker, "acquire")
        start_barrier, enqueued_barrier = threading.Barrier(4), threading.Barrier(4)
        released = threading.Event()
        guard = threading.Lock()
        positions, executions = {}, []

        def run_agent(name):
            opened = request(endpoint, token, "/v1/sessions", {"agent_id": name})
            self.assertTrue(opened.get("ok"), opened)
            session = opened["result"]
            start_barrier.wait(timeout=5)
            queued = api(session, "enqueue", {"task_id": name, "intent": "Fill, submit, verify", "quantum_ms": 100})
            self.assertEqual(queued["state"], "queued")
            with guard:
                positions[name] = queued["queue_position"]
            enqueued_barrier.wait(timeout=5)
            self.assertTrue(released.wait(timeout=5))
            try:
                for round_number in range(3):
                    deadline = time.monotonic() + 5
                    while True:
                        turn = api(session, "turn", {"task_id": name})
                        if turn["state"] == "running":
                            break
                        self.assertEqual(turn["state"], "queued")
                        self.assertLess(time.monotonic(), deadline, "Timed out waiting for FIFO turn")
                        time.sleep(0.003)
                    value = f"{name}:round-{round_number} 한글"
                    observed = api(session, "status")
                    payload = {"snapshot_id": observed["snapshot_id"], "lease_token": turn["lease_token"],
                               "request_id": f"{name}-set-{round_number}",
                               "action": {"kind": "fill", "tab_id": "tab-1", "node_id": "input", "value": value}}
                    self.assertTrue(api(session, "act", payload)["accepted"])
                    # Give other real HTTP clients time to inspect turn metadata.
                    time.sleep(0.004)
                    observed = api(session, "status")
                    nodes = {node["id"]: node for node in observed["state"]["nodes"]}
                    self.assertEqual(nodes["input"]["value"], value)
                    before_count = int(nodes["result"]["value"])
                    press = dict(payload, snapshot_id=observed["snapshot_id"], request_id=f"{name}-press-{round_number}",
                                 action={"kind": "click", "tab_id": "tab-1", "node_id": "submit"})
                    first = api(session, "act", press)
                    self.assertEqual(api(session, "act", copy.deepcopy(press)), first)
                    verified = api(session, "status")
                    nodes = {node["id"]: node for node in verified["state"]["nodes"]}
                    self.assertEqual(nodes["input"]["value"], value)
                    self.assertEqual(int(nodes["result"]["value"]), before_count + 1)
                    with guard:
                        executions.append((turn["generation"], name, round_number, value))
                    api(session, "yield", {"task_id": name, "lease_token": turn["lease_token"], "requeue": round_number < 2})
                self.assertEqual(api(session, "turn", {"task_id": name})["state"], "completed")
            finally:
                api(session, "close")

        try:
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = [pool.submit(run_agent, name) for name in ("a", "b", "c")]
                start_barrier.wait(timeout=5)
                enqueued_barrier.wait(timeout=5)
                self.assertEqual(sorted(positions.values()), [1, 2, 3])
                api(blocker, "release", {"lease_token": manual["lease_token"]})
                released.set()
                for future in futures:
                    future.result(timeout=10)
            initial_fifo = sorted(positions, key=positions.get)
            ordered = sorted(executions)
            self.assertEqual([item[1] for item in ordered], initial_fifo * 3)
            self.assertEqual([item[0] for item in ordered], list(range(2, 11)))
            self.assertEqual(backend.submitted, [item[3] for item in ordered])
            self.assertEqual(backend.presses, 9)
            self.assertIsNone(core.lease)
            api(blocker, "close")
        finally:
            released.set()
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
