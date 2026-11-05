"""CLI recovery regressions using real HTTP and the in-memory demo desktop."""

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import server
import client
from backend_common import BackendError
from mock_backend import MockBackend
from platform_support import private_file


class TransientBackend(MockBackend):
    def __init__(self):
        super().__init__()
        self.fail_next_snapshot = False
        self.snapshot_calls = 0

    def snapshot(self):
        self.snapshot_calls += 1
        if self.fail_next_snapshot:
            self.fail_next_snapshot = False
            raise BackendError("backend_unavailable", "Transient observation failure", {"retryable": True})
        return super().snapshot()


class ClientRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.token_path = Path(self.directory.name) / "token"
        self.token = "test-token-for-client-regression-only"
        with private_file(self.token_path, "r+", create=True) as stream:
            stream.write(self.token)
        self.session_path = Path(self.directory.name) / "session.json"
        self.backend = TransientBackend()
        self.core = server.ComputerServer(self.backend)
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

    def cli(self, *arguments, expected_exit=0, session_path=None):
        result = subprocess.run(
            [
                sys.executable, str(SCRIPTS / "client.py"),
                "--endpoint", f"http://127.0.0.1:{self.httpd.server_port}",
                "--token-file", str(self.token_path),
                "--session-file", str(session_path or self.session_path),
                *arguments,
            ],
            capture_output=True, text=True, encoding="utf-8", timeout=10, check=False,
        )
        self.assertEqual(result.returncode, expected_exit, msg=f"stdout={result.stdout}\nstderr={result.stderr}")
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def session(self):
        return json.loads(self.session_path.read_text())

    def test_cached_preflight_failure_clears_pending_and_allows_new_action_after_status(self):
        self.cli("open", "--agent-id", "recovery-agent")
        self.cli("status")
        self.cli("acquire")
        self.cli("status")
        action = json.dumps({"kind": "perform", "node_id": "submit", "action": "AXPress"})
        self.backend.fail_next_snapshot = True
        first = self.cli("act", "--action", action, "--request-id", "transient-preflight", expected_exit=1)
        self.assertEqual(first["error"]["code"], "backend_unavailable")
        self.assertTrue(first["error"]["details"]["request_recorded"])
        self.assertEqual(first["error"]["details"]["request_id"], "transient-preflight")
        self.assertFalse(self.session()["pending_unknown"])
        self.assertEqual(self.backend.presses, 0)
        calls_after_failure = self.backend.snapshot_calls

        retry = self.cli("retry", expected_exit=1)
        self.assertEqual(retry, first)
        self.assertEqual(self.backend.snapshot_calls, calls_after_failure)
        self.assertFalse(self.session()["pending_unknown"])
        self.assertEqual(self.backend.presses, 0)

        recovered = self.cli("status")
        self.assertTrue(recovered["ok"])
        self.assertEqual(self.session()["snapshot_id"], recovered["result"]["snapshot_id"])
        result = self.cli("act", "--action", action, "--request-id", "after-recovery")
        self.assertTrue(result["result"]["accepted"])
        self.assertEqual(result["result"]["request_id"], "after-recovery")
        self.assertFalse(self.session()["pending_unknown"])
        self.assertNotIn("snapshot_id", self.session())
        self.assertEqual(self.backend.presses, 1)

    def test_real_http_two_agents_yield_resume_and_retry_without_replaying_actions(self):
        other = Path(self.directory.name) / "other-session.json"
        self.cli("open", "--agent-id", "scheduled-a")
        first = self.cli("enqueue", "--task-id", "task-a", "--intent", "Edit A")
        self.assertEqual(first["result"]["state"], "running")
        self.assertNotIn("lease_token", first["result"])
        self.cli("open", "--agent-id", "scheduled-b", session_path=other)
        queued = self.cli("enqueue", "--task-id", "task-b", "--intent", "Edit B", session_path=other)
        self.assertEqual(queued["result"]["state"], "queued")
        for path, value in ((self.session_path, "from A"), (other, "from B")):
            if path == other:
                self.assertEqual(self.cli("turn", session_path=path)["result"]["state"], "running")
            self.cli("status", session_path=path)
            self.cli("act", "--action", json.dumps({"kind": "set_value", "node_id": "input", "value": value}), session_path=path)
            self.cli("status", session_path=path)
            self.cli("act", "--action", json.dumps({"kind": "perform", "node_id": "submit", "action": "AXPress"}), session_path=path)
            observed = self.cli("status", session_path=path)["result"]["state"]
            self.assertEqual(next(node["value"] for node in observed["nodes"] if node["id"] == "input"), value)
            if path == self.session_path:
                result = self.cli("yield", "--requeue")
                self.assertEqual(result["result"]["task"]["state"], "queued")
            else:
                self.cli("yield", session_path=path)
        resumed = self.cli("turn")
        generation = resumed["result"]["generation"]
        self.assertGreater(generation, first["result"]["generation"])
        self.assertNotIn("snapshot_id", self.session())
        replay = self.cli("retry-yield")
        self.assertEqual(replay["result"]["task"]["generation"], generation)
        self.assertEqual(self.session()["lease_generation"], generation)
        self.assertEqual(self.backend.presses, 2)
        self.cli("yield")
        self.cli("close")
        self.cli("close", session_path=other)


