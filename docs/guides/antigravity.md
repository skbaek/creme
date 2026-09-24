# Antigravity pseudo-subagents

`python3 -m creme antigravity` hands a bounded brief to Google's
Antigravity CLI (`agy`, default `~/.local/bin/agy`, override `CREME_AGY_BIN`).
Its value is a third model family (Gemini) for model-diverse review, on a quota
pool separate from Claude and Codex. Like Luna reserve, it is used only when the
user instructs it, and a result is a worker summary, not evidence.

## Commands

```sh
python3 -m creme antigravity status [--model M] [--effort low|medium|high] [--json]
python3 -m creme antigravity run --brief FILE|- --target DIR [--model M] \
    [--effort low|medium|high] [--timeout-seconds N] [--write] \
    [--allow-command REGEX]... [--lean-goal GOAL] [--json]
```

The model is 3.8 Flash (user decision 2026-09-25). `--model` takes the family
and `--effort` picks the level; the CLI builds the slug. A conflicting full
slug is refused, as `agy` itself refuses it. The default effort is provisional
(`medium`) until the effort-ladder experiment decides. Record runs in the goal
store's `model-fit/antigravity.md`.

`status` reads `agy -p /usage` and `/credits`, both zero-token, plus
`useG1Credits` from `~/.gemini/antigravity-cli/settings.json`. `agy models` lists
the slugs. A `gemini-*` model draws on the Gemini pool; `claude-*` and
`gpt-oss-*` models draw on the "Claude and GPT" pool. Each pool has a 5-hour and
a weekly window.

## How a run stays bounded and on plan quota

- **Admission.** The run is refused when the model's pool is below 5% on either
  window, or when paid AI credits are positive and `useG1Credits` is not
  explicitly `false`.
- **Guard.** Each run gets a directory under `.creme/antigravity/runs/`, passed
  as a second `--add-dir`. Its `.agents/hooks.json` installs a PreToolUse guard.
  In read-only mode it allows only `view_file`, `list_dir`, `grep_search`,
  `find_by_name` and `finish`; write mode adds only guarded file writes and
  explicitly allowed commands. Every tool is denied when the payload's
  `modelName` differs from `--model`, so a silent model fallback cannot act.
  Reads and writes outside the target are denied by the guard. A deny holds in
  headless mode. In read-only mode, the target repository and the shared
  `~/.gemini/config` are not touched by the harness.
- **Verdict.** `PASS` requires all of: exit 0, result `SUCCESS`, `init.model`
  equal to `--model`, every guarded payload on that model, and, for a Git target,
  an unchanged `HEAD` and porcelain status (ignored files included, the run
  directory excluded). The last check is the fail-closed evidence that the guard
  held.
- **Records.** `brief.md`, `events.jsonl` (stream-json), `payloads.jsonl`
  (every tool call with its guard decision), `last-message.md`,
  `usage-before.json`, `usage-after.json` and `verdict.json`. Write mode also
  records `git-before.txt`, `git-after.txt`, and `edits.json` for a Git target.

Headless `agy` treats the cwd as a scratch area, not a workspace. Only `--add-dir`
directories are workspaces, so `run` passes the target that way.

## Write mode

Write mode does not pass `agy --sandbox`: measured behavior makes that mode
unusable for workspace writes, blocks `os.nice` needed by `creme lake-build`,
and does not block the network. Because headless `agy` cannot ask for approval,
write mode passes `--dangerously-skip-permissions`; the fail-closed PreToolUse
guard is the control.

The guard allows reads inside the configured roots, file writes only when every
path is inside a root, and `run_command` only when its cwd is inside a root and
its stripped command fully matches a supplied `--allow-command` regex. All
other tools, including network, MCP, browser, subagent, and task tools, are
denied. Choose tight, anchored patterns: every allowed command runs
unsandboxed with the user's privileges. For example, a master might allow
`git (status|diff)( .*)?`, `git add [^;&|]+`, `git commit -m [^;&|]+`, or one
exact test command, with cwd constrained to the target. `--lean-goal GOAL`
adds the narrow owned `creme lake-build` command pattern. Before any pattern is tried, a command containing a
newline, carriage return, backtick or `$(` is denied, because those would let a
pattern such as `git add [^;&|]+` smuggle a second command.

The master reviews `git diff` and runs the owning repository gates before
accepting anything produced by write mode.

## Not yet supported

Interactive approval of unlisted commands is not supported. The session uses
the default HOME, which also exposes `~/.gemini/config/mcp_config.json` (Lean
MCP). The guard denies `call_mcp_tool`. Full per-run isolation would need a
Creme-owned HOME with its own one-time interactive sign-in (a user action).
Probe record: goal store `reports/antigravity-backend-probes-20260925.md`.

Never read Antigravity's `cloudcode-pa` quota endpoints with the keyring token;
`/usage` and `/credits` are the only quota surfaces.
