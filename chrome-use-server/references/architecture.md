# Architecture and guarantees

```text
Agent A / private session ─┐
Agent B / private session ─┼─ authenticated loopback broker
Agent C / private session ─┘     FIFO queue + fenced leases + replay cache
                                           │
                                 serialized backend calls
                                           │
                                 Node/Puppeteer worker
                                           │
                           existing Chrome + existing login state
```

The session, scheduler, HTTP authentication, client replay, private-file, and filtered-state implementations were adapted from this repository's `computer-use-server` at commit `55c02fa`. They are included locally so installing this skill does not require the desktop skill. Browser actions and the backend are separate implementations. Future broker fixes should be checked in both copies.

## Ownership and concurrency

One broker owns mutation rights for the connected browser as a whole. Different tabs can share cookies, local storage, service workers, and server-side application data, so this version deliberately grants sequential turns. One lock serializes backend observations and actions across HTTP handler threads. A per-user file lock prevents another real broker from starting under the same state-directory configuration.

Sessions have independent tokens, snapshots, tasks, and request histories. Every lease grant has a new fencing token and generation. Expired owners cannot act with their previous token. The scheduler stores intentions, never browser actions. A new turn requires fresh observation. Requeueing moves a task to the FIFO tail; the quantum is advisory, and an in-flight action is not forcibly preempted. Fairness requires participants to yield.

This is coordination within one trusted OS account, not isolation between hostile tenants. A desktop broker, another DevTools client, a second state-directory configuration, or human input can bypass these locks. Profile data is shared, including across agents' tasks.

## State and action safety

Snapshots have session ownership and expiration. The broker fingerprints deterministic observed state and records a revision. Immediately before action dispatch, it observes again and validates snapshot identity, revision, fingerprint, lease, target tab, node ownership, and action capabilities. Document-scoped node identities prevent an element replaced with identical text from inheriting its old target ID.

Browser reads and mutations are not one atomic transaction. Human input or asynchronous DOM changes can occur just after validation. Truncated content and unavailable controls are outside the fingerprint's complete coverage. An accepted action must be verified by subsequent observation.

The isolated Node worker owns its browser connection and element references. Python communicates through bounded JSONL transactions, independent of HTTP thread identity. A timeout stops the worker and invalidates references. `status` may reconnect; `act` never starts a new worker or automatically repeats an uncertain mutation. Worker cleanup disconnects the client rather than closing Chrome.

The pinned Puppeteer dependency uses an isolated execution realm for node identity and DOM observation. This is internal library surface: dependency upgrades require smoke testing. No caller-supplied script/CDP method is accepted by the broker.

## Replay limits

The broker remembers up to 256 action results/errors per session without evicting records for re-execution. Repeating an identical request retrieves its original result. This is duplicate suppression within a running process, not exactly-once execution across broker crashes. Session and replay history are in memory. After a restart or uncertain dispatch, inspect the browser before deciding whether another mutation is necessary.

The implementation binds only to `127.0.0.1`, uses bearer and session authentication, rejects browser Origin/incorrect Host headers, limits request sizes/connections, and avoids access logs containing page/form data. Internet hosting, remote TLS access, persistent queues, concurrent tab execution, and native browser-chrome controls are outside its scope.
