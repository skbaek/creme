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
            "schema_version": 4,
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

    def test_nonzero_git_fact_failures_are_inaccessible_not_missing(self):
        failing_commands = ("status", "rev-list", "show-ref", "cat-file")
        for command in failing_commands:
            with self.subTest(command=command):
                repository, _ = self.make_repository(f"denied-{command}")
                record, writer = self.new_record(f"denied-{command}")
                self.add_goal(writer, repository, goal_id=f"denied-{command}-goal")

                def denied(root, arguments):
                    if arguments[0] == command:
                        return master_reconcile.GitResult(
                            128, b"", b"fatal: permission denied\n"
                        )
                    return master_reconcile.run_git(root, arguments)

                result = self.reconcile_unchanged(record, repository, runner=denied)
                self.assertIn("inaccessible-fact", self.kinds(result))
                self.assertNotIn("missing-ref", self.kinds(result))

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
            return {"schema_version": 4, "lease": lease}

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

    def synthetic_rows(self, census: dict[tuple[str, str], int]) -> list[dict]:
        rows = []
        for (repository, kind), count in census.items():
            for index in range(count):
                rows.append(
                    master_reconcile._discrepancy(
                        repository,
                        kind,
                        f"subject-{index:04d}",
                        recorded="recorded",
                        observed=None,
                        detail="synthetic row",
                    )
                )
        return rows

    def test_checkpoint_ref_claims_are_classified_instead_of_assumed(self):
        commit = "a" * 39 + "7"
        bare = master_reconcile.checkpoint_claim(f"  {commit}  ")
        self.assertEqual((bare.commit, bare.candidates), (commit, ()))
        sha256 = "b" * 63 + "4"
        self.assertEqual(master_reconcile.checkpoint_claim(sha256).commit, sha256)

        prose = master_reconcile.checkpoint_claim(
            f"Source {commit} pushed; re-verified at {commit}."
        )
        self.assertIsNone(prose.commit)
        self.assertEqual(prose.candidates, (commit,))

        # Every shape below is data this record's prose actually carries, and
        # none of it is a Git object name the reconciler may accuse.
        not_claims = {
            "abbreviation": "Source 7b3a1e8 pushed; 39 proof recipes verified",
            "word": "the control was effaced and defaced; 39 recipes",
            "decimal": "cost is " + "1" * 40 + " calls over 12900000000 gas",
            "event-id": "detail retained verbatim in events.jsonl event " + "c" * 32,
            "content-digest": "author wind-down OK raw hash" + sha256 + " verified",
            "evm-address": "WETH10 at 0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2 is fixed",
            "glued-run": "Clean source" + commit + "; narrow build green",
            "upper-case": f"HEAD {commit.upper()} recorded",
        }
        for label, checkpoint in not_claims.items():
            with self.subTest(shape=label):
                claim = master_reconcile.checkpoint_claim(checkpoint)
                self.assertIsNone(claim.commit)
                self.assertEqual(claim.candidates, ())

    def test_prose_checkpoint_whose_named_commit_exists_is_not_a_missing_ref(self):
        repository, _ = self.make_repository("prose-present")
        head = self.git(repository, "rev-parse", "HEAD")
        record, writer = self.new_record("prose-present")
        self.add_goal(
            writer,
            repository,
            goal_id="prose-present-goal",
            checkpoint=(
                f"Source {head} pushed; CreationCoordinatesCertificate complete; "
                "39 proof recipes verified; root 2cbd0ff0 sealed"
            ),
        )
        result = self.reconcile_unchanged(record, repository)
        self.assertEqual(result.discrepancies, ())

    def test_prose_checkpoint_naming_an_absent_commit_is_still_reported(self):
        repository, _ = self.make_repository("prose-absent")
        head = self.git(repository, "rev-parse", "HEAD")
        absent = "f" * 40
        record, writer = self.new_record("prose-absent")
        self.add_goal(
            writer,
            repository,
            goal_id="prose-absent-goal",
            checkpoint=(
                f"Repair green at head {head} off main; evidence commit {absent} "
                "pushed; not merged"
            ),
        )
        result = self.reconcile_unchanged(record, repository)
        self.assertEqual(self.kinds(result), ["missing-ref"])
        row = result.discrepancies[0]
        self.assertEqual(row["repository"], "workspace")
        self.assertEqual(row["subject"], "goal:prose-absent-goal:checkpoint")
        # The resolvable name does not excuse the absent one, and only the
        # absent name is accused.
        self.assertEqual(row["recorded"], absent)

    def test_prose_checkpoint_without_a_ref_claim_is_never_reported(self):
        repository, _ = self.make_repository("prose-quiet")
        record, writer = self.new_record("prose-quiet")
        # An abbreviation that resolves nowhere is deliberately not asserted:
        # compaction glues names to neighbouring words, so a partial run cannot
        # be attributed. Absence is only claimed for a full object name.
        self.add_goal(
            writer,
            repository,
            goal_id="prose-quiet-goal",
            checkpoint=(
                "ACCEPTANCE WITHDRAWN. The tranche at deadbee is RED in isolation; "
                "nine sponges enumerated three independent ways; manifest root "
                + "d" * 32
                + "; oracle at 0x" + "e" * 40
            ),
        )
        result = self.reconcile_unchanged(record, repository)
        self.assertEqual(result.discrepancies, ())

    def test_prose_checkpoint_that_cannot_be_inspected_is_not_called_missing(self):
        repository, _ = self.make_repository("prose-denied")
        record, writer = self.new_record("prose-denied")
        self.add_goal(
            writer,
            repository,
            goal_id="prose-denied-goal",
            checkpoint="evidence commit " + "f" * 40 + " pushed",
        )

        def denied(root, arguments):
            if arguments[0] == "cat-file":
                return master_reconcile.GitResult(128, b"", b"fatal: permission denied\n")
            return master_reconcile.run_git(root, arguments)

        result = self.reconcile_unchanged(record, repository, runner=denied)
        self.assertEqual(self.kinds(result), ["inaccessible-fact"])
        self.assertEqual(result.discrepancies[0]["repository"], "workspace")

    def test_checkpoint_held_by_another_configured_repository_is_head_drift(self):
        alpha, _ = self.make_repository("alpha")
        beta, _ = self.make_repository("beta")
        (beta / "beta.txt").write_text("beta only\n", encoding="utf-8")
        self.git(beta, "add", "beta.txt")
        self.git(beta, "commit", "-q", "-m", "beta only")
        self.git(beta, "push", "-q", "origin", "main")
        beta_head = self.git(beta, "rev-parse", "HEAD")
        alpha_head = self.git(alpha, "rev-parse", "HEAD")
        self.assertNotEqual(alpha_head, beta_head)
        record, writer = self.new_record("cross-repository")
        self.add_goal(
            writer,
            alpha,
            goal_id="cross-goal",
            worktree=str(alpha),
            checkpoint=beta_head,
        )
        result = master_reconcile.reconcile_record(
            record,
            {"alpha": alpha, "beta": beta},
        )
        self.assertEqual(self.kinds(result), ["head-drift"])
        row = result.discrepancies[0]
        self.assertEqual(row["repository"], "alpha")
        self.assertEqual((row["recorded"], row["observed"]), (beta_head, alpha_head))

        absent_record, absent_writer = self.new_record("cross-absent")
        self.add_goal(
            absent_writer,
            alpha,
            goal_id="cross-absent-goal",
            worktree=str(alpha),
            checkpoint="f" * 40,
        )
        absent = master_reconcile.reconcile_record(
            absent_record,
            {"alpha": alpha, "beta": beta},
        )
        self.assertEqual(self.kinds(absent), ["missing-ref"])

    def test_reconciliation_at_and_above_the_record_cap_is_recorded_with_a_census(self):
        limit = master_runtime.MAX_LIST_ITEMS
        census = {
            ("blanc", "missing-worktree"): limit - 40,
            ("blanc", "detached-head"): 30,
            ("goal-store", "missing-worktree"): 60,
            ("goal-store", "upstream-drift"): 6,
            ("workspace", "missing-ref"): 1,
        }
        total = sum(census.values())
        self.assertGreater(total, limit)
        rows = self.synthetic_rows(census)

        at_cap = master_reconcile.ReconciliationResult((), tuple(rows[:limit]))
        self.assertEqual(
            master_reconcile.summarize_for_record(at_cap),
            list(rows[:limit]),
        )
        master_runtime.validate_payload("master", {
            "action": "start",
            "model": "synthetic-model",
            "effort": "high",
            "note": "synthetic",
            "next_unit": "",
            "reconciliation": master_reconcile.summarize_for_record(at_cap),
        })

        over_cap = master_reconcile.ReconciliationResult((), tuple(rows))
        recorded = master_reconcile.summarize_for_record(over_cap)
        self.assertEqual(len(recorded), limit)
        master_runtime.validate_payload("master", {
            "action": "start",
            "model": "synthetic-model",
            "effort": "high",
            "note": "synthetic",
            "next_unit": "",
            "reconciliation": recorded,
        })
        head = recorded[0]
        self.assertEqual(head["repository"], master_reconcile.CENSUS_REPOSITORY)
        self.assertEqual(head["subject"], master_reconcile.CENSUS_SUBJECT)
        self.assertEqual(head["recorded"], f"observed={total}")
        self.assertEqual(head["observed"], f"recorded={limit - 1}")
        # The census states every observed group, so the count is never
        # understated by the bounded selection.
        accounted = 0
        for (repository, kind), count in census.items():
            entry = f"{repository}/{kind}={count}"
            self.assertIn(entry, head["detail"])
            accounted += count
        self.assertEqual(accounted, total)
        self.assertNotIn("omitted", head["detail"])
        selected = {(row["repository"], row["kind"]) for row in recorded[1:]}
        self.assertEqual(selected, set(census))
        self.assertEqual(
            recorded[1:],
            sorted(recorded[1:], key=master_reconcile._sort_key),
        )

        # A budget smaller than the number of groups still states every group
        # and the true total.
        narrow = master_reconcile.summarize_for_record(over_cap, limit=3)
        self.assertEqual(len(narrow), 3)
        self.assertEqual(narrow[0]["recorded"], f"observed={total}")
        for (repository, kind), count in census.items():
            self.assertIn(f"{repository}/{kind}={count}", narrow[0]["detail"])

    def test_master_start_enters_when_observed_discrepancies_exceed_the_cap(self):
        limit = master_runtime.MAX_LIST_ITEMS
        census = {
            ("blanc", "missing-worktree"): limit,
            ("goal-store", "missing-worktree"): 48,
            ("workspace", "missing-ref"): 1,
        }
        total = sum(census.values())
        rows = self.synthetic_rows(census)
        reconciliation = master_reconcile.ReconciliationResult((), tuple(rows))
        record, _ = self.new_record("over-cap-start")
        lease = None

        def snapshot():
            return {"schema_version": 4, "lease": lease}

        def acquire(client, note, *, take_over=False):
            nonlocal lease
            lease = {"client": client, "lease_id": "3" * 32}
            return True, "acquired"

        result = master_operations.start_master(
            record,
            client="codex",
            model="synthetic-model",
            effort="high",
            note="synthetic start over the cap",
            reconciliation=reconciliation,
            acquire=acquire,
            renew=lambda: (True, "renewed"),
            release=lambda: (True, "released"),
            heartbeat=lambda interval: (True, "started"),
            lease_snapshot=snapshot,
            lease_status=lambda: "master: codex (live)\n",
        )
        self.assertEqual(result["status"], "master")
        persisted = master_runtime.read_record(record).events[-1]["payload"]["reconciliation"]
        self.assertEqual(len(persisted), limit)
        self.assertEqual(persisted[0]["recorded"], f"observed={total}")
        self.assertEqual(persisted[0]["subject"], master_reconcile.CENSUS_SUBJECT)
        # The observed result itself is untouched and still reaches the session.
        self.assertEqual(len(reconciliation.discrepancies), total)
        self.assertEqual(result["digest"]["live_reconciliation"]["schema_version"], 1)



if __name__ == "__main__":
    unittest.main()