class ClientSchedulerTests(unittest.TestCase):
    """Exercise CLI parsing and durable recovery against API response fixtures."""
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.token_path = Path(self.directory.name) / "token"
        self.session_path = Path(self.directory.name) / "session"
        self.endpoint = "http://127.0.0.1:8765"
        with private_file(self.token_path, "r+", create=True) as stream:
            stream.write("server-secret-never-printed")
        self.write_session({"session_id": "session-a", "session_token": "session-secret-never-printed",
                            "endpoint": self.endpoint})

    def write_session(self, session):
        with private_file(self.session_path, "r+", create=True) as stream:
            json.dump(session, stream)
            stream.truncate()

    def session(self):
        return json.loads(self.session_path.read_text())

    def amend(self, **fields):
        state = self.session()
        state.update(fields)
        self.write_session(state)

    @staticmethod
    def task(state="running", *, generation=1, task_id="task-a"):
        result = {"task_id": task_id, "state": state, "intent": "Edit the form",
                  "quantum_ms": 1000, "queue_position": 0 if state == "running" else 1, "waiting_count": 0}
        if state == "running":
            result.update(lease_token=f"lease-secret-{generation}", generation=generation, expires_in=30,
                          slice_elapsed_ms=1, quantum_exhausted=False, yield_due=False)
        return result

    @staticmethod
    def ok(result):
        return {"ok": True, "result": result}

    def invoke(self, arguments, responses, *, expected=0, clock=None, sleeps=None):
        argv = ["client.py", "--endpoint", self.endpoint, "--token-file", str(self.token_path),
                "--session-file", str(self.session_path), *arguments]
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(sys, "argv", argv))
            request = stack.enter_context(patch.object(client, "request", side_effect=responses))
            if clock is not None:
                stack.enter_context(patch.object(client.time, "monotonic", side_effect=lambda: clock[0]))
                def sleep(seconds):
                    sleeps.append(seconds)
                    clock[0] += seconds
                stack.enter_context(patch.object(client.time, "sleep", side_effect=sleep))
            output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            code = client.main()
        self.assertEqual(code, expected)
        text = output.getvalue()
        self.assertNotIn("lease-secret", text)
        self.assertNotIn("session-secret", text)
        self.assertNotIn("server-secret", text)
        return json.loads(text), request.call_args_list

    def test_enqueue_parses_defaults_persists_task_and_hides_lease(self):
        self.amend(snapshot_id="pre-turn-snapshot")
        reply, calls = self.invoke(["enqueue", "--task-id", "task-a", "--intent", "Edit the form"],
                                   [self.ok(self.task())])
        self.assertEqual(calls[0].args[3], {"task_id": "task-a", "intent": "Edit the form",
                                          "quantum_ms": 1000, "ttl": 30, "queue_timeout": 60})
        self.assertEqual(self.session()["task_id"], "task-a")
        self.assertEqual(self.session()["lease_token"], "lease-secret-1")
        self.assertNotIn("snapshot_id", self.session())
        self.assertNotIn("lease_token", reply["result"])

    def test_invalid_scheduler_arguments_fail_before_network(self):
        cases = [["turn", "--wait-seconds", "31"], ["turn", "--wait-seconds", "nan"],
                 ["enqueue", "--task-id", "a", "--intent", "x", "--quantum-ms", "99"],
                 ["enqueue", "--task-id", "a", "--intent", "x", "--ttl", "0"],
                 ["enqueue", "--task-id", "a", "--intent", "x", "--queue-timeout", "301"],
                 ["enqueue", "--task-id", "a" * 129, "--intent", "x"],
                 ["enqueue", "--task-id", "a", "--intent", "x" * 513]]
        for args in cases:
            with self.subTest(args=args), patch.object(sys, "argv", ["client.py", *args]), patch.object(client, "request") as request, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
                client.main()
            self.assertEqual(stopped.exception.code, 2)
            request.assert_not_called()

    def test_queued_and_terminal_status_drop_old_lease_and_snapshot(self):
        for state in ("queued", "completed", "cancelled", "expired"):
            with self.subTest(state=state):
                self.amend(task_id="task-a", task_state="running", lease_token="lease-secret-1",
                           lease_generation=1, snapshot_id="old")
                self.invoke(["turn"], [self.ok(self.task(state))])
                self.assertEqual(self.session()["task_state"], state)
                for field in ("lease_token", "lease_generation", "snapshot_id"):
                    self.assertNotIn(field, self.session())

    def test_same_turn_keeps_snapshot_and_new_generation_invalidates_it(self):
        self.amend(task_id="task-a", task_state="running", lease_token="lease-secret-1",
                   lease_generation=1, snapshot_id="current")
        self.invoke(["turn"], [self.ok(self.task())])
        self.assertEqual(self.session()["snapshot_id"], "current")
        self.invoke(["turn"], [self.ok(self.task(generation=2))])
        self.assertNotIn("snapshot_id", self.session())
        self.assertEqual(self.session()["lease_generation"], 2)

    def test_wait_polls_only_queued_then_recovers_running_turn(self):
        self.amend(task_id="task-a", task_state="queued")
        clock, sleeps = [0.0], []
        reply, calls = self.invoke(["turn", "--wait-seconds", "2"],
                                  [self.ok(self.task("queued")), self.ok(self.task("queued")), self.ok(self.task(generation=3))],
                                  clock=clock, sleeps=sleeps)
        self.assertEqual(len(calls), 3)
        self.assertEqual(reply["result"]["state"], "running")
        self.assertTrue(all(0.1 <= seconds <= 0.5 for seconds in sleeps))
        self.assertEqual(self.session()["lease_generation"], 3)

    def test_wait_stops_at_budget_and_transport_failure_is_not_retried(self):
        self.amend(task_id="task-a", task_state="queued")
        clock, sleeps = [0.0], []
        reply, calls = self.invoke(["turn", "--wait-seconds", "0.5"],
                                  [self.ok(self.task("queued")), self.ok(self.task("queued"))], clock=clock, sleeps=sleeps)
        self.assertEqual(reply["result"]["state"], "queued")
        self.assertEqual(len(calls), 2)
        self.assertLessEqual(sum(sleeps), 0.5)
        argv = ["client.py", "--token-file", str(self.token_path), "--session-file", str(self.session_path), "turn", "--wait-seconds", "30"]
        with patch.object(sys, "argv", argv), patch.object(client, "request", side_effect=urllib.error.URLError("lost")) as request:
            with self.assertRaises(urllib.error.URLError):
                client.main()
        self.assertEqual(request.call_count, 1)

    def test_yield_persists_exact_payload_before_dispatch_and_masks_nested_token(self):
        self.amend(task_id="task-a", task_state="running", lease_token="lease-secret-1", lease_generation=1, snapshot_id="old")
        def response(*args):
            saved = self.session()
            self.assertTrue(saved["pending_yield"])
            self.assertEqual(saved["last_yield"], args[3])
            return self.ok({"yielded": True, "task": self.task(generation=2)})
        reply, calls = self.invoke(["yield", "--requeue"], response)
        self.assertEqual(calls[0].args[3], {"task_id": "task-a", "lease_token": "lease-secret-1", "requeue": True})
        self.assertFalse(self.session()["pending_yield"])
        self.assertEqual(self.session()["lease_generation"], 2)
        self.assertNotIn("snapshot_id", self.session())
        self.assertNotIn("lease_token", reply["result"]["task"])

    def test_default_yield_completes_and_cancel_updates_terminal_state(self):
        self.amend(task_id="task-a", task_state="running", lease_token="lease-secret-1", lease_generation=1)
        _, calls = self.invoke(["yield"], [self.ok({"yielded": True, "task": self.task("completed")})])
        self.assertFalse(calls[0].args[3]["requeue"])
        self.assertEqual(self.session()["task_state"], "completed")
        self.amend(task_state="queued")
        _, calls = self.invoke(["cancel"], [self.ok(self.task("cancelled"))])
        self.assertEqual(calls[0].args[3], {"task_id": "task-a"})
        self.assertEqual(self.session()["task_state"], "cancelled")

    def test_retry_yield_refreshes_cached_status_and_does_not_replace_different_task(self):
        old = {"task_id": "task-a", "lease_token": "lease-secret-1", "requeue": True}
        self.amend(task_id="task-a", task_state="running", lease_token="lease-secret-3", lease_generation=3,
                   snapshot_id="fresh-third-turn", last_yield=old)
        _, calls = self.invoke(["retry-yield"], [self.ok({"yielded": True, "task": self.task("queued")}), self.ok(self.task(generation=3))])
        self.assertEqual(calls[0].args[3], old)
        self.assertTrue(calls[1].args[2].endswith("/turn"))
        self.assertEqual(self.session()["lease_generation"], 3)
        self.assertEqual(self.session()["snapshot_id"], "fresh-third-turn")
        self.amend(task_id="task-new", task_state="running", lease_token="lease-secret-4", lease_generation=4)
        self.invoke(["retry-yield"], [self.ok({"yielded": True, "task": self.task("completed")}), self.ok(self.task("completed"))])
        self.assertEqual(self.session()["task_id"], "task-new")
        self.assertEqual(self.session()["lease_generation"], 4)

    def test_turn_recovers_lost_yield_only_after_old_lease_is_gone(self):
        self.amend(task_id="task-a", task_state="running", lease_token="lease-secret-1", lease_generation=1,
                   last_yield={"task_id": "task-a", "lease_token": "lease-secret-1", "requeue": True}, pending_yield=True)
        self.invoke(["turn"], [self.ok(self.task())])
        self.assertTrue(self.session()["pending_yield"])
        self.invoke(["turn"], [self.ok(self.task(generation=2))])
        self.assertFalse(self.session()["pending_yield"])
        self.assertEqual(self.session()["lease_generation"], 2)

    def test_action_uncertainty_blocks_ownership_changes_but_retry_stays_action_only(self):
        action = {"request_id": "request-old", "snapshot_id": "snapshot-old", "lease_token": "lease-secret-1",
                  "action": {"kind": "focus", "node_id": "input"}}
        self.amend(task_id="task-a", task_state="running", pending_unknown=True, last_action=action)
        for arguments in (["yield"], ["release"], ["enqueue", "--task-id", "task-b", "--intent", "Next task"], ["close"]):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(ValueError, "Last action"):
                self.invoke(arguments, [])
        _, calls = self.invoke(["retry"], [self.ok({"accepted": True, "request_id": "request-old"})])
        self.assertEqual(calls[0].args[3], action)
        self.assertTrue(calls[0].args[2].endswith("/act"))
        self.assertFalse(self.session()["pending_unknown"])

    def test_lost_enqueue_response_retains_task_id_for_turn_recovery(self):
        with self.assertRaises(urllib.error.URLError):
            self.invoke(["enqueue", "--task-id", "task-a", "--intent", "Edit the form"],
                        urllib.error.URLError("lost reply"))
        self.assertEqual(self.session()["task_id"], "task-a")
        self.assertEqual(self.session()["task_state"], "unknown")
        self.invoke(["turn"], [self.ok(self.task())])
        self.assertEqual(self.session()["task_state"], "running")

    def test_rejected_requeue_preserves_lease_and_clears_pending_yield(self):
        self.amend(task_id="task-a", task_state="running", lease_token="lease-secret-1", lease_generation=1)
        self.invoke(["yield", "--requeue"], [{"ok": False, "error": {"code": "capacity", "message": "Grant history full"}}], expected=1)
        self.assertEqual(self.session()["lease_token"], "lease-secret-1")
        self.assertFalse(self.session()["pending_yield"])


if __name__ == "__main__":
    unittest.main()
