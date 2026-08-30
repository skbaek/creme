# Executable goal contracts

A goal is a durable contract for autonomous execution. It is specific enough
to prove complete, explicit about boundaries, and independent of disposable
client memory.

## Required shape

1. Stable ID, title, date, owner, status, and exact objective.
2. Mandatory outcome table: each condition has inspectable acceptance evidence.
3. Scope and non-goals.
4. Fixed decisions, invariants, and authority order.
5. Verified starting state with dated commits and uncertainty.
6. Workstreams with dependencies, ownership, resource class, and convergence
   gates—not an artificially rigid sequence.
7. Verification sources and goal-specific controls.
8. Autonomous decisions versus decisions reserved for the user.
9. State/recovery locations and completion-report requirements.

Use `ready` only when a competent lead can begin without inventing product
semantics. Use `active` while a live execution owns the goal, `blocked` only at
a genuine impasse, and `complete` only when every mandatory outcome has
evidence on the exact delivered candidate.

## Quality rules

- Express outcomes rather than implementation theater. Commands are evidence,
  not the objective.
- Name negative controls for likely false positives: stale output, wrong root,
  missing discovery, disabled enforcement, unsupported OS, or an absent
  dependency.
- Separate public facts from current-host observations and private strategy.
- Give every required artifact a canonical owner and location.
- Keep repository technical truth with the repository that owns it.
- Treat estimates as measured ranges in comparable units; do not compare a
  serialized wall-clock observation with an idealized parallel sum.
- Never mark completion based on a plan, a silent command, or a green signal
  whose failure mode was not shown to bite.

The lead may adapt internal implementation, packet boundaries, and cheap test
selection. Changes to objective, public claim, security boundary, supported
platforms, license, remote, or a protected/default merge require the authority
named in the goal.

