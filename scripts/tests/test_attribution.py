"""Commit attribution: hooks, journal, ledger, census, merge check, report.

Two simulated hosts ("wsl" and "mac") each hold a goal-store clone of one
local bare origin and a clone of one shared sibling origin. Every lease, host
profile and semaphore directory is a fixture; nothing touches a live
repository, goal store, hook or remote.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from creme import attribution as attr
from creme import attribution_cli, attribution_hook as hook, cli, doctor
from creme import git_channel as channels, master_runtime as runtime, semaphore, task_wind_down

GIT_ENV = {
    "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
}
# These tests name real public URLs to prove the guard refuses them. Git must
# never be able to reach the network from here, even with the guard disabled
# by a mutation: a control run once pushed a fixture ledger to a real public
# remote with the host's stored credentials. Only local transports are allowed.
NO_NETWORK = {"GIT_ALLOW_PROTOCOL": "file", "GIT_TERMINAL_PROMPT": "0"}
GIT_ENV.update(NO_NETWORK)
_NO_NETWORK = mock.patch.dict(os.environ, NO_NETWORK)


def setUpModule():
    _NO_NETWORK.start()


def tearDownModule():
    _NO_NETWORK.stop()


LEASES = {"wsl": "a" * 32, "mac": "b" * 32}


def git(cwd, *args, env=None):
    environ = dict(os.environ, **(env or {}))
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True, env=environ).stdout.strip()


def digest_of(lease_id):
    return runtime._actor_from_snapshot(
        {"schema_version": semaphore.MASTER_SCHEMA_VERSION,
         "lease": {"client": "claude", "lease_id": lease_id}})["acquisition_digest"]


def snapshot(lease_id):
    return {"schema_version": semaphore.MASTER_SCHEMA_VERSION,
            "lease": {"client": "claude", "lease_id": lease_id}}


class Host:
    def __init__(self, base: Path, name: str, plans_origin: Path, sibling_origin: Path):
        self.name = name
        self.home = base / name
        self.plans = self.home / "plans"
        self.blanc = self.home / "blanc"
        subprocess.run(["git", "clone", "-q", str(plans_origin), str(self.plans)], check=True)
        subprocess.run(["git", "clone", "-q", str(sibling_origin), str(self.blanc)], check=True)
        self.record = self.plans / "master"
        runtime.initialize_empty_record(self.record)
        self.state = self.home / "semaphore"
        self.state.mkdir()
        self.profile = self.home / "host-profile.json"
        self.profile.write_text(json.dumps({"host_name": name}))
        self.set_lease(LEASES[name])
        self.repos = {"blanc": self.blanc, "goal-store": self.plans}

    def set_lease(self, lease_id, *, renewed_at=None):
        self.lease_id = lease_id
        lease = None if lease_id is None else {
            "client": "claude", "lease_id": lease_id, "renewed_at": renewed_at or time.time(),
            "lease_seconds": 1800}
        (self.state / "master.json").write_text(json.dumps(
            {"schema_version": semaphore.MASTER_SCHEMA_VERSION, "lease": lease}))

    @property
    def digest(self):
        return digest_of(self.lease_id)

    def writer(self, renew=None):
        lease_id = self.lease_id
        return runtime.RecordWriter(self.record, renew=renew or (lambda: (True, "fixture")),
                                    lease_snapshot=lambda: snapshot(lease_id))

    def install(self, python=None):
        return attr.install(self.repos, python=python or sys.executable, script=attr.SCRIPT,
                            state_dir=self.state, profile=self.profile)

    def commit(self, repo=None, name="file", text=None, *extra):
        repo = repo or self.blanc
        path = Path(repo) / name
        path.write_text(text if text is not None else f"{self.name} {time.time_ns()}\n")
        git(repo, "add", name)
        git(repo, "commit", "-q", "-m", f"{self.name}: change {name}", *extra)
        return git(repo, "rev-parse", "HEAD")

    def records(self, repo=None):
        return [entry.record for entry in attr.journal(Path(repo or self.blanc))]

    def publish(self, **kw):
        return attr.publish(self.plans, self.repos, host=self.name, record_root=self.record,
                            writer=self.writer(), public_repos=[self.blanc], **kw)

    def ledger(self):
        return attr.read_ledger(self.plans, self.name)

    def location(self):
        return SimpleNamespace(goal_store=self.plans, record_root=self.record,
                               repository_roots=tuple(self.repos.items()))


class Fixture(unittest.TestCase):
    def setUp(self):
        environ = dict(GIT_ENV)
        patcher = mock.patch.dict(os.environ, environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("CREME_SEMAPHORE_DIR", None)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.base = base
        self.plans_origin = self.bare("plans.git", {".gitignore": "/master/\n", "README.md": "plans\n"})
        self.blanc_origin = self.bare("blanc.git", {"README.md": "blanc\n"})
        self.wsl = Host(base, "wsl", self.plans_origin, self.blanc_origin)
        self.mac = Host(base, "mac", self.plans_origin, self.blanc_origin)

    def bare(self, name, files):
        origin = self.base / name
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        seed = self.base / f"seed-{name}"
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
        for relative, text in files.items():
            (seed / relative).write_text(text)
        git(seed, "add", ".")
        git(seed, "commit", "-q", "-m", "seed", env={"GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
                                                     "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"})
        git(seed, "remote", "add", "origin", str(origin))
        git(seed, "push", "-q", "origin", "main")
        return origin

    def ledger_paths(self):
        listed = git(self.plans_origin, "ls-tree", "-r", "--name-only", attr.DEFAULT_BRANCH)
        return sorted(listed.splitlines())


class CaptureTest(Fixture):
    def test_hook_constants_mirror_the_runtime(self):
        self.assertEqual(hook.ACQUISITION_DOMAIN, runtime._ACQUISITION_DOMAIN)
        self.assertEqual(hook.MASTER_NAME, semaphore.MASTER_NAME)
        self.assertEqual(hook.SANCTIONED_SUFFIXES, task_wind_down.SANCTIONED_SUFFIXES)
        self.assertEqual(hook.CLIENT_RE.pattern, semaphore.CLIENT_LABEL.pattern)
        self.assertEqual(hook.LEASE_ID_RE.pattern, semaphore.HEX_32.pattern)

    def test_commit_is_recorded_without_changing_it(self):
        self.mac.install()
        worktree = self.mac.blanc / ".worktrees" / "goal-x-control"
        git(self.mac.blanc, "worktree", "add", "-q", "-b", "goal-x", str(worktree))
        dates = {"GIT_AUTHOR_DATE": "2026-11-15T10:00:00Z", "GIT_COMMITTER_DATE": "2026-11-15T10:00:00Z"}
        (worktree / "a.txt").write_text("same bytes\n")
        git(worktree, "add", "a.txt")
        git(worktree, "commit", "-q", "-m", "same message", env=dates)
        sha = git(worktree, "rev-parse", "HEAD")
        # The identical commit made in an unhooked clone has the same object id,
        # so message, author and every other byte are unchanged by the hook.
        plain = self.base / "plain"
        subprocess.run(["git", "clone", "-q", str(self.blanc_origin), str(plain)], check=True)
        git(plain, "checkout", "-q", "-b", "goal-x")
        (plain / "a.txt").write_text("same bytes\n")
        git(plain, "add", "a.txt")
        git(plain, "commit", "-q", "-m", "same message", env=dates)
        self.assertEqual(git(plain, "rev-parse", "HEAD"), sha)
        [record] = self.mac.records()
        self.assertEqual(record["sha"], sha)
        self.assertIn(sha[:12], record["id"])  # the resolved SHA, never "HEAD"
        self.assertNotIn("HEAD", record["id"])
        self.assertEqual(record["host"], "mac")
        self.assertEqual(record["client"], "claude")
        self.assertEqual(record["acquisition_digest"], self.mac.digest)
        self.assertEqual(record["lease_state"], "live")
        self.assertEqual(record["repository"], "blanc")
        self.assertEqual(record["goal"], "goal-x")
        self.assertEqual(record["hook"], "post-commit")
        self.assertEqual(record["committed_at"], "2026-11-15T10:00:00Z")
        expected = subprocess.run(["git", "-C", str(worktree), "patch-id", "--stable"], check=True,
                                  capture_output=True, input=subprocess.run(
                                      ["git", "-C", str(worktree), "show", sha], check=True,
                                      capture_output=True).stdout).stdout.split()[0].decode()
        self.assertEqual(record["patch_id"], expected)
        journal = attr.journal_root(self.mac.blanc)
        self.assertEqual(journal.stat().st_mode & 0o777, 0o700)
        self.assertEqual((journal / hook.JOURNAL).stat().st_mode & 0o777, 0o700)
        for path in (journal / hook.JOURNAL).iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        # Nothing tracked changed: the journal lives in the Git common directory.
        self.assertEqual(git(worktree, "status", "--porcelain"), "")

    def test_rewrites_merges_and_picks_are_recorded_fast_forwards_are_not(self):
        self.mac.install()
        repo = self.mac.blanc
        base = self.mac.commit(name="base")
        old = self.mac.commit(name="a")
        git(repo, "commit", "-q", "--amend", "-m", "amended")
        amended = git(repo, "rev-parse", "HEAD")
        git(repo, "checkout", "-q", "-b", "side", base)
        side = self.mac.commit(name="s")
        git(repo, "checkout", "-q", "main")
        git(repo, "merge", "-q", "--no-edit", "side")
        merge = git(repo, "rev-parse", "HEAD")
        git(repo, "checkout", "-q", "-b", "ff", "side")
        git(repo, "merge", "-q", "--ff-only", "main")  # fast-forward: no new commit
        git(repo, "checkout", "-q", "-b", "picks", base)
        git(repo, "cherry-pick", side)
        picked = git(repo, "rev-parse", "HEAD")
        git(repo, "checkout", "-q", "-b", "moving", base)
        moved_from = self.mac.commit(name="m")
        git(repo, "rebase", "-q", "main")
        moved = git(repo, "rev-parse", "HEAD")
        by_sha = {}
        for record in self.mac.records():
            by_sha.setdefault(record["sha"], []).append(record)
        self.assertIn(old, by_sha)
        self.assertEqual({r["hook"] for r in by_sha[amended]}, {"post-commit", "post-rewrite"})
        rewrite = [r for r in by_sha[amended] if r["hook"] == "post-rewrite"][0]
        self.assertEqual((rewrite["rewrite"], rewrite["rewritten_from"]), ("amend", old))
        self.assertEqual([r["hook"] for r in by_sha[merge]], ["post-merge"])
        self.assertIsNone(by_sha[merge][0]["patch_id"])
        self.assertIn(picked, by_sha)
        self.assertIn("post-rewrite", {r["hook"] for r in by_sha[moved]})
        self.assertIn(moved_from, by_sha)
        # The fast-forward added no record: every record is one of the above.
        self.assertEqual(set(by_sha), {base, old, amended, side, merge, picked, moved_from, moved})
        self.assertEqual([r["goal"] for r in by_sha[base]], ["main"])
        self.assertEqual(attr.census(self.mac.repos, 0, time.time() + 1), [])

    def test_absent_lease_records_none(self):
        self.mac.install()
        self.mac.set_lease(None)
        self.mac.commit()
        [record] = self.mac.records()
        self.assertEqual((record["acquisition_digest"], record["client"]), ("none", None))
        (self.mac.state / "master.json").write_text("{not json")
        self.mac.commit(name="other")
        self.assertEqual({r["acquisition_digest"] for r in self.mac.records()}, {"none"})

    def test_expired_lease_is_marked(self):
        self.mac.install()
        self.mac.set_lease(LEASES["mac"], renewed_at=time.time() - 7200)
        self.mac.commit()
        [record] = self.mac.records()
        self.assertEqual((record["acquisition_digest"], record["lease_state"], record["stale_lease"]),
                         ("none", "expired", self.mac.digest))

    def test_hook_never_fails_a_commit_and_always_exits_zero(self):
        self.mac.install()
        journal = attr.journal_root(self.mac.blanc)
        journal.chmod(0o500)
        self.addCleanup(journal.chmod, 0o700)
        result = subprocess.run(["git", "-C", str(self.mac.blanc), "commit", "-q", "--allow-empty",
                                 "-m", "unwritable journal"], capture_output=True)
        self.assertEqual(result.returncode, 0)
        journal.chmod(0o700)
        self.assertEqual(self.mac.records(), [])
        hook_file = attr.common_dir(self.mac.blanc) / "hooks" / "post-commit"
        for args in ([], ["--bogus"], ["post-commit"]):
            ran = subprocess.run([str(hook_file), *args], cwd=self.base, capture_output=True,
                                 stdin=subprocess.DEVNULL)
            self.assertEqual(ran.returncode, 0, ran)
        self.assertEqual(hook.run(["--repo", "blanc", "no-such-hook"], b"", cwd=str(self.mac.blanc)), 0)
        self.assertEqual(hook.run([], b"", cwd=str(self.base)), 0)
        self.assertIn("unknown hook", (journal / hook.LOG).read_text())

    def test_patch_id_failure_keeps_the_record(self):
        self.mac.install()
        attr.uninstall(self.mac.repos)
        sha = self.mac.commit()
        with mock.patch.object(hook, "patch_id", side_effect=subprocess.TimeoutExpired("git", 10)):
            self.assertEqual(hook.run(["--repo", "blanc", "--state-dir", str(self.mac.state),
                                       "post-commit"], b"", cwd=str(self.mac.blanc)), 0)
        [record] = self.mac.records()
        self.assertEqual((record["sha"], record["patch_id"]), (sha, None))

    def test_hook_never_reads_an_open_stdin(self):
        self.mac.install()
        reader, writer_end = os.pipe()  # a stdin that never closes
        self.addCleanup(os.close, writer_end)
        (self.mac.blanc / "f").write_text("x\n")
        git(self.mac.blanc, "add", "f")
        started = time.monotonic()
        result = subprocess.run(["git", "-C", str(self.mac.blanc), "commit", "-q", "-m", "x"],
                                stdin=reader, capture_output=True, timeout=30)
        os.close(reader)
        self.assertEqual(result.returncode, 0)
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(len(self.mac.records()), 1)

    def test_hook_uses_no_network_command(self):
        source = Path(hook.__file__).read_text()
        for word in ('"fetch"', '"push"', '"ls-remote"', '"clone"', '"remote"', "urllib", "socket"):
            self.assertNotIn(word, source)

    def test_hook_runs_on_the_oldest_interpreter(self):
        old = Path("/usr/bin/python3")
        if not old.exists():
            self.skipTest("no system python3")
        version = subprocess.run([str(old), "-c", "import sys; print(sys.version_info[:2] < (3, 11))"],
                                 capture_output=True, text=True)
        if version.returncode or version.stdout.strip() != "True":
            self.skipTest("system python3 is not older than 3.11")
        self.mac.install(python=str(old))
        sha = self.mac.commit()
        self.assertEqual([r["sha"] for r in self.mac.records()], [sha])

    def test_installer_refuses_to_displace_a_foreign_hook(self):
        hooks_dir = attr.common_dir(self.mac.blanc) / "hooks"
        (hooks_dir / "post-merge").write_text("#!/bin/sh\necho repository-owned\n")
        with self.assertRaisesRegex(attr.AttributionError, "post-merge exists"):
            self.mac.install()
        self.assertFalse((hooks_dir / "post-commit").exists())
        self.assertFalse(attr.journal_root(self.mac.plans).exists())
        self.assertEqual(attr.install_status(self.mac.blanc)["status"], "foreign")
        (hooks_dir / "post-merge").unlink()
        git(self.mac.plans, "config", "core.hooksPath", "elsewhere")
        with self.assertRaisesRegex(attr.AttributionError, "core.hooksPath"):
            self.mac.install()
        git(self.mac.plans, "config", "--unset", "core.hooksPath")
        self.mac.install()
        self.assertEqual(attr.install_status(self.mac.blanc)["status"], "installed")
        self.mac.install()  # reinstalling over its own hooks is allowed
        removed = attr.uninstall(self.mac.repos)["removed"]
        self.assertEqual(sorted(removed["blanc"]), sorted(hook.HOOKS))
        status = attr.install_status(self.mac.blanc)
        self.assertEqual(status["status"], "absent")
        self.assertIsNotNone(status["adopted_at"])  # adoption outlives the hooks

    def test_doctor_reports_install_state(self):
        checks = doctor.check_attribution(self.mac.repos, self.mac.plans)
        self.assertEqual(checks[0].status, doctor.STATUS_WARN)
        self.assertIn("blanc=absent", checks[0].detail)
        self.mac.install()
        checks = doctor.check_attribution(self.mac.repos, self.mac.plans)
        self.assertEqual([c.status for c in checks], [doctor.STATUS_OK, doctor.STATUS_OK])


class LedgerTest(Fixture):
    def test_publication_reaches_a_private_history_free_branch(self):
        self.mac.install()
        sha = self.mac.commit()
        published = self.mac.publish()
        self.assertEqual(published["published"], 1)
        [batch] = [p for p in self.ledger_paths() if p.startswith("hosts/mac/batches/")]
        self.assertEqual(self.ledger_paths(), ["README.md", batch])
        self.assertNotEqual(subprocess.run(
            ["git", "-C", str(self.plans_origin), "merge-base", "main", attr.DEFAULT_BRANCH],
            capture_output=True).returncode, 0)
        self.assertEqual(git(self.mac.plans, "status", "--porcelain"), "")
        # Journal files move to published/ only after the push.
        self.assertEqual([e.published for e in attr.journal(self.mac.blanc)], [True])
        self.assertIsNone(self.mac.publish()["commit"])  # nothing new, no commit
        ledger = self.wsl.ledger()  # readable from the other host
        self.assertEqual([r["sha"] for r in ledger.records], [sha])
        self.assertEqual(ledger.records[0]["host"], "mac")
        self.assertEqual(ledger.problems, [])
        # The publication commit itself is never attributed.
        self.assertEqual([r["sha"] for r in self.mac.records(self.mac.plans)], [])

    def test_hosts_write_distinct_paths_and_never_force(self):
        self.mac.install()
        self.wsl.install()
        self.mac.commit()
        self.mac.publish()
        self.wsl.commit()
        with mock.patch.object(channels, "reset_to_remote", lambda worktree, spec: None):
            self.wsl.publish()  # stale copy: rejected push, rebase, retry
        self.mac.commit(name="second")
        self.mac.publish()
        paths = self.ledger_paths()
        self.assertEqual(len([p for p in paths if p.startswith("hosts/mac/batches/")]), 2)
        self.assertEqual(len([p for p in paths if p.startswith("hosts/wsl/batches/")]), 1)
        self.assertEqual(git(self.plans_origin, "rev-list", "--merges", attr.DEFAULT_BRANCH), "")
        self.assertEqual({r["host"] for r in self.mac.ledger().records}, {"mac", "wsl"})

    def test_session_records_and_dead_predecessor_journal(self):
        self.mac.install()
        self.mac.set_lease("c" * 32)  # a predecessor that died without publishing
        stale = self.mac.commit()
        predecessor = self.mac.digest
        self.mac.set_lease(LEASES["mac"])
        started = attr.on_master_start(self.mac.location(), client="claude", model="opus",
                                       effort="high", writer=self.mac.writer(), host="mac")
        self.assertEqual(started["published"], 1)
        [session_file] = started["session_files"]
        self.assertIn(f"hosts/mac/sessions/{self.mac.digest}/start-", session_file)
        ledger = self.mac.ledger()
        self.assertEqual([(r["sha"], r["acquisition_digest"]) for r in ledger.records],
                         [(stale, predecessor)])
        session = ledger.sessions[("mac", self.mac.digest)]
        self.assertEqual((session["client"], session["model"], session["effort"]),
                         ("claude", "opus", "high"))

    def test_publication_refuses_a_public_remote(self):
        self.mac.install()
        self.mac.commit()
        git(self.mac.plans, "remote", "set-url", "origin", str(self.blanc_origin))
        with self.assertRaisesRegex(attr.AttributionError, "public repository"):
            self.mac.publish()
        git(self.mac.plans, "remote", "set-url", "origin", "https://github.com/skbaek/blanc.git")
        with self.assertRaisesRegex(attr.AttributionError, "public repository"):
            self.mac.publish()
        self.assertEqual(git(self.blanc_origin, "branch", "--list", attr.DEFAULT_BRANCH), "")
        self.assertEqual([e.published for e in attr.journal(self.mac.blanc)], [False])

    def test_publication_requires_the_master_lease(self):
        self.mac.install()
        self.mac.commit()
        with self.assertRaises(runtime.RenewalRefused):
            attr.publish(self.mac.plans, self.mac.repos, host="mac", record_root=self.mac.record,
                         writer=self.mac.writer(renew=lambda: (False, "foreign master")),
                         public_repos=[self.mac.blanc])
        self.assertEqual(git(self.plans_origin, "branch", "--list", attr.DEFAULT_BRANCH), "")

    def test_malformed_ledger_files_are_problems(self):
        self.mac.install()
        self.mac.commit()
        self.mac.publish()
        vandal = self.base / "vandal"
        subprocess.run(["git", "clone", "-q", "--branch", attr.DEFAULT_BRANCH, str(self.plans_origin),
                        str(vandal)], check=True)
        (vandal / "hosts/wsl/batches").mkdir(parents=True)
        forged = {"schema": 1, "host": "wsl", "records": [dict(self.mac.ledger().records[0])]}
        (vandal / "hosts/wsl/batches/x.json").write_text(json.dumps(forged))
        git(vandal, "add", ".")
        git(vandal, "commit", "-q", "-m", "forge")
        git(vandal, "push", "-q", "origin", attr.DEFAULT_BRANCH)
        problems = dict(self.wsl.ledger().problems)
        self.assertIn("foreign record", problems["hosts/wsl/batches/x.json"])


class CensusTest(Fixture):
    def test_unhooked_commit_is_unattributed(self):
        self.mac.install()
        start = time.time() - 1
        hooked = self.mac.commit()
        git(self.mac.blanc, "checkout", "-q", "-b", "elsewhere")
        (self.mac.blanc / "u").write_text("u\n")
        git(self.mac.blanc, "add", "u")
        git(self.mac.blanc, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "unhooked")
        unhooked = git(self.mac.blanc, "rev-parse", "HEAD")
        git(self.mac.blanc, "reset", "-q", "--hard", "HEAD~1")  # not a creation
        rows = attr.census(self.mac.repos, start, time.time() + 1)
        self.assertEqual([(r["repository"], r["sha"], r["status"]) for r in rows],
                         [("blanc", unhooked, "unattributed")])
        self.assertNotIn(hooked, [r["sha"] for r in rows])
        self.assertEqual(attr.census(self.mac.repos, time.time() + 5, time.time() + 9), [])

    def test_uninstalled_hook_records_nothing_and_census_lists_it(self):
        self.mac.install()
        attr.uninstall(self.mac.repos)
        sha = self.mac.commit()
        self.assertEqual(self.mac.records(), [])
        self.assertEqual([r["sha"] for r in attr.census(self.mac.repos, 0, time.time() + 1)], [sha])

    def test_master_end_publishes_the_census_count(self):
        self.mac.install()
        events = [{"kind": "master", "timestamp": attr.iso(time.time() - 60) + "",
                   "actor": {"client": "claude", "acquisition_digest": self.mac.digest}}]
        events[0]["timestamp"] = events[0]["timestamp"].replace("Z", ".000000Z")
        self.mac.commit()
        git(self.mac.blanc, "-c", "core.hooksPath=/dev/null", "commit", "-q", "--allow-empty",
            "-m", "missed")
        ended = attr.on_master_end(self.mac.location(), digest=self.mac.digest, client="claude",
                                   model="opus", effort="high", events=events,
                                   writer=self.mac.writer(), host="mac")
        self.assertEqual(ended["unattributed"], 1)
        session = self.mac.ledger().sessions[("mac", self.mac.digest)]
        self.assertEqual(len(session["census"]), 1)
        self.assertEqual(session["census"][0]["sha"], git(self.mac.blanc, "rev-parse", "HEAD"))
        self.assertTrue(session["ended_at"])

    def test_master_event_cli_only_acts_on_end(self):
        location = SimpleNamespace()
        self.assertIsNone(cli._attribution_end(location, {"kind": "note"}, {}))
        self.assertIsNone(cli._attribution_end(location, {"kind": "master"}, {"action": "start"}))
        with mock.patch.object(attr, "on_master_end", side_effect=RuntimeError("offline")), \
                mock.patch.object(runtime, "read_record",
                                  return_value=SimpleNamespace(events=())):
            result = cli._attribution_end(
                SimpleNamespace(record_root=self.mac.record),
                {"kind": "master", "actor": {"acquisition_digest": "d", "client": "c"}},
                {"action": "end"})
        self.assertEqual(result, {"status": "error", "detail": "offline"})


class MergeCheckTest(Fixture):
    def publish_first(self):
        return self.mac.publish()

    def test_unrecorded_local_commit_is_refused_until_adopted(self):
        self.mac.install()
        self.wsl.install()
        theirs = self.wsl.commit(name="w")
        git(self.wsl.blanc, "push", "-q", "origin", "main")
        git(self.mac.blanc, "pull", "-q", "--ff-only", "origin", "main")  # created elsewhere
        git(self.mac.blanc, "checkout", "-q", "-b", "candidate")
        recorded = self.mac.commit(name="r")
        git(self.mac.blanc, "-c", "core.hooksPath=/dev/null", "commit", "-q", "--allow-empty",
            "-m", "missed")
        missed = git(self.mac.blanc, "rev-parse", "HEAD")
        result = attr.merge_check(self.mac.repos, "blanc", "origin/main~1..candidate",
                                  publish_first=self.publish_first)
        self.assertEqual(result["status"], "refused")
        self.assertEqual([row["sha"] for row in result["unrecorded"]], [missed])
        self.assertNotIn(recorded, [row["sha"] for row in result["unrecorded"]])
        self.assertNotIn(theirs, [row["sha"] for row in result["unrecorded"]])
        adopted = attr.adopt(self.mac.repos, "blanc", [missed], reason="made with hooks off",
                             host="mac", adopter=hook.lease_identity(str(self.mac.state), time.time()))
        self.assertEqual(adopted["adopted"][0]["sha"], missed)
        result = attr.merge_check(self.mac.repos, "blanc", "origin/main~1..candidate",
                                  publish_first=self.publish_first)
        self.assertEqual(result["status"], "OK")
        record = [r for r in self.mac.ledger().records if r["sha"] == missed][0]
        self.assertEqual((record["source"], record["reason"], record["hook"]),
                         ("census", "made with hooks off", "adopt"))
        self.assertEqual(record["acquisition_digest"], "unknown")
        self.assertEqual(record["adopted_by"]["acquisition_digest"], self.mac.digest)
        self.assertEqual(record["goal"], "unknown")  # made in the primary checkout

    def test_publication_failure_refuses_the_check(self):
        self.mac.install()
        self.mac.commit()

        def failing():
            raise attr.AttributionError("could not publish; nothing sent")

        with self.assertRaisesRegex(attr.AttributionError, "nothing sent"):
            attr.merge_check(self.mac.repos, "blanc", "HEAD~1..HEAD", publish_first=failing)

    def test_unadopted_repository_says_so(self):
        result = attr.merge_check(self.mac.repos, "blanc", "HEAD", publish_first=self.publish_first)
        self.assertEqual(result["status"], "not-adopted")


class ReportTest(Fixture):
    def two_host_window(self):
        self.mac.install()
        self.wsl.install()
        start = time.time() - 1
        mac_shas = [self.mac.commit(name="m1"), self.mac.commit(name="m2")]
        git(self.mac.blanc, "push", "-q", "origin", "main")
        git(self.wsl.blanc, "pull", "-q", "--ff-only", "origin", "main")
        wsl_shas = [self.wsl.commit(name="w1")]
        git(self.wsl.blanc, "push", "-q", "origin", "main")
        git(self.mac.blanc, "pull", "-q", "--ff-only", "origin", "main")
        self.mac.publish()
        self.wsl.publish()
        return start, sorted(mac_shas), sorted(wsl_shas)

    def split(self, report):
        return {s["host"]: sorted(row["sha"] for rows in s["repositories"].get("blanc", {}).values()
                                  for row in rows) for s in report["sessions"]}

    def test_two_host_split_is_correct(self):
        start, mac_shas, wsl_shas = self.two_host_window()
        ledger = self.mac.ledger()
        report = attr.build_report(ledger, {"blanc": self.mac.blanc},
                                   attr.Filters(since=start, until=time.time() + 1))
        self.assertEqual(self.split(report), {"mac": mac_shas, "wsl": wsl_shas})
        self.assertEqual(report["unattributed"], [])
        mac_session = [s for s in report["sessions"] if s["host"] == "mac"][0]
        self.assertEqual(mac_session["acquisition_digest"], self.mac.digest)
        self.assertEqual(sorted(row["subject"] for row in mac_session["repositories"]["blanc"]["main"]),
                         ["mac: change m1", "mac: change m2"])
        by_session = attr.build_report(ledger, {"blanc": self.mac.blanc},
                                       attr.Filters(session=self.wsl.digest[:12]))
        self.assertEqual(self.split(by_session), {"wsl": wsl_shas})
        by_host = attr.build_report(ledger, {"blanc": self.mac.blanc}, attr.Filters(host="mac"))
        self.assertEqual(self.split(by_host), {"mac": mac_shas})
        self.assertIn("mac: change m2", attr.render_report(report))

    def test_ledger_missing_a_host_marks_its_commits_unattributed(self):
        start, mac_shas, wsl_shas = self.two_host_window()
        ledger = self.mac.ledger()
        ledger.records = [r for r in ledger.records if r["host"] != "wsl"]
        report = attr.build_report(ledger, {"blanc": self.mac.blanc},
                                   attr.Filters(since=start, until=time.time() + 1))
        self.assertEqual(self.split(report), {"mac": mac_shas})
        self.assertEqual([u["sha"] for u in report["unattributed"]], wsl_shas)

    def test_rewritten_commit_matches_by_patch_id(self):
        self.mac.install()
        start = time.time() - 1
        original = self.mac.commit(name="p")
        self.mac.publish()
        git(self.mac.blanc, "reset", "-q", "--hard", "HEAD~1")
        # Git dates have one-second precision. Make the rewritten commit
        # distinct even when both commits land in the same second.
        git(self.mac.blanc, "-c", "core.hooksPath=/dev/null", "cherry-pick", original,
            env={"GIT_COMMITTER_NAME": "Rewrite fixture"})
        rewritten = git(self.mac.blanc, "rev-parse", "HEAD")
        self.assertNotEqual(rewritten, original)
        report = attr.build_report(self.mac.ledger(), {"blanc": self.mac.blanc},
                                   attr.Filters(since=start, until=time.time() + 1))
        rows = report["sessions"][0]["repositories"]["blanc"]["main"]
        self.assertNotIn(rewritten, [row["sha"] for row in rows])  # never under a session
        self.assertEqual([(m["sha"], m["matches"]) for m in report["matched_by_patch_id"]],
                         [(rewritten, original)])
        self.assertEqual(report["unattributed"], [])
        self.assertIn("matched by patch-id, not attributed", attr.render_report(report))

    def test_report_includes_unpublished_local_records(self):
        self.mac.install()
        sha = self.mac.commit()
        local = [e.record for e in attr.journal(self.mac.blanc)]
        report = attr.build_report(attr.Ledger(attached=False), {"blanc": self.mac.blanc},
                                   attr.Filters(), local=local)
        row = report["sessions"][0]["repositories"]["blanc"]["main"][0]
        self.assertEqual((row["sha"], row["unpublished"]), (sha, True))

    def test_report_cli(self):
        self.two_host_window()
        location = self.mac.location()
        out = io.StringIO()
        with mock.patch.object(attribution_cli, "_location", return_value=location), \
                mock.patch.object(attribution_cli, "_host", return_value="mac"), redirect_stdout(out):
            arguments = cli.parser().parse_args(["attribution", "report", "--host", "wsl", "--json"])
            code = arguments.func(arguments)
        self.assertEqual(code, 0)
        self.assertEqual([s["host"] for s in json.loads(out.getvalue())["sessions"]], ["wsl"])


class PrivacyTest(Fixture):
    def planted(self, relative, text, message="ordinary change"):
        repo = self.mac.blanc
        base = git(repo, "rev-parse", "HEAD")
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        git(repo, "add", relative)
        git(repo, "commit", "-q", "-m", message)
        return attr.privacy_check(repo, base, host="mac", digests={self.mac.digest})

    def test_clean_change_passes(self):
        self.assertEqual(self.planted("docs/x.md", "Run `attribution report --host HOST`.\n"), [])

    def test_host_name_in_a_tracked_file_is_flagged(self):
        hits = self.planted("docs/x.md", "built on the mac master\n")
        self.assertEqual([h["rule"] for h in hits], ["host name"])

    def test_host_name_is_matched_in_any_case_as_a_word(self):
        hits = self.planted("NOTES.md", "Done by the Mac master. The MAC host did it.\n",
                            message="Done on the Mac master")
        self.assertEqual([h["rule"] for h in hits], ["host name", "host name"])
        self.assertEqual(self.planted("OTHER.md", "mac-host mac_master host=mac1 macOS\n"), [])

    def test_digest_session_field_and_message_are_flagged(self):
        hits = self.planted("creme/x.py", f'value = "{self.mac.digest}"\n')
        self.assertEqual([h["rule"] for h in hits], ["acquisition digest"])
        hits = self.planted("creme/y.py", 'lease_id = "' + "d" * 32 + '"\n')
        self.assertEqual([h["rule"] for h in hits], ["session field"])
        hits = self.planted("docs/z.md", "plain\n", message="Done on mac")
        self.assertEqual([h["rule"] for h in hits], ["host name"])

    def test_fixtures_may_use_synthetic_names_but_not_known_digests(self):
        self.assertEqual(self.planted("scripts/tests/t.py", 'HOST = "mac"\n'), [])
        hits = self.planted("scripts/tests/u.py", f'D = "{self.mac.digest}"\n')
        self.assertEqual([h["rule"] for h in hits], ["acquisition digest"])

    def test_doctor_fails_on_a_known_digest_in_a_public_tracked_file(self):
        self.mac.install()
        self.mac.commit()
        self.mac.commit(name="leak.txt", text=f"{self.mac.digest}\n")
        checks = {c.name: c for c in doctor.check_attribution({"blanc": self.mac.blanc}, None)}
        self.assertEqual(checks["attribution privacy"].status, doctor.STATUS_FAIL)
        self.assertIn("blanc:leak.txt", checks["attribution privacy"].detail)



class GuardTest(Fixture):
    """F1: publication goes only to the goal store's own, non-public origin."""

    def guard(self):
        return attr.require_private_remote(self.mac.plans, "origin", [self.mac.blanc])

    def test_every_public_spelling_is_refused(self):
        spellings = [
            "https://github.com/skbaek/creme", "https://github.com/skbaek/Creme.git",
            "https://github.com/Skbaek/creme", "https://github.com/skbaek/creme.git/",
            "ssh://git@github.com:22/skbaek/creme.git", "https://www.github.com/skbaek/creme",
            "ssh://git@ssh.github.com:443/skbaek/blanc.git", "git@github.com:skbaek/jaune.git",
            "https://GitHub.com/skbaek/creme", "https://github.com//skbaek/creme",
            str(self.blanc_origin), str(self.blanc_origin) + "/", str(self.mac.blanc),
            str(self.mac.blanc / ".git"), f"file://{self.blanc_origin}",
        ]
        for url in spellings:
            git(self.mac.plans, "remote", "set-url", "origin", url)
            with self.assertRaisesRegex(attr.AttributionError, "public repository", msg=url):
                self.guard()

    def test_insteadof_alias_of_a_public_remote_is_refused(self):
        git(self.mac.blanc, "config", "url.https://github.com/.insteadOf", "gh:")
        git(self.mac.blanc, "remote", "set-url", "origin", "gh:someone/otherpublic")
        git(self.mac.plans, "remote", "set-url", "origin", "https://github.com/someone/otherpublic.git")
        with self.assertRaisesRegex(attr.AttributionError, "public repository"):
            self.guard()

    def test_a_second_push_url_never_leaks(self):
        self.mac.install()
        self.mac.commit()
        git(self.mac.plans, "config", "--add", "remote.origin.pushurl", str(self.plans_origin))
        git(self.mac.plans, "config", "--add", "remote.origin.pushurl", str(self.blanc_origin))
        # Push URLs are never used: publication pushes to the one validated URL.
        self.assertEqual(self.mac.publish()["published"], 1)
        self.assertEqual(git(self.blanc_origin, "branch", "--list", attr.DEFAULT_BRANCH), "")
        self.assertTrue([p for p in self.ledger_paths() if p.startswith("hosts/mac/batches/")])

    def test_push_names_origin_never_the_branch_upstream(self):
        self.mac.install()
        self.mac.publish()
        self.mac.commit()
        git(self.mac.plans, "remote", "add", "mirror", str(self.blanc_origin))
        git(self.mac.plans, "push", "-q", "mirror", attr.DEFAULT_BRANCH)
        git(self.mac.plans, "fetch", "-q", "mirror")
        git(self.mac.plans, "config", "branch.attribution.remote", "mirror")
        before = git(self.blanc_origin, "ls-tree", "-r", "--name-only", attr.DEFAULT_BRANCH)
        self.assertEqual(self.mac.publish()["published"], 1)
        self.assertEqual(git(self.blanc_origin, "ls-tree", "-r", "--name-only", attr.DEFAULT_BRANCH),
                         before)
        self.assertTrue([p for p in self.ledger_paths() if p.startswith("hosts/mac/batches/")])


