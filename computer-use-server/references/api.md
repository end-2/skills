# HTTP API v1

The default endpoint is `http://127.0.0.1:8765`. Every request requires `Authorization: Bearer <token-file contents>`. Session requests also require `X-Session-Token`. POST requests use `Content-Type: application/json` and `Content-Length`. Browser origins, the `localhost` alias, and arbitrary Host headers are rejected.

Success responses have the form `{"ok":true,"result":{...}}`; errors use `{"ok":false,"error":{"code":"...","message":"...","details":{...}}}`. The API does not log UI text. Status responses can contain task data, so share only the necessary portions between agents.

| Path | Method | Body | Result |
| --- | --- | --- | --- |
| `/v1/health` | GET | None | Process liveness, instance_id, backend class, selected_backend, os. Does not test UI access |
| `/v1/sessions` | POST | `{"agent_id":"worker-a"}` | session_id, session_token, instance_id, idle_timeout_seconds |
| `/v1/sessions/{id}/status` | POST | `{}` | snapshot_id, revision, state, lease, snapshot_ttl_seconds |
| `/v1/sessions/{id}/acquire` | POST | `{"ttl":30}` | lease_token, generation, expires_in |
| `/v1/sessions/{id}/renew` | POST | `{"lease_token":"...","ttl":30}` | Renewed lease |
| `/v1/sessions/{id}/release` | POST | `{"lease_token":"..."}` | released |
| `/v1/sessions/{id}/enqueue` | POST | `{"task_id":"task-a","intent":"Enter data and verify results","quantum_ms":1000,"ttl":30,"queue_timeout":60}` | Task status object; state=running when granted a turn |
| `/v1/sessions/{id}/turn` | POST | `{"task_id":"task-a"}` | Task status object; lease_token and yield_due for this session's running turn |
| `/v1/sessions/{id}/yield` | POST | `{"task_id":"task-a","lease_token":"...","requeue":true}` | `{"yielded":true,"task":<task status>}`; requeue=false completes the task |
| `/v1/sessions/{id}/cancel` | POST | `{"task_id":"task-a"}` | Task status object; changes queued to cancelled. Running tasks must yield |
| `/v1/sessions/{id}/act` | POST | See below | accepted, result, revision, request_id, verification_required |
| `/v1/sessions/{id}/close` | POST | `{}` | closed; also removes the session's lease, queued tasks, and history |

```json
{
  "request_id": "unique-request-uuid",
  "snapshot_id": "copied-from-status",
  "lease_token": "copied-from-running-turn-or-acquire",
  "action": {"kind": "perform", "node_id": "copied-node-id", "action": "AXPress"}
}
```

`act` accepts exactly these four fields. Each request_id must be unique within its session and at most 128 characters. A retry returns the cached response only when the complete payload matches the original request with that ID. Changing the snapshot or lease also changes the payload. Validation failures are cached too; use a new ID for an action reconsidered after a fresh observation.

## Cooperative scheduling

The scheduler runs in the OS-independent broker. The queue stores task intent only; it does not schedule UI commands or replay previously observed node_id values when a turn arrives. After confirming `running`, the agent performs **fresh status → target selection → act → result verification**. Turns are granted automatically. After yielding, obtain a turn with a new generation; do not reuse the previous snapshot or lease.

| enqueue option | Default | Range and meaning |
| --- | --- | --- |
| task_id | Required | Task identifier within the session, 1–128 characters |
| intent | Required | Task purpose, 1–512 characters. Not a place to store UI commands or selectors |
| quantum_ms | 1000 | 100–10,000 ms. Elapsed turn time after which yielding is recommended |
| ttl | 30 | 1–120 seconds. Lease lifetime for the turn |
| queue_timeout | 60 | 1–300 seconds. Maximum time waiting in the queue |

Each session may have one queued or running task. Repeating enqueue with the same task_id and options returns the current task status. Different options produce `request_conflict`. Enqueuing a completed ID does not restart it; use a new ID for new work.

