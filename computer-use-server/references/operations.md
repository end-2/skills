# Running and validating the server

## Automatic OS selection

Python 3.10 or later is required. Set `SKILL_DIR` to the absolute path of this skill. Reuse a server already started by another agent. The following examples use a macOS/Linux shell.

```bash
SKILL_DIR=/absolute/path/to/computer-use-server
python3 "$SKILL_DIR/scripts/client.py" health
python3 "$SKILL_DIR/scripts/server.py"
```

If health succeeds, do not run the second command. The server stays in the foreground; keep it running in a long-lived orchestrator process or a separate terminal. After testing or completing the task, stop only server processes you own. Do not register a service or enable startup at login.

| Host OS | Selected backend | Requirements | Observation scope |
| --- | --- | --- | --- |
| Windows | windows | `uiautomation==2.0.29`, a logged-in user desktop | Apps with visible top-level windows and the foreground window's UIA tree |
| macOS | macos | Swift compiler, Accessibility permission | Regular GUI apps and the active app's AX tree |
| Linux | linux | pyatspi2, AT-SPI, user D-Bus, and a graphical session | Accessibility apps and a uniquely identified active window |

The default `--backend auto` maps the Windows/Darwin/Linux values from `platform.system()` to their corresponding backends. You can also specify `--backend windows`, `macos`, or `linux`; a mismatch with the host OS exits with `backend_os_mismatch`. Other operating systems exit with `unsupported_platform`. `--backend mock` works on every supported OS without loading native dependencies. Check the startup log and the `os` and `selected_backend` fields in `health` to confirm the selection. Health checks process availability; use status to verify actual UI access.

Default token-file locations:

- Windows: `%LOCALAPPDATA%\computer-use-server\token`, falling back to the user's `AppData\Local` if the variable is unset.
- macOS: `~/.local/state/computer-use-server/token`, preserving the existing location.
- Linux: `$XDG_STATE_HOME/computer-use-server/token`, falling back to `~/.local/state/computer-use-server/token` if the variable is unset.

## Windows

Install the dependencies into the same Python environment from PowerShell.

```powershell
$SkillDir = 'C:\path\to\computer-use-server'
python -m pip install -r "$SkillDir\scripts\requirements-windows.txt"
python "$SkillDir\scripts\server.py"
```

Open a separate PowerShell session and create a unique session for each agent.

```powershell
$SkillDir = 'C:\path\to\computer-use-server'
$SessionFile = Join-Path $env:TEMP ('cu-' + [guid]::NewGuid().ToString() + '.json')
python "$SkillDir\scripts\client.py" --session-file $SessionFile open --agent-id agent-a
python "$SkillDir\scripts\client.py" --session-file $SessionFile enqueue --task-id agent-a-task-1 --intent 'Inspect and validate the target form'
python "$SkillDir\scripts\client.py" --session-file $SessionFile turn --wait-seconds 10
```

When turn returns running, read status and choose the target and action. If it returns queued, continue waiting within a defined limit or do independent work.

UIA/COM objects are used only on the same MTA thread in the dedicated worker. Service Session 0, locked or secure desktops, other user sessions, and apps at different privilege levels may be inaccessible. Do not bypass these restrictions through elevation or synthetic keyboard input. Windows foreground activation failures are reported as `unsupported`. Controls exposing UIA ValuePattern support replacing their entire string value.

Authentication and session files are created with a protected DACL granting access only to the current user. Do not evaluate Windows permissions using POSIX chmod bits. Reusing an existing ordinary file as token-file may fail because of inherited ACLs; let the server create a file at a new path in the user's directory. Use a filesystem that supports ACLs and a trusted parent directory.

## macOS

`swiftc` is required. Install Xcode Command Line Tools if it is missing. The macOS backend does not require additional pip packages.

The first status request compiles the Swift helper into `~/Library/Caches/computer-use-server/`. Its executable name includes a source hash. Initial compilation can take longer than a normal status request. If the first status request hits the client's timeout, inspect the server log and health, then retry only the status request.