class PublicationFailureTest(Fixture):
    """F2, F7, F10: the journal stays the source of truth until the origin holds it."""

    def test_push_timeout_keeps_the_journal_and_the_next_publish_sends_it(self):
        self.mac.install()
        self.mac.publish()
        self.mac.commit()
        with mock.patch.object(channels, "push",
                               side_effect=attr.shared.SharedStackError("git push failed: timed out")):
            with self.assertRaisesRegex(attr.AttributionError, "nothing sent"):
                self.mac.publish()
        worktree = self.mac.plans / attr.LEDGER_DIR
        self.assertEqual(git(worktree, "rev-list", "--count", "origin/attribution..HEAD"), "0")
        self.assertEqual([e.published for e in attr.journal(self.mac.blanc)], [False])
        self.assertEqual(self.mac.publish()["published"], 1)
        self.assertEqual([e.published for e in attr.journal(self.mac.blanc)], [True])
        self.assertEqual(len(self.wsl.ledger().records), 1)

    def test_kill_between_commit_and_push_is_rebuilt_not_trusted(self):
        self.mac.install()
        self.mac.publish()
        self.mac.commit()
        with mock.patch.object(channels, "push", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.mac.publish()
        worktree = self.mac.plans / attr.LEDGER_DIR
        self.assertEqual(git(worktree, "rev-list", "--count", "origin/attribution..HEAD"), "1")
        self.assertEqual([e.published for e in attr.journal(self.mac.blanc)], [False])
        self.assertEqual(self.mac.publish()["published"], 1)
        self.assertEqual([e.published for e in attr.journal(self.mac.blanc)], [True])
        self.assertEqual(len(self.wsl.ledger().records), 1)
        self.assertEqual(len([p for p in self.ledger_paths() if "/batches/" in p]), 1)

    def test_offline_end_record_is_published_later(self):
        self.mac.install()
        events = [{"kind": "master", "timestamp": attr.iso(time.time() - 60).replace("Z", ".000000Z"),
                   "actor": {"client": "claude", "acquisition_digest": self.mac.digest}}]
        attr.on_master_start(self.mac.location(), client="claude", model="opus", effort="high",
                             writer=self.mac.writer(), host="mac")
        git(self.mac.blanc, "-c", "core.hooksPath=/dev/null", "commit", "-q", "--allow-empty",
            "-m", "missed")
        real = git(self.mac.plans, "remote", "get-url", "origin")
        git(self.mac.plans, "remote", "set-url", "origin", str(self.base / "offline.git"))
        ended = attr.on_master_end(self.mac.location(), digest=self.mac.digest, client="claude",
                                   model="opus", effort="high", events=events,
                                   writer=self.mac.writer(), host="mac")
        self.assertEqual((ended["status"], ended["unattributed"]), ("error", 1))
        git(self.mac.plans, "remote", "set-url", "origin", real)
        result = self.mac.publish()
        self.assertEqual(len(result["session_files"]), 1)
        session = self.mac.ledger().sessions[("mac", self.mac.digest)]
        self.assertTrue(session["ended_at"])
        self.assertEqual(len(session["census"]), 1)

    def test_foreign_host_record_is_quarantined_not_fatal(self):
        self.mac.install()
        self.mac.commit()
        entry = attr.journal(self.mac.blanc)[0]
        forged = dict(entry.record, id=entry.record["id"] + "x", host="elsewhere")
        (entry.path.parent / f"{forged['id']}.json").write_text(json.dumps(forged))
        result = self.mac.publish()
        self.assertEqual((result["published"], [q["host"] for q in result["quarantined"]]),
                         (1, ["elsewhere"]))
        self.assertTrue((attr.journal_root(self.mac.blanc) / "quarantine" / f"{forged['id']}.json").exists())
        self.assertEqual(self.mac.publish()["quarantined"], [])

    def test_stray_untracked_files_do_not_block(self):
        self.mac.install()
        self.mac.publish()
        self.mac.commit()
        worktree = self.mac.plans / attr.LEDGER_DIR
        (worktree / ".DS_Store").write_bytes(b"x")
        (worktree / "notes.txt").write_text("scratch\n")
        (worktree / "hosts" / "mac").mkdir(parents=True, exist_ok=True)
        (worktree / "hosts" / "mac" / ".DS_Store").write_bytes(b"x")
        result = attr.merge_check(self.mac.repos, "blanc", "HEAD~1..HEAD",
                                  publish_first=self.mac.publish)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["published"]["published"], 1)
        (worktree / "hosts" / "mac" / "forged.json").write_text("{}")
        with self.assertRaisesRegex(attr.AttributionError, "untracked"):
            self.mac.publish()


