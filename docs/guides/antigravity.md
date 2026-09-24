# Antigravity pseudo-subagents (read-only)

`python3 -m creme antigravity` hands a bounded **read-only** brief to Google's
Antigravity CLI (`agy`, default `~/.local/bin/agy`, override `CREME_AGY_BIN`).
Its value is a third model family (Gemini) for model-diverse review, on a quota
pool separate from Claude and Codex. Like Luna reserve, it is used only when the
user instructs it, and a result is a worker summary, not evidence.

## Commands

```sh
python3 -m creme antigravity status [--model M] [--json]
python3 -m creme antigravity run --brief FILE|- --target DIR [--model M] \
    [--effort low|medium|high] [--timeout-seconds N] [--json]
```

Real workloads use the default, `gemini-3.8-flash-high` at `--effort high`
(user decision 2026-09-25: it dominates the other Gemini models on quality,
speed and cost). Name another model only for a deliberate comparison, or use
`gemini-3.6-flash-low` for a plumbing check whose answer does not matter.

`status` reads `agy -p /usage` and `/credits`, both zero-token, plus
`useG1Credits` from `~/.gemini/antigravity-cli/settings.json`. `agy models` lists
the slugs. A `gemini-*` model draws on the Gemini pool; `claude-*` and
`gpt-oss-*` models draw on the "Claude and GPT" pool. Each pool has a 5-hour and
a weekly window.

## How a run stays read-only and on plan quota

- **Admission.** The run is refused when the model's pool is below 5% on either
  window, or when paid AI credits are positive and `useG1Credits` is not
  explicitly `false`.
- **Guard.** Each run gets a directory under `.creme/antigravity/runs/`, passed
  as a second `--add-dir`. Its `.agents/hooks.json` installs a PreToolUse guard.
  The guard allows only `view_file`, `list_dir`, `grep_search`, `find_by_name`
  and `finish`, and denies every tool when the payload's `modelName` differs from
  `--model`, so a silent model fallback cannot act. A deny holds in headless
  mode. The target repository and the shared `~/.gemini/config` are not touched.
- **Verdict.** `PASS` requires all of: exit 0, result `SUCCESS`, `init.model`
  equal to `--model`, every guarded payload on that model, and, for a Git target,
  an unchanged `HEAD` and porcelain status (ignored files included, the run
  directory excluded). The last check is the fail-closed evidence that the guard
  held.
- **Records.** `brief.md`, `events.jsonl` (stream-json), `payloads.jsonl`
  (every tool call with its guard decision), `last-message.md`,
  `usage-before.json`, `usage-after.json` and `verdict.json`.

Headless `agy` treats the cwd as a scratch area, not a workspace. Only `--add-dir`
directories are workspaces, so `run` passes the target that way.

## Not yet supported

Write and Lean modes wait on two unsettled probes: the `--sandbox` writable set
(including whether `creme lake-build` can run in it) and the hook timeout
ceiling. The session uses the default HOME, which also exposes
`~/.gemini/config/mcp_config.json` (Lean MCP). The guard denies
`call_mcp_tool`. Full per-run isolation would need a Creme-owned HOME with its
own one-time interactive sign-in (a user action). Probe record: goal store
`reports/antigravity-backend-probes-20260925.md`.

Never read Antigravity's `cloudcode-pa` quota endpoints with the keyring token;
`/usage` and `/credits` are the only quota surfaces.
