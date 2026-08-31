# Host semaphore

`./semaphore` is the client-neutral entry point for host memory coordination.
Codex, Claude Code, future local agents, and humans use the same interface.

Runtime files live in `state/` and are intentionally ignored by Git. The
launcher and this protocol note are tracked. Every supported invocation
resolves linked Git worktrees back to the canonical Creme checkout, so all
sessions coordinate through one host state.

Existing installations keep using the legacy state directory until an explicit
migration:

```sh
~/creme/.semaphore/semaphore migrate-state
```

Migration copies validated holds under both mutexes and leaves the legacy state
untouched. The existing `~/.codex/bin/codex-host-semaphore` delegate is also
retained during the compatibility period and follows the newly active state
after the canonical checkout is updated.