Actual UI access requires macOS Accessibility permission. On `permission_denied`, first identify the subject of the permission decision. The error's `helper_binary` is the executable that called the AX check; it does not necessarily identify the item that needs permission in System Settings. Depending on how the process was launched, macOS may attribute the request to the responsible host process. Do not identify CodexCLI, Terminal, or the helper as the permission subject from a parent PID or app name alone.

Enabling the built-in `Codex Computer Use` does not imply that this skill's independent Swift helper has permission. This server does not share that app's service or permissions. To diagnose access, issue a read-only status request and correlate `kTCCServiceAccessibility`, the requesting PID, `responsible_path`, `Sub`, and the allow/deny result in the macOS TCC logs immediately afterward. For example, narrow the records with `log show --last 1m --info --style compact --predicate 'subsystem == "com.apple.TCC" AND eventMessage CONTAINS[c] "ax-bridge"'`. If logs are unavailable or the subject is redacted, do not guess which item needs permission.

After the user grants permission to the identified subject, restart the server and verify access with status. The server does not open permission prompts automatically. Source changes that alter the helper path may require another approval. Screen capture permission is not used.

Use an unlocked, logged-in desktop for live UI validation. If status succeeds but reports `loginwindow` as the frontmost process, that alone is not a permission denial. Do not use background controls as targets; resume validation when the intended app is in the foreground.

If permissions on an older version's `desktop.lock` file prevent startup, use the following command to change permissions on that same file. Do not delete or replace it, because a server may still be running.

```bash
chmod 600 ~/.local/state/computer-use-server/desktop.lock
```

## Linux

On Debian/Ubuntu, use the distribution's Python and pyatspi package.

```bash
sudo apt install python3-pyatspi
/usr/bin/python3 "$SKILL_DIR/scripts/server.py"
```

On other distributions, install their pyatspi2/AT-SPI Python bindings. An arbitrary venv or pyenv Python may not see the system bindings. The worker uses the same Python interpreter as the server, so select an interpreter where `import pyatspi` succeeds.

Run from the logged-in user's graphical terminal. DISPLAY or WAYLAND_DISPLAY and a user D-Bus connection are required. Do not fabricate screen state on a headless server or an ordinary SSH session. If an app does not expose AT-SPI, or there is no ACTIVE window or more than one, `foreground_status` explains the condition and actions are rejected. App activation uses Component.grabFocus on a uniquely selectable, focusable window; the Wayland compositor or toolkit may reject it.

## Per-agent sessions and turns

Share only the server endpoint and token-file path; give each agent its own session-file. The following example shows two agents' flows in one shell.

```bash
RUN_DIR=$(mktemp -d)
python3 "$SKILL_DIR/scripts/client.py" --session-file "$RUN_DIR/a.json" open --agent-id agent-a
python3 "$SKILL_DIR/scripts/client.py" --session-file "$RUN_DIR/b.json" open --agent-id agent-b
python3 "$SKILL_DIR/scripts/client.py" --session-file "$RUN_DIR/a.json" enqueue \
  --task-id a-task-1 --intent 'Enter and verify agent A input' --quantum-ms 1000 --ttl 30 --queue-timeout 60
python3 "$SKILL_DIR/scripts/client.py" --session-file "$RUN_DIR/b.json" enqueue \
  --task-id b-task-1 --intent 'Enter and verify agent B input' --quantum-ms 1000 --ttl 30 --queue-timeout 60
python3 "$SKILL_DIR/scripts/client.py" --session-file "$RUN_DIR/a.json" turn --wait-seconds 10
```

When A is running, it identifies targets from a fresh status, performs a safe work unit, and verifies the result. For example, when filling in and submitting one form, keep the turn until the submission result has been checked. Send each action separately and refresh state after it. B waits using turn instead of polling status. `turn --wait-seconds` accepts a polling budget of up to 30 seconds; this is not a strict wall-clock limit covering individual HTTP requests. The command can therefore take longer than the budget and may still return queued. Check that the returned state is running before acting.

If A has more work after finishing its current work unit, it yields. The server gives B a turn and places A at the end of the queue.

