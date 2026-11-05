"""Bounded native calls over JSONL, with portable Windows/POSIX pipe handling.

One child owns all UI objects and COM/AT-SPI state on a single thread. Killing a
timed-out worker invalidates its references; an action never starts a new child.
"""
from __future__ import annotations

import json
from pathlib import Path
import queue
import subprocess
import sys
import threading

from backend_common import BackendError


class ProcessBackend:
    def __init__(self, backend_name, *, request_timeout=20.0):
        if backend_name not in ("windows", "linux"):
            raise ValueError("Only Windows and Linux use the Python native worker")
        if isinstance(request_timeout, bool) or not 0 < request_timeout <= 120:
            raise ValueError("request_timeout must be between 0 and 120 seconds")
        self.backend_name = backend_name
        self.request_timeout = request_timeout
        self._lock = threading.RLock()
        self._process = None
        self._closed = False
        self._has_snapshot = False
        # Fail early at server startup for missing OS dependencies/session access.
        self._start()

    def _command(self):
        return [sys.executable, "-u", str(Path(__file__).with_name("python_worker.py")), self.backend_name]

    def _stop(self):
        process, self._process = self._process, None
        self._has_snapshot = False
        if process is None:
            return
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()

    def _exchange(self, payload=None):
        process = self._process
        if process is None:
            raise BackendError("backend_unavailable", "Native worker is not running; read status again")
        encoded = None
        if payload is not None:
            try:
                encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
            except (ValueError, TypeError, UnicodeError) as exc:
                raise BackendError("invalid_action", "Expected finite JSON data") from exc
            if len(encoded) > 131072:
                raise BackendError("invalid_action", "Native request exceeds 128 KiB")
        completed = queue.Queue(maxsize=1)

        def transact():
            try:
                if encoded is not None:
                    sent = 0
                    while sent < len(encoded):
                        written = process.stdin.write(encoded[sent:])
                        if not written:
                            raise OSError("Native worker closed its input")
                        sent += written
                received = bytearray()
                while b"\n" not in received:
                    chunk = process.stdout.read(65536)
                    if not chunk:
                        raise ValueError("Native worker exited before returning a complete response")
                    received.extend(chunk)
                    if len(received) > 32 * 1024 * 1024:
                        raise ValueError("Native worker response exceeds 32 MiB")
                line, extra = bytes(received).split(b"\n", 1)
                if extra.strip():
                    raise ValueError("Native worker returned unexpected extra output")
                response = json.loads(line)
                if not isinstance(response, dict) or type(response.get("ok")) is not bool:
                    raise ValueError("Invalid native response")
                completed.put((response, None))
            except Exception as exc:
                completed.put((None, exc))

        # select() cannot wait on anonymous Windows pipes. A bounded transaction
        # thread handles both blocking writes and reads; timeout kills the child.
        worker = threading.Thread(target=transact, daemon=True)
        worker.start()
        try:
            response, error = completed.get(timeout=self.request_timeout)
            if error:
                raise error
            if response["ok"]:
                if not isinstance(response.get("result"), dict):
                    raise ValueError("Invalid native result")
                return response["result"]
            error = response.get("error")
            if not isinstance(error, dict) or not isinstance(error.get("code"), str) or not isinstance(error.get("message"), str):
                raise ValueError("Invalid native error")
            self._has_snapshot = False
            raise BackendError(error["code"], error["message"], error.get("details"))
        except BackendError:
            raise
        except Exception as exc:
            self._stop()
            worker.join(timeout=0.1)
            message = "Native worker timed out" if isinstance(exc, queue.Empty) else str(exc)
            raise BackendError("backend_unavailable", message,
                               {"requires_snapshot": True,
                                "action_outcome_unknown": bool(payload and payload.get("op") == "act")}) from exc

    def _start(self):
        if self._closed:
            raise BackendError("backend_unavailable", "Backend is closed")
        if self._process is not None and self._process.poll() is None:
            return
        self._stop()
        try:
            self._process = subprocess.Popen(self._command(), stdin=subprocess.PIPE,
                                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                             bufsize=0)
            ready = self._exchange()
            if ready.get("ready") is not True:
                raise BackendError("backend_unavailable", "Native worker failed its startup handshake")
        except Exception:
            self._stop()
            raise

    def snapshot(self):
        with self._lock:
            self._start()
            result = self._exchange({"op": "snapshot"})
            self._has_snapshot = True
            return result

    def act(self, action):
        with self._lock:
            if self._closed or not self._has_snapshot or self._process is None or self._process.poll() is not None:
                raise BackendError("element_unavailable", "A fresh snapshot from a live worker is required")
            self._has_snapshot = False
            return self._exchange({"op": "act", "action": action})

    def close(self):
        with self._lock:
            self._closed = True
            self._stop()
