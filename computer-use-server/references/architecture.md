# Architecture and Guarantees

```text
Agent A ─ client.py / HTTP ─ session A ─┐
Agent B ─ client.py / HTTP ─ session B ─┼─ shared broker (127.0.0.1)
Agent C ─ client.py / HTTP ─ session C ─┘       │
                                      sessions + cooperative task queue
                                              │
                                      lease + request cache
                                              │
                                      serialized backend calls
                                              │
                                      OS-selected native backend
                                  macOS AX / Windows UIA / Linux AT-SPI
                                              │
                                      one OS desktop
```

## Multiple agents

HTTP connections run on multiple threads, but backend calls and session changes share one mutex. Applications on the same desktop share focus and the active application, so a separate lock for each application is insufficient. Session tokens prevent accidental reuse of another agent's session or snapshots. Each lease acquisition issues a new token and increments its generation, preventing an expired owner from acting with an old token.

For every native backend, the server CLI uses a per-user file lock to reject a second broker: `flock` on POSIX and an `msvcrt` byte-range lock on Windows. All agents must use the same state directory configuration. Do not increase the number of broker processes or run copies on different ports. Other user accounts, external automation tools, and human input do not participate in this lock.

The shared broker schedules turns above the OS-specific input layer. It assigns enqueued intentions in FIFO order and grants the next turn when the current agent yields at a safe work boundary. Requeued tasks move to the tail, allowing cooperating agents to take turns. The quantum is a soft limit that recommends yielding; it does not interrupt a native call or force a handoff during a sequence of UI actions. `renew` extends the lease TTL without resetting the quantum.

The queue stores intentions, not pending UI commands or `node_id` values. Each turn receives a fresh lease token and generation and requires a new `status` observation. `turn` reads scheduler metadata without calling the UI backend; `status` invokes the backend and is serialized with other UI calls. Waiting agents should poll `turn` at bounded intervals. Lease or queue expiration records a terminal task state and does not replay actions. Agents must inspect the UI to resolve uncertain outcomes.

Manual `acquire` remains available for compatibility. Fair rotation is not guaranteed if a manual lease holder keeps renewing or a scheduled owner does not yield. For shared forms and similar tasks, define the unit of work that must be completed and verified before another agent can change its intermediate results. Cooperative scheduling does not isolate application data or human input.

For parallel UI execution, isolate desktops using separate VMs or OS login sessions, each with its own broker. Different applications or tabs do not justify removing this implementation's global lock.

## Observation and actions

A `snapshot_id` is a short-lived reference owned by one session. The `revision` increases when observed state changes, an action is dispatched, or a scheduled turn is granted. Before dispatch, the broker reads the backend state again and compares both its SHA-256 fingerprint and revision with the snapshot. Each native worker retains the accessibility object references from its latest observation. A path-based node ID cannot authorize an action without passing this state comparison, even if that path now identifies another item.

OS UI reads and actions cannot form one atomic transaction. Changes during observation, human input just after dispatch, and state omitted by accessibility APIs cannot be detected completely. When a tree is truncated, matching fingerprints do not prove that the entire UI is unchanged. Observe and verify each step of a sequence of actions.

## OS selection and workers

`backend_factory.py` detects the OS and imports only the selected backend. The mock backend does not import native dependencies. macOS uses a Swift process with a JSONL protocol. On Windows and Linux, `process_backend.py` starts `python_worker.py`, which loads the native adapter in a single-threaded process. Windows uses UIA COM objects on the MTA thread that created them. Linux refreshes the AT-SPI cache before observation.

Pipe I/O on Windows and Linux does not depend on POSIX `select`. A response timeout terminates the worker and invalidates its object references. A subsequent `status` may start a new worker, but `act` never automatically restarts the worker or resends an action. An action sent to a worker that dies has an uncertain outcome. Initial dependency failures produce structured errors at server startup. macOS compiles the Swift helper and checks accessibility permission on the first `status` call.

Repeating an identical action request returns the result or error held in session memory. Each session retains up to 256 action records; records are not evicted to permit re-execution. Reaching the limit requires a new session. This provides **duplicate suppression within the same session of a running server**. It does not guarantee exactly-once execution across a process crash. After a restart, inspect the UI before deciding how to handle an uncertain action.

## Deployment scope

This is a local agent tool built on Python's standard HTTP server. It binds only to loopback and applies bearer authentication, session tokens, Host/Origin checks, and request-size and connection limits. Clients running as the same OS user and sharing the bearer token belong to one trust domain; this is not a security boundary between hostile tenants. Internet exposure, remote TLS service, a persistent task queue, and MCP transport are outside this implementation's scope.

Remote hosts require a separate gateway with authentication, TLS, permission isolation, and a broker on each host. An MCP adapter can call the HTTP API, but must preserve its locking and state validation.

## Adding backends

A backend implements `snapshot() -> dict`, `act(action) -> dict`, and `close()`. Its state includes `platform`, `desktop_id`, `frontmost_app`, `applications`, `roots`, `nodes`, `truncated`, and `capabilities`; each node includes `id`, `role`, `actions`, and `settable`. An unchanged UI must serialize deterministically. Exclude wall-clock timestamps, newly generated IDs, and other unstable fields from the state fingerprint. Backends that manipulate the active application's UI must retain global serialization.

The available adapters use Windows UI Automation, macOS AX, and Linux AT-SPI. OS-specific role and action names are preserved rather than forced into one vocabulary, so agents must use the capabilities returned by each observation. Backends such as isolated browser DOM control remain future work.

Official references: [Apple AXUIElement](https://developer.apple.com/documentation/applicationservices/axuielement), [Python http.server deployment considerations](https://docs.python.org/3/library/http.server.html).
