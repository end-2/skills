"""Bounded JSONL transport to one Node worker attached to existing Chrome.

Worker startup checks dependencies only. A snapshot may establish a connection
and trigger Chrome's consent prompt; actions never reconnect or retry.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import queue
import shutil
import subprocess
import threading
from urllib.parse import urlsplit

from backend_common import BackendError


class ChromeBackend:
    backend_name = "chrome"

    def __init__(self, browser_url=None, user_data_dir=None, request_timeout=45):
        if (isinstance(request_timeout, bool) or not isinstance(request_timeout, (int, float))
                or not 1 <= request_timeout <= 120 or not math.isfinite(request_timeout)):
            raise ValueError("request_timeout must be between 1 and 120 seconds")
        if browser_url and user_data_dir:
            raise ValueError("browser_url and user_data_dir are mutually exclusive")
        if browser_url:
            url = urlsplit(browser_url)
            if (url.scheme != "http" or url.hostname != "127.0.0.1" or url.username
                    or url.password or url.path not in ("", "/") or url.query or url.fragment
                    or not url.port or not 1 <= url.port <= 65535):
                raise ValueError("browser_url must be http://127.0.0.1:PORT")
        self.request_timeout = float(request_timeout)
        self._options = {"browser_url": browser_url,
                         "user_data_dir": str(Path(user_data_dir).expanduser().absolute()) if user_data_dir else None,
                         "timeout_ms": max(500, int((request_timeout - min(5, request_timeout / 4)) * 1000))}
        self._lock = threading.RLock()
        self._process = None
        self._closed = False
        self._has_snapshot = False
        self._start()

    def _command(self):
        node = shutil.which("node")
        if not node:
            raise BackendError("dependency_missing", "Node.js >=22.12.0 is required")
        return [node, str(Path(__file__).with_name("chrome_worker.mjs")),
                json.dumps(self._options, ensure_ascii=False, allow_nan=False)]

    def _stop(self):
        process, self._process = self._process, None
        self._has_snapshot = False
        if process is None:
            return
        if process.poll() is None:
            process.kill()  # Only our Node transport, never Chrome.
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
            raise BackendError("backend_unavailable", "Chrome worker is unavailable; read status again")
        encoded = None
        if payload is not None:
            try:
                encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
            except (ValueError, TypeError, UnicodeError) as exc:
                raise BackendError("invalid_action", "Expected finite JSON data") from exc
            if len(encoded) > 131072:
                raise BackendError("invalid_action", "Chrome request exceeds 128 KiB")
        completed = queue.Queue(maxsize=1)

        def transact():
            try:
                if encoded is not None:
                    sent = 0
                    while sent < len(encoded):
                        written = process.stdin.write(encoded[sent:])
                        if not written:
                            raise OSError("Chrome worker closed its input")
                        sent += written
                received = bytearray()
                while b"\n" not in received:
                    chunk = process.stdout.read(65536)
                    if not chunk:
                        raise ValueError("Chrome worker exited before a complete response")
                    received.extend(chunk)
                    if len(received) > 32 * 1024 * 1024:
                        raise ValueError("Chrome worker response exceeds 32 MiB")
                line, extra = bytes(received).split(b"\n", 1)
                if extra.strip():
                    raise ValueError("Chrome worker returned unexpected extra output")
                response = json.loads(line)
                if not isinstance(response, dict) or type(response.get("ok")) is not bool:
                    raise ValueError("Invalid Chrome worker response")
                completed.put((response, None))
            except Exception as exc:
                completed.put((None, exc))

        worker = threading.Thread(target=transact, daemon=True)
        worker.start()
        try:
            response, error = completed.get(timeout=self.request_timeout)
            if error:
                raise error
            if response["ok"]:
                if not isinstance(response.get("result"), dict):
                    raise ValueError("Invalid Chrome worker result")
                return response["result"]
            error = response.get("error")
            if not isinstance(error, dict) or not isinstance(error.get("code"), str) or not isinstance(error.get("message"), str):
                raise ValueError("Invalid Chrome worker error")
            self._has_snapshot = False
            raise BackendError(error["code"], error["message"], error.get("details"))
        except BackendError:
            raise
        except Exception as exc:
            self._stop()
            worker.join(timeout=0.1)
            message = "Chrome worker timed out" if isinstance(exc, queue.Empty) else str(exc)
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
                                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
            ready = self._exchange()
            if ready.get("ready") is not True:
                raise BackendError("backend_unavailable", "Chrome worker failed its dependency handshake")
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
                raise BackendError("element_unavailable", "A fresh snapshot from a live Chrome worker is required")
            self._has_snapshot = False
            return self._exchange({"op": "act", "action": action})

    def close(self):
        with self._lock:
            self._closed = True
            process = self._process
            if process is not None and process.poll() is None:
                # Graceful disconnect leaves the user's Chrome and tabs open.
                try:
                    process.stdin.write(b'{"op":"close"}\n')
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            self._stop()