class AdoptTest(Fixture):
    """F3: adopt only this host's census, without claiming the creation session."""

    def test_adopt_records_unknown_session_with_adopter_and_goal(self):
        self.mac.install()
        self.wsl.install()
        self.mac.set_lease("c" * 32)
        worktree = self.mac.blanc / ".worktrees" / "some-goal-v1"
        git(self.mac.blanc, "worktree", "add", "-q", "-b", "g", str(worktree))
        git(worktree, "-c", "core.hooksPath=/dev/null", "commit", "-q", "--allow-empty",
            "-m", "missed in A")
        missed = git(worktree, "rev-parse", "HEAD")
        self.mac.set_lease(LEASES["mac"])
        adopter = hook.lease_identity(str(self.mac.state), time.time())
        attr.adopt(self.mac.repos, "blanc", [missed], reason="hooks were off", host="mac",
                   adopter=adopter)
        record = [r for r in self.mac.records() if r["sha"] == missed][0]
        self.assertEqual(record["acquisition_digest"], "unknown")
        self.assertEqual(record["adopted_by"]["acquisition_digest"], self.mac.digest)
        self.assertEqual(record["goal"], "some-goal-v1")
        self.assertTrue(record["created_at"])
        theirs = self.wsl.commit(name="w")
        git(self.wsl.blanc, "push", "-q", "origin", "main")
        git(self.mac.blanc, "pull", "-q", "--ff-only", "origin", "main")
        with self.assertRaisesRegex(attr.AttributionError, "not in this host's census"):
            attr.adopt(self.mac.repos, "blanc", [theirs], reason="oops", host="mac", adopter=adopter)
        with self.assertRaisesRegex(attr.AttributionError, "not in this host's census"):
            attr.adopt(self.mac.repos, "blanc", [missed], reason="again", host="mac", adopter=adopter)
        self.mac.publish()
        text = attr.render_report(attr.build_report(self.mac.ledger(), self.mac.repos,
                                                    attr.Filters(), host="mac"))
        self.assertIn("[adopted: hooks were off]", text)
        self.assertIn("session unknown", text)


