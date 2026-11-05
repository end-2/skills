"""Persistent, bounded JSONL client for the native macOS Accessibility helper.

Only standard-library modules are required. The helper is compiled on first
snapshot into the user's cache; no binary or screenshots enter the skill tree.
All actions refer to the latest snapshot's retained AXUIElement references.

Native nodes include ``text_truncated`` and ``title_hash``,
``description_hash``, ``value_hash``. Text is displayed up to 4096 characters;
hashes cover the entire corresponding string so suffix-only changes affect
the server's state fingerprint. Non-string and protected values have a null
hash. Any clipped text also sets the snapshot's ``truncated`` flag.

The server must serialize observation and mutation under its desktop lease.
macOS does not offer an atomic snapshot-and-act transaction; human input and
independent applications can still change state between those operations.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import selectors
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any

from backend_common import BackendError


class MacOSBackend:
    """One persistent helper per backend instance; pipe transactions serialize."""

    def __init__(self, *, max_nodes: int = 1200, max_depth: int = 12,
                 request_timeout: float = 20.0):
        if type(max_nodes) is not int or not 1 <= max_nodes <= 10000:
            raise ValueError("max_nodes must be an integer from 1 to 10000")
        if type(max_depth) is not int or not 1 <= max_depth <= 40:
            raise ValueError("max_depth must be an integer from 1 to 40")
        if isinstance(request_timeout, bool) or not 0 < request_timeout <= 120:
            raise ValueError("request_timeout must be positive and at most 120 seconds")
        self.max_nodes = max_nodes
        self.max_depth = max_depth
        self.request_timeout = request_timeout
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._binary: Path | None = None
        self._closed = False

    def _compile(self) -> Path:
        if platform.system() != "Darwin":
            raise BackendError("backend_unavailable", "The macOS backend requires macOS.")
        compiler = shutil.which("swiftc")
        if not compiler:
            raise BackendError("backend_unavailable", "swiftc is required; install Xcode Command Line Tools manually.")
        source = Path(__file__).with_name("ax_bridge.swift")
        try:
            source_bytes = source.read_bytes()
            signature = hashlib.sha256(source_bytes + platform.machine().encode()).hexdigest()[:24]
            cache = Path.home() / "Library" / "Caches" / "computer-use-server"
            cache.mkdir(parents=True, mode=0o700, exist_ok=True)
            if cache.is_symlink() or cache.stat().st_uid != os.getuid():
                raise BackendError("backend_unavailable", "Helper cache must be owned by the current user and not a symlink.")
            cache.chmod(0o700)
            binary = cache / f"ax-bridge-{signature}"
            if binary.is_file() and not binary.is_symlink() and os.access(binary, os.X_OK):
                return binary
            with tempfile.TemporaryDirectory(prefix="build-", dir=cache) as temp:
                output = Path(temp) / "ax-bridge"
                result = subprocess.run([compiler, "-O", str(source), "-o", str(output)],
                                        capture_output=True, timeout=120, check=False)
                if result.returncode:
                    raise BackendError("backend_unavailable", "Failed to compile the Accessibility helper.",
                                       {"compiler_stderr": result.stderr.decode("utf-8", errors="replace")[-8000:]})
                output.chmod(0o700)
                os.replace(output, binary)
            return binary
        except BackendError:
            raise
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BackendError("backend_unavailable", f"Cannot prepare Accessibility helper: {exc}") from exc

    def _start(self) -> None:
        if self._closed:
            raise BackendError("backend_unavailable", "Backend has been closed.")
        if self._process is not None and self._process.poll() is None:
            return
        self._stop()
        self._binary = self._binary or self._compile()
        try:
            self._process = subprocess.Popen(
                [str(self._binary), str(self.max_nodes), str(self.max_depth)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                bufsize=0, start_new_session=True,
            )
            assert self._process.stdin is not None and self._process.stdout is not None
            os.set_blocking(self._process.stdin.fileno(), False)
            os.set_blocking(self._process.stdout.fileno(), False)
        except OSError as exc:
            self._stop()
            raise BackendError("backend_unavailable", f"Cannot start Accessibility helper: {exc}") from exc

    def _stop(self) -> None:
        process, self._process = self._process, None
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

    def _exchange(self, payload: dict[str, Any]) -> dict[str, Any]:
        process = self._process
        if process is None or process.poll() is not None:
            self._stop()
            raise BackendError("element_unavailable", "Accessibility helper restarted or exited; read a fresh snapshot.")
        assert process.stdin is not None and process.stdout is not None
        deadline = time.monotonic() + self.request_timeout
        try:
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
        except (TypeError, ValueError, UnicodeError) as exc:
            raise BackendError("invalid_action", "Action must contain valid JSON data.") from exc
        if len(encoded) > 131072:
            raise BackendError("invalid_action", "Bridge request is too large.")
        received = bytearray()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdin, selectors.EVENT_WRITE)
                sent = 0
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Accessibility helper request timed out")
                    ready = selector.select(remaining)
                    if not ready:
                        raise TimeoutError("Accessibility helper request timed out")
                    for key, _ in ready:
                        if key.fileobj is process.stdin:
                            try:
                                sent += os.write(process.stdin.fileno(), encoded[sent:])
                            except BlockingIOError:
                                continue
                            if sent == len(encoded):
                                selector.unregister(process.stdin)
                                selector.register(process.stdout, selectors.EVENT_READ)
                        else:
                            try:
                                chunk = os.read(process.stdout.fileno(), 65536)
                            except BlockingIOError:
                                continue
                            if not chunk:
                                raise OSError("Accessibility helper closed its output")
                            received.extend(chunk)
                            if len(received) > 32 * 1024 * 1024:
                                raise ValueError("Accessibility helper response exceeds size limit")
                            if b"\n" in received:
                                line, extra = bytes(received).split(b"\n", 1)
                                if extra.strip():
                                    raise ValueError("Unexpected extra Accessibility helper output")
                                response = json.loads(line)
                                if not isinstance(response, dict) or type(response.get("ok")) is not bool:
                                    raise ValueError("Invalid Accessibility helper response")
                                if response["ok"]:
                                    if not isinstance(response.get("result"), dict):
                                        raise ValueError("Invalid Accessibility result")
                                    return response["result"]
                                error = response.get("error")
                                if not isinstance(error, dict) or not isinstance(error.get("code"), str) or not isinstance(error.get("message"), str):
                                    raise ValueError("Invalid Accessibility error")
                                details = error.get("details")
                                if error["code"] == "permission_denied":
                                    details = dict(details) if isinstance(details, dict) else {}
                                    details.update({"helper_binary": str(self._binary),
                                                    "permission_prompted": False,
                                                    "requires_restart_after_grant": True})
                                raise BackendError(error["code"], error["message"], details)
        except BackendError:
            raise
        except (OSError, ValueError, TimeoutError, UnicodeError) as exc:
            self._stop()
            raise BackendError("backend_unavailable", str(exc),
                               {"requires_snapshot": True, "action_outcome_unknown": payload.get("op") == "act"}) from exc

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self._lock.acquire(timeout=self.request_timeout):
            raise BackendError("backend_unavailable", "Accessibility helper is busy.")
        try:
            if self._closed:
                raise BackendError("backend_unavailable", "Backend has been closed.")
            if payload["op"] == "snapshot":
                self._start()
            return self._exchange(payload)
        finally:
            self._lock.release()

    def snapshot(self) -> dict[str, Any]:
        return self._request({"op": "snapshot"})

    def act(self, action: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(action, dict):
            raise BackendError("invalid_action", "action must be an object.")
        return self._request({"op": "act", "action": action})

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._stop()
