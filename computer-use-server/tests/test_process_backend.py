import json
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from backend_common import BackendError
from process_backend import ProcessBackend


FAKE_WORKER = r'''
import json, sys, time
def emit(result=None, error=None):
    sys.stdout.buffer.write((json.dumps({'ok':error is None, 'error':error} if error else {'ok':True,'result':result}, ensure_ascii=False)+'\n').encode('utf-8'))
    sys.stdout.buffer.flush()
emit({'ready':True})
calls=0
for line in sys.stdin.buffer:
    request=json.loads(line)
    if request['op']=='snapshot':
        emit({'platform':'fake','value':'한글 🐍','calls':calls})
    elif request['action'].get('hang'):
        time.sleep(60)
    elif request['action'].get('fail'):
        emit(error={'code':'unsupported','message':'unsupported action','details':{'native':True}})
    else:
        calls+=1
        emit({'calls':calls,'value':request['action'].get('value')})
'''


class FakeProcessBackend(ProcessBackend):
    script = FAKE_WORKER

    def _command(self):
        return [sys.executable, "-u", "-c", self.script]


class ProcessBackendTests(unittest.TestCase):
    def create(self, **kwargs):
        backend = FakeProcessBackend("windows", **kwargs)
        self.addCleanup(backend.close)
        return backend

    def test_persistent_state_and_utf8_protocol(self):
        backend = self.create()
        self.assertEqual(backend.snapshot()["value"], "한글 🐍")
        value = "가" * 16000
        self.assertEqual(backend.act({"value": value}), {"calls": 1, "value": value})
        self.assertEqual(backend.snapshot()["calls"], 1)

    def test_actions_require_fresh_observation(self):
        backend = self.create()
        with self.assertRaises(BackendError) as caught:
            backend.act({})
        self.assertEqual(caught.exception.code, "element_unavailable")
        backend.snapshot()
        backend.act({})
        with self.assertRaises(BackendError):
            backend.act({})

    def test_native_error_preserves_details_and_invalidates_snapshot(self):
        backend = self.create()
        backend.snapshot()
        with self.assertRaises(BackendError) as caught:
            backend.act({"fail": True})
        self.assertEqual(caught.exception.code, "unsupported")
        self.assertTrue(caught.exception.details["native"])
        with self.assertRaises(BackendError):
            backend.act({})
        self.assertEqual(backend.snapshot()["calls"], 0)

    def test_timeout_kills_worker_and_never_replays_action(self):
        backend = self.create(request_timeout=0.3)
        backend.snapshot()
        process = backend._process
        with self.assertRaises(BackendError) as caught:
            backend.act({"hang": True})
        self.assertTrue(caught.exception.details["action_outcome_unknown"])
        self.assertIsNotNone(process.poll())
        with self.assertRaises(BackendError):
            backend.act({})
        self.assertEqual(backend.snapshot()["calls"], 0)
        self.assertIsNot(backend._process, process)

    def test_dead_worker_cannot_receive_action(self):
        backend = self.create()
        backend.snapshot()
        backend._process.kill()
        backend._process.wait(timeout=2)
        with self.assertRaises(BackendError) as caught:
            backend.act({})
        self.assertEqual(caught.exception.code, "element_unavailable")
        self.assertEqual(backend.snapshot()["calls"], 0)

    def test_startup_failure_is_structured(self):
        class MissingDependency(FakeProcessBackend):
            script = "print(" + repr(json.dumps({"ok": False, "error": {
                "code": "dependency_missing", "message": "Install native dependency", "details": {"package": "example"}}})) + ")"
        with self.assertRaises(BackendError) as caught:
            MissingDependency("linux")
        self.assertEqual(caught.exception.code, "dependency_missing")
        self.assertEqual(caught.exception.details["package"], "example")

    def test_closed_backend_does_not_restart(self):
        backend = self.create()
        backend.close()
        with self.assertRaises(BackendError):
            backend.snapshot()

    def test_descendant_holding_stdout_cannot_block_timeout_cleanup(self):
        class InheritedPipe(FakeProcessBackend):
            script = r'''
import json, subprocess, sys, time
subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)'], stdin=subprocess.DEVNULL)
print(json.dumps({'ok':True,'result':{'ready':True}}), flush=True)
for line in sys.stdin:
    time.sleep(60)
'''
        backend = InheritedPipe("linux", request_timeout=0.15)
        self.addCleanup(backend.close)
        started = time.monotonic()
        with self.assertRaises(BackendError):
            backend.snapshot()
        self.assertLess(time.monotonic() - started, 1.5)


if __name__ == "__main__":
    unittest.main()