class CensusPersistenceTest(Fixture):
    """F5, F6, F10, F12: the census and merge check judge what they must, no more."""

    def test_census_survives_worktree_and_branch_removal(self):
        self.mac.install()
        start = time.time() - 1
        worktree = self.mac.blanc / ".worktrees" / "goal-x"
        git(self.mac.blanc, "worktree", "add", "-q", "-b", "gx", str(worktree))
        (worktree / "a").write_text("a\n")
        git(worktree, "add", "a")
        git(worktree, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "wt commit")
        self.mac.commit(name="m")
        git(worktree, "-c", "core.hooksPath=/dev/null", "rebase", "-q", "main")
        tip = git(worktree, "rev-parse", "HEAD")
        self.assertIn(tip, [r["sha"] for r in attr.census(self.mac.repos, start, time.time() + 1)])
        git(self.mac.blanc, "merge", "-q", "--ff-only", "gx")
        git(self.mac.blanc, "worktree", "remove", str(worktree))
        git(self.mac.blanc, "branch", "-d", "gx")
        rows = attr.census(self.mac.repos, start, time.time() + 1)
        self.assertIn(tip, [r["sha"] for r in rows])
        self.assertEqual([r["goal"] for r in rows if r["sha"] == tip], ["goal-x"])
        result = attr.merge_check(self.mac.repos, "blanc", "origin/main..main",
                                  publish_first=self.mac.publish)
        self.assertEqual(result["status"], "refused")
        self.assertIn(tip, [r["sha"] for r in result["unrecorded"]])

    def test_missing_marker_with_hooks_refuses_and_reinstall_keeps_adoption_time(self):
        self.mac.install()
        adopted_at = attr.adoption(self.mac.blanc)[1]
        (attr.journal_root(self.mac.blanc) / hook.INSTALLED).unlink()
        git(self.mac.blanc, "-c", "core.hooksPath=/dev/null", "commit", "-q", "--allow-empty",
            "-m", "missed")
        result = attr.merge_check(self.mac.repos, "blanc", "HEAD~1..HEAD",
                                  publish_first=self.mac.publish)
        self.assertEqual(result["status"], "refused")
        self.assertIn("attribution install", result["remedy"])
        self.assertEqual(attr.install_status(self.mac.blanc)["status"], "unmarked")
        checks = {c.name: c for c in doctor.check_attribution(self.mac.repos, None)}
        self.assertEqual(checks["attribution hooks"].status, doctor.STATUS_FAIL)
        time.sleep(1.1)
        self.mac.install()
        self.assertLessEqual(attr.adoption(self.mac.blanc)[1], adopted_at + 1)
        result = attr.merge_check(self.mac.repos, "blanc", "HEAD~1..HEAD",
                                  publish_first=self.mac.publish)
        self.assertEqual(result["status"], "refused")

    def test_fast_forward_and_abort_entries_are_not_creations(self):
        self.mac.install()
        self.wsl.install()
        theirs = self.wsl.commit(name="w")
        git(self.wsl.blanc, "push", "-q", "origin", "main")
        git(self.mac.blanc, "fetch", "-q", "origin")
        git(self.mac.blanc, "cherry-pick", "--ff", "origin/main")
        self.assertEqual(git(self.mac.blanc, "rev-parse", "HEAD"), theirs)
        patch = self.base / "bad.patch"
        patch.write_text(
            "From 0000000000000000000000000000000000000000 Mon Sep 17 00:00:00 2001\n"
            "From: a <a@b>\nDate: Mon, 1 Jan 2026 00:00:00 +0000\nSubject: [PATCH] x\n\n---\n"
            " nofile | 1 +\n\ndiff --git a/nofile b/nofile\nindex 1111111..2222222 100644\n"
            "--- a/nofile\n+++ b/nofile\n@@ -1 +1 @@\n-a\n+b\n")
        subprocess.run(["git", "-C", str(self.mac.blanc), "am", str(patch)], capture_output=True)
        subprocess.run(["git", "-C", str(self.mac.blanc), "am", "--abort"], capture_output=True)
        self.assertEqual(attr.census(self.mac.repos, 0, time.time() + 1), [])
        result = attr.merge_check(self.mac.repos, "blanc", "HEAD~1..HEAD",
                                  publish_first=self.mac.publish)
        self.assertEqual(result["status"], "OK")

    def test_partial_install_judges_only_adopted_repositories(self):
        attr.install({"goal-store": self.mac.plans}, python=sys.executable, script=attr.SCRIPT,
                     state_dir=self.mac.state, profile=self.mac.profile)
        start = time.time() - 1
        self.mac.commit()  # blanc never adopted here
        self.assertEqual(attr.census(self.mac.repos, start, time.time() + 1), [])
        report = attr.build_report(attr.Ledger(attached=False), self.mac.repos,
                                   attr.Filters(since=start, until=time.time() + 1), host="mac")
        self.assertEqual(report["unattributed"], [])

    def test_worktree_scoped_hooks_path_is_reported(self):
        self.mac.install()
        git(self.mac.blanc, "config", "extensions.worktreeConfig", "true")
        worktree = self.mac.blanc / ".worktrees" / "access-control"
        git(self.mac.blanc, "worktree", "add", "-q", "-b", "ac", str(worktree))
        git(worktree, "config", "--worktree", "core.hooksPath", "/dev/null")
        checks = {c.name: c for c in doctor.check_attribution(self.mac.repos, None)}
        self.assertEqual(checks["attribution worktree hooks"].status, doctor.STATUS_WARN)
        self.assertIn("access-control", checks["attribution worktree hooks"].detail)

    def test_retire_adoption_is_deliberate_and_recorded(self):
        self.mac.install()
        attr.uninstall(self.mac.repos)
        self.assertEqual(attr.adoption(self.mac.blanc)[0], "adopted")
        result = attr.uninstall(self.mac.repos, retire_adoption=True)
        self.assertIn("retired_at", result["retired"]["blanc"])
        self.assertEqual(attr.adoption(self.mac.blanc)[0], "not-adopted")
        self.assertTrue(list(attr.journal_root(self.mac.blanc).glob("retired-*.json")))