A task status object's `state` is one of `queued`, `running`, `completed`, `cancelled`, or `expired`. Common fields are task_id, intent, quantum_ms, turns, waiting_count, and queue_position. Queued tasks also include queue_expires_in. Running tasks include these **top-level fields**: lease_token, generation, expires_in, slice_elapsed_ms, quantum_exhausted, and yield_due. Completed, cancelled, and expired are terminal states. In contrast, `state` in a `status` response is the UI state object. That response's lease also includes yield information for the session's scheduled turn.

`turn` does not inspect the UI. While queued, poll at 0.1–0.5-second intervals with a bounded total wait, or perform independent work. CLI `turn --wait-seconds` polls on the client; its default is 0 and its maximum is 30 seconds. This limits the polling budget, not an individual HTTP request's transport timeout. The HTTP `turn` body contains only task_id. Avoid status polling while waiting: it shares the mutex used by other agents' UI operations.

Once the quantum elapses, quantum_exhausted=true; if another task is waiting, yield_due=true as well. Neither forcibly revokes control during an action. Finish a safe work unit, then call `yield(requeue=true)` to join the FIFO queue's tail. If nobody else is waiting, the same task may immediately receive a new turn. `renew` extends only the lease TTL, leaving the quantum start time unchanged. `yield(requeue=false)` marks the task completed. When editing a shared form, keep entry, submission, and result verification in one work unit; do not yield between them.

If a yield response is lost, resend the same task_id, previous lease_token, and requeue value. The server returns the cached response without yielding twice. CLI `retry-yield` resends the saved original payload. Changing requeue for the same turn token produces `request_conflict`. A replay can return an earlier yield's result, so confirm the next turn with `turn`. Completed, cancelled, and expired tasks are never automatically replayed. Queue waiting time is independent of lease TTL and restarts upon requeue. Expiration of either the turn's lease or its queue wait makes the task expired. Do not infer that an already dispatched action failed; inspect fresh state before deciding whether to continue.

Use cancel for queued tasks and yield to finish running tasks. Manual release of a scheduled turn returns `scheduled_turn`. The existing acquire/renew/release flow remains available, but the queue waits for an existing manual lease to end or expire. Enqueue while holding your own manual lease returns `lease_busy`; release it first. A manual owner that keeps renewing prevents guaranteed rotation, so all participating agents should use enqueue.

## UI state

```json
{
  "platform": "macos",
  "desktop_id": "local",
  "frontmost_app": {"pid": 123, "name": "Example", "bundle_id": "com.example.app"},
  "applications": [{"pid": 123, "name": "Example", "bundle_id": "com.example.app"}],
  "roots": ["123/0"],
  "nodes": [{
    "id": "123/0.0", "pid": 123, "role": "AXButton", "title": "Apply",
    "description": null, "value": null, "enabled": true, "focused": false,
    "actions": ["AXPress"], "settable": [], "children": [], "secure": false
  }],
  "truncated": false,
  "capabilities": {"scope": "frontmost_application", "screenshots": false}
}
```

This example shows the macOS schema; its values are not actual node references. Traverse `children` from `roots`. Node IDs refer to a particular observation and are not permanent selectors. Trees cover the active application on macOS, the foreground window on Windows, and the identified active window on Linux. To switch applications, check the observed pid and activation support, activate the target, then read status again. A `null` attribute is unknown or not exposed by the application.

Windows returns `platform: windows` and `capabilities.scope: foreground_window`; nodes include runtime_id and toggle/selection/expand state. Linux returns `platform: linux`, `scope: active_window`, `foreground_status`, and per-application `can_activate`. If the ACTIVE window is missing or ambiguous, frontmost_app=null and mutation capabilities are disabled. Linux's application list contains apps exposed by AT-SPI; Windows lists apps with visible top-level windows.

