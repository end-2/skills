# Operations

## Install and start

The skill is self-contained. Install its exact Node dependencies once:

```bash
npm ci --prefix "$SKILL_DIR/scripts" --ignore-scripts
python3 "$SKILL_DIR/scripts/client.py" health
# If no broker is running:
python3 "$SKILL_DIR/scripts/server.py"
```

Python uses only its standard library. `puppeteer-core` controls installed Chrome and does not download or launch a browser. The dependency is pinned in `scripts/package-lock.json`; upgrade it with the real-browser smoke test because the worker uses Puppeteer's isolated execution realm. Node.js 22.12 or newer is required by the pinned package.

Use a persistent terminal/process for the broker. Its startup line prints the endpoint and token-file path, never the token. All agents must use the same broker. A per-user `browser.lock` rejects a second real broker, including on another port. `--backend mock` is explicit test-only simulation.

Runtime files default to `~/.local/state/chrome-use-server` on macOS and Linux, `$XDG_STATE_HOME/chrome-use-server` when absolute on Linux, and `%LOCALAPPDATA%\chrome-use-server` on Windows. POSIX credential files must have mode 600; Windows files use a protected current-user ACL. Prefer those private runtime paths over repository files.

CLI environment variables are `CHROME_USE_ENDPOINT`, `CHROME_USE_TOKEN_FILE`, and `CHROME_USE_SESSION_FILE`. Each agent needs a distinct session-file path. Do not share or print session-file contents; the file includes credentials and exact replay payloads, which can contain form values.

PowerShell equivalent setup:

```powershell
$SkillDir = 'C:\path\to\chrome-use-server'
npm ci --prefix "$SkillDir\scripts" --ignore-scripts
py -3 "$SkillDir\scripts\server.py"
# In another terminal, using a private per-agent session path:
py -3 "$SkillDir\scripts\client.py" --session-file "$env:LOCALAPPDATA\chrome-use-server\agent-a.json" open --agent-id agent-a
```

## Connect to existing Chrome

The user opens `chrome://inspect/#remote-debugging`, enables remote debugging, and approves the connection request Chrome displays when the first `status` runs. This flow requires Chrome 144+. The broker discovers the running stable Chrome profile, then connects through the same DevTools transport used by Chrome's official agent integration. It does not copy cookies, migrate profiles, close the browser, or start a clean profile.

For a custom **already running** Chrome user-data directory:

```bash
python3 "$SKILL_DIR/scripts/server.py" --user-data-dir '/path/to/Chrome User Data'
```

Supply the root user-data directory containing `DevToolsActivePort`, not a child such as `Default` or `Profile 1`. The user must enable debugging in the intended running Chrome. Multiple profiles/instances require the orchestrator to choose the correct directory before starting the single broker.

For an already configured local debugging endpoint, including isolated testing:

```bash
python3 "$SKILL_DIR/scripts/server.py" --browser-url http://127.0.0.1:9222
```

This option accepts loopback only and does not create or restart Chrome. It is not a workaround for default-profile restrictions. Chrome 136+ ignores the old command-line remote-debugging flags against its default user-data directory; use the Chrome 144+ permission flow for the current profile.

`health` confirms only the HTTP broker. Verify Chrome connectivity with `open` then `status` before `enqueue`: the initial human approval can outlast a 30-second lease. Once granted a turn, obtain another fresh snapshot. If Chrome's consent dialog remains open, finish the human approval before retrying observation. Do not keep making action attempts while connection is unavailable. On a denied connection or browser policy restriction, report it and ask the user to enable access or choose another workflow.

A stopped or timed-out worker disconnects its DevTools client. It does not intentionally terminate the user's Chrome. Subsequent observation can reconnect and may prompt again. Broker shutdown does not close existing or task-created tabs.

## Recovery

The CLI persists the exact action before sending it. If a response is lost, `retry` sends the same request ID, snapshot, lease, and action. The broker returns its cached result/error without executing it again. After receiving a known validation rejection such as `stale_snapshot`, read `status`, reconsider, and issue a new action only when appropriate.

An `action_outcome_unknown` error means dispatch may have happened. First inspect the page. Do not assume a timeout means a click, submit, navigation, or tab creation failed. A replay returns the cached uncertainty; it does not resolve the browser's outcome. After a server restart, session IDs and replay history are gone: inspect through a new session before deciding whether any mutation is needed.

`retry-enqueue` recovers a lost queue response using the saved task ID/options. `retry-yield` recovers a lost handoff response. Do not create a new task or repeat `yield` with changed parameters while its outcome is unresolved. `turn --wait-seconds 10` can legitimately return `queued`; it does not grant mutation rights. If the task or lease expires, observe again under a newly granted task.

## Scope and diagnostics

- DOM inspection covers the selected tab; the tab list covers the connected browser. State reports truncation, unsupported frames, and capabilities. Structured labels are derived from DOM content; this is not a complete accessibility audit or a screenshot.
- Hidden content, closed shadow roots, browser-internal pages, PDF viewers, extension UI, and canvas-only controls may be unavailable. Do not invent targets. Explain the concrete unsupported control before proposing another authorized tool.
- Pending JavaScript dialogs are reported separately. Handle only the observed dialog with `handle_dialog`, then inspect again. Accepting a confirm dialog can complete a consequential action and remains subject to the user's task authorization.
- The broker serializes agents, not people or external automation. It cannot guarantee a transaction against a human click or background page update occurring immediately after validation.
- Linux must have a running graphical Chrome session for attachment; no X11/Wayland input-injection permission is needed for DOM control. Cross-platform setup logic is provided, but only platforms actually tested should be reported as verified.

Official references: [Chrome's existing-session permission flow](https://developer.chrome.com/blog/chrome-devtools-mcp-debug-your-browser-session), [Chrome 136 debugging changes](https://developer.chrome.com/blog/remote-debugging-port), [Puppeteer connection API](https://pptr.dev/api/puppeteer.puppeteer.connect).

## Validate changes

From the repository root, `python3 -m unittest discover -s chrome-use-server/tests` runs broker, client, scheduler, action validation, and worker-transport tests without attaching to the user's browser. The real-browser test is opt-in:

```bash
CHROME_USE_SMOKE=1 python3 -m unittest discover -s chrome-use-server/tests -p test_browser_smoke.py
```

It launches a temporary headless Chrome with a disposable profile and loopback fixtures, then cleans it up. Set `CHROME_USE_EXECUTABLE` to a Chrome executable when automatic discovery is unavailable. Do not substitute the user's profile for this test.
