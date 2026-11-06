#!/usr/bin/env python3
"""Loopback-only shared Chrome broker with FIFO leases and replay-safe actions.

Session, scheduler, HTTP, and replay core adapted from computer-use-server.
This skill is self-contained and does not import its sibling at runtime.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import platform
import secrets
import threading
import time
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from backend_common import BackendError
from browser_actions import validate_action
from platform_support import default_state_dir, default_token_path, private_file, lock_file


class APIError(Exception):
    def __init__(self, code, message, status=409, details=None):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.details = details or {}

    def document(self):
        return {"code": self.code, "message": self.message, "details": self.details}


def require_string(value, name, maximum=256):
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise APIError("invalid_request", f"{name} must be a nonempty string of at most {maximum} characters", 400)
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def fingerprint(state):
    return hashlib.sha256(canonical(state).encode()).hexdigest()


class ChromeServer:
    """One process, one backend, one browser lock. Never run multiple workers."""

    def __init__(self, backend, clock=time.monotonic, lease_ttl=30, snapshot_ttl=30):
        self.backend, self.clock = backend, clock
        self.lease_ttl, self.snapshot_ttl = lease_ttl, snapshot_ttl
        self.lock = threading.RLock()
        self.sessions = {}
        self.lease = None
        self.generation = 0
        self.revision = 0
        self.last_fingerprint = None
        self.instance_id = secrets.token_urlsafe(16)
        # Only intentions are queued. UI targets must be observed after a turn
        # is granted; a queued snapshot/node reference would already be stale.
        self.ready = []

    def _finish_lease(self, state=None):
        if self.lease and self.lease.get("task_id") and state:
            session = self.sessions.get(self.lease["owner"])
            if session:
                session["tasks"][self.lease["task_id"]]["state"] = state
        self.lease = None

    def _grant_next(self):
        if self.lease or not self.ready:
            return
        sid, task_id = self.ready.pop(0)
        session = self.sessions[sid]
        task = session["tasks"][task_id]
        now = self.clock()
        self.generation += 1
        self.revision += 1
        session["snapshots"].clear()
        task.update(state="running", started=now, turns=task["turns"] + 1)
        self.lease = {"owner": sid, "task_id": task_id, "token": secrets.token_urlsafe(32),
                      "generation": self.generation, "deadline": now + task["options"]["ttl"]}

    def _expire(self):
        now = self.clock()
        if self.lease and self.lease["deadline"] <= now:
            self._finish_lease("expired")
        for sid in list(self.sessions):
            if self.sessions[sid]["touched"] + 3600 <= now:
                del self.sessions[sid]
                if self.lease and self.lease["owner"] == sid:
                    self.lease = None
        ready = []
        for sid, task_id in self.ready:
            session = self.sessions.get(sid)
            if not session:
                continue
            task = session["tasks"][task_id]
            if task["state"] != "queued":
                continue
            if task["queue_deadline"] <= now:
                task["state"] = "expired"
            else:
                ready.append((sid, task_id))
        self.ready = ready
        self._grant_next()

    def create_session(self, agent_id):
        require_string(agent_id, "agent_id", 128)
        with self.lock:
            self._expire()
            if len(self.sessions) >= 128:
                raise APIError("capacity", "Too many sessions; close an unused session", 429)
            sid, token = secrets.token_urlsafe(24), secrets.token_urlsafe(32)
            self.sessions[sid] = {"token": token, "agent_id": agent_id,
                                  "touched": self.clock(), "snapshots": {}, "requests": {}, "tasks": {}}
            return {"session_id": sid, "session_token": token, "instance_id": self.instance_id,
                    "idle_timeout_seconds": 3600}

    def _session(self, sid, token):
        self._expire()
        session = self.sessions.get(sid) if isinstance(sid, str) else None
        if not session or not isinstance(token, str) or not hmac.compare_digest(session["token"], token):
            raise APIError("invalid_session", "Invalid or expired session", 401)
        session["touched"] = self.clock()
        return session

    def _ttl(self, payload):
        ttl = payload.get("ttl", self.lease_ttl)
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not 1 <= ttl <= 120 or not math.isfinite(ttl):
            raise APIError("invalid_request", "ttl must be between 1 and 120 seconds", 400)
        return ttl

    def _owned_lease(self, sid, token):
        self._expire()
        if not self.lease or self.lease["owner"] != sid or not isinstance(token, str) or not hmac.compare_digest(self.lease["token"], token):
            raise APIError("lease_required", "Acquire a current lease before acting")
        return self.lease

    def _lease_result(self):
        return {"lease_token": self.lease["token"], "generation": self.lease["generation"],
                "expires_in": max(0, self.lease["deadline"] - self.clock())}

    def _task(self, session, task_id):
        require_string(task_id, "task_id", 128)
        task = session["tasks"].get(task_id)
        if task is None:
            raise APIError("task_not_found", "Task is not owned by this session", 404)
        return task

    def _task_result(self, sid, task):
        key = (sid, task["task_id"])
        result = {"task_id": task["task_id"], "intent": task["options"]["intent"],
                  "state": task["state"], "quantum_ms": task["options"]["quantum_ms"],
                  "turns": task["turns"], "waiting_count": len(self.ready),
                  "queue_position": self.ready.index(key) + 1 if key in self.ready else None}
        if task["state"] == "queued":
            result["queue_expires_in"] = max(0, task["queue_deadline"] - self.clock())
        if task["state"] == "running":
            elapsed = max(0, (self.clock() - task["started"]) * 1000)
            exhausted = elapsed >= task["options"]["quantum_ms"]
            result.update(self._lease_result(), slice_elapsed_ms=elapsed,
                          quantum_exhausted=exhausted, yield_due=exhausted and bool(self.ready))
        return result

    def _enqueue(self, sid, session, payload):
        allowed = {"task_id", "intent", "quantum_ms", "ttl", "queue_timeout"}
        if set(payload) - allowed:
            raise APIError("invalid_request", "Unsupported enqueue fields", 400)
        task_id = require_string(payload.get("task_id"), "task_id", 128)
        intent = require_string(payload.get("intent"), "intent", 512)
        quantum = payload.get("quantum_ms", 1000)
        timeout = payload.get("queue_timeout", 60)
        for value, name, low, high in ((quantum, "quantum_ms", 100, 10000),
                                        (timeout, "queue_timeout", 1, 300)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high or not math.isfinite(value):
                raise APIError("invalid_request", f"{name} must be between {low} and {high}", 400)
        options = {"intent": intent, "quantum_ms": quantum, "ttl": self._ttl(payload),
                   "queue_timeout": timeout}
        existing = session["tasks"].get(task_id)
        if existing:
            if existing["options"] != options:
                raise APIError("request_conflict", "task_id was already used with different options")
            return self._task_result(sid, existing)
        if any(task["state"] in {"queued", "running"} for task in session["tasks"].values()):
            raise APIError("task_busy", "Finish or cancel the session's current task first")
        if self.lease and self.lease["owner"] == sid:
            raise APIError("lease_busy", "Release the manual lease before enqueuing a task")
        if len(session["tasks"]) >= 256:
            raise APIError("capacity", "Session task history is full; create a new session", 429)
        task = {"task_id": task_id, "options": options, "state": "queued", "turns": 0,
                "queue_deadline": self.clock() + timeout, "yields": {}}
        session["tasks"][task_id] = task
        self.ready.append((sid, task_id))
        self._grant_next()
        return self._task_result(sid, task)

    def _yield(self, sid, session, payload):
        if set(payload) != {"task_id", "lease_token", "requeue"} or not isinstance(payload.get("requeue"), bool):
            raise APIError("invalid_request", "yield requires task_id, lease_token and boolean requeue", 400)
        task = self._task(session, payload["task_id"])
        token = require_string(payload["lease_token"], "lease_token")
        # The unique lease token identifies this boundary, so a lost yield
        # response can be replayed without releasing the agent's NEXT turn.
        saved = task["yields"].get(token)
        if saved:
            if saved["requeue"] != payload["requeue"]:
                raise APIError("request_conflict", "This turn was already yielded with different options")
            return copy.deepcopy(saved["result"])
        lease = self._owned_lease(sid, token)
        if lease.get("task_id") != task["task_id"]:
            raise APIError("lease_required", "This lease does not own the requested task")
        if payload["requeue"] and task["turns"] >= 256:
            raise APIError("capacity", "Task turn limit reached; finish this task with requeue=false", 429)
        self._finish_lease()
        session["snapshots"].clear()
        if payload["requeue"]:
            task.update(state="queued", queue_deadline=self.clock() + task["options"]["queue_timeout"])
            self.ready.append((sid, task["task_id"]))
        else:
            task["state"] = "completed"
        self._grant_next()
        result = {"yielded": True, "task": self._task_result(sid, task)}
        task["yields"][token] = {"requeue": payload["requeue"], "result": copy.deepcopy(result)}
        return result

    def _observe(self):
        state = self.backend.snapshot()
        digest = fingerprint(state)
        if digest != self.last_fingerprint:
            self.revision += 1
            self.last_fingerprint = digest
        return state, digest

    def _validate_action(self, action, state):
        try:
            return validate_action(action, state)
        except BackendError as error:
            status = 409 if error.code in {"element_unavailable", "stale_snapshot"} else 400
            raise APIError(error.code, error.message, status, error.details) from error

    def call(self, session_id, session_token, operation, payload=None):
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise APIError("invalid_request", "Request body must be an object", 400)
        with self.lock:
            session = self._session(session_id, session_token)
            if operation == "close":
                if self.lease and self.lease["owner"] == session_id:
                    self.lease = None
                del self.sessions[session_id]
                self._expire()
                return {"closed": True}
            if operation == "enqueue":
                return self._enqueue(session_id, session, payload)
            if operation in {"turn", "cancel"}:
                if set(payload) != {"task_id"}:
                    raise APIError("invalid_request", f"{operation} requires task_id only", 400)
                task = self._task(session, payload["task_id"])
                if operation == "cancel":
                    if task["state"] == "running":
                        raise APIError("turn_active", "Finish the active work unit and yield instead")
                    if task["state"] == "queued":
                        task["state"] = "cancelled"
                        self._expire()
                return self._task_result(session_id, task)
            if operation == "yield":
                return self._yield(session_id, session, payload)
            if operation == "acquire":
                ttl = self._ttl(payload)
                if self.lease:
                    raise APIError("lease_busy", "Chrome is currently leased", details={
                        "owner_session_id": self.lease["owner"],
                        "retry_after_seconds": max(0, self.lease["deadline"] - self.clock())})
                self.generation += 1
                self.lease = {"owner": session_id, "token": secrets.token_urlsafe(32),
                              "generation": self.generation, "deadline": self.clock() + ttl}
                return self._lease_result()
            if operation == "renew":
                ttl = self._ttl(payload)
                lease = self._owned_lease(session_id, payload.get("lease_token"))
                lease["deadline"] = self.clock() + ttl
                return self._lease_result()
            if operation == "release":
                lease = self._owned_lease(session_id, payload.get("lease_token"))
                if lease.get("task_id"):
                    raise APIError("scheduled_turn", "Use yield to finish or requeue a scheduled work unit")
                self.lease = None
                self._grant_next()
                return {"released": True}
            if operation == "status":
                state, digest = self._observe()
                # Observation can outlast a lease. Apply any handoff before
                # storing the snapshot so a new grant cannot invalidate it.
                self._expire()
                snapshot_id = secrets.token_urlsafe(24)
                snapshots = session["snapshots"]
                while len(snapshots) >= 16:
                    del snapshots[next(iter(snapshots))]
                snapshots[snapshot_id] = {"digest": digest, "revision": self.revision,
                                          "created": self.clock()}
                lease = {"held": bool(self.lease)}
                if self.lease:
                    lease.update(owner_session_id=self.lease["owner"],
                                 expires_in=max(0, self.lease["deadline"] - self.clock()))
                    if self.lease["owner"] == session_id and self.lease.get("task_id"):
                        task = self._task_result(session_id, session["tasks"][self.lease["task_id"]])
                        lease.update(task_id=task["task_id"], quantum_ms=task["quantum_ms"],
                                     slice_elapsed_ms=task["slice_elapsed_ms"], yield_due=task["yield_due"],
                                     quantum_exhausted=task["quantum_exhausted"], waiting_count=len(self.ready))
                return {"snapshot_id": snapshot_id, "revision": self.revision,
                        "snapshot_ttl_seconds": self.snapshot_ttl, "state": state, "lease": lease}
            if operation == "act":
                return self._act(session_id, session, payload)
            raise APIError("not_found", "Unknown operation", 404)

    def _act(self, sid, session, payload):
        request_id = require_string(payload.get("request_id"), "request_id", 128)
        try:
            digest = fingerprint(payload)
        except (TypeError, ValueError):
            raise APIError("invalid_request", "Request must contain finite JSON values", 400)
        records = session["requests"]
        if request_id in records:
            saved = records[request_id]
            if saved["digest"] != digest:
                raise APIError("request_conflict", "request_id was already used with a different payload")
            if "error" in saved:
                error = saved["error"]
                raise APIError(error["code"], error["message"], error["status"], copy.deepcopy(error["details"]))
            return copy.deepcopy(saved["result"])
        if len(records) >= 256:
            raise APIError("capacity", "Session action history is full; create a new session", 429)
        # Retain all terminal results for this session, including validation failures.
        record = {"digest": digest}
        records[request_id] = record
        try:
            if set(payload) != {"request_id", "snapshot_id", "lease_token", "action"}:
                raise APIError("invalid_request", "act requires request_id, snapshot_id, lease_token, action only", 400)
            self._owned_lease(sid, payload.get("lease_token"))
            snapshot_id = require_string(payload.get("snapshot_id"), "snapshot_id")
            snapshot = session["snapshots"].get(snapshot_id)
            if not snapshot or self.clock() - snapshot["created"] >= self.snapshot_ttl:
                raise APIError("stale_snapshot", "Snapshot is missing or expired; read status again")
            state, current = self._observe()
            # Observation can be slow; recheck lease and snapshot deadlines before dispatch.
            self._owned_lease(sid, payload.get("lease_token"))
            if self.clock() - snapshot["created"] >= self.snapshot_ttl or snapshot["digest"] != current or snapshot["revision"] != self.revision:
                raise APIError("stale_snapshot", "Browser state changed; read status and re-evaluate")
            native_action = self._validate_action(payload.get("action"), state) or payload["action"]
            # Invalidate all snapshots even if an action has no visible effect or times out.
            self.revision += 1
            try:
                result = self.backend.act(native_action)
            except Exception as exc:
                raise APIError("action_outcome_unknown", "Action did not return a confirmed result; inspect status before deciding next steps", 502,
                               {"backend_code": getattr(exc, "code", "backend_error"), "outcome_unknown": True}) from exc
            response = {"accepted": True, "result": result, "revision": self.revision,
                        "request_id": request_id, "verification_required": True}
            record["result"] = copy.deepcopy(response)
            return response
        except APIError as exc:
            exc.details = dict(exc.details, request_recorded=True, request_id=request_id)
            record["error"] = dict(exc.document(), status=exc.status)
            raise
        except Exception as exc:
            error = APIError(getattr(exc, "code", "backend_unavailable"), str(exc), 503,
                             dict(getattr(exc, "details", None) or {}, request_recorded=True, request_id=request_id))
            record["error"] = dict(error.document(), status=error.status)
            raise error from exc


class LocalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, broker, token):
        if address[0] != "127.0.0.1":
            raise ValueError("Only 127.0.0.1 is supported; do not expose browser control to a network")
        self.broker, self.token = broker, token
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "ChromeUse/1"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *args):
        pass  # UI state, credentials, and typed values must not enter access logs.

    def _reply(self, status, body):
        raw = canonical(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self, method):
        try:
            # Reject browser-originated access, including DNS rebinding hostnames.
            if self.headers.get("Origin") is not None or self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
                raise APIError("forbidden", "Only direct loopback clients are supported", 403)
            auth = self.headers.get("Authorization", "")
            if not hmac.compare_digest(auth, "Bearer " + self.server.token):
                raise APIError("unauthorized", "Bearer authentication required", 401)
            if method == "GET" and self.path == "/v1/health":
                result = {"ok": True, "instance_id": self.server.broker.instance_id,
                          "backend": type(self.server.broker.backend).__name__,
                          "selected_backend": getattr(self.server.broker.backend, "backend_name", None),
                          "os": platform.system()}
            elif method == "POST":
                if self.headers.get_content_type() != "application/json" or self.headers.get("Transfer-Encoding"):
                    raise APIError("invalid_request", "Send an application/json body with Content-Length", 400)
                try:
                    size = int(self.headers.get("Content-Length", "-1"))
                except ValueError:
                    size = -1
                if not 0 < size <= 65536:
                    raise APIError("invalid_request", "Body must contain 1 to 65536 bytes", 400)
                try:
                    raw = self.rfile.read(size)
                    if len(raw) != size:
                        raise ValueError("Incomplete body")
                    payload = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite number")))
                except (ValueError, UnicodeDecodeError):
                    raise APIError("invalid_request", "Body must be valid finite JSON", 400)
                if not isinstance(payload, dict):
                    raise APIError("invalid_request", "Body must be an object", 400)
                if self.path == "/v1/sessions":
                    result = self.server.broker.create_session(payload.get("agent_id"))
                else:
                    parts = self.path.split("/")
                    if len(parts) != 5 or parts[1:3] != ["v1", "sessions"]:
                        raise APIError("not_found", "Unknown endpoint", 404)
                    result = self.server.broker.call(parts[3], self.headers.get("X-Session-Token"), parts[4], payload)
            else:
                raise APIError("not_found", "Unknown endpoint", 404)
            self._reply(200, {"ok": True, "result": result})
        except APIError as exc:
            self._reply(exc.status, {"ok": False, "error": exc.document()})
        except (TimeoutError, BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:
            self._reply(503, {"ok": False, "error": {"code": getattr(exc, "code", "backend_unavailable"),
                                                      "message": str(exc), "details": getattr(exc, "details", None) or {}}})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def load_or_create_token(path):
    path = Path(path).expanduser()
    with private_file(path, "a+", create=True) as stream, lock_file(stream):
        stream.seek(0)
        content = stream.read()
        if not content:
            content = secrets.token_urlsafe(48) + "\n"
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        token = content.strip()
    if len(token) < 32:
        raise ValueError("Token file must contain at least 32 characters")
    return token


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("chrome", "mock"), default="chrome")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--token-file", default=os.environ.get("CHROME_USE_TOKEN_FILE", str(default_token_path())))
    connection = parser.add_mutually_exclusive_group()
    connection.add_argument("--browser-url", help="Optional loopback Chrome debugging URL")
    connection.add_argument("--user-data-dir", help="Existing Chrome user data directory used for endpoint discovery")
    parser.add_argument("--request-timeout", type=float, default=45)
    args = parser.parse_args()
    token_path = Path(args.token_file).expanduser()
    try:
        if not 1 <= args.request_timeout <= 120 or not math.isfinite(args.request_timeout):
            raise ValueError("request-timeout must be between 1 and 120 seconds")
        with ExitStack() as resources:
            # One real Chrome broker per OS user, even across ports or token paths.
            if args.backend != "mock":
                stream = resources.enter_context(private_file(default_state_dir() / "browser.lock", "a+", create=True))
                try:
                    resources.enter_context(lock_file(stream, blocking=False))
                except BlockingIOError:
                    parser.exit(1, "A Chrome broker is already running for this user. Reuse it.\n")
            token = load_or_create_token(token_path)
            if args.backend == "mock":
                from mock_backend import MockBackend
                backend = MockBackend()
            else:
                from chrome_backend import ChromeBackend
                backend = ChromeBackend(browser_url=args.browser_url, user_data_dir=args.user_data_dir,
                                        request_timeout=args.request_timeout)
            resources.callback(backend.close)
            broker = ChromeServer(backend)
            httpd = resources.enter_context(LocalHTTPServer(("127.0.0.1", args.port), broker, token))
            print(f"chrome-use-server os={platform.system()} backend={args.backend} url=http://127.0.0.1:{httpd.server_port} token_file={token_path}", flush=True)
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                pass
    except (BackendError, OSError, ValueError) as exc:
        parser.exit(1, canonical({"ok": False, "error": {"code": getattr(exc, "code", "startup_failed"),
                    "message": str(exc), "details": getattr(exc, "details", None) or {}}}) + "\n")


if __name__ == "__main__":
    main()