String title/description/value fields display at most 4,096 characters. Clipping sets both the node's `text_truncated` and the overall `truncated` to true. `title_hash`, `description_hash`, and `value_hash` contain SHA-256 hashes of the full original strings, so changes confined to an omitted suffix still invalidate state verification. A hash cannot reveal omitted text; do not replace an entire long field whose full contents are unknown. Secure fields have both value and value_hash set to null.

## Supported actions

| kind | Fields | Requirements |
| --- | --- | --- |
| `perform` | node_id, action | Name advertised in the node's actions. OS-specific examples: AXPress, Invoke, click |
| `set_value` | node_id, value | value appears in settable; replaces the entire string, up to 16,384 characters |
| `focus` | node_id | focused appears in settable |
| `activate_app` | pid | Positive pid from observed applications. Subject to OS activation restrictions |

Each action object contains only kind and the fields listed for it. Disabled nodes cannot be acted on. Secure input values are neither read nor modified. Even if Accessibility reports a successful value change, verify subsequent state to confirm the application processed the intended operation.

## Error handling

| code | Next step |
| --- | --- |
| `lease_busy` | Check the owner session and retry_after_seconds; back off or perform independent work |
| `lease_required` | For scheduled work, inspect turn for expiry and reassess after fresh observation. In manual mode, acquire → status |
| `scheduled_turn` | Use yield instead of release for a scheduled turn |
| `task_busy` | Finish the current queued/running task, or cancel the queued task, before registering new work |
| `turn_active` | Yield at a safe boundary instead of cancelling a running task |
| `task_not_found` | Check that task_id was registered in this session; do not replay work after a server restart |
| `stale_snapshot` | Read status, reconsider the action, and use a new request_id |
| `request_conflict` | Check for different content under the same act request_id, enqueue task_id, or yield turn token |
| `action_outcome_unknown` | Inspect status for the outcome instead of executing again. retry returns the cached error |
| `permission_denied` | Grant macOS Accessibility permission manually, then restart the server |
| `unsupported` / `element_unavailable` | Read status again and reassess the target or capability |
| `invalid_session` | Check for session expiry or server restart. Investigate uncertain actions before creating a new session |
| `capacity` | Check session/request/task limits. At the task's 256-turn limit, the current lease remains valid, so finish with requeue=false |
| `backend_unavailable` | Check compilation, OS, or process failures. Observe retry semantics when an action response was lost |

HTTP status codes are 401 for authentication failure, 403 for browser/Host rejection, 400 for malformed requests, 404 for missing tasks or routes, 409 for conflicts, 429 for capacity limits, and 502/503 for backend failures. Native errors after dispatch may be conservatively wrapped as `action_outcome_unknown`, with the original code in `details.backend_code`.

Cached act errors include `details.request_recorded: true` and `details.request_id`. The client uses this marker to recognize a terminal response, including a transient backend error during preflight. A cached `action_outcome_unknown` still requires a separate check of the actual UI outcome.

## Lifetimes and limits

- Lease TTL: default 30 seconds; allowed range 1–120 seconds. No automatic renewal.
- Scheduled tasks: one active task per session, at most 256 task records including terminal tasks, and at most 256 turns per task. Default quantum: 1,000 ms; default queue wait: 60 seconds. Requeue on turn 256 returns capacity but retains the current lease, allowing completion with requeue=false.
- Snapshot TTL: 30 seconds; the latest 16 snapshots per session.
- Sessions: expire one hour after the last request; at most 128 per server.
- Action results: 256 per session; discarded on session close/expiry or server shutdown.
- HTTP body: 64 KiB; at most 32 connections handled concurrently. Multibyte text, including Korean, can reach the body byte limit before the character limit.
- Accessibility tree: default 1,200 nodes and depth 12. Slow requests are subject to backend timeout. Partial trees set truncated.
- State verification and actions run sequentially under the server mutex. Even if a lease expires during an action, other actions wait for that call to finish or time out.

A server restart changes instance_id and clears all sessions, leases, task queues, and caches. Do not automatically replay uncertain actions from before the restart under a new request_id or task_id.
