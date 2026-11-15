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
        self.assertEqual((record["acquisition_digest"], record["lease_state"]),
                         (self.mac.digest, "expired"))

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
        with mock.patch.object(channels, "sync", lambda worktree, spec, rebase_onto=None: None):
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
        self.assertIn(f"hosts/mac/sessions/{self.mac.digest}/start-", started["session_file"])
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
                             host="mac", identity=hook.lease_identity(str(self.mac.state), time.time()))
        self.assertEqual(adopted["adopted"][0]["sha"], missed)
        result = attr.merge_check(self.mac.repos, "blanc", "origin/main~1..candidate",
                                  publish_first=self.publish_first)
        self.assertEqual(result["status"], "OK")
        record = [r for r in self.mac.ledger().records if r["sha"] == missed][0]
        self.assertEqual((record["source"], record["reason"], record["hook"]),
                         ("census", "made with hooks off", "adopt"))
        self.assertEqual(record["acquisition_digest"], self.mac.digest)

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
        git(self.mac.blanc, "-c", "core.hooksPath=/dev/null", "cherry-pick", original)
        rewritten = git(self.mac.blanc, "rev-parse", "HEAD")
        self.assertNotEqual(rewritten, original)
        report = attr.build_report(self.mac.ledger(), {"blanc": self.mac.blanc},
                                   attr.Filters(since=start, until=time.time() + 1))
        rows = report["sessions"][0]["repositories"]["blanc"]["main"]
        matched = [row for row in rows if row["sha"] == rewritten]
        self.assertEqual(matched[0]["matched"], "patch-id")
        self.assertEqual(report["unattributed"], [])

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
        checks = doctor.check_attribution({"blanc": self.mac.blanc}, None)
        self.assertEqual(checks[1].status, doctor.STATUS_FAIL)
        self.assertIn("blanc:leak.txt", checks[1].detail)


if __name__ == "__main__":
    unittest.main()
