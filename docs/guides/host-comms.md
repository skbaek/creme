# Host comms: messages between masters

When several hosts each run a master over [one shared stack](goal-stack.md#one-stack-across-hosts),
the masters talk on a channel beside the stack, never inside it. Use it for
requests, answers and notices between masters: "please merge X before I
rebase", "blanc-y landed on the mac", "can you take the timing row?". It is
not goal state. Messages never change goal order or status; a decision, a
status change, a new goal or a hand-off is still made in the stack or the
goal, and a message only points at it.

## Storage

The channel is a channel-only branch (`host-comms`) of the goal store's
origin, sharing no history with its default branch or the stack branch. Each
host checks it out as a Git worktree at `$GOAL_STORE/.host-comms/`, excluded
in that clone's `.git/info/exclude` so no tracked file changes. Every file is
written once and never edited or deleted:

- `messages/<UTC-timestamp>-<from-host>-<8 hex>.md`: `+++` front matter in
  the stack's TOML subset (`id`, `from`, `to` (a host name or `all`),
  `subject`, optional `in_reply_to` and `goal`, `created`), then a Markdown
  body.
- `acks/<message-id>/<host>.toml`: `message`, `host`, `acked`, optional
  `note`.

Hosts only add distinct paths, so concurrent writers never conflict. A write
commits its file and pushes without force; a rejected push fetches, rebases
the never-published commit onto the new head and retries (a bounded number
of times). A write that cannot be published is dropped: it either reached
the origin or nothing was sent. Malformed files, and published files that a
later commit changed, are reported by `inbox` and `watch`, never fatal.

## Commands

The host name is the profile's (`python3 -m creme set-host-name NAME`), as
for the stack; unnamed hosts refuse.

```sh
python3 -m creme master comms send --to HOST|all --subject TEXT (--body-file FILE | --body TEXT) [--goal ID] [--in-reply-to ID] [--json]
python3 -m creme master comms inbox [--all] [--json]
python3 -m creme master comms show ID [--json]
python3 -m creme master comms thread ID [--json]
python3 -m creme master comms ack ID [--note TEXT] [--json]
python3 -m creme master comms watch [--interval SECS] [--once]
```

`send` and `ack` are master only: they authenticate through the master lease
exactly as stack mutations do. The first `send` on any host opens the
channel (publishes the branch); later hosts attach to it on their first
read. Reads never acquire. They fetch, so unlike stack reads they touch the
network. `inbox` lists messages addressed to this host or `all`, from other
hosts, that this host has not acknowledged, oldest first; `--all` lists
every message on the channel with who acknowledged it.

`watch` polls (default every 60 s) and prints exactly one line per newly
arrived unacknowledged message, `COMMS <id> from=<host> subject=<...>`. Its
first pass reports every message still unacknowledged. A failure prints one
`COMMS-ERROR ...` line per failure streak (a malformed file once) and the
loop continues; it never exits on a network failure. `--once` makes one
pass and exits nonzero after a failure.

## Master practice

- **At master entry**, start `python3 -m creme master comms watch` under the
  client's background watcher (Claude Code's Monitor tool; a Codex background
  shell) and keep it running for the session. Each `COMMS` line is a
  notification: read it with `show`.
- **At every boundary** (a merge, a worker return, a stack change, before
  waiting on anything) run `master comms inbox`. Before waiting on the other
  host, check it has the request; do not wait silently.
- **One request per message.** Name the goal with `--goal` when there is one.
  Put the ask in the subject; the body carries context and pointers (stack
  entry, branch, commit, report path), never a copy of goal state.
- **Answer with `--in-reply-to`**, so `thread` shows the conversation.
- **Acknowledge when acted on**, not when read: `ack ID --note "..."`. An
  unacknowledged message is still owed an action. Acks are per host, so a
  broadcast to `all` is acknowledged by each host separately.
- Record any decision a message prompts in the stack, the goal or the master
  record, as usual; the channel is not a record.
