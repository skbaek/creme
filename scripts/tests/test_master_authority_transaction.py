from __future__ import annotations

import contextlib
import multiprocessing
import os
import threading
import unittest
from unittest import mock

from creme import master_migrate, master_runtime, semaphore
from scripts.tests.test_master_transition_model import (
    Action,
    ConcreteWorld,
    LEASE_SECONDS,
)


class SyntheticFault(RuntimeError):
    pass


class MasterAuthorityTransactionTest(unittest.TestCase):
    def test_actual_child_waiting_for_record_lock_refuses_after_succession(self):
        with ConcreteWorld(0xA710) as world:
            self.assertEqual(world.execute(Action("acquire", "process-a")), "ok")
            before = world.record_bytes()
            context = multiprocessing.get_context("fork")
            renewed = context.Event()
            resume = context.Event()
            waiting = context.Event()

            def predecessor() -> None:
                world.select("process-a")
                first = True

                def renew():
                    nonlocal first
                    result = world._renew()
                    if first:
                        first = False
                        renewed.set()
                        if not resume.wait(5):
                            os._exit(90)
                    return result

                original_lock = master_runtime._locked_record

                @contextlib.contextmanager
                def observed_lock(root):
                    waiting.set()
                    with original_lock(root):
                        yield

                writer = master_runtime.RecordWriter(
                    world.record_root,
                    renew=renew,
                    lease_snapshot=semaphore.master_snapshot,
                    authority_transaction=world._authority_transaction,
                )
                try:
                    with mock.patch.object(
                        master_runtime,
                        "_locked_record",
                        side_effect=observed_lock,
                    ):
                        writer.append("note", world.note_payload(1))
                except master_runtime.RenewalRefused:
                    os._exit(72)
                except Exception:
                    os._exit(91)
                os._exit(73)

            process = context.Process(target=predecessor)
            process.start()
            self.assertTrue(renewed.wait(5))
            with master_runtime._locked_record(world.record_root):
                resume.set()
                self.assertTrue(waiting.wait(5))
                self.assertTrue(process.is_alive())
                self.assertEqual(world.execute(Action("lapse", "process-a")), "ok")
                self.assertEqual(world.execute(Action("takeover", "process-b")), "ok")
            process.join(5)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 72)
            self.assertEqual(world.record_bytes(), before)

    def _assert_successor_waits(
        self,
        world: ConcreteWorld,
        owner,
    ) -> None:
        entered = threading.Event()
        resume = threading.Event()
        successor_done = threading.Event()
        outcomes: dict[str, object] = {}

        def pause(stage: str) -> None:
            if stage == "transaction-description:before-create" or stage == "backup:before-root":
                entered.set()
                if not resume.wait(5):
                    raise AssertionError("authorized transaction was not resumed")

        def run_owner() -> None:
            world.select("process-a")
            try:
                outcomes["owner"] = owner(pause)
            except Exception as exc:  # pragma: no cover - retained for failure detail
                outcomes["owner"] = exc

        def run_successor() -> None:
            world.select("process-b")
            outcomes["successor"] = world.execute(Action("takeover", "process-b"))
            successor_done.set()

        owner_thread = threading.Thread(target=run_owner)
        owner_thread.start()
        self.assertTrue(entered.wait(5))
        world.now += LEASE_SECONDS + 1
        successor_thread = threading.Thread(target=run_successor)
        successor_thread.start()
        self.assertFalse(successor_done.wait(0.2))
        resume.set()
        owner_thread.join(5)
        successor_thread.join(5)
        self.assertFalse(owner_thread.is_alive() or successor_thread.is_alive())
        self.assertEqual(outcomes.get("successor"), "ok")
        self.assertNotIsInstance(outcomes.get("owner"), Exception)

    def test_successor_waits_for_append_snapshot_and_publication(self):
        with ConcreteWorld(0xA711) as world:
            self.assertEqual(world.execute(Action("acquire", "process-a")), "ok")
            self._assert_successor_waits(
                world,
                lambda pause: world.writer().append(
                    "note",
                    world.note_payload(1),
                    fault=pause,
                ),
            )
            state = world.state()
            self.assertEqual(state["lease_generation"], 2)
            self.assertEqual(state["events"], [("note", 1)])
            self.assertTrue(state["board_current"])

    def test_successor_waits_for_stale_board_recovery(self):
        with ConcreteWorld(0xA712) as world:
            self.assertEqual(world.execute(Action("acquire", "process-a")), "ok")
            world.writer().append("note", world.note_payload(1))
            (world.record_root / master_runtime.BOARD_NAME).write_bytes(
                master_runtime.render_board(())
            )
            self._assert_successor_waits(
                world,
                lambda pause: world.writer().append(
                    "note",
                    world.note_payload(2),
                    fault=pause,
                    once_per_acquisition=True,
                ),
            )
            view = master_runtime.read_record(world.record_root)
            self.assertEqual(len(view.events), 1)
            self.assertTrue(view.board_current)

    def test_successor_waits_for_explicit_migration(self):
        with ConcreteWorld(0xA713) as world:
            self.assertEqual(world.execute(Action("acquire", "process-a")), "ok")
            self.assertEqual(world.execute(Action("legacy-record", "process-a")), "ok")
            self._assert_successor_waits(
                world,
                lambda pause: master_migrate.migrate(
                    world.record_root,
                    apply=True,
                    renew=world._renew,
                    authority_transaction=world._authority_transaction,
                    fault=pause,
                ),
            )
            self.assertEqual(master_migrate.plan_migration(world.record_root).status, "CURRENT")

    def test_exception_releases_public_transaction_mutex(self):
        with ConcreteWorld(0xA714) as world:
            self.assertEqual(world.execute(Action("acquire", "process-a")), "ok")

            def fail(stage: str) -> None:
                if stage == "transaction-description:before-create":
                    raise SyntheticFault(stage)

            with self.assertRaises(SyntheticFault):
                world.writer().append("note", world.note_payload(1), fault=fail)
            self.assertEqual(world.execute(Action("lapse", "process-a")), "ok")
            self.assertEqual(world.execute(Action("takeover", "process-b")), "ok")


if __name__ == "__main__":
    unittest.main()
