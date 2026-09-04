# Host semaphore

`./semaphore` is the client-neutral entry point for host memory coordination.
Codex, Claude Code, future local agents, and humans use the same interface.

Runtime files live in `state/` and are intentionally ignored by Git. The
launcher and this protocol note are tracked. Every supported invocation
resolves linked Git worktrees back to the canonical Creme checkout, so all
sessions coordinate through one host state.

Heavy work uses adaptive admission rather than assuming every elaboration can
share the host safely:

```sh
~/creme/.semaphore/semaphore adaptive-acquire GOAL \
  --note "focused proof loop" --memory-gib 4 --contention tolerant
~/creme/.semaphore/semaphore renew GOAL
~/creme/.semaphore/semaphore release GOAL
```

The result is `ADMITTED_SOFT`, `ADMITTED_HARD`, `DEFER_FOR_HARD`, or
`LIGHT_ONLY`. `sensitive` requests serialize work whose peak or broad rebuild
shape makes contention unwise; `exclusive` is for authoritative exclusive
runs. On `YIELD_HEAVY` or `DRAIN_HEAVY` renewal, start no further heavy step:
checkpoint, wind down, and run light work. Explicit soft/hard commands remain
for compatibility but use the same pressure guard.

Wind-down is goal-scoped: it resolves configured `.worktrees/GOAL` roots,
reclaims only Lean candidates whose cwd is inside those roots, verifies that
scope, and releases only the matching label. Other soft holders may wind down
in any order; an uninspectable candidate fails closed before signalling.

One session at a time holds the master lease, kept in `state/master.json`
under the same mutex. It charges no memory and never affects admission:

```sh
~/creme/.semaphore/semaphore master-acquire --client claude --note "why"
~/creme/.semaphore/semaphore master-renew --heartbeat 1500 --detach
~/creme/.semaphore/semaphore master-release
```

A second `master-acquire` is refused while the lease is live. A lease is
*stranded* as soon as the client process that took it is gone, or when its
window passes with that process never identified; it is *lapsed* when the
window passes while the process is still alive. `status` prints the
take-over command, and `master-acquire --take-over` replaces only a lapsed or
stranded lease.

Existing installations keep using the legacy state directory until an explicit migration:

```sh
~/creme/.semaphore/semaphore migrate-state
```

Migration copies validated holds under both mutexes and leaves the legacy state
untouched. Retire any pre-neutral delegate and legacy state only after every
session launched before the cutover has wound down.
