"""Host comms: one immutable file per message on a channel-only branch.

Two simulated hosts ("wsl" and "mac") are two clones of one local bare
origin. Nothing touches a live goal store or a real remote.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from creme import cli, goal_stack_git as shared, host_comms as comms, master_runtime as runtime

GIT_ENV = {
    "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
}
NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def writer(root, lease="a", renew=None):
    return runtime.RecordWriter(
        root, renew=renew or (lambda: (True, "fixture authority")),
        lease_snapshot=lambda: {"schema_version": 4,
                                "lease": {"client": "codex", "lease_id": lease * 32}},
    )


class Host:
    def __init__(self, name, plans):
        self.name = name
        self.plans = plans
        self.record = plans / "master"
        runtime.initialize_empty_record(self.record)
        self.writer = writer(self.record, "a" if name == "wsl" else "b")

    def send(self, to, subject="hello", body="Body text.\n", **kw):
        kw.setdefault("now", NOW)
        return comms.send(self.record, to=to, subject=subject, body=body,
                          writer=self.writer, host=self.name, **kw)

    def ack(self, message_id, **kw):
        kw.setdefault("now", NOW)
        return comms.ack(self.record, message_id, writer=self.writer, host=self.name, **kw)

    def channel(self):
        return comms.read_channel(self.plans, self.name)

    def inbox(self, **kw):
        return comms.inbox(self.channel(), self.name, **kw)


class CommsFixture(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, GIT_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.origin = base / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        seed = base / "seed"
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
        (seed / ".gitignore").write_text("/master/\n")
        (seed / "README.md").write_text("plans\n")
        git(seed, "add", ".")
        git(seed, "commit", "-q", "-m", "seed")
        git(seed, "remote", "add", "origin", str(self.origin))
        git(seed, "push", "-q", "origin", "main")
        self.base = base
        hosts = []
        for name in ("wsl", "mac"):
            plans = base / name / "plans"
            subprocess.run(["git", "clone", "-q", str(self.origin), str(plans)], check=True)
            hosts.append(Host(name, plans))
        self.wsl, self.mac = hosts

    def remote_paths(self):
        listed = git(self.origin, "ls-tree", "-r", "--name-only", comms.DEFAULT_BRANCH)
        return sorted(listed.splitlines())


class HostCommsTest(CommsFixture):
    def test_reads_before_first_use_change_nothing(self):
        channel = self.mac.channel()
        self.assertFalse(channel.attached)
        self.assertEqual(comms.inbox(channel, "mac"), [])
        self.assertFalse((self.mac.plans / comms.COMMS_DIR).exists())
        self.assertEqual(git(self.origin, "branch", "--list", comms.DEFAULT_BRANCH), "")

    def test_first_send_opens_a_history_free_channel_branch(self):
        sent = self.wsl.send("mac", subject="please rebase", goal="blanc-x")
        self.assertEqual(sent["status"], "OK")
        self.assertRegex(sent["id"], r"^20261024T120000Z-wsl-[0-9a-f]{8}$")
        self.assertEqual(self.remote_paths(), ["README.md", f"messages/{sent['id']}.md"])
        # The channel shares no history with the goal store's default branch.
        self.assertNotEqual(subprocess.run(
            ["git", "-C", str(self.origin), "merge-base", "main", comms.DEFAULT_BRANCH],
            capture_output=True).returncode, 0)
        self.assertEqual(git(self.origin, "rev-list", "--count", comms.DEFAULT_BRANCH), "2")
        worktree = self.wsl.plans / comms.COMMS_DIR
        self.assertEqual(shared.upstream(worktree), ("origin", comms.DEFAULT_BRANCH))
        # Ignored in this clone only: the goal store's tracked files are untouched.
        self.assertEqual(git(self.wsl.plans, "status", "--porcelain"), "")
        text = (worktree / f"messages/{sent['id']}.md").read_text()
        self.assertTrue(text.startswith("+++\nid = "))
        self.assertIn('goal = "blanc-x"\n', text)
        self.assertIn('created = "2026-10-24T12:00:00Z"\n+++\n\nBody text.\n', text)

    def test_send_inbox_ack_round_trip_across_hosts(self):
        sent = self.wsl.send("mac", subject="question", body="Is blanc-x merged?\n")
        self.assertEqual(self.wsl.inbox(), [])  # own messages are never inbox items
        inbox = self.mac.inbox()
        self.assertEqual([m.id for m in inbox], [sent["id"]])
        self.assertEqual((inbox[0].sender, inbox[0].subject), ("wsl", "question"))
        self.assertEqual(inbox[0].body, "Is blanc-x merged?\n")
        acked = self.mac.ack(sent["id"], note="answered")
        self.assertTrue(acked["changed"])
        self.assertEqual(self.mac.inbox(), [])
        self.assertFalse(self.mac.ack(sent["id"])["changed"])
        reply = self.mac.send("wsl", subject="answer", in_reply_to=sent["id"],
                              now=NOW + timedelta(minutes=5))
        channel = self.wsl.channel()
        self.assertEqual(channel.acked_by(sent["id"], "mac").note, "answered")
        self.assertEqual([m.id for m in comms.inbox(channel, "wsl")], [reply["id"]])
        self.assertEqual([m.id for m in comms.thread(channel, reply["id"])],
                         [sent["id"], reply["id"]])
        self.assertEqual(self.remote_paths(), sorted([
            "README.md", f"messages/{sent['id']}.md", f"messages/{reply['id']}.md",
            f"acks/{sent['id']}/mac.toml"]))
        # Broadcasts reach every other host; acks are per host.
        notice = self.wsl.send("all", subject="notice")
        self.assertEqual([m.id for m in self.mac.inbox()], [notice["id"]])
        self.assertEqual(len(self.wsl.inbox(include_all=True)), 3)

    def test_ack_refuses_messages_for_other_hosts_and_unknown_ids(self):
        sent = self.wsl.send("mac")
        with self.assertRaisesRegex(comms.CommsError, "not addressed to wsl"):
            self.wsl.ack(sent["id"])
        with self.assertRaisesRegex(comms.CommsError, "unknown message id"):
            self.mac.ack("20261024T120000Z-wsl-00000000")
        with self.assertRaisesRegex(comms.CommsError, "not on the channel"):
            self.mac.send("wsl", in_reply_to="20261024T120000Z-wsl-00000000")
        with self.assertRaisesRegex(comms.CommsError, "own host"):
            self.mac.send("mac")
        with self.assertRaisesRegex(comms.CommsError, "one line"):
            self.mac.send("wsl", subject="two\nlines")

    def test_concurrent_sends_rebase_and_retry(self):
        self.wsl.send("mac", subject="opening")
        self.mac.channel()  # mac attaches at the opening commit
        self.mac.send("wsl", subject="from mac")
        # WSL's copy is now stale: its first push is rejected, then rebased.
        real_push = shared.push
        pushes = []

        def counting(worktree):
            result = real_push(worktree)
            pushes.append(result[0])
            return result

        # Skipping the pre-write sync models a host that read just before the
        # other host's push landed.
        with mock.patch.object(comms, "sync", lambda worktree: None), \
                mock.patch.object(shared, "push", side_effect=counting):
            sent = self.wsl.send("mac", subject="from wsl")
        self.assertEqual(pushes, [False, True])
        paths = self.remote_paths()
        self.assertEqual(len([p for p in paths if p.startswith("messages/")]), 3)
        self.assertIn(f"messages/{sent['id']}.md", paths)
        # One linear chain, no merges, and both hosts see both messages.
        self.assertEqual(git(self.origin, "rev-list", "--merges", comms.DEFAULT_BRANCH), "")
        self.assertEqual({m.subject for m in self.mac.inbox()}, {"opening", "from wsl"})
        self.assertEqual({m.subject for m in self.wsl.inbox()}, {"from mac"})

    def test_unpublishable_send_leaves_nothing_behind(self):
        self.wsl.send("mac")
        head = shared.head(self.wsl.plans / comms.COMMS_DIR)
        hook = self.origin / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        with self.assertRaisesRegex(comms.CommsError, "nothing sent"):
            self.wsl.send("mac", subject="lost")
        worktree = self.wsl.plans / comms.COMMS_DIR
        self.assertEqual(shared.head(worktree), head)
        self.assertTrue(shared.clean(worktree))

    def test_writes_refuse_without_the_master_lease(self):
        denied = writer(self.wsl.record, renew=lambda: (False, "foreign master"))
        with self.assertRaises(runtime.RenewalRefused):
            comms.send(self.wsl.record, to="mac", subject="x", body="y", writer=denied,
                       host="wsl", now=NOW)
        self.assertEqual(git(self.origin, "branch", "--list", comms.DEFAULT_BRANCH), "")
        self.assertFalse((self.wsl.plans / comms.COMMS_DIR).exists())
        sent = self.wsl.send("mac")
        denied = writer(self.mac.record, renew=lambda: (False, "foreign master"))
        with self.assertRaises(runtime.RenewalRefused):
            comms.ack(self.mac.record, sent["id"], writer=denied, host="mac", now=NOW)
        self.assertEqual(len(self.mac.inbox()), 1)

    def test_unnamed_host_refuses(self):
        with mock.patch.object(comms.store, "local_host_name", return_value=None):
            with self.assertRaisesRegex(comms.CommsError, "set-host-name"):
                comms.read_channel(self.wsl.plans)

    def test_malformed_files_are_reported_not_fatal(self):
        sent = self.wsl.send("mac")
        clone = self.base / "vandal"
        subprocess.run(["git", "clone", "-q", "--branch", comms.DEFAULT_BRANCH, str(self.origin),
                        str(clone)], check=True)
        (clone / "messages/20261024T120000Z-wsl-deadbeef.md").write_text("no front matter\n")
        (clone / "messages/notes.txt").write_text("stray\n")
        (clone / f"acks/{sent['id']}").mkdir(parents=True)
        (clone / f"acks/{sent['id']}/mac.toml").write_text('host = "wsl"\n')
        (clone / f"messages/{sent['id']}.md").write_text("+++\nedited\n+++\n")
        git(clone, "add", ".")
        git(clone, "commit", "-q", "-m", "vandalism")
        git(clone, "push", "-q", "origin", comms.DEFAULT_BRANCH)
        channel = self.mac.channel()
        reasons = dict(channel.problems)
        self.assertIn((f"messages/{sent['id']}.md",
                       "changed after publication (git status M); files are immutable"),
                      channel.problems)
        self.assertIn("front matter", reasons[f"messages/{sent['id']}.md"])
        self.assertIn("missing +++", reasons["messages/20261024T120000Z-wsl-deadbeef.md"])
        self.assertIn("file name", reasons["messages/notes.txt"])
        self.assertIn("do not match", reasons[f"acks/{sent['id']}/mac.toml"])
        self.assertEqual(comms.inbox(channel, "mac"), [])
        lines = []
        self.assertEqual(comms.watch(self.mac.plans, host="mac", once=True, emit=lines.append), 0)
        self.assertTrue(lines)
        self.assertTrue(all(line.startswith("COMMS-ERROR malformed ") for line in lines))

    def test_parse_round_trip_and_unknown_keys(self):
        message = comms.Message(id="20261024T120000Z-my-host-0123abcd", sender="my-host", to="all",
                                subject="s", created="2026-10-24T12:00:00Z", body="b\n",
                                path="", in_reply_to=None, goal=None)
        relative = f"messages/{message.id}.md"
        data = comms.render_message(message)
        self.assertEqual(comms.parse_message(relative, data).sender, "my-host")
        newer = data.replace(b"+++\n\n", b'priority = "high"\n+++\n\n', 1)
        self.assertEqual(comms.parse_message(relative, newer).subject, "s")


class WatchTest(CommsFixture):
    def test_watch_once_prints_one_line_per_unacknowledged_message(self):
        first = self.wsl.send("mac", subject="first  request")
        second = self.wsl.send("all", subject="notice", now=NOW + timedelta(seconds=1))
        third = self.wsl.send("all", subject="also", now=NOW + timedelta(seconds=2))
        self.mac.ack(third["id"])
        lines = []
        self.assertEqual(comms.watch(self.mac.plans, host="mac", once=True, emit=lines.append), 0)
        self.assertEqual(lines, [
            f"COMMS {first['id']} from=wsl subject=first request",
            f"COMMS {second['id']} from=wsl subject=notice",
        ])
        lines = []
        self.assertEqual(comms.watch(self.wsl.plans, host="wsl", once=True, emit=lines.append), 0)
        self.assertEqual(lines, [])

    def test_watch_reports_only_new_arrivals_and_survives_failures(self):
        self.wsl.send("mac", subject="one")
        lines, ticks = [], []
        failures = iter([None, OSError("network down"), OSError("still down"), None, None])
        real_read = comms.read_channel

        def flaky(root, host=None, **kw):
            failure = next(failures)
            if failure is not None:
                raise failure
            return real_read(root, host, **kw)

        def sleep(_):
            ticks.append(1)
            if len(ticks) == 3:
                self.wsl.send("mac", subject="two", now=NOW + timedelta(minutes=1))
            if len(ticks) == 5:
                raise KeyboardInterrupt

        with mock.patch.object(comms, "read_channel", side_effect=flaky):
            with self.assertRaises(KeyboardInterrupt):
                comms.watch(self.mac.plans, host="mac", interval=1, emit=lines.append, sleep=sleep)
        self.assertEqual([line.split(" ", 2)[0] for line in lines],
                         ["COMMS", "COMMS-ERROR", "COMMS"])
        self.assertEqual(lines[1], "COMMS-ERROR network down")
        self.assertTrue(lines[2].endswith("subject=two"))

    def test_watch_once_exits_nonzero_on_error(self):
        lines = []
        with mock.patch.object(comms, "read_channel", side_effect=OSError("offline\nnow")):
            self.assertEqual(comms.watch(self.mac.plans, host="mac", once=True,
                                         emit=lines.append), 2)
        self.assertEqual(lines, ["COMMS-ERROR offline now"])


class CommsCliTest(CommsFixture):
    def run_cli(self, host, *argv):
        location = SimpleNamespace(record_root=host.record)
        out = io.StringIO()
        with mock.patch.object(cli, "_master_location", return_value=(location, None)), \
                mock.patch.object(cli, "_stack_preamble", return_value=(location, None)), \
                mock.patch.object(comms.store, "local_host_name", return_value=host.name), \
                mock.patch.object(runtime, "RecordWriter", lambda root: host.writer), \
                redirect_stdout(out):
            arguments = cli.parser().parse_args(["master", "comms", *argv])
            code = arguments.func(arguments)
        return code, out.getvalue()

    def test_cli_send_inbox_show_ack_watch(self):
        body = self.base / "body.md"
        body.write_text("Please look at **blanc-x**.\n")
        code, text = self.run_cli(self.wsl, "send", "--to", "mac", "--subject", "look",
                                  "--body-file", str(body), "--json")
        self.assertEqual(code, 0, text)
        sent = json.loads(text)
        code, text = self.run_cli(self.mac, "inbox")
        self.assertEqual(code, 0)
        self.assertIn(f"{sent['id']}  from=wsl to=mac", text)
        code, text = self.run_cli(self.mac, "inbox", "--json")
        self.assertEqual(json.loads(text)["messages"][0]["subject"], "look")
        code, text = self.run_cli(self.mac, "show", sent["id"])
        self.assertIn("Please look at **blanc-x**.", text)
        code, text = self.run_cli(self.mac, "watch", "--once")
        self.assertEqual(text, f"COMMS {sent['id']} from=wsl subject=look\n")
        code, text = self.run_cli(self.mac, "ack", sent["id"], "--note", "on it")
        self.assertEqual((code, text.strip()), (0, f"comms ack OK: {sent['id']} acknowledged by mac"))
        code, text = self.run_cli(self.mac, "watch", "--once")
        self.assertEqual((code, text), (0, ""))
        code, text = self.run_cli(self.mac, "thread", sent["id"])
        self.assertIn("acked by mac at 2026-", text)

    def test_cli_refusal_is_json(self):
        code, text = self.run_cli(self.wsl, "send", "--to", "Mac!", "--subject", "x", "--body", "y")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(text)["status"], "refused")


if __name__ == "__main__":
    unittest.main()