class CaptureEdgeTest(Fixture):
    """F8, F12: capture edge cases."""

    def test_post_rewrite_continues_past_a_bad_target(self):
        self.mac.install()
        good = self.mac.commit()
        attr.uninstall(self.mac.repos)
        before = {r["id"] for r in self.mac.records()}
        stdin = f"{'0' * 40} {'1' * 40}\n{'0' * 40} {good}\n".encode()
        self.assertEqual(hook.run(["--repo", "blanc", "--state-dir", str(self.mac.state),
                                   "post-rewrite", "rebase"], stdin, cwd=str(self.mac.blanc)), 0)
        added = [r for r in self.mac.records() if r["id"] not in before]
        self.assertEqual([(r["sha"], r["hook"]) for r in added], [(good, "post-rewrite")])
        self.assertIn(good[:12], added[0]["id"])  # ids carry the resolved SHA prefix

    def test_detached_primary_checkout_is_labelled_detached(self):
        self.mac.install()
        git(self.mac.blanc, "checkout", "-q", "--detach")
        self.mac.commit(name="d")
        self.assertEqual([r["goal"] for r in self.mac.records()], ["detached"])

    def test_stale_lease_reports_under_none(self):
        self.mac.install()
        self.mac.set_lease("d" * 32, renewed_at=time.time() - 86400)
        self.mac.commit()
        self.mac.set_lease(LEASES["mac"])
        self.mac.publish()
        report = attr.build_report(self.mac.ledger(), self.mac.repos, attr.Filters(), host="mac")
        self.assertEqual([s["acquisition_digest"] for s in report["sessions"]], ["none"])
        self.assertIn("[stale lease]", attr.render_report(report))


