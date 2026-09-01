# Executable goal contracts

A goal is a durable contract for autonomous execution. It is specific enough
to prove complete, explicit about boundaries, and independent of disposable
client memory.

## Required shape

1. Stable ID, title, date, owner, status, and exact objective.
2. Recommended lead configuration: client, model, effort selector, rationale,
   delegation posture, and reconsideration conditions.
3. Mandatory outcome table: each condition has inspectable acceptance evidence.
4. Scope and non-goals.
5. Fixed decisions, invariants, and authority order.
6. Verified starting state with dated commits and uncertainty.
7. Workstreams with dependencies, ownership, resource class, and convergence
   gates—not an artificially rigid sequence.
8. Verification sources and goal-specific controls.
9. Autonomous decisions versus decisions reserved for the user.
10. State/recovery locations and completion-report requirements.

Use `ready` only when a competent lead can begin without inventing product
semantics. Use `active` while a live execution owns the goal, `blocked` only at
a genuine impasse, and `complete` only when every mandatory outcome has
evidence on the exact delivered candidate.

For a repository with content-addressed gate evidence, write checkpoint and
merge-candidate closure as a **complete content-valid manifest**: every
catalogue row is freshly green or has successful evidence with an identical
verdict-relevant identity. Require all-fresh execution only when freshness is
itself an acceptance subject; routine draft pushes should run the affected set
and required cheap invariants without inheriting a blanket freshness claim.

## Recommended lead configuration

Every substantial goal gives a best-guess starting configuration for its
execution lead. The recommendation is required but advisory: it records the
author's sizing judgment, not a permanent dependency on one vendor release.

Include:

- the intended client, exact currently available model, and exact visible
  effort selector;
- an optional equivalent for another supported client;
- a short rationale based on the hardest likely **non-delegable** judgment,
  rather than the total volume of mechanical work;
- the expected worker allocation, including any per-worker model or effort
  overrides the client actually supports; and
- concrete conditions for de-escalating, escalating, or handing off to a
  successor lead.

Date-stamp or reconcile the recommendation with the goal. At launch, recheck
the installed client's available models and selectors. A renamed or retired
model may be replaced by its closest current capability equivalent, but the
substitution and effective effort must be recorded in the state brief. Do not
silently fall back to a client default.

### Model choice

Use the client-visible model name, not a redundant API-family prefix or suffix
that the user does not select. As reconciled on 2026-08-31, this workflow's
Codex choices are **Sol**, **Terra**, and **Luna**; its Claude Code choices are
**Fable** and **Opus**. Recheck those names at launch rather than treating this
snapshot as a permanent product catalogue.

For Codex, use Sol for the hardest quality-first lead judgment, Terra for a
balanced everyday lead or reviewer, and Luna for efficient bounded work where
the route and falsifier are already clear. Fable is the established Claude
hard-lead equivalent in this workflow. Opus is an alternate capable Claude
choice, including when independent model diversity is useful; do not claim a
fixed Fable-versus-Opus ranking without a current representative comparison.

### The six-selector model

Codex and Claude Code currently expose six user-visible selector positions.
The first five form the lead-intelligence ladder. The sixth is an orchestration
mode, not a higher intelligence effort:

| Position | Codex label | Claude Code label | Use |
|---|---|---|---|
| 1 | Light | `low` | Fast bounded work: inventories, direct edits, routine checks, and other tasks with a short falsifiable route. |
| 2 | Medium | `medium` | Ordinary multi-step implementation with clear semantics and modest ambiguity. |
| 3 | High | `high` | Complex implementation or proof repair along a known architecture; latency is secondary to reliability. |
| 4 | Extra High | `xhigh` / extra | Hard architecture, proof strategy, integration, or ambiguous diagnosis that benefits from sustained reasoning. |
| 5 | Max | `max` | The intelligence ceiling: the hardest quality-first, non-delegable reasoning chains. Compare against position 4; more effort can add latency or overthinking without a measured gain. |
| orchestration | Ultra: Max reasoning with automatic task delegation | `ultracode` / Ultra: automatic multi-agent orchestration; recheck its effective lead effort at launch | Choose an orchestration mode when automatic decomposition across genuinely independent workstreams is itself desired. Do not infer one client's exact lead-reasoning semantics from the other's label. |

Current OpenAI model guidance describes Codex Max as maximum reasoning for one
task and Codex Ultra as maximum reasoning with automatic task delegation. The
latter can reduce wall-clock time on work that divides cleanly. Recheck the
[current Codex model guidance](https://learn.chatgpt.com/docs/models) and
[API model guidance](https://developers.openai.com/api/docs/guides/latest-model)
when authoring or reconciling a goal because model support and defaults drift.

Ultra is therefore not a sixth intelligence rung and must not be recommended
as shorthand for “strongest.” Creme already specifies explicit delegation,
ownership, and host-headroom rules. In that workflow, prefer Extra High when
that is enough lead reasoning, Max when the lead's own judgment needs the
ceiling, and Ultra only when its automatic orchestration is a deliberate
benefit rather than duplicate policy.

Do not choose Max reflexively. Use it when the hard part is one coupled chain
that cannot be safely packetized—for example, freezing a novel invariant or
integration boundary. De-escalate after that boundary becomes mechanical.
Bounded discovery, fixture generation, routine proof repair, gate execution,
and independent review normally belong to appropriately sized workers rather
than inflating the lead setting.

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
