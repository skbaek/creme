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
~/creme/.semaphore/semaphore master-acquire --client codex --note "why"
~/creme/.semaphore/semaphore master-renew --heartbeat 1500 --detach
~/creme/.semaphore/semaphore master-release
```

A second `master-acquire` is refused while the lease is live. An
adapter-supplied session identity is stored only as a digest and prevents a
second task of the same client from renewing or releasing the lease when
process discovery is unavailable or shared. A detached heartbeat starts only
after its parent authenticates as the holder. A random one-time capability
crosses to the child through an inherited pipe; only its short-lived digest is
persisted, and the child consumes it atomically before adopting the exact lease
id. It uses only an explicitly supplied, task-owned neutral liveness socket
when available. Codex Desktop's shared app pid and app-tools
pipe are never task-liveness evidence. A task without a task-scoped process or
listener gets two self-renewals, never later than 3,000 seconds after the last
verified direct holder activity, and then becomes passive. A helper cannot
advance that anchor. With the standard 1,500-second heartbeat and 1,800-second
lease an orphan is take-overable within 4,800 seconds of the last direct
activity. Child argv contains no lease id, capability, raw session identity,
or liveness path, and the helper stops before renewing if its bound lease id
changed. A lease is *stranded* when its process is gone or its window passes
without process liveness, and *lapsed* when the window passes while the process
is still alive. `status` prints the take-over command, and
`master-acquire --take-over` replaces only a lapsed or stranded lease.

Existing installations keep using the legacy state directory until an explicit migration:

```sh
~/creme/.semaphore/semaphore migrate-state
```

Migration copies validated holds under both mutexes and leaves the legacy state
untouched. Retire any pre-neutral delegate and legacy state only after every
session launched before the cutover has wound down.
