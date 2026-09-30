"""Lifecycle regressions reproduced by independent review; finite controls."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from creme import model_fit_runtime as R, model_fit_episodes as E
from scripts.tests.test_model_fit_runtime import (RuntimeTest as Fixture, base_config, launch_receipt,
                                                run_receipt, accept_receipt, LOW)


class JoinControls(unittest.TestCase):
    setUp = Fixture.setUp
    tearDown = Fixture.tearDown
    configure = Fixture.configure
    prepare = Fixture.prepare
    candidate = Fixture.candidate
    drive_episode = Fixture.drive_episode
    def test_new_envelope_for_same_launch_does_not_destroy_ready_prefix(self):
        config = self.configure()
        self.drive_episode('a', 'r')
        self.assertEqual(R.submit(self.store, launch_receipt('a', 'r', LOW, receipt_id='another-envelope'))['status'], 'applied')
        self.assertEqual(R.statistics(self.store, config, self.candidate(config)).n, 1)
        path = self.store.path
        self.store.close()
        self.store = E.open_store(path)
        R.reconcile(self.store)
        self.assertEqual(R.statistics(self.store, config, self.candidate(config)).n, 1)

    def test_incomplete_revision_then_final_revision_restores_join(self):
        config = self.configure()
        self.drive_episode('a', 'r')
        R.submit(self.store, run_receipt('a','r',LOW, receipt_id='late-gap', final=False))
        self.assertEqual(R.statistics(self.store, config, self.candidate(config)).n, 0)
        R.submit(self.store, run_receipt('a','r',LOW, receipt_id='resolved-gap'))
        self.assertEqual(R.statistics(self.store, config, self.candidate(config)).n, 1)
        self.assertEqual(E.episode_accounting(self.store,'a')['spend_uncapped_tokens'],110)

    def test_pending_invalid_input_is_replaced_explicitly_without_erasing_it(self):
        self.configure()
        self.prepare('a')
        R.submit(self.store, run_receipt('a','r',LOW))
        invalid = accept_receipt('a', milestones=['not-declared'], receipt_id='bad')
        self.assertEqual(R.submit(self.store, invalid)['status'], 'pending')
        fixed = accept_receipt('a', receipt_id='corrected', supersedes='bad')
        self.assertEqual(R.submit(self.store, fixed)['status'], 'applied')
        self.assertEqual(E.get_episode(self.store,'a')['status'], 'closed')
        self.assertEqual(self.store.conn.execute("SELECT status FROM fit_inbox WHERE receipt_id='bad'").fetchone()[0], 'superseded')
        self.assertEqual(json.loads(self.store.conn.execute("SELECT payload FROM fit_inbox WHERE receipt_id='bad'").fetchone()[0]), invalid)

    def test_old_gaps_do_not_starve_later_complete_joins(self):
        self.configure()
        # Using the public API also demonstrates more than the old 32 pending cap.
        for i in range(103):
            self.prepare(f'gap{i}')
            R.submit(self.store, accept_receipt(f'gap{i}'))
        self.drive_episode('later','later-run')
        for _ in range(3):
            R.reconcile(self.store)
        self.assertEqual(E.get_episode(self.store,'later')['status'], 'closed')
        self.assertEqual(self.store.conn.execute('SELECT COUNT(*) FROM fit_opportunities').fetchone()[0],104)

    def test_correction_removes_credit_but_not_cost(self):
        config = self.configure()
        self.drive_episode('a','r')
        correction = accept_receipt('a', receipt_id='rejected', verdict='fail', correction_of='a:accept')
        correction['milestones'] = []
        self.assertEqual(R.submit(self.store,correction)['status'],'applied')
        moments=R.statistics(self.store,config,self.candidate(config))
        self.assertEqual((moments.n,moments.work,moments.tokens),(1,0,110))

    def test_duplicate_preparation_does_not_move_clock(self):
        self.configure()
        a=self.prepare('a')
        for _ in range(12): self.assertEqual(self.prepare('a'),a)
        self.assertEqual(self.prepare('b')['opportunity'],2)

    def test_mixed_windows_cannot_be_charged_as_worker_and_master(self):
        self.configure()
        self.prepare('a')
        window={'total_input':50,'total_output':10,'source':'source','start_offset':1,'end_offset':10}
        receipt=run_receipt('a','r',LOW,segments=[{'id':'window','usage':window}])
        R.submit(self.store,receipt)
        master=accept_receipt('a',segment_usage=window)
        self.assertIn('overlaps',R.submit(self.store,master)['error'])
        duplicate=run_receipt('a','r',LOW,receipt_id='bigger',segments=[{'id':'other-key','usage':{**window,'end_offset':20}}])
        self.assertIn('overlaps',R.submit(self.store,duplicate)['error'])
        self.assertEqual(E.episode_accounting(self.store,'a')['spend_uncapped_tokens'],60)

    def test_unknown_base_cannot_be_moved_back_by_late_snapshot(self):
        self.configure()
        self.prepare('a')
        E.record_cumulative_delta(self.store,'unknown','a',{},'source',10)
        E.record_cumulative_delta(self.store,'late','a',{'total_input':10,'total_output':2},'source',5)
        row=self.store.conn.execute('SELECT last_sequence,last_total FROM cumulative_sources').fetchone()
        self.assertEqual(tuple(row),(10,'null'))

    def test_invalid_credit_and_reservation_numbers_refuse(self):
        self.configure()
        for value in [True, '0.5', float('nan'), float('inf'), -1]:
            with self.assertRaises((ValueError,E.EpisodeError)):
                self.prepare('bad',credits=[{'milestone':'done','credit':value}])
        self.prepare('a')
        for value in [True,'10',float('nan'),-1]:
            with self.assertRaises(E.EpisodeError): E.reserve(self.store,'bad','a',value)

    def test_roll_back_selection_preserves_pending_and_cost(self):
        self.configure()
        for i in range(10): self.prepare(str(i))
        before=R.table(self.store,'pol-1')['budget']
        R.set_mode(self.store,'pol-1','off','rollback','rollback-1')
        after=R.table(self.store,'pol-1')['budget']
        self.assertEqual(before,after)
        self.assertFalse(self.prepare('11')['actual_exploration'])


# Imported base class is just the shared test fixture; unittest also discovers
# its cases, so remove that symbol after defining the derived class.
del Fixture
if __name__ == '__main__': unittest.main()
