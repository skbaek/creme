"""Multi-host goal stack: host-qualified refs, one chain, shareable branch.

Two simulated hosts ("wsl" and "mac") are two clones of one local bare
origin. Nothing touches a live goal store or a real remote.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from creme import goal_stack, goal_stack_git as shared, goal_stack_migrate as migration
from creme import goal_stack_store as store, master_runtime as runtime

GIT_ENV = {
    "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def writer(root, lease="a"):
    return runtime.RecordWriter(
        root, renew=lambda: (True, "fixture authority"),
        lease_snapshot=lambda: {"schema_version": 4,
                                "lease": {"client": "codex", "lease_id": lease * 32}},
    )


def entry(identity, **extra):
    value = {"id": identity, "title": identity, "status": "ready", "done": "evidence"}
    value.update(extra)
    return value


class Host:
    def __init__(self, name, plans):
        self.name = name
        self.plans = plans
        self.record = plans / "master"
        runtime.initialize_empty_record(self.record)
        self.writer = writer(self.record, "a" if name == "wsl" else "b")

    def mutate(self, action, payload=None, **kw):
        kw.setdefault("host", self.name)
        return store.mutate(self.record, action, payload or {}, writer=self.writer, **kw)

    def read(self):
        return store.read(self.record, host=self.name)

    def sync(self, **kw):
        return store.sync(self.record, writer=self.writer, host=self.name, **kw)


class SharedStackTest(unittest.TestCase):
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
        (seed / ".gitignore").write_text("/master/\n/.goal-stack-pending.json\n/.worktrees/\n")
        (seed / "goals").mkdir()
        (seed / "goals/shared.md").write_text("# shared goal\n")
        git(seed, "add", ".")
        git(seed, "commit", "-q", "-m", "seed")
        git(seed, "remote", "add", "origin", str(self.origin))
        git(seed, "push", "-q", "origin", "main")
        self.base = base
        wsl_plans = base / "wsl" / "plans"
        mac_plans = base / "mac" / "plans"
        for path in (wsl_plans, mac_plans):
            subprocess.run(["git", "clone", "-q", str(self.origin), str(path)], check=True)
        self.wsl = Host("wsl", wsl_plans)
        self.mac = Host("mac", mac_plans)
        # WSL-private Plans history that must never be pushed.
        (wsl_plans / "goals/private.md").write_text("# wsl-only goal\n")
        git(wsl_plans, "add", "goals/private.md")
        git(wsl_plans, "commit", "-q", "-m", "private WSL history")
        self.private_commit = git(wsl_plans, "rev-parse", "HEAD")
        (mac_plans / ".worktrees/x").mkdir(parents=True)
        (mac_plans / ".worktrees/x/STATE.md").write_text("mac state\n")

    # -- helpers ---------------------------------------------------------

    def legacy_wsl(self):
        """A schema-1 WSL stack with a shared ref, a WSL-only ref and Mac paths."""
        self.wsl.mutate("init", {"stack": {"schema_version": 1, "revision": 0, "entries": [
            entry("shared-work", goal="goals/shared.md"),
        ]}})
        self.wsl.mutate("push", {"entry": entry("wsl-work", goal="goals/private.md",
                                                worktree=str(self.wsl.plans))})

    def migrate_wsl(self, **kw):
        kw.setdefault("host_map", ["/Users/agent/=mac", ".worktrees/=mac"])
        return migration.migrate(self.wsl.record, host="wsl", shared_ref="origin/main",
                                 writer=self.wsl.writer, **kw)

    def copy_legacy(self, source, target):
        target_receipts = target.plans / shared.RECEIPTS
        target_receipts.mkdir(parents=True, exist_ok=True)
        for path in (source.plans / shared.RECEIPTS).iterdir():
            (target_receipts / path.name).write_bytes(path.read_bytes())
        (target.plans / store.NAME).write_bytes((source.plans / store.NAME).read_bytes())

    def shared_ready(self):
        self.legacy_wsl()
        self.copy_legacy(self.wsl, self.mac)
        self.migrate_wsl()
        migration.adopt(self.mac.record, host="mac", writer=self.mac.writer)

    def canonical_receipts(self):
        clone = self.base / "inspect"
        if clone.exists():
            git(clone, "fetch", "-q", "origin", "goal-stack:refs/remotes/origin/goal-stack")
        else:
            subprocess.run(["git", "clone", "-q", "--branch", "goal-stack", str(self.origin), str(clone)],
                           check=True)
        tip = git(clone, "rev-parse", "refs/remotes/origin/goal-stack")
        root = git(clone, "rev-list", "--max-parents=0", tip)
        return shared.verify_chain(clone, root, tip), clone, tip

    # -- R4/R5: migration publishes only the stack branch ----------------

    def test_migration_preserves_chain_and_publishes_no_private_history(self):
        self.legacy_wsl()
        legacy_bytes = (self.wsl.plans / store.NAME).read_bytes()
        receipts = {p.name: p.read_bytes() for p in (self.wsl.plans / shared.RECEIPTS).iterdir()}
        result = self.migrate_wsl()
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["qualified"]["wsl-work"]["goal"][1], "wsl:goals/private.md")
        self.assertEqual(result["qualified"]["wsl-work"]["worktree"][1], f"wsl:{self.wsl.plans}")
        self.assertNotIn("shared-work", result["qualified"])
        # The stub replaces the schema-1 manifest; old readers refuse it.
        stub = (self.wsl.plans / store.NAME).read_text()
        self.assertTrue(shared.is_stub(stub.encode()))
        with self.assertRaises(goal_stack.GoalStackError):
            goal_stack.loads(stub, self.wsl.plans, schema=goal_stack.SCHEMA_VERSION)
        # Import commit holds the exact schema-1 bytes and every receipt.
        _, clone, tip = self.canonical_receipts()
        root = git(clone, "rev-list", "--max-parents=0", tip)
        self.assertEqual(shared.show(clone, root, store.NAME), legacy_bytes)
        for name, data in receipts.items():
            self.assertEqual(shared.show(clone, tip, f"{shared.RECEIPTS}/{name}"), data)
        migrated = json.loads(shared.show(clone, tip, shared.receipt_path(2)))
        self.assertEqual(migrated["action"], "migrate")
        self.assertEqual(migrated["actor"]["host"], "wsl")
        # M4: the private main commit never reached the origin.
        probe = subprocess.run(["git", "-C", str(self.origin), "cat-file", "-e", self.private_commit],
                               capture_output=True)
        self.assertNotEqual(probe.returncode, 0)
        self.assertEqual(git(self.origin, "rev-list", "--count", "main"), "1")
        self.assertEqual(self.wsl.read()["revision"], 2)
        events = runtime.read_record(self.wsl.record).events
        self.assertTrue(any(e["kind"] == "procedure"
                            and e["payload"]["procedure_id"] == store.SHARED_PROCEDURE for e in events))
        # The stack worktree is ignored on the goal store's default branch.
        self.assertEqual(subprocess.run(
            ["git", "-C", str(self.wsl.plans), "check-ignore", "-q", ".goal-stack/x"]).returncode, 0)

    def test_dry_run_plan_writes_nothing_and_lists_unresolved_hosts(self):
        self.wsl.mutate("init", {"stack": {"schema_version": 1, "revision": 0, "entries": [
            entry("mac-work", worktree="/Users/agent/blanc/.worktrees/m"),
        ]}})
        before = sorted(p.name for p in self.wsl.plans.iterdir())
        refused = migration.plan(self.wsl.plans, host="wsl", shared_ref="origin/main")
        self.assertEqual(refused["status"], "REFUSED")
        self.assertEqual(refused["unresolved"][0]["field"], "worktree")
        ok = migration.plan(self.wsl.plans, host="wsl", shared_ref="origin/main",
                            host_map=["/Users/agent/=mac"])
        self.assertEqual(ok["status"], "OK")
        self.assertIn('worktree = "mac:/Users/agent/blanc/.worktrees/m"', ok["manifest"])
        self.assertEqual(sorted(p.name for p in self.wsl.plans.iterdir()), before)

    def test_migration_refuses_broken_receipt_chain(self):
        self.legacy_wsl()
        receipt = self.wsl.plans / shared.receipt_path(1)
        receipt.unlink()
        with self.assertRaisesRegex(migration.MigrationError, "receipts must be exactly"):
            self.migrate_wsl()
        self.assertFalse((self.wsl.plans / shared.SHARED_DIR).exists())

    def test_second_migration_refuses_and_adoption_verifies_prefix(self):
        self.legacy_wsl()
        self.copy_legacy(self.wsl, self.mac)
        self.migrate_wsl()
        self.mac.writer.append("procedure", {
            "procedure_id": store.PROCEDURE, "action": "replace", "failure": "f",
            "replacement": "r", "control": "c", "evidence": "e"})
        with self.assertRaisesRegex(migration.MigrationError, "another host migrated"):
            migration.migrate(self.mac.record, host="mac", shared_ref="origin/main",
                              writer=self.mac.writer, host_map=["/=wsl", "goals/private.md=wsl"])
        self.assertFalse((self.mac.plans / shared.SHARED_DIR).exists())
        result = migration.adopt(self.mac.record, host="mac", writer=self.mac.writer)
        self.assertEqual(result["common_revision"], 1)
        self.assertEqual(result["set_aside"], [])
        stack = self.mac.read()
        # M1: the WSL-only goal and worktree are absent on the Mac, yet the
        # stack reads; they are reported unverifiable here.
        work = next(e for e in stack["entries"] if e["id"] == "wsl-work")
        self.assertFalse((self.mac.plans / "goals/private.md").exists())
        status = goal_stack.ref_status(work, "mac")
        self.assertEqual(status["goal"], ("wsl", "goals/private.md", False))

    def test_adoption_refuses_local_only_revisions_unless_set_aside(self):
        self.legacy_wsl()
        self.copy_legacy(self.wsl, self.mac)
        (self.mac.plans / "goals/private.md").write_text("legacy copy\n")
        # The Mac forks: its own revision 2 that the shared chain lacks.
        self.mac.mutate("push", {"entry": entry("mac-fork")})
        self.migrate_wsl()
        with self.assertRaisesRegex(migration.MigrationError, "set-aside-local"):
            migration.adopt(self.mac.record, host="mac", writer=self.mac.writer)
        self.assertFalse((self.mac.plans / shared.SHARED_DIR).exists())
        result = migration.adopt(self.mac.record, host="mac", writer=self.mac.writer,
                                 set_aside_local=True)
        self.assertEqual([row["revision"] for row in result["set_aside"]], [2])
        self.assertEqual(result["set_aside"][0]["payload"]["entry"]["id"], "mac-fork")
        self.assertNotIn("mac-fork", [e["id"] for e in self.mac.read()["entries"]])

    # -- R3: one chain under interleaved writers -------------------------

    def test_interleaved_writes_form_one_linear_chain(self):
        self.shared_ready()
        self.mac.mutate("push", {"entry": entry("mac-a")})               # rev 3
        self.wsl.mutate("push", {"entry": entry("wsl-a")})               # rev 4 (fetched rev 3)
        # The Mac prepared against revision 3; WSL's rev 4 touched another
        # entry, so the update replays on top of the new head.
        replayed = self.mac.mutate("update", {"id": "mac-a", "changes": {"next": "go"}},
                                   expected_revision=3)
        self.assertEqual(replayed["replayed_from"], 3)
        self.assertEqual(replayed["stack"]["revision"], 5)
        # WSL never read revision 5, which changed mac-a: completing it from
        # its stale copy refuses; the refusal leaves the local copy synced,
        # so the re-read shows revision 5 and the re-issued write applies.
        with self.assertRaisesRegex(store.StackStoreError, "stack advanced on host mac"):
            self.wsl.mutate("complete", {"id": "mac-a", "evidence": "done on wsl"})
        self.assertEqual(self.wsl.read()["revision"], 5)
        self.wsl.mutate("complete", {"id": "mac-a", "evidence": "done on wsl"})  # rev 6
        receipts, _, _ = self.canonical_receipts()
        revisions = [r["revision"] for r in receipts]
        self.assertEqual(revisions, list(range(2, 7)))
        hosts = [r["actor"]["host"] for r in receipts]
        self.assertEqual(hosts, ["wsl", "mac", "wsl", "mac", "wsl"])
        self.assertEqual(self.mac.read()["revision"], 5)  # reads never fetch
        self.mac.mutate("push", {"entry": entry("mac-b")})               # syncs first
        self.assertEqual(self.mac.read()["revision"], 7)

    def test_stale_write_on_the_same_entry_refuses_instead_of_forking(self):
        self.shared_ready()
        self.mac.mutate("push", {"entry": entry("shared-x")})            # rev 3
        self.wsl.sync()
        self.wsl.mutate("update", {"id": "shared-x", "changes": {"next": "wsl view"}})  # rev 4
        head = (self.mac.plans / shared.SHARED_DIR / store.NAME).read_bytes()
        with self.assertRaisesRegex(store.StackStoreError,
                                    r"stack advanced on host wsl \(revision 3 -> 4\).*shared-x"):
            self.mac.mutate("update", {"id": "shared-x", "changes": {"next": "mac view"}},
                            expected_revision=3)
        receipts, _, tip = self.canonical_receipts()
        self.assertEqual(receipts[-1]["revision"], 4)
        self.assertEqual(len({r["revision"] for r in receipts}), len(receipts))
        # Local copy fast-forwarded to the canonical head; nothing unpublished.
        worktree = self.mac.plans / shared.SHARED_DIR
        self.assertEqual(shared.head(worktree), tip)
        self.assertNotEqual((worktree / store.NAME).read_bytes(), head)

    def test_push_race_replays_or_refuses_without_duplicate_revisions(self):
        self.shared_ready()
        self.mac.mutate("push", {"entry": entry("target")})              # rev 3
        self.wsl.mutate("push", {"entry": entry("other")})               # rev 4
        self.mac.sync()
        raced = []

        def interleave(stage):
            if stage == "shared:before-push" and not raced:
                raced.append(stage)
                self.mac.sync()
                self.mac.mutate("update", {"id": "other", "changes": {"next": "mac"}})

        # WSL prepares rev 5 locally; the Mac publishes rev 5 first; WSL's
        # push is rejected, its commit dropped, and the write replays as 6.
        result = store.mutate(self.wsl.record, "update", {"id": "target", "changes": {"next": "wsl"}},
                              writer=self.wsl.writer, host="wsl", fault=interleave)
        self.assertEqual(result["stack"]["revision"], 6)
        receipts, _, _ = self.canonical_receipts()
        self.assertEqual([r["revision"] for r in receipts], list(range(2, 7)))
        self.assertEqual(receipts[-1]["actor"]["host"], "wsl")
        self.assertEqual(receipts[-2]["actor"]["host"], "mac")
        # A conflicting race refuses and publishes nothing from the loser.
        raced.clear()

        def conflict(stage):
            if stage == "shared:before-push" and not raced:
                raced.append(stage)
                self.mac.sync()
                self.mac.mutate("update", {"id": "target", "changes": {"next": "mac wins"}})

        with self.assertRaisesRegex(store.StackStoreError, "target"):
            store.mutate(self.wsl.record, "update", {"id": "target", "changes": {"next": "wsl"}},
                         writer=self.wsl.writer, host="wsl", fault=conflict, expected_revision=6)
        receipts, _, _ = self.canonical_receipts()
        self.assertEqual(receipts[-1]["revision"], 7)
        self.assertEqual(receipts[-1]["actor"]["host"], "mac")
        self.assertFalse((self.wsl.plans / shared.SHARED_DIR / shared.PENDING).exists())

    def test_rewritten_canonical_history_refuses(self):
        self.shared_ready()
        self.mac.mutate("push", {"entry": entry("a")})
        self.wsl.mutate("push", {"entry": entry("b")})
        self.mac.sync()
        # Someone force-pushes the canonical branch back one revision.
        _, clone, _ = self.canonical_receipts()
        git(clone, "push", "-q", "--force", "origin", "refs/remotes/origin/goal-stack~1:refs/heads/goal-stack")
        with self.assertRaisesRegex(store.StackStoreError, "unreachable or rewritten"):
            self.mac.mutate("push", {"entry": entry("c")})

    def test_crash_after_commit_before_push_recovers(self):
        self.shared_ready()

        def crash(stage):
            if stage == "shared:before-push":
                raise RuntimeError("simulated crash")

        with self.assertRaises(RuntimeError):
            store.mutate(self.wsl.record, "push", {"entry": entry("crashy")},
                         writer=self.wsl.writer, host="wsl", fault=crash)
        with self.assertRaisesRegex(store.StackStoreError, "unfinished"):
            self.wsl.read()
        result = self.wsl.mutate("recover")
        self.assertTrue(result["recovered"])
        receipts, _, _ = self.canonical_receipts()
        self.assertEqual(receipts[-1]["payload"]["entry"]["id"], "crashy")

    def test_crash_then_superseded_write_is_preserved_not_published(self):
        self.shared_ready()

        def crash(stage):
            if stage == "shared:before-push":
                raise RuntimeError("simulated crash")

        with self.assertRaises(RuntimeError):
            store.mutate(self.wsl.record, "push", {"entry": entry("lost")},
                         writer=self.wsl.writer, host="wsl", fault=crash)
        self.mac.mutate("push", {"entry": entry("winner")})
        with self.assertRaisesRegex(store.StackStoreError, "superseded.*preserved at refs/goal-stack/"):
            self.wsl.mutate("recover")
        receipts, _, _ = self.canonical_receipts()
        self.assertEqual(receipts[-1]["payload"]["entry"]["id"], "winner")
        self.assertEqual([e["id"] for e in self.wsl.mutate("recover")["stack"]["entries"]][:1], ["winner"])

    # -- R1/R2: host-qualified references and host identity -------------

    def test_own_host_and_shared_refs_fail_closed(self):
        self.shared_ready()
        with self.assertRaisesRegex(goal_stack.GoalStackError, "does not name an existing file"):
            self.mac.mutate("push", {"entry": entry("bad-own", context="mac:.worktrees/x/MISSING.md")})
        with self.assertRaisesRegex(goal_stack.GoalStackError, "does not name an existing file"):
            self.mac.mutate("push", {"entry": entry("bad-shared", goal="goals/missing.md")})
        ok = self.mac.mutate("push", {"entry": entry("mac-ctx", context="mac:.worktrees/x/STATE.md",
                                                     worktree="mac:/Users/agent/blanc/.worktrees/x")})
        self.assertTrue(ok["changed"])
        # The WSL host reads the Mac-only context without it existing there.
        self.assertFalse((self.wsl.plans / ".worktrees/x/STATE.md").exists())
        self.wsl.mutate("recover")  # sync
        self.assertIn("mac-ctx", [e["id"] for e in self.wsl.read()["entries"]])
        # A ref qualified with the reading host that is missing refuses reads.
        (self.mac.plans / ".worktrees/x/STATE.md").unlink()
        with self.assertRaisesRegex(store.StackStoreError, "does not name an existing file"):
            self.mac.read()

    def test_unnamed_host_refuses_shared_reads_and_writes(self):
        self.shared_ready()
        before = shared.head(self.mac.plans / shared.SHARED_DIR)
        with mock.patch.object(store, "local_host_name", return_value=None):
            with self.assertRaisesRegex(store.StackStoreError, "no name"):
                store.mutate(self.mac.record, "push", {"entry": entry("x")}, writer=self.mac.writer)
            with self.assertRaisesRegex(store.StackStoreError, "no name"):
                store.read(self.mac.record)
        self.assertEqual(shared.head(self.mac.plans / shared.SHARED_DIR), before)

    def test_worktree_must_be_host_qualified_in_schema_two(self):
        self.shared_ready()
        with self.assertRaisesRegex(goal_stack.GoalStackError, "host-qualified"):
            self.mac.mutate("push", {"entry": entry("w", worktree="/Users/agent/x")})
        with self.assertRaisesRegex(goal_stack.GoalStackError, "absolute path"):
            self.mac.mutate("push", {"entry": entry("w", worktree="mac:relative/x")})

    def test_hand_edited_shared_manifest_refuses(self):
        self.shared_ready()
        path = self.mac.plans / shared.SHARED_DIR / store.NAME
        path.write_text(path.read_text().replace("shared-work", "shared-edit"))
        with self.assertRaisesRegex(store.StackStoreError, "differs from its committed revision"):
            self.mac.read()


class OldCremeRefusesSchemaTwo(unittest.TestCase):
    """M5 negative control: the pre-change reader must refuse schema 2."""

    BASE = "5ebf7ec"

    def test_old_reader_refuses_stub_and_schema_two(self):
        repo = Path(__file__).resolve().parents[2]
        source = subprocess.run(["git", "-C", str(repo), "show", f"{self.BASE}:creme/goal_stack.py"],
                                capture_output=True, text=True)
        if source.returncode:
            self.skipTest("base commit unavailable")
        namespace: dict = {"__name__": "old_goal_stack"}
        exec(compile(source.stdout, "old_goal_stack.py", "exec"), namespace)
        with tempfile.TemporaryDirectory() as temp:
            for text in (shared.STUB, "schema_version = 2\nrevision = 3\nentries = []\n"):
                with self.assertRaises(namespace["GoalStackError"]):
                    namespace["loads"](text, temp)


if __name__ == "__main__":
    unittest.main()
