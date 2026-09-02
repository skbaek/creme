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

Existing installations keep using the legacy state directory until an explicit
migration:

```sh
~/creme/.semaphore/semaphore migrate-state
```

Migration copies validated holds under both mutexes and leaves the legacy state
untouched. Retire any pre-neutral delegate and legacy state only after every
session launched before the cutover has wound down.
