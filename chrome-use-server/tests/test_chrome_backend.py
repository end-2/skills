"""Browser worker transport recovery without touching any browser/profile."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from backend_common import BackendError
from chrome_backend import ChromeBackend


FAKE_WORKER = '''
import json, sys, time
mode = sys.argv[1]
if mode == "startup_error":
    print(json.dumps({"ok": False, "error": {"code": "dependency_missing", "message": "missing"}}), flush=True)
    sys.exit(1)
print(json.dumps({"ok": True, "result": {"ready": True}}), flush=True)
for line in sys.stdin:
    payload = json.loads(line)
    if payload["op"] == "close":
        break
    if payload["op"] == "act" and mode == "timeout":
        time.sleep(10)
    if payload["op"] == "act" and mode == "exit":
        sys.exit(1)
    if payload["op"] == "snapshot" and mode == "error":
        reply = {"ok": False, "error": {"code": "connection_required", "message": "enable debugging"}}
    else:
        reply = {"ok": True, "result": {"operation": payload["op"]}}
    print(json.dumps(reply), flush=True)
'''


class ChromeBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.worker = Path(self.temp.name) / "worker.py"
        self.worker.write_text(FAKE_WORKER)
        self.addCleanup(self.temp.cleanup)

    def create(self, mode="ok", timeout=2):
        command = [sys.executable, "-u", str(self.worker), mode]
        patcher = patch.object(ChromeBackend, "_command", return_value=command)
        patcher.start()
        self.addCleanup(patcher.stop)
        backend = ChromeBackend(request_timeout=timeout)
        self.addCleanup(backend.close)
        return backend

    def test_startup_only_handshakes_and_act_requires_snapshot(self):
        backend = self.create()
        with self.assertRaises(BackendError) as caught:
            backend.act({"kind": "new_tab", "url": "about:blank"})
        self.assertEqual(caught.exception.code, "element_unavailable")
        self.assertEqual(backend.snapshot(), {"operation": "snapshot"})
        self.assertEqual(backend.act({"kind": "new_tab", "url": "about:blank"}), {"operation": "act"})
        with self.assertRaises(BackendError):
            backend.act({"kind": "new_tab", "url": "about:blank"})

    def test_structured_connection_error_does_not_break_worker(self):
        backend = self.create("error")
        process = backend._process
        with self.assertRaises(BackendError) as caught:
            backend.snapshot()
        self.assertEqual(caught.exception.code, "connection_required")
        self.assertIs(backend._process, process)
        self.assertFalse(backend._has_snapshot)

    def test_timeout_kills_only_worker_and_never_replays_action(self):
        backend = self.create("timeout", timeout=1)
        backend.snapshot()
        with self.assertRaises(BackendError) as caught:
            backend.act({"kind": "new_tab", "url": "about:blank"})
        self.assertTrue(caught.exception.details["action_outcome_unknown"])
        self.assertIsNone(backend._process)
        with self.assertRaises(BackendError):
            backend.act({"kind": "new_tab", "url": "about:blank"})
        self.assertIsNone(backend._process)
        self.assertEqual(backend.snapshot(), {"operation": "snapshot"})

    def test_worker_exit_during_action_has_unknown_outcome(self):
        backend = self.create("exit")
        backend.snapshot()
        with self.assertRaises(BackendError) as caught:
            backend.act({"kind": "new_tab", "url": "about:blank"})
        self.assertTrue(caught.exception.details["action_outcome_unknown"])

    def test_missing_dependency_is_startup_error(self):
        with self.assertRaises(BackendError) as caught:
            self.create("startup_error")
        self.assertEqual(caught.exception.code, "dependency_missing")

    def test_configuration_rejects_remote_endpoints_and_nonfinite_timeouts(self):
        for url in ("https://127.0.0.1:9222", "http://example.com:9222", "http://localhost:9222",
                    "http://127.0.0.1:9222/path", "http://u:p@127.0.0.1:9222", "http://127.0.0.1:9222#x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ChromeBackend(browser_url=url)
        for timeout in (True, float("nan"), float("inf"), 0, -1, 121):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                ChromeBackend(request_timeout=timeout)


if __name__ == "__main__":
    unittest.main()
