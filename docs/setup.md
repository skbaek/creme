# First-machine setup

This guide takes a new macOS or Linux account from an empty development
environment to a working public Creme, Jaune, and Blanc sibling layout. It is
also the checklist an agent should follow when preparing a machine for Lean
contract work.

Creme is the launch root and owns the shared agent workflow. Jaune and Blanc
remain standalone Lean projects and own their builds, proofs, fixtures, and
verification gates. A private Plans repository is optional: public contributors
do not need it, and no command in this guide reads it.

## Safety boundary

Begin with a read-only inventory. Before an agent runs any command that uses
`sudo`, installs a global tool, executes a downloaded installer, starts a large
download, changes client trust, or writes user configuration, it must show the
exact action and obtain the user's approval. Preserve existing checkouts and
configuration; do not repair, replace, or delete them as part of setup.

The supported hosts are macOS and Linux. Windows is not a v0.1 acceptance
target. Plan for at least 35 GiB of free space for both Lean repositories and
their build artifacts, plus the separately documented space for any external
fixture lane. A first build can take substantial time and memory. Build Jaune
and Blanc sequentially unless the host has been measured for concurrent Lean
loads.

Committed version policy lives in [`scripts/versions.json`](../scripts/versions.json).
At the time of writing it requires Python 3.9 or newer, pins the Lean project
toolchain, and pins the Lean MCP package. Do not replace project pins with a
machine-global version.

## 1. Inventory the host

Run these checks before installing anything:

```sh
uname -a
git --version
python3 --version
curl --version
cc --version
elan --version
uvx --version
codex --version
claude --version
```

Missing optional commands may print `command not found`; record that rather
than treating the inventory itself as a failure. Only one supported agent
client, Codex or Claude Code, is required.

The base system packages are Git, curl, CA certificates, Python 3.9 or newer,
and a C/C++ build toolchain. On macOS, install Apple's Command Line Tools if
`git` or `cc` is unavailable. On Ubuntu, the conventional package set is:

```sh
sudo apt update
sudo apt install -y git curl ca-certificates build-essential python3 python3-venv
```

These are examples, not permission to mutate the host. A user or administrator
must approve them, and other Linux distributions should use their native
package manager.

## 2. Install Lean and `uvx`