class ReportShapeTest(Fixture):
    """F4, F11, F13: what the report attributes, folds and warns about."""

    def test_patch_id_never_matches_across_repositories(self):
        self.mac.install()
        start = time.time() - 1
        (self.mac.plans / "toolchain").write_text("v4.35.0\n")
        git(self.mac.plans, "add", "toolchain")
        git(self.mac.plans, "commit", "-q", "-m", "bump in repo A")
        (self.wsl.blanc / "toolchain").write_text("v4.35.0\n")
        git(self.wsl.blanc, "add", "toolchain")
        git(self.wsl.blanc, "commit", "-q", "-m", "bump in repo B")
        theirs = git(self.wsl.blanc, "rev-parse", "HEAD")
        git(self.wsl.blanc, "push", "-q", "origin", "main")
        git(self.mac.blanc, "pull", "-q", "--ff-only", "origin", "main")
        self.mac.publish()
        report = attr.build_report(self.mac.ledger(), self.mac.repos,
                                   attr.Filters(since=start, until=time.time() + 1), host="mac")
        self.assertEqual([u["sha"] for u in report["unattributed"]], [theirs])
        self.assertEqual(report["matched_by_patch_id"], [])

    def test_amended_commit_shows_final_sha_with_superseded_folded(self):
        self.mac.install()
        old = self.mac.commit(name="f")
        git(self.mac.blanc, "commit", "-q", "--amend", "-m", "amended")
        new = git(self.mac.blanc, "rev-parse", "HEAD")
        self.mac.publish()
        report = attr.build_report(self.mac.ledger(), self.mac.repos, attr.Filters(), host="mac")
        rows = report["sessions"][0]["repositories"]["blanc"]["main"]
        self.assertEqual([(r["sha"], r.get("supersedes")) for r in rows], [(new, [old])])
        self.assertIn(f"[supersedes {old[:12]}]", attr.render_report(report))

    def test_report_warns_without_ledger_or_host(self):
        report = attr.build_report(attr.Ledger(attached=False), self.mac.repos, attr.Filters())
        self.assertEqual(len(report["warnings"]), 2)
        self.assertIn("warning: no ledger", attr.render_report(report))



