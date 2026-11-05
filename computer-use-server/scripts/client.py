#!/usr/bin/env python3
"""JSON CLI for the shared computer-use broker. Use one session file per agent."""
import argparse
import json
import math
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from platform_support import default_token_path, private_file, lock_file
from status_view import filter_status


def bounded_number(minimum, maximum, *, integer=False):
    def parse(value):
        try:
            number = int(value) if integer else float(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("Expected a number") from exc
        if not minimum <= number <= maximum or not math.isfinite(number):
            raise argparse.ArgumentTypeError(f"Must be between {minimum} and {maximum}")
        return number
    return parse


def bounded_text(maximum):
    def parse(value):
        if not value or len(value) > maximum:
            raise argparse.ArgumentTypeError(f"Must contain 1 to {maximum} characters")
        return value
    return parse


def without_credentials(value):
    """Scheduler replies can nest leases inside task status objects."""
    if isinstance(value, dict):
        return {key: without_credentials(item) for key, item in value.items()
                if key not in {"lease_token", "session_token"}}
    if isinstance(value, list):
        return [without_credentials(item) for item in value]
    return value


def forget_lease(session):
    for key in ("lease_token", "lease_generation", "snapshot_id"):
        session.pop(key, None)


def remember_lease(session, result):
    if (session.get("lease_generation") != result["generation"]
            or session.get("lease_token") != result["lease_token"]):
        session.pop("snapshot_id", None)
    session["lease_token"] = result["lease_token"]
    session["lease_generation"] = result["generation"]


def remember_task(session, task):
    if session.get("task_id") != task["task_id"]:
        forget_lease(session)
    session["task_id"], session["task_state"] = task["task_id"], task["state"]
    if task["state"] == "running":
        remember_lease(session, task)
    else:
        forget_lease(session)
    pending = session.get("last_yield", {})
    if (session.get("pending_yield") and pending.get("task_id") == task["task_id"]
            and (task["state"] != "running" or task.get("lease_token") != pending.get("lease_token"))):
        session["pending_yield"] = False


def request(endpoint, token, route, payload=None, session_token=None):
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("endpoint must be http://127.0.0.1:PORT")
    headers = {"Authorization": "Bearer " + token}
    if session_token:
        headers["X-Session-Token"] = session_token
    raw = None
    if payload is not None:
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(endpoint.rstrip("/") + route, raw, headers)
    # Credentials belong only to the broker, never to an environment proxy.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        response = opener.open(req, timeout=60)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=os.environ.get("CU_ENDPOINT", "http://127.0.0.1:8765"))
    parser.add_argument("--token-file", default=os.environ.get("CU_TOKEN_FILE", str(default_token_path())))
    parser.add_argument("--session-file", default=os.environ.get("CU_SESSION_FILE"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("health")
    create = sub.add_parser("open")
    create.add_argument("--agent-id", required=True)
    status = sub.add_parser("status")
    status.add_argument("--match", help="Filter titles/descriptions by literal text, ignoring case")
    status.add_argument("--role", help="Filter by exact accessibility role")
    status.add_argument("--actionable", action="store_true", help="Show nodes with actions or settable attributes unless disabled")
    status.add_argument("--limit", type=bounded_number(1, 1200, integer=True), help="Limit matching nodes; ancestor nodes are retained")
    for name in ("acquire", "renew"):
        sub.add_parser(name).add_argument("--ttl", type=float, default=30)
    enqueue = sub.add_parser("enqueue", help="Queue a resumable task for a fair scheduler turn")
    enqueue.add_argument("--task-id", required=True, type=bounded_text(128))
    enqueue.add_argument("--intent", required=True, type=bounded_text(512))
    enqueue.add_argument("--quantum-ms", type=bounded_number(100, 10000, integer=True), default=1000)
    enqueue.add_argument("--ttl", type=bounded_number(1, 120), default=30)
    enqueue.add_argument("--queue-timeout", type=bounded_number(1, 300), default=60)
    sub.add_parser("retry-enqueue", help="Resend the exact last enqueue with its original task ID and options")
    turn = sub.add_parser("turn", help="Read the current task state and recover its lease")
    turn.add_argument("--wait-seconds", type=bounded_number(0, 30), default=0)
    yielding = sub.add_parser("yield", help="Finish the current task, or requeue at a safe boundary")
    yielding.add_argument("--requeue", action="store_true")
    sub.add_parser("cancel", help="Cancel the current queued task")
    sub.add_parser("retry-yield", help="Replay the exact last yield, then refresh its task status")
    sub.add_parser("release")
    sub.add_parser("close")
    action = sub.add_parser("act")
    action.add_argument("--action", required=True, help="Action JSON object")
    action.add_argument("--request-id", default=None)
    action.add_argument("--snapshot-id", default=None)
    sub.add_parser("retry", help="Resend the exact last action, preserving its request ID and payload")
    args = parser.parse_args()
    token_path = Path(args.token_file).expanduser()
    with private_file(token_path, "r") as stream:
        token = stream.read().strip()
    if args.command == "health":
        reply = request(args.endpoint, token, "/v1/health")
    else:
        if not args.session_file:
            parser.error("--session-file (or CU_SESSION_FILE) is required; use a unique path for each agent")
        path = Path(args.session_file).expanduser()
        with private_file(path, "r+", create=True) as stream, lock_file(stream):
            content = stream.read()
            session = json.loads(content) if content.strip() else {}

            def save():
                stream.seek(0)
                json.dump(session, stream, ensure_ascii=False)
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())

            def call_session(operation, payload):
                return request(args.endpoint, token, f"/v1/sessions/{session['session_id']}/{operation}",
                               payload, session["session_token"])

            if args.command == "open":
                if session.get("session_id"):
                    raise ValueError("This session file is already open; use it or close it first")
                reply = request(args.endpoint, token, "/v1/sessions", {"agent_id": args.agent_id})
                if reply.get("ok"):
                    session = dict(reply["result"], endpoint=args.endpoint)
                    save()
                    # The credential stays in the session file, not agent-visible output.
                    reply = {"ok": True, "result": {"session_id": session["session_id"],
                             "instance_id": session["instance_id"], "session_file": str(path)}}
            else:
                if not session.get("session_id") or session.get("endpoint") != args.endpoint:
                    raise ValueError("Open a session for this endpoint first")
                op = args.command
                if session.get("pending_unknown") and op in {
                        "act", "yield", "retry-yield", "release", "enqueue", "retry-enqueue", "acquire", "close"}:
                    raise ValueError("Last action has an unknown outcome; use retry with its original payload, then inspect status before changing ownership")
                if session.get("pending_yield") and op in {
                        "act", "yield", "release", "enqueue", "retry-enqueue", "acquire", "renew", "close"}:
                    raise ValueError("Last yield has an unknown outcome; use turn to recover, or retry-yield with its original payload")
                payload = {}
                previous_task = {key: session[key] for key in ("task_id", "task_state") if key in session}
                previous_enqueue = session.get("last_enqueue")
                if op in ("acquire", "renew"):
                    payload["ttl"] = args.ttl
                if op in ("renew", "release"):
                    payload["lease_token"] = session.get("lease_token")
                if op == "act":
                    payload = {"action": json.loads(args.action), "request_id": args.request_id or str(uuid.uuid4()),
                               "snapshot_id": args.snapshot_id or session.get("snapshot_id"),
                               "lease_token": session.get("lease_token")}
                    # Persist before dispatch so a lost response can be retried safely.
                    session["last_action"] = payload
                    session["pending_unknown"] = True
                    save()
                elif op == "retry":
                    if not session.get("last_action"):
                        raise ValueError("No action is available to retry")
                    payload, op = session["last_action"], "act"
                elif op in {"enqueue", "retry-enqueue"}:
                    if op == "retry-enqueue":
                        if not session.get("last_enqueue"):
                            raise ValueError("No enqueue is available to retry; use enqueue with the original task ID and options")
                        payload, op = dict(session["last_enqueue"]), "enqueue"
                    else:
                        payload = {"task_id": args.task_id, "intent": args.intent, "quantum_ms": args.quantum_ms,
                                   "ttl": args.ttl, "queue_timeout": args.queue_timeout}
                    if (session.get("task_id") != payload["task_id"]
                            and session.get("task_state") in {"queued", "running", "unknown"}):
                        raise ValueError("Finish or cancel the current task before enqueueing a different task; use turn to refresh it, or retry-enqueue to recover an unknown enqueue")
                    if (session.get("task_state") == "unknown" and previous_enqueue
                            and previous_enqueue != payload):
                        raise ValueError("Last enqueue has an unknown outcome; use retry-enqueue with its original task ID and options")
                    session["last_enqueue"] = dict(payload)
                    session["task_id"], session["task_state"] = payload["task_id"], "unknown"
                    # The request may never arrive, so turn alone cannot always
                    # recover it. Preserve the exact idempotent enqueue payload.
                    save()
                elif op in {"turn", "yield", "cancel"}:
                    if not session.get("task_id"):
                        raise ValueError("Enqueue a task before using scheduler commands")
                    payload = {"task_id": session["task_id"]}
                    if op == "yield":
                        payload.update(lease_token=session.get("lease_token"), requeue=args.requeue)
                        session["last_yield"], session["pending_yield"] = dict(payload), True
                        save()
                elif op == "retry-yield":
                    if not session.get("last_yield"):
                        raise ValueError("No yield is available to retry")
                    payload, op = session["last_yield"], "yield"
                    if payload["task_id"] == session.get("task_id"):
                        session["pending_yield"] = True
                        save()
                wait_deadline = time.monotonic() + args.wait_seconds if args.command == "turn" else None
                reply = call_session(op, payload)
                if args.command == "retry-yield" and reply.get("ok"):
                    # A cached yield result describes an earlier turn. Never use
                    # its lease or queued state to overwrite the current turn.
                    refreshed = call_session("turn", {"task_id": payload["task_id"]})
                    if refreshed.get("ok"):
                        reply = {"ok": True, "result": {"yielded": True, "task": refreshed["result"]}}
                    else:
                        reply = refreshed
                if args.command == "turn":
                    while reply.get("ok"):
                        remember_task(session, reply["result"])
                        save()
                        remaining = wait_deadline - time.monotonic()
                        if reply["result"]["state"] != "queued" or remaining < 0.1:
                            break
                        time.sleep(min(0.25, remaining))
                        if time.monotonic() >= wait_deadline:
                            break
                        # Only a confirmed queued response triggers another poll.
                        # Transport failures propagate and are never auto-retried.
                        reply = call_session("turn", payload)
                if op == "act":
                    # Only a successful response or a terminal, cached action error settles this request.
                    code = reply.get("error", {}).get("code")
                    recorded = reply.get("error", {}).get("details", {}).get("request_recorded") is True
                    session["pending_unknown"] = (not recorded and code in {
                        "invalid_session", "unauthorized", "forbidden", "backend_unavailable"})
                if reply.get("ok"):
                    result = reply["result"]
                    if op in ("acquire", "renew"):
                        remember_lease(session, result)
                    if op in ("enqueue", "turn", "cancel"):
                        remember_task(session, result)
                    if op == "yield" and payload["task_id"] == session.get("task_id"):
                        session["pending_yield"] = False
                        remember_task(session, result["task"])
                    if op == "status":
                        session["snapshot_id"] = result["snapshot_id"]
                    if op == "release":
                        forget_lease(session)
                    if op == "act":
                        session.pop("snapshot_id", None)
                    if op == "close":
                        session = {}
                elif op == "enqueue":
                    for key in ("task_id", "task_state"):
                        session.pop(key, None)
                    session.update(previous_task)
                    if previous_enqueue and previous_enqueue != payload:
                        session["last_enqueue"] = previous_enqueue
                elif op == "yield" and payload["task_id"] == session.get("task_id"):
                    # A definite rejection (including requeue capacity) leaves
                    # the current lease intact. Auth/server errors may be a lost
                    # success and still need turn/retry-yield recovery.
                    if reply.get("error", {}).get("code") not in {"invalid_session", "unauthorized", "forbidden", "backend_unavailable"}:
                        session["pending_yield"] = False
                save()
    if args.command == "status" and reply.get("ok"):
        reply = filter_status(reply, match=args.match, role=args.role,
                              actionable=args.actionable, limit=args.limit)
    print(json.dumps(without_credentials(reply), ensure_ascii=False, indent=2))
    return 0 if reply.get("ok") else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except (ValueError, OSError, urllib.error.URLError) as exc:
        print(json.dumps({"ok": False, "error": {"code": "client_error", "message": str(exc)}}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
