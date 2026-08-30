# Capability contract

Every host operation reports a structured status. `OK` means the operation or
sample completed. `PREVIEW` means nothing was changed. `UNAVAILABLE` means the
selected OS cannot safely supply the fact or action. `BUSY` and `REFUSED` are
fail-closed safety outcomes. `ERROR` is an attempted operation that failed.

| Capability | macOS | Linux | Safe limited behavior |
|---|---|---|---|
| static facts | sysctl with portable runtime fallback | `/proc/meminfo` and portable runtime facts | host profile stays missing/limited; one heavy worker |
| telemetry | `memory_pressure`, swap sysctl, process snapshot | `/proc` plus `ps` | no pressure inference; reduce concurrency |
| semaphore core | portable locked JSON state | portable locked JSON state | expired holds continue blocking |
| manual GUI hold | local-user and launchd GUI-domain checks | `UNAVAILABLE` | explicit manual coordination outside Creme |
| Lean reclaim | frozen Darwin process snapshot and signals | `UNAVAILABLE` | restart the client |
| cache copy | APFS clone, then recursive-copy fallback | reflink-auto, then recursive-copy fallback | portable recursive copy |
| temporary root | `TMPDIR` or runtime temp directory | `TMPDIR` or runtime temp directory | `UNAVAILABLE` if no writable root exists |

Shared code does not invoke another OS's command as a fallback. An unavailable
telemetry sample never proves a host is quiet or under pressure.

## Reclamation safety

Ordinary macOS reclamation selects only Lean server candidates that share the
deepest non-init ancestor with the invoking agent and have a recognized agent
client above that ancestor. The complete descendant closure is frozen before
any action. A non-server descendant protects the whole root in ordinary mode.
The explicit `--hard-pressure` mode includes that frozen closure.

Immediately before each signal the adapter checks the PID's start time and
full command against the snapshot, preventing PID reuse from widening the
target set. Foreign, orphaned, and ambiguous trees are left alone. Public
output reports only PID, RSS, and process kind—never command arguments.

## Semaphore safety

The semaphore stores one optional hard hold and a list of identified soft
holds under an `flock` mutex. A lease expiry does not release a hold. Breaking
requires expiry plus a same-invocation quiet-host result; manual macOS holds
also require a successful other-GUI-session scan. Corrupt state is reported
and never reset automatically.