class LedgerWorktreeConfigTest(Fixture):
    """R2-1: the target is resolved where the push runs, the ledger worktree."""

    def opened(self):
        self.mac.install()
        self.mac.publish()
        self.mac.commit()
        return self.mac.plans / attr.LEDGER_DIR

    def public_ledger(self):
        result = subprocess.run(["git", "-C", str(self.blanc_origin), "ls-tree", "-r", "--name-only",
                                 attr.DEFAULT_BRANCH], capture_output=True, text=True)
        return result.stdout.split() if result.returncode == 0 else []

    def test_worktree_scoped_pushurl_never_redirects_the_push(self):
        worktree = self.opened()
        git(self.mac.plans, "config", "extensions.worktreeConfig", "true")
        git(worktree, "config", "--worktree", "remote.origin.pushurl", str(self.blanc_origin))
        try:
            result = self.mac.publish()
        finally:  # the leak check holds whether or not publication succeeded
            self.assertEqual(self.public_ledger(), [])
        self.assertEqual(result["published"], 1)
        self.assertEqual(len([p for p in self.ledger_paths() if "/batches/" in p]), 1)

    def test_onbranch_include_pushurl_never_redirects_the_push(self):
        self.opened()
        include = self.base / "extra.cfg"
        include.write_text(f'[remote "origin"]\n\tpushurl = {self.blanc_origin}\n')
        git(self.mac.plans, "config", "includeIf.onbranch:attribution.path", str(include))
        try:
            result = self.mac.publish()
        finally:
            self.assertEqual(self.public_ledger(), [])
        self.assertEqual(result["published"], 1)

    def test_onbranch_include_rewrite_or_url_is_refused(self):
        self.opened()
        include = self.base / "rewrite.cfg"
        include.write_text(f'[url "{self.blanc_origin}"]\n\tpushInsteadOf = {self.plans_origin}\n')
        git(self.mac.plans, "config", "includeIf.onbranch:attribution.path", str(include))
        with self.assertRaisesRegex(attr.AttributionError, "redirect"):
            self.mac.publish()
        include.write_text(f'[remote "origin"]\n\turl = {self.blanc_origin}\n')
        with self.assertRaisesRegex(attr.AttributionError, "exactly one URL|public|differently"):
            self.mac.publish()
        self.assertEqual(self.public_ledger(), [])
        self.assertEqual([e.published for e in attr.journal(self.mac.blanc)], [False])

    def test_root_rewrite_and_remote_named_after_the_url_are_refused(self):
        self.mac.install()
        self.mac.commit()
        git(self.mac.plans, "config", f"url.{self.blanc_origin}.pushInsteadOf", str(self.plans_origin))
        with self.assertRaisesRegex(attr.AttributionError, "redirect"):
            self.mac.publish()
        git(self.mac.plans, "config", "--unset", f"url.{self.blanc_origin}.pushInsteadOf")
        git(self.mac.plans, "config", f"remote.{self.plans_origin}.pushurl", str(self.blanc_origin))
        with self.assertRaisesRegex(attr.AttributionError, "named after"):
            self.mac.publish()
        self.assertEqual(self.public_ledger(), [])

    def test_rewrite_rule_with_a_spaced_base_is_refused(self):
        # R3-1: a base containing a space must be read as Git reads it.
        self.opened()
        spaced = self.base / "pub lic.git"
        git(self.mac.plans, "config", f"url.{spaced}.pushInsteadOf", str(self.plans_origin))
        with self.assertRaisesRegex(attr.AttributionError, "redirect"):
            self.mac.publish()
        self.assertEqual(self.public_ledger(), [])

    def test_a_tied_rewrite_rule_refuses_whichever_git_would_pick(self):
        # R3-1: an identity rule tied with a redirecting one never wins the check.
        # The redirecting base appears first, the identity rule follows, and the
        # redirect comes back in a second section, so a longest-prefix emulation
        # that takes the first tie in file order sees only the identity.
        self.opened()
        config = Path(git(self.mac.plans, "rev-parse", "--git-path", "config").strip())
        if not config.is_absolute():
            config = self.mac.plans / config
        with config.open("a", encoding="utf-8") as stream:
            stream.write(f'[url "{self.blanc_origin}"]\n\tpushInsteadOf = unused-prefix:\n'
                         f'[url "{self.plans_origin}"]\n\tpushInsteadOf = {self.plans_origin}\n'
                         f'[url "{self.blanc_origin}"]\n\tpushInsteadOf = {self.plans_origin}\n')
        with self.assertRaisesRegex(attr.AttributionError, "redirect"):
            self.mac.publish()
        self.assertEqual(self.public_ledger(), [])


