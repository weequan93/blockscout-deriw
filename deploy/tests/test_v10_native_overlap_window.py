from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import test_v10_native_overlap_trial as T
import v10_native_normalization as N
import v10_native_overlap_trial as O
import v10_native_overlap_window as W


class WindowTests(unittest.TestCase):
    write_binding = T.OverlapTests.write_binding
    prepare_context = T.OverlapTests.prepare_context
    database = T.OverlapTests.database
    run_context = T.OverlapTests.run_context
    trial_context = T.OverlapTests.trial_context

    def setUp(self):
        T.OverlapTests.setUp(self)
        T.OverlapTests.prepare_trial(self)
        self.clock += timedelta(days=2)
        self.state.update(phase='paused', reason='operator_requested', overlap_trial_ended_reason='expired')
        N.R.update_state(self.path, self.state)

    def prepare_window(self):
        with self.trial_context():
            result = W.prepare(self.api, ROOT, self.path)
        self.state = N.load(self.path)
        return result, W.load_window(self.path, self.state)

    def run_once(self, free=None):
        def sleeping(_):
            N.STOP_REQUESTED = True
        with self.run_context(), patch.object(N, 'STOP_REQUESTED', False), \
             patch.object(N, 'current_runtime_health', return_value={'status': 'verified'}), \
             patch.object(N, 'rpc', return_value=self.db), \
             patch.object(N.J.P, 'free_bytes', return_value=free or 800 * 1024**3), \
             patch.object(N, 'lease') as leases, patch.object(N, 'stop_client'), \
             patch.object(N.time, 'sleep', side_effect=sleeping):
            result = N.run(self.api, ROOT, self.path)
        return result, leases

    def test_preparation_preserves_short_trial_frozen_worker_inputs_and_cursor(self):
        frozen = {name: (self.directory / name).read_bytes() for name in (
            'inputs.json', 'native.exs', self.state['worker_repair']['script_file'],
            self.state['overlap_trial_file'])}
        before_db = deepcopy(self.state['database'])
        result, window = self.prepare_window()
        self.assertFalse(result['database_writes'])
        self.assertEqual(window['limits']['duration_seconds'], 14400)
        self.assertEqual(window['previous_trial_sha256'], self.state['overlap_trial_sha256'])
        self.assertEqual(self.state['database'], before_db)
        self.assertEqual(self.state['overlap_trial_ended_reason'], 'expired')
        for name, data in frozen.items():
            self.assertEqual((self.directory / name).read_bytes(), data)
        public = N.public(self.path, self.state)
        self.assertEqual(public['overlap_trial']['status'], 'expired')
        self.assertEqual(public['overlap_window']['status'], 'active')
        self.assertEqual(public['effective_dead_row_pause_threshold'], 20000000)

    def test_preparation_never_extends_or_replaces_a_window(self):
        _, window = self.prepare_window()
        self.clock += timedelta(hours=5)
        self.state.update(phase='paused')
        N.R.update_state(self.path, self.state)
        before = self.path.read_bytes()
        with self.trial_context():
            result = W.prepare(self.api, ROOT, self.path)
        self.assertEqual(result['status'], 'native_overlap_window_already_prepared')
        self.assertEqual(result['overlap_window']['status'], 'expired')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(W.load_window(self.path, N.load(self.path)), window)

    def test_a_followup_requires_the_original_latched_end(self):
        self.state.pop('overlap_trial_ended_reason')
        N.R.update_state(self.path, self.state)
        before = self.path.read_bytes()
        with self.trial_context(), self.assertRaisesRegex(N.U.UpgradeError, 'latched end'):
            W.prepare(self.api, ROOT, self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(list(self.directory.glob('native-overlap-window-*.json')))

    def test_preparation_rejects_live_worker_low_space_and_changed_checkpoint(self):
        before = self.path.read_bytes()
        with self.trial_context():
            with patch.object(N, 'owned', return_value={'State': {'Running': True}}), \
                 self.assertRaisesRegex(N.U.UpgradeError, 'must be stopped'):
                W.prepare(self.api, ROOT, self.path)
            with patch.object(N.J.P, 'free_bytes', return_value=399 * 1024**3), \
                 self.assertRaisesRegex(N.U.UpgradeError, '400 GiB'):
                W.prepare(self.api, ROOT, self.path)
            for mutation in ({'dead_rows': 20000001}, {'autovacuum_enabled': False},
                             {'native_meta': {**self.db['native_meta'], 'max_block_number': 9179900}}):
                with self.subTest(mutation=mutation), patch.object(N, 'rpc', return_value={**self.db, **mutation}), \
                     self.assertRaises(N.U.UpgradeError):
                    W.prepare(self.api, ROOT, self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_preparation_rejects_clock_before_parent_without_writing_a_receipt(self):
        self.clock -= timedelta(days=3)
        before = self.path.read_bytes()
        with self.trial_context(), self.assertRaisesRegex(N.U.UpgradeError, 'Clock precedes'):
            W.prepare(self.api, ROOT, self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(list(self.directory.glob('native-overlap-window-*.json')))

    def test_no_window_is_created_unless_the_original_pause_and_active_vacuum_apply(self):
        before = self.path.read_bytes()
        for mutation in ({'autovacuum_running': 0}, {'dead_rows': 10000000}):
            with self.subTest(mutation=mutation), self.trial_context(), \
                 patch.object(N, 'rpc', return_value={**self.db, **mutation}):
                result = W.prepare(self.api, ROOT, self.path)
            self.assertEqual(result['status'], 'native_overlap_window_not_needed_now')
            self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(list(self.directory.glob('native-overlap-window-*.json')))

    def test_gate_allows_twenty_million_and_preserves_all_other_protections(self):
        _, window = self.prepare_window()
        free = window['free_bytes_at_start']
        self.assertEqual(N.gate({}, free, self.db, True), 'waiting_for_autovacuum')
        self.assertIsNone(N.gate({}, free, self.db, True, window=window))
        self.assertIsNone(N.gate({}, free, {**self.db, 'dead_rows': 20000000}, True, window=window))
        self.assertEqual(N.gate({}, free, {**self.db, 'dead_rows': 20000001}, True, window=window),
                         'waiting_for_autovacuum')
        self.assertEqual(N.gate({}, free, self.db, False, window=window), 'application_health')
        self.assertEqual(N.gate({}, free, {**self.db, 'long_client_transactions': 1}, True, window=window),
                         'long_transaction')
        self.assertEqual(N.gate({}, free, {**self.db, 'autovacuum_enabled': False}, True, window=window),
                         'autovacuum_disabled')
        self.assertEqual(N.gate({}, 209 * 1024**3, self.db, True, window=window), 'low_space')

    def test_expiry_restores_default_and_never_reactivates_after_clock_correction(self):
        _, window = self.prepare_window()
        state = deepcopy(self.state)
        self.clock += timedelta(hours=4)
        self.assertEqual(N.gate(state, window['free_bytes_at_start'], self.db, True, window=window),
                         'waiting_for_autovacuum')
        self.assertEqual(state['overlap_window_ended_reason'], 'expired')
        self.clock -= timedelta(minutes=1)
        self.assertEqual(N.gate(state, window['free_bytes_at_start'], self.db, True, window=window),
                         'waiting_for_autovacuum')
        self.assertEqual(state['database'], self.state['database'])

    def test_space_budget_and_absolute_floor_latch_default_after_recovery(self):
        _, window = self.prepare_window()
        for free, expected in ((window['free_bytes_at_start'] - 16 * 1024**3, 'space_budget'),
                               (399 * 1024**3, 'free_space_floor')):
            state = deepcopy(self.state)
            with self.subTest(expected=expected):
                self.assertEqual(N.gate(state, free, self.db, True, window=window), 'waiting_for_autovacuum')
                self.assertEqual(state['overlap_window_ended_reason'], expected)
                self.assertEqual(N.gate(state, window['free_bytes_at_start'], self.db, True, window=window),
                                 'waiting_for_autovacuum')

    def test_invalid_clock_or_unavailable_free_space_ends_the_window(self):
        _, window = self.prepare_window()
        self.assertEqual(W.decision(window, None)['status'], 'free_space_floor')
        self.clock -= timedelta(seconds=1)
        state = deepcopy(self.state)
        self.assertEqual(N.gate(state, window['free_bytes_at_start'], self.db, True, window=window),
                         'waiting_for_autovacuum')
        self.assertEqual(state['overlap_window_ended_reason'], 'clock_invalid')

    def test_forged_limits_wrong_parent_or_changed_worker_are_rejected(self):
        _, window = self.prepare_window()
        for mutation in ({'limits': {**W.LIMITS, 'max_dead_rows': 30000000}},
                         {'limits': {**W.LIMITS, 'duration_seconds': 28800}},
                         {'limits': {**W.LIMITS, 'max_free_drop_bytes': 32 * 1024**3}},
                         {'limits': {**W.LIMITS, 'minimum_free_bytes': 200 * 1024**3}},
                         {'previous_trial_sha256': '0' * 64}, {'worker_sha256': '0' * 64},
                         {'native_inputs_sha256': '0' * 64},
                         {'expires_at_utc': (self.clock + timedelta(hours=5)).isoformat()}):
            data = N.R.encoded({**window, **mutation})
            digest = N.R.digest(data)
            filename = f'native-overlap-window-{digest[:12]}.json'
            N.R.write_new(self.directory / filename, data)
            state = {**self.state, 'overlap_window_file': filename, 'overlap_window_sha256': digest}
            N.R.update_state(self.path, state)
            with self.subTest(mutation=mutation), self.assertRaises(N.U.UpgradeError):
                N.load(self.path)

    def test_manual_end_does_not_overwrite_running_state_or_old_receipts(self):
        self.prepare_window()
        self.state['phase'] = 'running'
        N.R.update_state(self.path, self.state)
        before = self.path.read_bytes()
        with patch.object(N, 'require_service'):
            W.request_end(self.path)
            W.request_end(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(W.stop_requested(self.path, self.state))
        result, leases = self.run_once()
        self.assertTrue(all(not call.args[2] for call in leases.call_args_list))
        self.assertEqual(result['overlap_window']['status'], 'operator_ended')
        self.assertEqual(result['effective_dead_row_pause_threshold'], 10000000)

    def test_foreign_stop_request_fails_closed(self):
        self.prepare_window()
        N.R.write_new(self.directory / W.STOP_FILE, N.R.encoded({'wrong': 'window'}))
        with self.assertRaisesRegex(N.U.UpgradeError, 'stop request changed'):
            W.stop_requested(self.path, self.state)
        with self.assertRaisesRegex(N.U.UpgradeError, 'stop request changed'):
            self.run_once()
        self.assertEqual(N.load(self.path)['phase'], 'needs_attention')

    def test_controller_grants_then_withdraws_authorization_on_expiry(self):
        _, window = self.prepare_window()
        calls = []
        def sleeping(_):
            calls.append(True)
            if len(calls) == 1:
                self.clock += timedelta(hours=4)
            else:
                N.STOP_REQUESTED = True
        next_db = deepcopy(self.db)
        next_db['native_meta']['max_block_number'] -= 1
        with self.run_context(), patch.object(N, 'current_runtime_health', return_value={'status': 'verified'}), \
             patch.object(N, 'rpc', side_effect=[self.db, next_db]), \
             patch.object(N.J.P, 'free_bytes', return_value=window['free_bytes_at_start']), \
             patch.object(N, 'lease') as leases, patch.object(N, 'stop_client'), \
             patch.object(N.time, 'sleep', side_effect=sleeping):
            result = N.run(self.api, ROOT, self.path)
        self.assertEqual([call.args[2] for call in leases.call_args_list], [True, False])
        self.assertEqual(result['native_cursor'], 9179900)
        self.assertEqual(result['overlap_window']['status'], 'expired')

    def test_controller_withdraws_authorization_on_space_budget(self):
        _, window = self.prepare_window()
        result, leases = self.run_once(window['free_bytes_at_start'] - W.LIMITS['max_free_drop_bytes'])
        self.assertTrue(leases.called)
        self.assertTrue(all(not call.args[2] for call in leases.call_args_list))
        self.assertEqual(result['overlap_window']['status'], 'space_budget')

    def test_write_lease_cannot_outlive_the_window(self):
        self.prepare_window()
        self.clock += timedelta(seconds=14390)
        _, leases = self.run_once()
        self.assertTrue(leases.call_args.args[2])
        self.assertLessEqual(leases.call_args.args[4], 10000)

    def test_expiry_during_worker_launch_never_grants_a_write_lease(self):
        self.prepare_window()
        with patch.object(W, 'remaining_ms', return_value=0):
            result, leases = self.run_once()
        self.assertTrue(leases.called)
        self.assertTrue(all(not call.args[2] for call in leases.call_args_list))
        self.assertEqual(result['overlap_window']['status'], 'expired')

    def test_completed_native_job_ends_window_and_never_restarts(self):
        self.prepare_window()
        finished = self.database(-1, 'completed')
        with self.run_context(), patch.object(N, 'current_runtime_health', return_value={'status': 'verified'}), \
             patch.object(N, 'rpc', side_effect=[finished, {'cache_refreshed': True}]), \
             patch.object(N.J.P, 'free_bytes', return_value=800 * 1024**3), \
             patch.object(N, 'lease') as leases, patch.object(N, 'stop_client'), \
             patch.object(N, 'launch') as launch:
            result = N.run(self.api, ROOT, self.path)
        self.assertEqual(result['status'], 'native_normalization_completed')
        self.assertEqual(result['overlap_window']['status'], 'normalization_completed')
        launch.assert_not_called()
        leases.assert_not_called()

    def test_database_progress_is_public_but_query_text_and_private_fields_are_not(self):
        self.state['database'].update(dead_rows=15000000, long_client_transactions=1,
            autovacuum_running=1, autovacuum_enabled=True, private_environment='private-canary',
            vacuum_progress=[{'pid': 42, 'phase': 'vacuuming indexes', 'heap_blks_total': 100,
                              'heap_blks_scanned': 100, 'heap_blks_vacuumed': 0,
                              'query': 'private-canary', 'index_vacuum_count': 0}])
        result = N.public(self.path, self.state)
        self.assertEqual(result['database_activity']['dead_rows'], 15000000)
        self.assertEqual(result['database_activity']['vacuum_progress'][0]['pid'], 42)
        self.assertNotIn('private-canary', N.R.encoded(result).decode())


if __name__ == '__main__':
    unittest.main()