[elan](https://github.com/leanprover/elan) selects the Lean version named by
each repository's `lean-toolchain` file. Its official macOS/Linux installer is:

```sh
curl -sSf https://elan.lean-lang.org/elan-init.sh | less
curl https://elan.lean-lang.org/elan-init.sh -sSf | sh
```

The first command is an inspection step. Run the second only after approval,
then open a new shell and verify `elan --version`. Do not force a global Lean
version; `lake` and `lean` must be invoked from inside Jaune or Blanc so elan
selects the committed toolchain.

Creme's Lean MCP launch uses `uvx`. Follow the
[official uv installation guide](https://docs.astral.sh/uv/getting-started/installation/)
or an approved system package. The official macOS/Linux installer is:

```sh
curl -LsSf https://astral.sh/uv/install.sh | less
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Again, inspect first and execute only after approval. Open a new shell and
verify `uvx --version`.

## 3. Install one supported client

Client installation, sign-in, project trust, and MCP approval are interactive
user actions. Client documentation changes more quickly than Creme, so use the
linked official page as the authority.

For Codex, follow the [Codex CLI installation guide](https://learn.chatgpt.com/docs/codex/cli).
Its official standalone installer for macOS and Linux is:

```sh
curl -fsSL https://chatgpt.com/codex/install.sh | sh
codex --version
```

For Claude Code, follow the
[Claude Code installation guide](https://code.claude.com/docs/en/installation).
Its recommended native installer for macOS and Linux is:

```sh
curl -fsSL https://claude.ai/install.sh | bash
claude --version
claude doctor
```

Remote installers must be reviewed and approved under the safety boundary
above. Stop for the user at sign-in, workspace-trust, and MCP-approval prompts.

## 4. Clone the public sibling layout

The supported default layout places all three repositories beside each other.
If any destination already exists, inspect it and stop rather than overwriting
or repurposing it.

```sh
cd ~
git clone https://github.com/skbaek/creme.git creme
git clone https://github.com/skbaek/jaune.git jaune
git clone https://github.com/skbaek/blanc.git blanc
```

The important relationship is:

```text
~/creme
~/jaune
~/blanc
```

An owner with access to a private goal store may also clone it separately as
`~/plans`. It is not a Creme, Jaune, or Blanc build dependency. Other users can
store concrete goal documents in any private or local location they control.

## 5. Initialize Creme

Creme's initializer previews by default. Run the read-only checks first, review
the proposed profile, and only then write it:

```sh
cd ~/creme
python3 -m creme platform
python3 -m creme init --workspace-root ..
python3 -m creme init --workspace-root .. --write
python3 -m creme validate-profile
python3 -m creme doctor --workspace-root ..
./scripts/check.sh
```

The live host profile is `.creme/host-profile.json` and is ignored by Git. Use
`--replace` only after reviewing a changed host fingerprint or policy. `doctor`
may report an optional capability as `UNAVAILABLE` on Linux; that is supported
when the capability is not required by the chosen gate.

### Codex desktop app

Launching the ChatGPT desktop app from Spotlight, the Dock, or Finder is fine;
the important launch root is the local project selected inside Codex. Follow
the official [Projects and chats](https://learn.chatgpt.com/docs/projects)
workflow:

1. Add a local project for `~/creme`, or open its project menu and choose
   **Edit project** if it already exists.
2. Choose **Add folder** and attach `~/creme`, `~/jaune`, and `~/blanc`.
3. Choose **Make primary** for `~/creme`.
4. Start each contract task from that Creme project.

The primary folder is the default working directory and the automatic discovery
root for `AGENTS.md`, skills, and project `config.toml`. Jaune and Blanc remain
secondary folders: they are available to search, read, and edit without
becoming competing discovery roots. An owner who needs the private goal store
may attach `~/plans` as another secondary folder after completing the
public-only acceptance run; public contributors and that acceptance run omit
Plans.

Before trusting the project, confirm that the displayed working directory is
`~/creme` and review the pinned Lean MCP server. A projectless task, or a
project whose primary folder is Jaune or Blanc, does not exercise Creme's
client contract. Opening a file from another task also does not change that
task's project root.

### Codex CLI

Codex CLI users who need sibling writes should preview and then explicitly
install the generated least-privilege profile:

```sh
cd ~/creme
python3 -m creme client-profile --workspace-root ..
python3 -m creme client-profile --workspace-root .. \
  --output ~/.codex/creme.config.toml --write
codex --profile creme
```

Review the preview before `--write`. Permission-profile syntax is beta; compare
the generated file with the installed client's current documentation. Creme
does not edit the user's main Codex configuration.

The CLI alternative to the desktop project is to start Codex with
`cd ~/creme && codex --profile creme`; the directory where the CLI starts is its
project.

### Codex host-capability delegates

Some Codex installations authorize a stable executable path for host
operations that the project sandbox cannot perform directly. Creme can create
three user-local delegates for that boundary: semaphore, telemetry, and Lean
reclamation. They contain no copied host logic and always execute the current
checkout's capability CLI.

Preview the complete contents and destinations before the first write:

```sh
cd ~/creme
python3 -m creme host-wrappers --output-dir ~/.codex/bin
python3 -m creme host-wrappers --output-dir ~/.codex/bin --write
```

If those paths already contain an older install, compare the preview and use
`--replace` only after review. The installer refuses implicit destinations and
existing files. It writes all three files mode `0700` through same-directory
temporary files. Relocating Creme changes their target, so regenerate them
from the canonical checkout. `doctor` warns when none are installed and fails
when it finds a partial, stale, linked, or non-executable set.

Claude Code users launch `claude` from `~/creme`, accept that exact workspace
when prompted, and review the pinned `lean-lsp-mcp` project server before
approving it. The relative sibling access in `.claude/settings.json` does not
copy or trust user-global state.

For either client, always launch from `~/creme`. Launching from `~/jaune`,
`~/blanc`, or a projectless directory does not select Creme's instructions,
skills, or MCP configuration. See [client discovery and trust](client-discovery.md)
for the complete contract.

## 6. Build Jaune and Blanc

Start with Jaune. Its README recommends a selective Mathlib cache download so
the first build does not compile all of Mathlib from source:

```sh
cd ~/jaune
lake exe cache get Mathlib/Data/Nat/Basic.lean Mathlib/Data/List/Lemmas.lean \
  Mathlib/Data/List/TakeDrop.lean Mathlib/Data/List/TakeWhile.lean \
  Mathlib/Data/UInt.lean Mathlib/Tactic/NormNum.lean \
  Mathlib/Data/List/Chain.lean
lake build
scripts/check-hygiene.sh
scripts/check-integrity.sh
```

Then build Blanc, whose Lake manifest fetches its pinned Jaune dependency:

```sh
cd ~/blanc
lake exe cache get
lake build
scripts/check-doc-counts.sh
scripts/check-layering.sh
```

The cache fetches are network operations and may be large. If one fails, do not
hide the error or pretend the later build used the intended cache; diagnose it
or obtain approval for an uncached build. Blanc's full proof audit can then run
without rebuilding:

```sh
cd ~/blanc
scripts/check.sh --no-build
```

Before editing, read Jaune's
[`scripts/GATES.md`](https://github.com/skbaek/jaune/blob/main/scripts/GATES.md)
and Blanc's
[`scripts/GATES.md`](https://github.com/skbaek/blanc/blob/main/scripts/GATES.md).
Those catalogues, not this setup guide, decide which additional gates a change
requires and which ones may share a host.

## 7. Add only the fixtures a goal needs

Basic proof editing and repository builds do not require every external oracle
or corpus. Do not download all optional inputs pre-emptively.

Jaune's
[`scripts/vectors/SOURCES.md`](https://github.com/skbaek/jaune/blob/main/scripts/vectors/SOURCES.md)
is the authority for public fixture origins, pins, environment checks, path
overrides, and disk costs. Its safe bootstrap commands all support a preview:

```sh
cd ~/jaune
python3 scripts/bootstrap_mainnet.py --dry-run
python3 scripts/bootstrap_legacy.py --dry-run
python3 scripts/bootstrap_eest.py --dry-run
```

Choose only the lane required by the goal, review the preview, and then obtain
approval before omitting `--dry-run`. Current-mainnet needs at least 13 GiB of
free space for a fresh install, legacy fixtures at least 7 GiB, and the EEST
release lane at least 9 GiB. These lanes can overlap in purpose but are not
interchangeable.

Some oracle gates require the exact frozen Python 3.11.9 environment documented
in Jaune's source ledger; system Python is not a substitute. Blanc's generated
[`docs/GATE_INPUTS.md`](https://github.com/skbaek/blanc/blob/main/docs/GATE_INPUTS.md)
names the direct and external inputs for every gate. Resolve a goal's gate set
first, then provision the corresponding inputs.

## 8. Verify the client workflow

After both builds pass, launch the selected client from `~/creme` and confirm:

1. Creme's root `AGENTS.md` is active.
2. The `lean-inspector` and `lean-prover` skills are discoverable.
3. The pinned `lean-lsp-mcp` server is available after user approval.
4. Jaune and Blanc are readable, and writable only when the selected profile
   grants it.
5. A small representative diagnostic or edit in a disposable branch/worktree
   succeeds without starting an unplanned whole-repository rebuild.

Treat a large unexpected rebuild or severe memory pressure as a diagnostic
failure: stop the exact client-owned process group, preserve evidence, and
recover resources before retrying. Do not substitute a static file check for a
fresh client observation.

## Goal work after setup

Public goal-writing and execution methods are part of Creme:

- [`docs/guides/goal.md`](guides/goal.md) is the authoritative method for
  authoring a goal document.
- [`docs/guides/execution.md`](guides/execution.md) is the authoritative method
  for executing it.

Launch the authoring or execution agent from `~/creme` and refer to those
paths. The concrete goal may live in an authorized private repository such as
`~/plans`, but Plans is not the method authority and is not required for public
contributors. Multiple goal documents can be authored concurrently; later
executions must coordinate when they share a repository integration line,
external state, or memory-heavy gates.

## Agent handoff prompt

The following is a safe starting prompt for a first-machine setup task:

> Work from the public Creme repository and follow `docs/setup.md` completely.
> Begin with a read-only inventory. Before every `sudo` command, global install,
> downloaded installer, large network download, trust/MCP approval, or user
> configuration write, show the exact proposed action and wait for my approval.
> Preserve existing checkouts and configuration. Stop for interactive login and
> trust decisions. Build Jaune and Blanc sequentially, install only fixtures
> required by the selected goal, and report exact verification commands and
> results.
