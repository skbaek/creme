# Antigravity pseudo-subagents

`python3 -m creme antigravity` hands a bounded brief to Google's
Antigravity CLI (`agy`, default `~/.local/bin/agy`, override `CREME_AGY_BIN`).
Its value is a third model family (Gemini) for model-diverse review, on a quota
pool separate from Claude and Codex, and Claude 5.5 models on a further pool. Like Luna reserve, it is used only when the
user instructs it, and a result is a worker summary, not evidence.

## Commands

```sh
python3 -m creme antigravity status [--model M] [--effort low|medium|high] \
    [--min-remaining-percent P] [--json]
python3 -m creme antigravity run --brief FILE|- --target DIR [--model M] \
    [--effort low|medium|high] [--timeout-seconds N] [--write] \
    [--min-remaining-percent P] [--allow-command REGEX]... \
    [--lean-goal GOAL] [--json]
```

Three families are in use: `gemini-3.8-flash` (the default; user decision
2026-09-25) and, since 2026-10-04, `claude-sonnet-5-5` and `claude-opus-5-5`.
`--model` takes the family and `--effort` (`low`, `medium` or `high`, the only
levels `agy` exposes) picks the level; the CLI builds the slug, e.g.
`claude-opus-5-5-medium`. A conflicting full slug is refused, as `agy` itself
refuses it. The default effort is provisional (`medium`) until the effort-ladder
experiment decides. Record a dispatch as an episode of the `antigravity` client
([model selection](model-fit.md)); the goal store's `model-fit/antigravity.md`
is historical. The normal acceptance imports the run record by its directory
(`antigravity_runs`, see [episode requests](model-fit-episodes.md)); a run with
a checkpoint step or an invoked subagent stays usage-incomplete, because those
steps report no tokens.

The Claude families are separate model-fit options from Claude Code's
`opus`/`sonnet` (user decision 2026-10-04): the harness, toolset and quota pool
differ, so no Claude Code cell transfers. Whether `agy`'s low/medium/high equal
Claude Code's levels of the same name is likely but unverified. Measured
differences (2026-10-04, `agy` 1.2.16):

- `agy` advertises the full toolset in its `init` event, but a Claude model can
  call only `view_file`, `run_command`, `write_to_file`, `replace_file_content`
  and non-file tools. It has no `list_dir`, `grep_search`, `find_by_name`,
  `multi_replace_file_content` or `sed_file`. In read-only mode it therefore
  reads only files the brief names by absolute path; it cannot list or search.
  Give a Claude-family read-only brief the exact paths, or use a Gemini model
  for open exploration.
- `agy` passes the brief as the whole prompt, so a brief must name the target
  path; a Claude model does not infer it from the workspace.
- `init.model` and every payload's `modelName` carry the exact slug, so the
  model-pin guard and verdict apply unchanged.
- The "Claude and GPT" pool is small: one trivial read-only run cost about 1% of
  the 5-hour window on `claude-sonnet-5-5-low` and about 5% on
  `claude-opus-5-5-low`.

`status` reads `agy -p /usage` and `/credits`, both zero-token, plus
`useG1Credits` from `~/.gemini/antigravity-cli/settings.json`. `agy models` lists
the slugs. A `gemini-*` model draws on the Gemini pool; `claude-*` and
`gpt-oss-*` models draw on the "Claude and GPT" pool. Each pool has a 5-hour and
a weekly window.

## How a run stays bounded and on plan quota

- **Admission.** The run is refused when the model's pool is below the floor
  (default 5% on either window, configured via `--min-remaining-percent 0..100`),
  or when paid AI credits are positive and `useG1Credits` is not explicitly
  `false`. Setting `--min-remaining-percent 0` allows running down to 0% remaining
  plan quota when explicitly user-authorized; it is a quota reserve preference,
  not permission for paid credits or model fallback.
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

Write-mode prompts carry the shared-library rule from `templates/shared/library-first.md` (check the shared library first; hoist generically applicable results into it).

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