```bash
python3 "$SKILL_DIR/scripts/client.py" --session-file "$RUN_DIR/a.json" yield --requeue
python3 "$SKILL_DIR/scripts/client.py" --session-file "$RUN_DIR/b.json" turn --wait-seconds 10
```

B also confirms running before following **fresh status → action → verification**. The yield below applies when B has finished its task.

```bash
python3 "$SKILL_DIR/scripts/client.py" --session-file "$RUN_DIR/b.json" yield
python3 "$SKILL_DIR/scripts/client.py" --session-file "$RUN_DIR/a.json" turn --wait-seconds 10
```

A reads the state again in its new turn before deciding how to finish the remaining work. Once A is also done, clean up.

```bash
python3 "$SKILL_DIR/scripts/client.py" --session-file "$RUN_DIR/a.json" yield
python3 "$SKILL_DIR/scripts/client.py" --session-file "$RUN_DIR/a.json" close
python3 "$SKILL_DIR/scripts/client.py" --session-file "$RUN_DIR/b.json" close
```

The client keeps the current task_id in its session file, so turn/yield/cancel do not require it again. Only queued tasks can be cancelled with `cancel`; finish a running task with `yield`. Use `yield --requeue` only when continuing remaining work after reaching a safe boundary. The default one-second quantum is not a forced interruption deadline. Check `yield_due` and yield when the current work unit finishes. `renew --ttl 30` extends only the TTL and does not restart the quantum.

