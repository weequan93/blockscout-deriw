from copy import deepcopy
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import v10_native_normalization as N


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.addCleanup(patch.stopall)
        patch.object(N, 'BASE', self.base).start()
        patch.object(N, 'STOP_REQUESTED', False).start()
        self.directory = self.base / 'job-20261106T090000Z-1234abcd'
        self.directory.mkdir(mode=0o700)
        self.path = self.directory / 'state.json'
        script = b'frozen native worker source'
        token = 'a' * 16
        self.inputs = {
            'tool_source_commit': 'a' * 40, 'chain_id': '18417507517',
            'upgrade_state': '/private/upgrade/state.json',
            'candidate_image': 'sha256:' + 'b' * 64, 'network': 'fixture',
            'backend_environment': {'PRIVATE': 'native-private-canary'}, 'runtime_before': {},
            'source_root': str(ROOT), 'python_executable': '/usr/bin/python3',
            'min_free_bytes': N.MIN_FREE, 'pause_free_bytes': N.PAUSE_FREE,
            'resume_free_bytes': N.RESUME_FREE, 'cumulative_growth_cap_bytes': None,
            'maximum_batch_blocks': 1000, 'manual_phase_rows': 4999097,
            'maintenance_selected_rows': 11294954,
            'client_name': 'blockscout-v10-native-' + token,
            'script_sha256': N.R.digest(script),
            'payload': {'token': token, 'application_name': 'blockscout-v10-native-' + token,
                        'start_before_block': 21}}
        self.state = {'format': N.FORMAT, 'inputs': self.inputs,
                      'inputs_sha256': N.R.digest(N.R.encoded(self.inputs)),
                      'phase': 'prepared', 'batch_blocks': 1000,
                      'reason': None, 'observed_at_utc': 'fixture'}
        N.R.write_new(self.directory / 'inputs.json', N.R.encoded(self.inputs))
        N.R.write_new(self.directory / 'native.exs', script)
        N.R.write_new(self.path, N.R.encoded(self.state))
        self.api = types.SimpleNamespace(
            configuration=lambda *a: ({'chain_id': '18417507517'}, {}, {}),
            inspect_project=lambda *a: {}, ensure_stable=lambda *a: None,
            run=lambda *a, **k: '')

    def database(self, cursor=20, status='started'):
        return {'native_meta': {'v10_native_job': self.inputs['payload']['token'],
                                'max_block_number': cursor},
                'native_status': status, 'dead_rows': 0,
                'long_client_transactions': 0, 'autovacuum_enabled': True}

    def run_context(self):
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(N.B, 'clean_source', return_value=('a' * 40, '10.2.6')))
        stack.enter_context(patch.object(N.U, 'load_state', return_value={}))
        stack.enter_context(patch.object(N.U, 'verify', return_value={'status': 'verified'}))
        stack.enter_context(patch.object(N.Q, 'verify_holds'))
        stack.enter_context(patch.object(N, 'reserve_sources'))
        stack.enter_context(patch.object(N, 'launch'))
        stack.enter_context(patch.object(N, 'read_worker', return_value={'phase': 'running'}))
        stack.enter_context(patch.object(N, 'owned', return_value={'State': {'Running': True}}))
        return stack

    def test_frozen_job_has_no_cumulative_cap_and_does_not_leak_environment(self):
        state = N.load(self.path)
        result = N.public(self.path, state)
        self.assertIsNone(result['cumulative_growth_cap_bytes'])
        self.assertEqual(result['minimum_data_free_bytes'], 200 * 1024**3)
        self.assertNotIn('native-private-canary', json.dumps(result))
        self.assertFalse(result['heavy_index_and_primary_key_migrations_started'])
        self.inputs['cumulative_growth_cap_bytes'] = 32 * 1024**3
        self.state['inputs_sha256'] = N.R.digest(N.R.encoded(self.inputs))
        N.R.update_state(self.directory / 'inputs.json', self.inputs)
        N.R.update_state(self.path, self.state)
        with self.assertRaisesRegex(N.U.UpgradeError, 'policy changed'):
            N.load(self.path)

    def test_worker_source_and_batch_bounds_remain_frozen(self):
        changed = deepcopy(self.state)
        changed['batch_blocks'] = 1001
        N.R.update_state(self.path, changed)
        with self.assertRaisesRegex(N.U.UpgradeError, 'checkpoint'):
            N.load(self.path)
        N.R.update_state(self.path, self.state)
        (self.directory / 'native.exs').write_bytes(b'changed')
        with self.assertRaisesRegex(N.U.UpgradeError, 'source changed'):
            N.load(self.path)

    def test_disk_pause_has_hysteresis_and_no_original_growth_limit(self):
        db = self.database()
        state = {'reason': None}
        self.assertEqual(N.gate(state, 209 * 1024**3, db, True), 'low_space')
        self.assertIsNone(N.gate(state, 300 * 1024**3, db, True))
        state['reason'] = 'low_space'
        self.assertEqual(N.gate(state, 219 * 1024**3, db, True), 'low_space')
        self.assertIsNone(N.gate(state, 220 * 1024**3, db, True))

    def test_health_long_reads_and_cleanup_waits_are_automatic_gates(self):
        db = self.database()
        free = 800 * 1024**3
        self.assertEqual(N.gate({}, free, db, False), 'application_health')
        db['long_client_transactions'] = 1
        self.assertEqual(N.gate({}, free, db, True), 'long_transaction')
        db['long_client_transactions'] = 0
        db['dead_rows'] = 10000001
        self.assertEqual(N.gate({}, free, db, True), 'waiting_for_autovacuum')
        db['autovacuum_enabled'] = False
        self.assertEqual(N.gate({}, free, db, True), 'autovacuum_disabled')

    def test_low_space_then_added_space_resumes_without_resetting_db_cursor(self):
        calls = []
        def sleeping(_):
            calls.append(True)
            if len(calls) == 2:
                N.STOP_REQUESTED = True
        with self.run_context(), \
             patch.object(N, 'rpc', side_effect=[self.database(20), self.database(19)]), \
             patch.object(N.J.P, 'free_bytes', side_effect=[205 * 1024**3, 220 * 1024**3]), \
             patch.object(N, 'lease') as leases, patch.object(N, 'stop_client'), \
             patch.object(N.time, 'sleep', side_effect=sleeping):
            result = N.run(self.api, ROOT, self.path)
        self.assertEqual([c.args[2] for c in leases.call_args_list], [False, True])
        self.assertEqual(result['native_cursor'], 19)
        self.assertEqual(result['status'], 'native_normalization_paused')
        self.assertEqual(N.load(self.path)['inputs'], self.inputs)

    def test_unowned_database_cursor_stops_without_issuing_an_update_lease(self):
        db = self.database()
        db['native_meta']['v10_native_job'] = 'c' * 16
        with self.run_context(), patch.object(N, 'rpc', return_value=db), \
             patch.object(N.J.P, 'free_bytes', return_value=800 * 1024**3), \
             patch.object(N, 'lease') as lease, patch.object(N, 'stop_client'):
            result = N.run(self.api, ROOT, self.path)
        self.assertEqual(result['status'], 'native_normalization_needs_attention')
        lease.assert_not_called()

    def test_native_completion_refreshes_cache_and_never_restarts_a_finished_job(self):
        with self.run_context(), patch.object(N, 'rpc', side_effect=[self.database(-1, 'completed'), {'cache_refreshed': True}]), \
             patch.object(N.J.P, 'free_bytes', return_value=100 * 1024**3), \
             patch.object(N, 'lease') as lease, patch.object(N, 'stop_client') as stop, \
             patch.object(N, 'launch') as launch:
            result = N.run(self.api, ROOT, self.path)
        self.assertEqual(result['status'], 'native_normalization_completed')
        lease.assert_not_called()
        launch.assert_not_called()
        stop.assert_called_once()
        with patch.object(N.B, 'clean_source', side_effect=AssertionError('finished job restarted')):
            self.assertEqual(N.run(self.api, ROOT, self.path)['status'], 'native_normalization_completed')

    def test_partial_handoff_reservations_recover_only_for_the_same_job(self):
        for label in ('parent', 'vacuum'):
            directory = self.base / label
            directory.mkdir(mode=0o700)
            source = directory / 'state.json'
            N.R.write_new(source, label.encode())
            self.inputs[label + '_state'] = str(source)
            self.inputs[label + '_sha256'] = N.R.digest(label.encode())
            self.inputs[label + '_inputs_sha256'] = label + '-inputs'
        reserve = N.Q.reserve
        def interrupted(parent, digest, name, path, record):
            if name == 'normalization-reuse.json':
                raise OSError('fixture interruption')
            return reserve(parent, digest, name, path, record)
        with patch.object(N.Q, 'reserve', side_effect=interrupted):
            with self.assertRaises(OSError):
                N.reserve_sources(self.path, self.state)
        N.reserve_sources(self.path, self.state)
        N.reserve_sources(self.path, self.state)
        different = deepcopy(self.state)
        different['inputs_sha256'] = 'other-job'
        with self.assertRaisesRegex(N.U.UpgradeError, 'another job'):
            N.reserve_sources(self.path, different)

    def test_batch_bound_restarts_smaller_without_resetting_native_progress(self):
        def sleeping(_):
            N.STOP_REQUESTED = True
        with self.run_context(), \
             patch.object(N, 'rpc', side_effect=[self.database(20), self.database(19)]), \
             patch.object(N.J.P, 'free_bytes', return_value=800 * 1024**3), \
             patch.object(N, 'read_worker', side_effect=[{'phase': 'needs_attention', 'reason': 'native_batch_requires_smaller_range'}, {'phase': 'running'}]), \
             patch.object(N, 'lease') as leases, patch.object(N, 'stop_client'), \
             patch.object(N.time, 'sleep', side_effect=sleeping):
            result = N.run(self.api, ROOT, self.path)
        self.assertEqual(result['batch_blocks'], 500)
        self.assertEqual(result['native_cursor'], 19)
        self.assertEqual(leases.call_args.args[3], 500)

    def test_persistent_database_errors_stop_after_bounded_retries(self):
        with self.run_context(), patch.object(N, 'rpc', return_value=self.database()), \
             patch.object(N.J.P, 'free_bytes', return_value=800 * 1024**3), \
             patch.object(N, 'read_worker', return_value={'phase': 'needs_attention', 'reason': 'native_transient_database_error'}), \
             patch.object(N, 'lease') as leases, patch.object(N, 'stop_client'), \
             patch.object(N.time, 'sleep') as sleep:
            result = N.run(self.api, ROOT, self.path)
        self.assertEqual(result['status'], 'native_normalization_needs_attention')
        self.assertEqual(N.load(self.path)['transient_retries'], 5)
        self.assertEqual(sleep.call_count, 5)
        leases.assert_not_called()

    def test_lease_has_a_short_deadline_and_is_bound_to_its_job(self):
        captured = []
        self.api.run = lambda args, **kwargs: captured.append((args, kwargs)) or ''
        with patch.object(N.time, 'time', return_value=1000):
            N.lease(self.api, self.inputs, True, batch_blocks=125, valid_ms=20000)
        payload = json.loads(captured[0][1]['input_text'])
        self.assertEqual(payload['valid_until_unix_ms'], 1020000)
        self.assertEqual(payload['batch_blocks'], 125)
        self.assertEqual(payload['token'], self.inputs['payload']['token'])
        with self.assertRaises(N.U.UpgradeError):
            N.lease(self.api, self.inputs, True, valid_ms=45000)

    def test_container_ownership_is_required_before_removal(self):
        row = {'Id': 'a' * 64, 'Image': self.inputs['candidate_image'],
               'Config': {'Labels': {N.U.STATE_LABEL: str(self.path), N.U.ROLE_LABEL: 'native-normalizer'}}}
        self.api.run = lambda args, **kwargs: 'a' * 64 if args[1] == 'ps' else json.dumps([row])
        self.assertEqual(N.owned(self.api, self.path, self.inputs)['Id'], 'a' * 64)
        row['Config']['Labels'][N.U.ROLE_LABEL] = 'backend'
        with self.assertRaisesRegex(N.U.UpgradeError, 'ownership changed'):
            N.stop_client(self.api, self.path, self.inputs)

    def test_controller_restart_reattaches_its_owned_running_worker(self):
        with patch.object(N, 'owned', return_value={'State': {'Running': True}}), \
             patch.object(N.subprocess, 'Popen') as popen:
            N.launch(self.api, self.path, self.state)
        popen.assert_not_called()
        self.assertEqual(N.load(self.path)['phase'], 'prepared')

    def test_private_diagnostic_failure_does_not_prevent_owned_worker_cleanup(self):
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            if args[0] == 'sh':
                raise N.D.ConfigError('fixture unavailable')
            return ''
        self.api.run = run
        with patch.object(N, 'owned', return_value={'Id': 'b' * 64}):
            N.stop_client(self.api, self.path, self.inputs)
        self.assertEqual(calls[-1], ['docker', 'rm', '-f', 'b' * 64])

    def test_service_uses_the_frozen_python_and_prevents_review_failure_restart_loops(self):
        with patch.object(N, 'ROOT', Path('/data/blockscout-v10-rehearsal')), \
             patch.object(N, 'load', return_value={'inputs': {**self.inputs, 'source_root': '/data/blockscout-v10-rehearsal'}}):
            text = N.service_text(Path('/data/private/job/state.json'))
        self.assertIn('ExecStart=/usr/bin/python3 -u /data/blockscout-v10-rehearsal/', text)
        self.assertIn('Restart=on-failure', text)
        self.assertIn('RestartPreventExitStatus=2', text)
        self.assertNotIn('native-private-canary', text)


if __name__ == '__main__':
    unittest.main()