class CanonicalFormsTest(Fixture):
    """R2-2: more spellings and local forms of a public repository."""

    def guard(self):
        return attr.require_private_remote(self.mac.plans, "origin", [self.mac.blanc])

    def test_more_network_spellings_are_refused(self):
        for url in ["https://github.com./skbaek/creme.git", "git@github.com.:skbaek/creme.git",
                    "https://github.com/skbaek/%63reme.git", "https://github.com/skbaek/creme%2Egit",
                    "https://github.com/skbaek/x/../creme.git", "https://github.com/skbaek/./creme.git",
                    "HTTPS://GITHUB.COM/SKBAEK/CREME.GIT", "https://github.com/skbaek/creme.GIT",
                    "https://user:tok@github.com/skbaek/creme.git", "git+ssh://git@github.com/skbaek/creme.git",
                    "github.com:skbaek/creme.git", "https://github.com/skbaek/creme.git#main",
                    "https://github.com/skbaek/creme.git.git", "ssh://git@[github.com]:22/skbaek/creme.git"]:
            git(self.mac.plans, "remote", "set-url", "origin", url)
            with self.assertRaisesRegex(attr.AttributionError, "public repository", msg=url):
                self.guard()

    def test_local_forms_of_a_public_repository_are_refused(self):
        worktree = self.mac.blanc / ".worktrees" / "g"
        git(self.mac.blanc, "worktree", "add", "-q", "-b", "g", str(worktree))
        link = self.base / "link.git"
        link.symlink_to(self.blanc_origin)
        for url in [str(self.blanc_origin)[:-len(".git")], str(worktree),
                    str(self.mac.blanc / ".git" / "worktrees" / "g") + "/../..", str(link),
                    "file://localhost" + str(self.blanc_origin), str(self.base / "x" / ".." / "blanc.git")]:
            git(self.mac.plans, "remote", "set-url", "origin", url)
            with self.assertRaisesRegex(attr.AttributionError, "public repository", msg=url):
                self.guard()
        git(self.mac.plans, "remote", "set-url", "origin", str(self.plans_origin))
        self.assertTrue(self.guard())


class SessionFoldTest(Fixture):
    """R2-4: a later session's rebase does not take over an earlier session's commits."""

    def test_rebase_in_a_later_session_keeps_the_earlier_rows(self):
        self.mac.install()
        self.mac.set_lease("c" * 32)
        first = self.mac.digest
        git(self.mac.blanc, "checkout", "-q", "-b", "cand")
        old = self.mac.commit(name="x")
        git(self.mac.blanc, "checkout", "-q", "main")
        self.mac.commit(name="m")
        self.mac.set_lease(LEASES["mac"])
        git(self.mac.blanc, "checkout", "-q", "cand")
        git(self.mac.blanc, "rebase", "-q", "main")
        new = git(self.mac.blanc, "rev-parse", "HEAD")
        self.mac.publish()
        report = attr.build_report(self.mac.ledger(), self.mac.repos, attr.Filters(), host="mac")
        rows = {s["acquisition_digest"]: [r for g in s["repositories"].values() for rs in g.values()
                                          for r in rs] for s in report["sessions"]}
        self.assertIn(first, rows)  # the earlier session keeps its own rows
        earlier = [r for r in rows[first] if r["sha"] == old]
        self.assertEqual(len(earlier), 1)
        self.assertEqual(earlier[0]["superseded_by"], new)
        later = [r for r in rows[self.mac.digest] if r["sha"] == new]
        self.assertNotIn("supersedes", later[0])
        self.assertIn(f"[superseded by {new[:12]}]", attr.render_report(report))


class LedgerScaleTest(unittest.TestCase):
    """R2-5: a ledger load costs a fixed number of Git processes, not one per file."""

    def test_six_hundred_files_load_with_two_git_processes(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, GIT_ENV):
            repo = Path(temp) / "ledger"
            subprocess.run(["git", "init", "-q", "-b", attr.DEFAULT_BRANCH, str(repo)], check=True)
            batches = repo / "hosts" / "mac" / "batches"
            batches.mkdir(parents=True)
            for index in range(600):
                record = {"schema": 1, "id": f"r{index}", "sha": f"{index:040x}", "repository": "blanc",
                          "acquisition_digest": "none", "host": "mac"}
                (batches / f"{index:04d}.json").write_text(json.dumps(
                    {"schema": 1, "host": "mac", "records": [record]}))
            git(repo, "add", ".")
            git(repo, "commit", "-q", "-m", "ledger")
            real = attr._git
            calls = []

            def counting(*args, **kwargs):
                calls.append(args[1:3])
                return real(*args, **kwargs)

            with mock.patch.object(attr, "_git", side_effect=counting):
                ledger = attr.load_ledger(repo)
            self.assertEqual(len(ledger.records), 600)
            self.assertEqual(ledger.problems, [])
            self.assertLessEqual(len(calls), 2, calls[:5])


if __name__ == "__main__":
    unittest.main()