Enqueuing the same task_id does not restart a terminal task. Each task is limited to 256 turns; if another requeue is rejected with capacity, the current turn can still be finished with `yield`. See [recovery](#recovery-and-request-replay) for lost responses or expiry and the [API reference](api.md) for HTTP fields and idempotency rules.

Single-agent work and existing integrations can continue using `acquire --ttl 30` → fresh status → actions and verification → `release`. The queue waits for a manual lease to end, so use enqueue for every participating agent when fair turn-taking is needed. Calling release during a scheduled turn returns a scheduled_turn error.

To override defaults, give every agent the same `--endpoint` and `--token-file` options. The client also supports `CU_ENDPOINT`, `CU_TOKEN_FILE`, and `CU_SESSION_FILE`. Session files contain credentials and the last action payload, including any entered text. Files use mode 0600 on POSIX and a protected DACL allowing only the current user on Windows. Close clears the file's contents. Do not share these files externally or commit them to a repository.

The client retains the latest snapshot/lease and task_id. `act --action '<JSON>'` generates a UUID and saves the request before sending it. After an action transport error, the client blocks another `act` until the uncertain request is resolved.

## Focused status output

Read-only inspection does not require a lease or a queued task. For example, open a private session and inspect controls with a matching title or description:

```bash
RUN_DIR=$(mktemp -d)
SESSION_FILE="$RUN_DIR/inspect.json"
python3 "$SKILL_DIR/scripts/client.py" --session-file "$SESSION_FILE" open --agent-id inspector
python3 "$SKILL_DIR/scripts/client.py" --session-file "$SESSION_FILE" status --match 'Apply' --actionable --limit 20
python3 "$SKILL_DIR/scripts/client.py" --session-file "$SESSION_FILE" close
```

`--match` searches exposed title and description with a literal, case-insensitive substring; it does not search values. `--role` selects an exact observed native role. `--actionable` selects nodes whose enabled state is not false and which advertise actions or settable attributes. Filters combine with AND. `--limit` accepts 1–1,200 matching nodes; their ancestors are added for context, so the returned node count may exceed the limit. Use `result.view.matched_node_ids` to distinguish selected matches from context ancestors.

These options reduce CLI output only. HTTP status still sends `{}` and captures the whole backend snapshot; action validation still checks that whole state. `state.truncated` includes nodes omitted by the projection. The `result.view` counts and `source_truncated` explain whether the CLI omitted nodes or the backend observation was already incomplete. Narrow filters may hide useful controls: broaden them or omit all options to see the unchanged default output. If ancestor context cannot distinguish duplicate labels, inspect unfiltered status before acting; sibling labels may supply the missing context. See the [API reference](api.md#cli-status-projection) for exact metadata fields.

For scheduled actions, wait for running, then obtain fresh status before selecting targets. Do not use read-only status access to poll the UI while queued; use `turn`, which does not contend for native UI observation.

## Recovery and request replay

The client saves requests before sending them. Choose recovery based on the operation whose response was lost:

| Operation | Recovery |
| --- | --- |
| enqueue | Use `retry-enqueue` to resend the original task_id and all options, then `turn` to check the current state. Do not replace the task ID or change options. A turn response of task_not_found alone does not prove a delayed enqueue failed |
| act | Use `retry` with the saved request ID and identical payload, including snapshot and lease. A timeout alone does not justify repeating the action under a new ID |
| yield | Use `retry-yield` to resend the original task_id, old lease token, and requeue value, then confirm the current state with `turn`. Do not substitute a new turn's token or a different requeue value |

On `stale_snapshot`, read fresh status, reconsider the action, and use a new request ID only for the newly decided action. On `lease_required`, inspect turn for expiration; in manual mode, reacquire before taking a fresh snapshot. If the task or lease expired, inspect the current result before deciding whether to continue as a new task. Do not automatically repeat input or submission.

On `action_outcome_unknown`, inspect status for the actual result; retry returns the cached error, not a new UI attempt. Status requires only a valid session, so a lease is unnecessary for this recovery check. Server restart and session expiration can remove the request history: investigate uncertain outcomes before opening a replacement session or issuing new request/task IDs. A new session does not mean previous work failed.

## Mock exercise without UI changes

Use a port and token-file distinct from the real server.

```bash
python3 "$SKILL_DIR/scripts/server.py" --backend mock --port 8766 \
  --token-file /tmp/cu-demo-token
```

In another terminal, run the following manual-lease compatibility example. For agent turn-taking, use the enqueue/turn/yield flow above.

```bash
RUN_DIR=$(mktemp -d)
export CU_ENDPOINT=http://127.0.0.1:8766
export CU_TOKEN_FILE=/tmp/cu-demo-token
export CU_SESSION_FILE="$RUN_DIR/demo.json"
python3 "$SKILL_DIR/scripts/client.py" open --agent-id demo
python3 "$SKILL_DIR/scripts/client.py" acquire
python3 "$SKILL_DIR/scripts/client.py" status
python3 "$SKILL_DIR/scripts/client.py" act --action '{"kind":"set_value","node_id":"input","value":"hello"}'
python3 "$SKILL_DIR/scripts/client.py" status
python3 "$SKILL_DIR/scripts/client.py" act --action '{"kind":"perform","node_id":"submit","action":"AXPress"}'
python3 "$SKILL_DIR/scripts/client.py" retry
python3 "$SKILL_DIR/scripts/client.py" status
python3 "$SKILL_DIR/scripts/client.py" release
python3 "$SKILL_DIR/scripts/client.py" close
unset CU_ENDPOINT CU_TOKEN_FILE CU_SESSION_FILE
```

`input.value` becomes hello and `result.value` becomes 1. Retry does not increment the button invocation count again. Do not reuse the mock's fixed IDs in real apps.

## Development validation

```bash
python3 -m unittest discover -s "$SKILL_DIR/tests" -v
python3 -m compileall -q "$SKILL_DIR/scripts"
```

Tests use only fake/mock UI. They cover lease contention, round-robin turns, expiry, state changes, duplicate requests, unknown outcomes, and HTTP authentication. Native UI actions must be validated separately in an environment where the required permissions are granted.

Official references: [Apple Accessibility](https://developer.apple.com/documentation/applicationservices/1462085-axuielementcopyattributevalue), [Windows UIA threading](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-threading), [uiautomation 2.0.29](https://pypi.org/project/uiautomation/2.0.29/), [GNOME pyatspi2](https://github.com/GNOME/pyatspi2), [AT-SPI grab_focus](https://gnome.pages.gitlab.gnome.org/at-spi2-core/libatspi/method.Component.grab_focus.html).
