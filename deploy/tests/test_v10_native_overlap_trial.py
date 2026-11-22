from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import test_v10_native_normalization as B
import test_v10_native_oversized_block_repair as F
import v10_native_normalization as N
import v10_native_row_chunk_repair as C
import v10_native_overlap_trial as O


class OverlapTests(unittest.TestCase):
    write_binding = F.RepairTests.write_binding
    prepare_context = F.RepairTests.prepare_context
    database = B.NativeTests.database
    run_context = B.NativeTests.run_context

    def setUp(self):
        F.RepairTests.setUp(self)
        with self.prepare_context():
            C.prepare(self.api, ROOT, self.path, 9179901)
        self.state = N.load(self.path)
        self.state.update(phase='paused', reason='operator_requested')
        N.R.update_state(self.path, self.state)
        self.clock = datetime(2026, 11, 22, 9, tzinfo=timezone.utc)
        patch.object(O, 'now_utc', side_effect=lambda: self.clock).start()
        self.db = self.database(9179901)
        self.db['dead_rows'] = 15000000
        self.db['autovacuum_running'] = 1

    def trial_context(self):
        stack = self.prepare_context()
        stack.enter_context(patch.object(N, 'rpc', return_value=self.db))
        return stack

    def prepare_trial(self):
        with self.trial_context():
            result = O.prepare(self.api, ROOT, self.path)
        self.state = N.load(self.path)
        return result, O.load_trial(self.path, self.state)

    def test_preparation_preserves_frozen_inputs_worker_and_cursor_and_cannot_renew(self):
        before_inputs = (self.directory / 'inputs.json').read_bytes()
        before_original = (self.directory / 'native.exs').read_bytes()
        before_repair = deepcopy(self.state['worker_repair'])
        before_worker = (self.directory / before_repair['script_file']).read_bytes()
        result, trial = self.prepare_trial()
        self.assertFalse(result['database_writes'])
        self.assertEqual(result['overlap_trial']['dead_row_pause_threshold'], 20000000)
        self.assertEqual(trial['dead_rows_at_start'], 15000000)
        before_file = self.state['overlap_trial_file']
        self.clock += timedelta(minutes=31)
        with self.trial_context():
            repeated = O.prepare(self.api, ROOT, self.path)
        saved = N.load(self.path)
        self.assertEqual(repeated['overlap_trial']['status'], 'expired')
        self.assertEqual(saved['overlap_trial_file'], before_file)
        self.assertEqual(saved['database'], self.state['database'])
        self.assertEqual(saved['worker_repair'], before_repair)
        self.assertEqual((self.directory / before_repair['script_file']).read_bytes(), before_worker)
        self.assertEqual((self.directory / 'inputs.json').read_bytes(), before_inputs)
        self.assertEqual((self.directory / 'native.exs').read_bytes(), before_original)

    def test_active_cap_and_expiry_restore_default_without_resetting_cursor(self):
        _, trial = self.prepare_trial()
        state = deepcopy(self.state)
        free = trial['free_bytes_at_start']
        self.assertEqual(N.gate({}, free, self.db, True), 'waiting_for_autovacuum')
        self.assertIsNone(N.gate(state, free, self.db, True, trial))
        self.assertEqual(N.gate(state, free, {**self.db, 'dead_rows': 20000001}, True, trial),
                         'waiting_for_autovacuum')
        self.clock += timedelta(seconds=1800)
        self.assertEqual(N.gate(state, free, self.db, True, trial), 'waiting_for_autovacuum')
        self.assertEqual(state['overlap_trial_ended_reason'], 'expired')
        self.clock -= timedelta(minutes=1)  # A clock correction must not reactivate a latched trial.
        self.assertEqual(N.gate(state, free, self.db, True, trial), 'waiting_for_autovacuum')
        self.assertEqual(state['database'], self.state['database'])

    def test_receipt_written_before_interrupted_state_save_can_be_reused(self):
        before = self.path.read_bytes()
        with self.trial_context(), patch.object(N, 'save', side_effect=OSError('synthetic interruption')):
            with self.assertRaises(OSError):
                O.prepare(self.api, ROOT, self.path)
        self.assertEqual(self.path.read_bytes(), before)
        receipts = list(self.directory.glob('native-overlap-trial-*.json'))
        self.assertEqual(len(receipts), 1)
        _, trial = self.prepare_trial()
        self.assertEqual(self.directory / self.state['overlap_trial_file'], receipts[0])
        self.assertEqual(trial['prepared_at_utc'], self.clock.isoformat())

    def test_space_budget_latches_default_even_if_free_space_recovers(self):
        _, trial = self.prepare_trial()
        state = deepcopy(self.state)
        free = trial['free_bytes_at_start']
        self.assertIsNone(N.gate(state, free - O.LIMITS['max_free_drop_bytes'] + 1, self.db, True, trial))
        self.assertEqual(N.gate(state, free - O.LIMITS['max_free_drop_bytes'], self.db, True, trial),
                         'waiting_for_autovacuum')
        self.assertEqual(state['overlap_trial_ended_reason'], 'space_budget')
        self.assertEqual(N.gate(state, free, self.db, True, trial), 'waiting_for_autovacuum')

    def test_other_safety_gates_remain_in_force_during_trial(self):
        _, trial = self.prepare_trial()
        free = trial['free_bytes_at_start']
        self.assertEqual(N.gate({}, free, self.db, False, trial), 'application_health')
        self.assertEqual(N.gate({}, free, {**self.db, 'long_client_transactions': 1}, True, trial), 'long_transaction')
        self.assertEqual(N.gate({}, free, {**self.db, 'autovacuum_enabled': False}, True, trial), 'autovacuum_disabled')
        self.assertEqual(N.gate({}, 209 * 1024**3, self.db, True, trial), 'low_space')

    def test_no_trial_is_created_without_an_active_vacuum_or_original_pause_condition(self):
        before = self.path.read_bytes()
        for mutation in ({'autovacuum_running': 0}, {'dead_rows': 10000000}):
            with self.subTest(mutation=mutation), self.trial_context(), \
                 patch.object(N, 'rpc', return_value={**self.db, **mutation}):
                result = O.prepare(self.api, ROOT, self.path)
            self.assertEqual(result['status'], 'native_overlap_trial_not_needed_now')
            self.assertEqual(result['dead_row_pause_threshold'], 10000000)
            self.assertEqual(self.path.read_bytes(), before)
            self.assertFalse(list(self.directory.glob('native-overlap-trial-*.json')))

    def test_preparation_rejects_running_worker_bad_space_or_checkpoint(self):
        before = self.path.read_bytes()
        with self.trial_context():
            with patch.object(N, 'owned', return_value={'State': {'Running': True}}):
                with self.assertRaisesRegex(N.U.UpgradeError, 'must be stopped'):
                    O.prepare(self.api, ROOT, self.path)
            with patch.object(N.J.P, 'free_bytes', return_value=399 * 1024**3):
                with self.assertRaisesRegex(N.U.UpgradeError, '400 GiB'):
                    O.prepare(self.api, ROOT, self.path)
            for mutation in ({'dead_rows': 20000001}, {'autovacuum_enabled': False},
                             {'native_meta': {**self.db['native_meta'], 'max_block_number': 9179900}}):
                with self.subTest(mutation=mutation), patch.object(N, 'rpc', return_value={**self.db, **mutation}):
                    with self.assertRaises(N.U.UpgradeError):
                        O.prepare(self.api, ROOT, self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(list(self.directory.glob('native-overlap-trial-*.json')))

    def test_hashed_record_rejects_wider_caps_longer_duration_or_wrong_job(self):
        _, trial = self.prepare_trial()
        saved = deepcopy(self.state)
        for mutation in ({'limits': {**O.LIMITS, 'max_dead_rows': 30000000}},
                         {'limits': {**O.LIMITS, 'max_dead_rows': 20000000.0}},
                         {'native_inputs_sha256': '0' * 64},
                         {'expires_at_utc': (self.clock + timedelta(hours=2)).isoformat()},
                         {'free_bytes_at_start': 200 * 1024**3}):
            changed = {**trial, **mutation}
            data = N.R.encoded(changed)
            digest = N.R.digest(data)
            filename = f'native-overlap-trial-{digest[:12]}.json'
            N.R.write_new(self.directory / filename, data)
            state = {**saved, 'overlap_trial_file': filename, 'overlap_trial_sha256': digest}
            N.R.update_state(self.path, state)
            with self.subTest(mutation=mutation), self.assertRaises(N.U.UpgradeError):
                N.load(self.path)

    def test_stop_request_is_idempotent_and_does_not_overwrite_running_state(self):
        _, trial = self.prepare_trial()
        self.state['phase'] = 'running'
        N.R.update_state(self.path, self.state)
        before = self.path.read_bytes()
        with patch.object(N, 'require_service'):
            for _ in range(2):
                self.assertEqual(O.request_end(self.path)['status'], 'native_overlap_trial_stop_requested')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(O.stop_requested(self.path, self.state))
        N.R.update_state(self.directory / O.STOP_FILE, {'wrong': 'job'})
        with self.assertRaisesRegex(N.U.UpgradeError, 'stop request changed'):
            O.stop_requested(self.path, self.state)

    def test_controller_grants_overlap_then_withdraws_on_expiry_preserving_progress(self):
        _, trial = self.prepare_trial()
        calls = []
        def sleeping(_):
            calls.append(True)
            if len(calls) == 1:
                self.clock += timedelta(seconds=1800)
            else:
                N.STOP_REQUESTED = True
        next_db = deepcopy(self.db)
        next_db['native_meta']['max_block_number'] -= 1
        with self.run_context(), patch.object(N, 'current_runtime_health', return_value={'status': 'verified'}), \
             patch.object(N, 'rpc', side_effect=[self.db, next_db]), \
             patch.object(N.J.P, 'free_bytes', return_value=trial['free_bytes_at_start']), \
             patch.object(N, 'lease') as leases, patch.object(N, 'stop_client'), \
             patch.object(N.time, 'sleep', side_effect=sleeping):
            result = N.run(self.api, ROOT, self.path)
        self.assertEqual([c.args[2] for c in leases.call_args_list], [True, False])
        self.assertEqual(result['native_cursor'], 9179900)
        self.assertEqual(result['overlap_trial']['dead_row_pause_threshold'], 10000000)
        self.assertEqual(result['overlap_trial']['status'], 'expired')

    def test_lease_is_clamped_to_trial_deadline(self):
        _, trial = self.prepare_trial()
        self.clock += timedelta(seconds=1790)
        def sleeping(_):
            N.STOP_REQUESTED = True
        with self.run_context(), patch.object(N, 'current_runtime_health', return_value={'status': 'verified'}), \
             patch.object(N, 'rpc', return_value=self.db), \
             patch.object(N.J.P, 'free_bytes', return_value=trial['free_bytes_at_start']), \
             patch.object(N, 'lease') as leases, patch.object(N, 'stop_client'), \
             patch.object(N.time, 'sleep', side_effect=sleeping):
            N.run(self.api, ROOT, self.path)
        self.assertTrue(leases.call_args.args[2])
        self.assertLessEqual(leases.call_args.args[4], 10000)

    def test_expiry_between_gate_and_authorization_does_not_grant_a_lease(self):
        _, trial = self.prepare_trial()
        def sleeping(_):
            N.STOP_REQUESTED = True
        with self.run_context(), patch.object(N, 'current_runtime_health', return_value={'status': 'verified'}), \
             patch.object(N, 'rpc', return_value=self.db), \
             patch.object(N.J.P, 'free_bytes', return_value=trial['free_bytes_at_start']), \
             patch.object(O, 'remaining_ms', return_value=0), \
             patch.object(N, 'lease') as leases, patch.object(N, 'stop_client'), \
             patch.object(N.time, 'sleep', side_effect=sleeping):
            result = N.run(self.api, ROOT, self.path)
        self.assertTrue(leases.called)
        self.assertTrue(all(not c.args[2] for c in leases.call_args_list))
        self.assertEqual(result['overlap_trial']['status'], 'expired')

    def test_controller_honors_end_request_without_reactivating_the_trial(self):
        _, trial = self.prepare_trial()
        with patch.object(N, 'require_service'):
            O.request_end(self.path)
        def sleeping(_):
            N.STOP_REQUESTED = True
        with self.run_context(), patch.object(N, 'current_runtime_health', return_value={'status': 'verified'}), \
             patch.object(N, 'rpc', return_value=self.db), \
             patch.object(N.J.P, 'free_bytes', return_value=trial['free_bytes_at_start']), \
             patch.object(N, 'lease') as leases, patch.object(N, 'stop_client'), \
             patch.object(N.time, 'sleep', side_effect=sleeping):
            result = N.run(self.api, ROOT, self.path)
        self.assertTrue(leases.called)
        self.assertTrue(all(not c.args[2] for c in leases.call_args_list))
        self.assertEqual(result['overlap_trial']['status'], 'operator_ended')
        self.assertEqual(result['overlap_trial']['dead_row_pause_threshold'], 10000000)


if __name__ == '__main__':
    unittest.main()
