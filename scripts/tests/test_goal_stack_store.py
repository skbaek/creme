from __future__ import annotations

import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from creme import goal_stack, goal_stack_store as store, master_runtime as runtime


def writer(root):
    return runtime.RecordWriter(
        root, renew=lambda: (True, 'fixture authority'),
        lease_snapshot=lambda: {'schema_version': 4, 'lease': {'client': 'codex', 'lease_id': 'a' * 32}},
    )


def entry(identity='work'):
    return {'id': identity, 'title': identity, 'status': 'ready', 'done': 'Inspect completion evidence.'}


def push_process(root, index, start, results):
    root = Path(root)
    try:
        if not start.wait(10):
            raise RuntimeError('start timed out')
        store.mutate(root, 'push', {'entry': entry(f'work-{index}')}, writer=writer(root))
        results.put(None)
    except Exception as exc:
        results.put(repr(exc))


class StackStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.plans = Path(self.temp.name).resolve()
        self.root = self.plans / 'master'
        runtime.initialize_empty_record(self.root)
        self.writer = writer(self.root)

    def mutate(self, action, payload=None, **kw):
        return store.mutate(self.root, action, payload or {}, writer=self.writer, **kw)

    def initialize(self, entries=None):
        return self.mutate('init', {'stack': {'schema_version': 1, 'revision': 0, 'entries': entries or []}})

    def test_empty_roundtrip_and_last_completion(self):
        self.initialize()
        self.assertEqual(store.read(self.root)['entries'], [])
        self.mutate('push', {'entry': entry()})
        result = self.mutate('complete', {'id': 'work', 'evidence': 'report passed'})
        self.assertEqual(store.read(self.root)['entries'], [])
        self.assertEqual(result['archive']['entry'], entry())
        receipts = sorted((self.plans / 'archive/goal-stack/transitions').glob('*.json'))
        self.assertEqual(len(receipts), 3)
        terminal = json.loads(receipts[-1].read_text())
        self.assertEqual(terminal['archive']['evidence'], 'report passed')
        self.assertEqual(terminal['revision'], 2)
        self.assertTrue(store.adopted(runtime.read_record(self.root).events))

    def test_renewal_refusal_writes_nothing(self):
        denied = runtime.RecordWriter(self.root, renew=lambda: (False, 'foreign master'))
        with self.assertRaises(runtime.RenewalRefused):
            store.mutate(self.root, 'init', {'stack': {}}, writer=denied)
        self.assertFalse((self.plans / store.NAME).exists())
        self.assertFalse((self.plans / store.PENDING).exists())

    def test_authority_rechecked_inside_lock(self):
        calls = []
        def renew():
            calls.append(1)
            return (len(calls) == 1, 'lease changed')
        denied = runtime.RecordWriter(self.root, renew=renew,
                                     lease_snapshot=self.writer.lease_snapshot)
        with self.assertRaises(runtime.RenewalRefused):
            store.mutate(self.root, 'init', {'stack': {}}, writer=denied)
        self.assertEqual(len(calls), 2)
        self.assertFalse((self.plans / store.NAME).exists())

    def test_revision_compare_and_swap(self):
        self.initialize()
        self.mutate('push', {'entry': entry()}, expected_revision=0)
        before = (self.plans / store.NAME).read_bytes()
        with self.assertRaisesRegex(store.StackStoreError, 'revision changed'):
            self.mutate('push', {'entry': entry('other')}, expected_revision=0)
        self.assertEqual((self.plans / store.NAME).read_bytes(), before)
        for invalid in [True, -1, '1']:
            with self.assertRaises(store.StackStoreError):
                self.mutate('move', {'id': 'work', 'position': 1}, expected_revision=invalid)

    def test_no_post_adoption_bootstrap_or_revision_reset(self):
        self.initialize()
        self.mutate('push', {'entry': entry()})
        self.mutate('complete', {'id': 'work', 'evidence': 'done'})
        with self.assertRaisesRegex(store.StackStoreError, 'unsupported'):
            self.mutate('bootstrap')
        self.assertEqual(store.read(self.root)['revision'], 2)

    def test_failed_validation_leaves_no_transaction(self):
        self.initialize()
        before = (self.plans / store.NAME).read_bytes()
        with self.assertRaises(goal_stack.GoalStackError):
            self.mutate('push', {'entry': {**entry(), 'goal': 'missing.md'}})
        self.assertEqual((self.plans / store.NAME).read_bytes(), before)
        self.assertFalse((self.plans / store.PENDING).exists())

    def test_retirement_never_satisfies_dependencies(self):
        self.initialize([entry(), {**entry('child'), 'depends_on': ['work']}])
        with self.assertRaises(goal_stack.GoalStackError):
            self.mutate('retire', {'id': 'work', 'reason': 'obsolete'})
        self.mutate('complete', {'id': 'work', 'evidence': 'proof reviewed'})
        self.assertEqual(goal_stack.select_next(store.read(self.root))['id'], 'child')

    def test_missing_and_corrupt_manifest_never_fall_back(self):
        self.initialize()
        path = self.plans / store.NAME
        path.write_text('garbage')
        with self.assertRaises(store.StackStoreError):
            store.read(self.root)
        path.unlink()
        self.assertTrue(store.enabled(self.root, runtime.read_record(self.root).events))
        with self.assertRaises(store.StackStoreError):
            store.read(self.root)
        with self.assertRaisesRegex(store.StackStoreError, 'already initialized'):
            self.initialize()

    def test_failed_recovery_cannot_record_adoption(self):
        with mock.patch.object(store, '_record_adoption', side_effect=InterruptedError('crash before event')):
            with self.assertRaises(InterruptedError):
                self.initialize()
        before = runtime.read_record(self.root).log_bytes
        (self.plans / store.NAME).write_text('invalid file')
        with self.assertRaises(store.StackStoreError):
            self.mutate('recover')
        self.assertEqual(runtime.read_record(self.root).log_bytes, before)
        self.assertFalse(store.adopted(runtime.read_record(self.root).events))

    def test_recovery_refuses_unregistered_valid_file_without_mutation(self):
        raw = goal_stack.dumps({'schema_version': 1, 'revision': 0, 'entries': []})
        (self.plans / store.NAME).write_text(raw)
        for action, payload in [('recover', {}), ('push', {'entry': entry()})]:
            with self.assertRaisesRegex(store.StackStoreError, 'unregistered'):
                self.mutate(action, payload)
        self.assertFalse(store.adopted(runtime.read_record(self.root).events))
        self.assertEqual((self.plans / store.NAME).read_text(), raw)

    def test_initial_commit_before_adoption_event_recovers(self):
        with mock.patch.object(store, '_record_adoption', side_effect=InterruptedError('crash before event')):
            with self.assertRaises(InterruptedError):
                self.initialize([entry()])
        self.assertFalse(store.adopted(runtime.read_record(self.root).events))
        self.mutate('recover')
        self.assertTrue(store.adopted(runtime.read_record(self.root).events))
        self.assertEqual(store.read(self.root)['entries'], [entry()])

    def test_atomic_publication_faults_recover_exactly(self):
        # Every injected interruption is between actual publication operations.
        stages = ['before-temp-create', 'after-temp-create', 'after-write', 'after-flush',
                  'after-fsync', 'before-replace', 'after-replace', 'before-dir-fsync', 'after-dir-fsync']
        labels = [store.PENDING, 'archive/goal-stack/transitions/00000001.json', store.NAME]
        boundaries = [f'{label}:{stage}' for label in labels for stage in stages]
        boundaries += ['stack:before-clear', 'stack:after-clear']
        for boundary in boundaries:
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temporary:
                plans = Path(temporary).resolve()
                root = plans / 'master'
                runtime.initialize_empty_record(root)
                w = writer(root)
                store.mutate(root, 'init', {'stack': {'schema_version': 1, 'revision': 0, 'entries': []}}, writer=w)
                def fault(stage):
                    if stage == boundary:
                        raise InterruptedError(stage)
                with self.assertRaises((InterruptedError, runtime.MasterRecordError)):
                    store.mutate(root, 'push', {'entry': entry()}, writer=w, fault=fault)
                if (plans / store.PENDING).exists():
                    with self.assertRaisesRegex(store.StackStoreError, 'unfinished'):
                        store.read(root)
                store.mutate(root, 'recover', {}, writer=w)
                observed = store.read(root)
                committed = not (boundary.startswith(store.PENDING + ':')
                                 and boundary.rsplit(':', 1)[1] in stages[:6])
                self.assertEqual(observed['revision'], 1 if committed else 0)
                self.assertEqual(observed['entries'], [entry()] if committed else [])
                again = store.mutate(root, 'recover', {}, writer=w)
                self.assertFalse(again['recovered'])
                self.assertEqual(len(list((plans / 'archive/goal-stack/transitions').glob('*.json'))), 2 if committed else 1)

    def leave_pending(self):
        self.initialize()
        def fault(stage):
            if stage == f'{store.NAME}:before-replace':
                raise InterruptedError(stage)
        with self.assertRaises(runtime.MasterRecordError):
            self.mutate('push', {'entry': entry()}, fault=fault)

    def test_conflicting_receipt_never_overwritten(self):
        self.leave_pending()
        receipt = self.plans / 'archive/goal-stack/transitions/00000001.json'
        receipt.write_text('conflict')
        with self.assertRaisesRegex(store.StackStoreError, 'receipt conflicts'):
            self.mutate('recover')
        self.assertEqual(receipt.read_text(), 'conflict')
        self.assertTrue((self.plans / store.PENDING).exists())

    def test_pending_snapshot_tampering_refuses(self):
        self.leave_pending()
        path = self.plans / store.PENDING
        data = json.loads(path.read_text())
        data['after'] = data['after'].replace('revision = 1', 'revision = 2')
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(store.StackStoreError, 'not bound'):
            self.mutate('recover')

    def test_external_manifest_change_during_pending_refuses(self):
        self.leave_pending()
        (self.plans / store.NAME).write_text(goal_stack.dumps({'schema_version': 1, 'revision': 3, 'entries': []}))
        with self.assertRaisesRegex(store.StackStoreError, 'moved outside'):
            self.mutate('recover')

    def test_symlink_archive_refuses_without_writing_target(self):
        self.initialize()
        with tempfile.TemporaryDirectory() as target:
            archive = self.plans / 'archive/goal-stack/transitions'
            for p in archive.iterdir():
                p.unlink()
            archive.rmdir()
            archive.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(store.StackStoreError, 'unsafe'):
                self.mutate('push', {'entry': entry()})
            self.assertEqual(list(Path(target).iterdir()), [])

    def test_transaction_size_checked_before_publication(self):
        self.initialize()
        before = (self.plans / store.NAME).read_bytes()
        with mock.patch.object(store, 'MAX_BYTES', 500):
            with self.assertRaisesRegex(store.StackStoreError, 'exceeds'):
                self.mutate('push', {'entry': {**entry(), 'done': 'x' * 1000}})
        self.assertEqual((self.plans / store.NAME).read_bytes(), before)
        self.assertFalse((self.plans / store.PENDING).exists())

    def test_concurrent_process_writes_preserve_all_entries(self):
        self.initialize()
        ctx = multiprocessing.get_context('spawn')
        start, results = ctx.Event(), ctx.Queue()
        processes = [ctx.Process(target=push_process, args=(str(self.root), n, start, results)) for n in range(4)]
        for p in processes:
            p.start()
        start.set()
        for p in processes:
            p.join(20)
            self.assertEqual(p.exitcode, 0)
        self.assertEqual([results.get(timeout=2) for _ in processes], [None] * 4)
        current = store.read(self.root)
        self.assertEqual(current['revision'], 4)
        self.assertEqual({e['id'] for e in current['entries']}, {f'work-{n}' for n in range(4)})

