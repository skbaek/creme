from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from creme import cli, master_operations, master_reconcile, master_runtime


class MasterReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        self.lease = {
            "schema_version": 3,
            "lease": {"client": "codex", "lease_id": "1" * 32},
        }

    def git(self, root: Path, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            text=True,
        )
        return completed.stdout.strip()

    def make_repository(self, name: str) -> tuple[Path, Path]:
        remote = self.workspace / f"{name}.git"
        remote.mkdir()
        self.git(remote, "init", "--bare", "-q")
        repository = self.workspace / name
        repository.mkdir()
        self.git(repository, "init", "-q")
        self.git(repository, "config", "user.name", "Synthetic User")
        self.git(repository, "config", "user.email", "synthetic@example.invalid")
        self.git(repository, "checkout", "-q", "-b", "main")
        (repository / ".gitignore").write_text("/.worktrees/\n", encoding="utf-8")
        (repository / "tracked.txt").write_text("initial\n", encoding="utf-8")
        self.git(repository, "add", ".gitignore", "tracked.txt")
        self.git(repository, "commit", "-q", "-m", "initial")
        self.git(repository, "remote", "add", "origin", str(remote))
        self.git(repository, "push", "-q", "-u", "origin", "main")
        return repository, remote

    def clone_repository(self, remote: Path, name: str) -> Path:
        clone = self.workspace / name
        subprocess.run(
            ["git", "clone", "-q", "-b", "main", str(remote), str(clone)],
            check=True,
        )
        self.git(clone, "config", "user.name", "Synthetic Peer")
        self.git(clone, "config", "user.email", "peer@example.invalid")
        return clone

    def new_record(self, name: str) -> tuple[Path, master_runtime.RecordWriter]:
        root = self.workspace / f"record-{name}"
        master_runtime.initialize_empty_record(root)
        writer = master_runtime.RecordWriter(
            root,
            renew=lambda: (True, "synthetic holder"),
            lease_snapshot=lambda: self.lease,
        )
        return root, writer

    def add_goal(
        self,
        writer: master_runtime.RecordWriter,
        repository: Path,
        *,
        goal_id: str,
        worktree: str = ".",
        branch: str = "main",
        checkpoint: str | None = None,
    ) -> None:
        writer.append(
            "goal",
            {
                "goal_id": goal_id,
                "status": "active",
                "worktree": worktree,
                "branch": branch,
                "checkpoint": checkpoint or self.git(repository, "rev-parse", "HEAD"),
                "next_unit": "synthetic-next",
            },
        )

    @staticmethod
    def tree_digest(root: Path, *, exclude_git_directory: bool = False) -> str:
        digest = hashlib.sha256()
        if not root.exists():
            return "missing"
        paths = [root, *sorted(root.rglob("*"))]
        for path in paths:
            relative = path.relative_to(root)
            if exclude_git_directory and relative.parts[:1] == (".git",):
                continue
            digest.update(str(relative).encode("utf-8"))
            info = path.lstat()
            digest.update(str(info.st_mode).encode("ascii"))
            if path.is_symlink():
                digest.update(os.readlink(path).encode("utf-8"))
            elif path.is_file():
                digest.update(path.read_bytes())
        return digest.hexdigest()

    def repository_snapshot(self, repository: Path) -> dict[str, str]:
        common_value = self.git(repository, "rev-parse", "--git-common-dir")
        common = Path(common_value)
        if not common.is_absolute():
            common = (repository / common).resolve()
        git_dir_value = self.git(repository, "rev-parse", "--git-dir")
        git_dir = Path(git_dir_value)
        if not git_dir.is_absolute():
            git_dir = (repository / git_dir).resolve()
        return {
            "objects": self.tree_digest(common / "objects"),
            "refs": self.tree_digest(common / "refs")
            + self.tree_digest(common / "packed-refs"),
            "index": self.tree_digest(git_dir / "index"),
            "worktree-metadata": self.tree_digest(common / "worktrees"),
            "worktree": self.tree_digest(repository, exclude_git_directory=True),
        }

    def reconcile_unchanged(
        self,
        record: Path,
        repository: Path,
        *,
        runner=master_reconcile.run_git,
    ) -> master_reconcile.ReconciliationResult:
        before = self.repository_snapshot(repository)
        result = master_reconcile.reconcile_record(
            record,
            {"synthetic": repository},
            runner=runner,
        )
        self.assertEqual(self.repository_snapshot(repository), before)
        return result

    @staticmethod
    def kinds(result: master_reconcile.ReconciliationResult) -> list[str]:
        return [row["kind"] for row in result.discrepancies]

    def test_clean_repository_and_digest_reconciliation_are_exact_and_read_only(self):
        repository, _ = self.make_repository("clean")
        record, writer = self.new_record("clean")
        self.add_goal(writer, repository, goal_id="clean-goal")
        record_before = self.tree_digest(record)

        result = self.reconcile_unchanged(record, repository)
        self.assertEqual(result.discrepancies, ())
        self.assertEqual(
            result.repositories[0],
            {
                "repository": "synthetic",
                "status": "OK",
                "head": self.git(repository, "rev-parse", "HEAD"),
                "branch": "main",
                "upstream": "refs/remotes/origin/main",
                "ahead": 0,
                "behind": 0,
                "worktree_count": 1,
                "recorded_worktrees": 1,
                "extra_worktrees": 0,
                "detached_worktrees": 0,
                "tracked_dirty_worktrees": 0,
                "untracked_worktrees": 0,
                "inaccessible_worktrees": 0,
            },
        )
        digest = master_operations.digest_record(
            record,
            live_reconciliation=result,
            lease_snapshot=lambda: self.lease,
            lease_status=lambda: "master: codex (live)\n",
        )
        self.assertEqual(digest["live_reconciliation"]["discrepancies"]["items"], [])
        self.assertEqual(
            master_operations.digest_record(
                record,
                discrepancies_limit=0,
                live_reconciliation=result,
                lease_snapshot=lambda: self.lease,
                lease_status=lambda: "master: codex (live)\n",
            )["live_reconciliation"]["discrepancies"],
            {"items": [], "limit": 0, "omitted": 0, "continuation_key": None},
        )
        self.assertEqual(self.tree_digest(record), record_before)
        self.assertEqual(
            master_reconcile.reconcile_record(record, {"synthetic": repository}),
            result,
        )

    def test_tracked_and_untracked_classifications_do_not_expose_names_or_contents(self):
        for classification in ("tracked-dirt", "untracked-data"):
            with self.subTest(classification=classification):
                repository, _ = self.make_repository(classification)
                record, writer = self.new_record(classification)
                self.add_goal(writer, repository, goal_id=f"{classification}-goal")
                canary = f"PRIVATE-{classification}-CANARY"
                if classification == "tracked-dirt":
                    (repository / "tracked.txt").write_text(canary, encoding="utf-8")
                else:
                    (repository / "private-name.txt").write_text(canary, encoding="utf-8")
                result = self.reconcile_unchanged(record, repository)
                self.assertIn(classification, self.kinds(result))
                rendered = json.dumps(result.to_dict(), sort_keys=True)
                self.assertNotIn(canary, rendered)
                self.assertNotIn("private-name.txt", rendered)

    def test_ahead_behind_and_diverged_are_distinct_upstream_drift_facts(self):
        for state in ("ahead", "behind", "diverged"):
            with self.subTest(state=state):
                repository, remote = self.make_repository(state)
                if state in {"behind", "diverged"}:
                    peer = self.clone_repository(remote, f"{state}-peer")
                    (peer / "peer.txt").write_text("peer\n", encoding="utf-8")
                    self.git(peer, "add", "peer.txt")
                    self.git(peer, "commit", "-q", "-m", "peer")
                    self.git(peer, "push", "-q", "origin", "main")
                    self.git(repository, "fetch", "-q", "origin")
                if state in {"ahead", "diverged"}:
                    (repository / "local.txt").write_text("local\n", encoding="utf-8")
                    self.git(repository, "add", "local.txt")
                    self.git(repository, "commit", "-q", "-m", "local")
                record, writer = self.new_record(state)
                self.add_goal(writer, repository, goal_id=f"{state}-goal")
                result = self.reconcile_unchanged(record, repository)
                rows = [
                    row for row in result.discrepancies
                    if row["kind"] == "upstream-drift"
                ]
                self.assertEqual(len(rows), 1)
                self.assertTrue(rows[0]["observed"].startswith(state + ":"))

    def test_detached_head_and_head_drift_are_distinguished(self):
        repository, _ = self.make_repository("detached")
        initial = self.git(repository, "rev-parse", "HEAD")
        (repository / "second.txt").write_text("second\n", encoding="utf-8")
        self.git(repository, "add", "second.txt")
        self.git(repository, "commit", "-q", "-m", "second")
        self.git(repository, "checkout", "-q", "--detach")
        record, writer = self.new_record("detached")
        self.add_goal(
            writer,
            repository,
            goal_id="detached-goal",
            checkpoint=initial,
        )
        result = self.reconcile_unchanged(record, repository)
        self.assertIn("detached-head", self.kinds(result))
        self.assertIn("head-drift", self.kinds(result))

    def test_attached_head_drift_is_not_inferred_from_cleanliness(self):
        repository, _ = self.make_repository("head-drift")
        recorded = self.git(repository, "rev-parse", "HEAD")
        (repository / "second.txt").write_text("second\n", encoding="utf-8")
        self.git(repository, "add", "second.txt")
        self.git(repository, "commit", "-q", "-m", "second")
        self.git(repository, "push", "-q", "origin", "main")
        record, writer = self.new_record("head-drift")
        self.add_goal(
            writer,
            repository,
            goal_id="head-drift-goal",
            checkpoint=recorded,
        )

        result = self.reconcile_unchanged(record, repository)
        self.assertEqual(self.kinds(result), ["head-drift"])
        self.assertEqual(result.discrepancies[0]["subject"], "goal:head-drift-goal:checkpoint")

    def test_missing_ref_worktree_repository_and_extra_worktree_are_explicit(self):
        repository, _ = self.make_repository("missing")
        record, writer = self.new_record("missing")
        self.add_goal(
            writer,
            repository,
            goal_id="missing-ref-goal",
            branch="codex/missing",
            checkpoint="f" * 40,
        )
        result = self.reconcile_unchanged(record, repository)
        self.assertEqual(self.kinds(result).count("missing-ref"), 2)

        absent_record, absent_writer = self.new_record("absent-worktree")
        self.add_goal(
            absent_writer,
            repository,
            goal_id="absent-worktree-goal",
            worktree=".worktrees/absent",
        )
        extra = repository / ".worktrees/extra"
        self.git(repository, "worktree", "add", "-q", "-b", "codex/extra", str(extra))
        extra_before = self.tree_digest(extra)
        result = self.reconcile_unchanged(absent_record, repository)
        self.assertEqual(self.tree_digest(extra), extra_before)
        missing_rows = [row for row in result.discrepancies if row["kind"] == "missing-worktree"]
        self.assertEqual(len(missing_rows), 2)
        self.assertTrue(any(row["observed"] is None for row in missing_rows))
        self.assertTrue(any(row["observed"] == "registered" for row in missing_rows))
        self.assertEqual(result.repositories[0]["extra_worktrees"], 1)

        missing_repository = self.workspace / "does-not-exist"
        missing = master_reconcile.reconcile_record(
            absent_record,
            {"missing": missing_repository},
        )
        self.assertEqual(self.kinds(missing), ["missing-repository", "missing-worktree"])

    def test_inaccessible_and_symlinked_facts_are_not_followed_or_suppressed(self):
        repository, _ = self.make_repository("inaccessible")
        record, _ = self.new_record("inaccessible")
        link = self.workspace / "repository-link"
        link.symlink_to(repository, target_is_directory=True)
        linked = master_reconcile.reconcile_record(record, {"synthetic": link})
        self.assertEqual(self.kinds(linked), ["inaccessible-fact"])

        calls = 0

        def unavailable(root, arguments):
            nonlocal calls
            calls += 1
            raise master_reconcile.ReconciliationError("synthetic refusal")

        result = self.reconcile_unchanged(record, repository, runner=unavailable)
        self.assertEqual(calls, 1)
        self.assertEqual(self.kinds(result), ["inaccessible-fact"])

    def test_stale_board_is_reported_without_repair(self):
        repository, _ = self.make_repository("stale")
        record, writer = self.new_record("stale")
        old_board = (record / master_runtime.BOARD_NAME).read_bytes()
        self.add_goal(writer, repository, goal_id="stale-goal")
        (record / master_runtime.BOARD_NAME).write_bytes(old_board)
        record_before = self.tree_digest(record)

        result = self.reconcile_unchanged(record, repository)
        self.assertIn("stale-board", self.kinds(result))
        self.assertEqual(self.tree_digest(record), record_before)
        self.assertFalse(master_runtime.read_record(record).board_current)

    def test_start_persists_reconciliation_and_cli_digest_mode_forwards_it(self):
        repository, _ = self.make_repository("integration")
        record, writer = self.new_record("integration")
        self.add_goal(writer, repository, goal_id="integration-goal")
        (repository / "tracked.txt").write_text("changed\n", encoding="utf-8")
        reconciliation = self.reconcile_unchanged(record, repository)
        lease = None

        def snapshot():
            return {"schema_version": 3, "lease": lease}

        def acquire(client, note, *, take_over=False):
            nonlocal lease
            lease = {"client": client, "lease_id": "2" * 32}
            return True, "acquired"

        result = master_operations.start_master(
            record,
            client="codex",
            model="synthetic-model",
            effort="high",
            note="synthetic start",
            reconciliation=reconciliation,
            acquire=acquire,
            renew=lambda: (True, "renewed"),
            release=lambda: (True, "released"),
            heartbeat=lambda interval: (True, "started"),
            lease_snapshot=snapshot,
            lease_status=lambda: "master: codex (live)\n",
        )
        self.assertEqual(result["status"], "master")
        self.assertEqual(
            master_runtime.read_record(record).events[-1]["payload"]["reconciliation"],
            list(reconciliation.discrepancies),
        )
        self.assertEqual(result["digest"]["live_reconciliation"]["schema_version"], 1)

        bounded = master_operations.digest_record(
            record,
            discrepancies_limit=1,
            live_reconciliation=reconciliation,
            lease_snapshot=snapshot,
            lease_status=lambda: "master: codex (live)\n",
        )["live_reconciliation"]["discrepancies"]
        self.assertEqual(len(bounded["items"]), 1)
        self.assertEqual(bounded["omitted"], len(reconciliation.discrepancies) - 1)
        if bounded["omitted"]:
            self.assertIsNotNone(bounded["continuation_key"])
        with self.assertRaisesRegex(master_operations.MasterOperationError, "0..100"):
            master_operations.digest_record(
                record,
                discrepancies_limit=101,
                live_reconciliation=reconciliation,
                lease_snapshot=snapshot,
                lease_status=lambda: "master: codex (live)\n",
            )

        location = master_operations.RuntimeLocation(
            repository,
            self.workspace,
            repository,
            record,
            (("synthetic", repository),),
        )
        output = io.StringIO()
        with (
            mock.patch("creme.cli._master_location", return_value=(location, None)),
            mock.patch("creme.cli.master_operations.reconcile_location", return_value=reconciliation) as reconcile,
            mock.patch("creme.cli.master_operations.digest_record") as digest,
            mock.patch("sys.stdout", output),
        ):
            digest.return_value = {"schema_version": 1, "status": "OK"}
            self.assertEqual(cli.main(["master", "digest", "--reconcile"]), 0)
        reconcile.assert_called_once_with(location)
        self.assertIs(digest.call_args.kwargs["live_reconciliation"], reconciliation)
        self.assertEqual(json.loads(output.getvalue())["status"], "OK")

        output = io.StringIO()
        reader = {"status": "reader", "holder": {"client": "peer", "state": "live"}}
        with (
            mock.patch("creme.cli._master_location", return_value=(location, None)),
            mock.patch("creme.cli.master_operations.reconcile_location", return_value=reconciliation),
            mock.patch("creme.cli.master_operations.start_master", return_value=reader) as start,
            mock.patch("sys.stdout", output),
        ):
            self.assertEqual(
                cli.main(
                    [
                        "master",
                        "start",
                        "--client",
                        "codex",
                        "--model",
                        "synthetic-model",
                        "--effort",
                        "high",
                        "--note",
                        "synthetic start",
                    ]
                ),
                0,
            )
        self.assertIs(start.call_args.kwargs["reconciliation"], reconciliation)


if __name__ == "__main__":
    unittest.main()
