# Broker API

The CLI implements authenticated loopback HTTP. Global options precede subcommands. Default endpoint: `http://127.0.0.1:8766`. Successful responses are `{"ok":true,"result":...}`; failures contain `error.code`, `message`, and `details`. Do not expose the service to a network.

HTTP calls require `Authorization: Bearer <token-file contents>` and exactly `Host: 127.0.0.1:PORT`, with no Origin header. POST JSON bodies are limited to 64 KiB. Credentials belong only to the local broker; the CLI disables environment proxies and redirects.

| Endpoint | Purpose |
| --- | --- |
| `GET /v1/health` | Broker identity and backend; does not connect to Chrome |
| `POST /v1/sessions` | Create session with `agent_id` |
| `POST /v1/sessions/ID/OPERATION` | Session operation; also requires `X-Session-Token` |

## Sessions and turns

| CLI command | Behavior |
| --- | --- |
| `open --agent-id NAME` | Save a private session token locally |
| `enqueue --task-id ID --intent TEXT` | Queue work; defaults to 1,000 ms advisory quantum, 30 s lease, 60 s queue expiry |
| `turn --wait-seconds 10` | Poll scheduler without reading Chrome; saves a granted lease |
| `status [--match TEXT] [--role ROLE] [--actionable] [--limit N]` | Observe; save snapshot; optional filters affect CLI output only |
| `act --action JSON` | Use saved lease/snapshot and persist a new request ID before dispatch |
| `renew --ttl 30` | Extend current lease without restarting quantum |
| `yield [--requeue]` | Finish work unit or move it to the queue tail |
| `cancel` | Cancel queued work |
| `close` | Close session; does not close tabs or Chrome |
| `retry`, `retry-enqueue`, `retry-yield` | Replay original saved requests |

`enqueue` accepts `--quantum-ms 100..10000`, `--ttl 1..120`, and `--queue-timeout 1..300`. `acquire`/`release` support manual leases, but scheduled work uses `yield` and all participating agents should enqueue for fair rotation. Sessions idle for one hour expire. Each session retains at most 256 action outcomes; reaching that limit requires a new session.

Direct `act` payload: `{"request_id":"unique","snapshot_id":"observed","lease_token":"current","action":{...}}`. Every action requires a session-owned fresh snapshot, a current lease token, and a matching state revision/fingerprint. The broker observes again before dispatch. The same request ID with the same payload returns the cached result; a different payload is rejected.

## Browser state

`state.tabs` contains stable tab IDs and observed URLs/titles. `selected_tab_id` is the broker's logical target. `nodes`/`roots` describe that tab's structured DOM, including frame context, labels, supported actions, settable values, and focus where available. Password values are omitted. `pending_dialog` describes a JavaScript dialog when present; `truncated` and capability/scope fields describe incomplete observation.

All targeted actions explicitly include `tab_id`. Node, keyboard, scroll, navigate, and dialog actions require the currently selected tab. Selecting another tab requires a new observation before acting on it. `close_tab` can target any observed tab.

| `kind` | Additional fields | Meaning |
| --- | --- | --- |
| `select_tab` | `tab_id` | Change logical browser target |
| `new_tab` | `url` | Create a tab and select it |
| `navigate` | `tab_id`, `url` | Navigate selected tab |
| `close_tab` | `tab_id` | Close the specified observed tab |
| `click`, `hover` | `tab_id`, `node_id` | Perform advertised node action |
| `focus` | `tab_id`, `node_id` | Focus a node advertising `focused` as settable |
| `fill` | `tab_id`, `node_id`, `value` | Replace a field advertising `value` as settable |
| `press_key` | `tab_id`, `key` | Press a key/chord such as `Enter` or `Control+A` in current page focus |
| `scroll` | `tab_id`, `direction`, `pixels` | Scroll up/down/left/right by 1–5,000 pixels |
| `handle_dialog` | `tab_id`, `response`, optional `text` | Accept/dismiss observed dialog; text only for accepting a prompt |

Navigation accepts HTTP, HTTPS, and `about:blank`. It rejects credentials in URLs, script URLs, and file URLs. Fill/prompt text is bounded to 16,384 characters. Unknown kinds/fields, unobserved IDs, disabled targets, and unadvertised node capabilities are rejected. No arbitrary JavaScript or generic CDP passthrough is exposed.

`accepted` and `verification_required` do not prove a page processed the input. Reobserve and verify. An action can return `completion_pending: true` and `dialog_open: true` when paused behind an observed JavaScript dialog. Resolve that dialog before switching tabs or yielding the work unit, then verify the original action. A dialog response can reveal another dialog; inspect again instead of assuming the sequence is complete. Important errors include `lease_required`, `stale_snapshot`, `element_unavailable`, `unsupported`, `dialog_pending`, `request_conflict`, `capacity`, and `action_outcome_unknown`. See the [recovery procedure](operations.md#recovery).