class PortableManifestTests(unittest.TestCase):
    def test_generated_toml_agrees_with_standard_reader_when_available(self):
        try:
            import tomllib
        except ImportError:
            self.skipTest('standard TOML reader only available on Python 3.11+')
        with tempfile.TemporaryDirectory() as path:
            for entries in [[], [{**entry(), 'done': 'Unicode α, newline\nquote " and backslash \\', 'depends_on': []}]]:
                data = {'schema_version': 1, 'revision': 4, 'entries': entries}
                text = goal_stack.dumps(data)
                self.assertEqual(tomllib.loads(text), data)
                self.assertEqual(goal_stack.loads(text, Path(path)), data)

    def test_portable_parser_rejects_duplicate_keys_and_table_conflicts(self):
        for source in [
            'schema_version = 1\nschema_version = 1\n',
            'entries = []\n[[entries]]\n',
            '[[entries]]\nid = "a"\nid = "b"\n',
            '[unreviewed]\nvalue = 1\n',
            'revision = 1 trailing\n',
        ]:
            with self.subTest(source=source), self.assertRaises(goal_stack.GoalStackError):
                goal_stack.parse(source)

    def test_comments_and_hash_inside_text(self):
        text = '# comment\nschema_version = 1 # schema\nrevision = 0\n[[entries]] # item\nid = "one"\ntitle = "text # still text"\nstatus = "ready"\ndone = "done"\n'
        with tempfile.TemporaryDirectory() as root:
            stack = goal_stack.loads(text, Path(root))
        self.assertEqual(stack['entries'][0]['title'], 'text # still text')


if __name__ == '__main__':
    unittest.main()
