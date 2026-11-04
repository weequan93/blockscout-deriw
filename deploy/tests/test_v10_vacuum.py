from copy import deepcopy
from datetime import datetime, timedelta, timezone
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
import v10_vacuum as V
REAL_OBSERVE = V.observe


def resources():
    return {'observed_at': 'fixture', 'settings': [['autovacuum', 'on', None], ['track_counts', 'on', None],
        ['autovacuum_vacuum_threshold', '50', None], ['autovacuum_vacuum_scale_factor', '0.2', None]],
        'table': {'options': [], 'estimated_rows': 1000, 'dead_rows': 500, 'total_bytes': 10000, 'stats_reset': 'fixture'},
        'io': {'heap_blocks_read': 100, 'index_blocks_read': 100},
        'wal': {'directory_bytes': 1000, 'retained_slot_bytes': 0, 'bytes_since_stats_reset': '1000', 'stats_reset': 'fixture'},
        'transactions': {'oldest_transaction_seconds': 1, 'prepared_transactions': 0, 'slots_with_xmin': 0},
        'vacuum': [], 'target_sessions': [], 'observer_role_oid': 10, 'runtime_holds_verified': True}


class VacuumTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        directory = Path(self.temp.name).resolve()
        os.chmod(directory, 0o700)
        patch.object(V, 'BASE', directory / 'vacuum').start()
        patch.object(V.B, 'validate_state_directory').start()
        patch.object(V.B, 'clean_source', return_value=('a' * 40, '10.2.6')).start()
        patch.object(V.P, 'free_bytes', return_value=888 * 1024**3).start()
        patch.object(V, 'no_normalizer').start()
        patch.object(V, 'observe', side_effect=lambda *a, **k: deepcopy(resources())).start()
        self.path = directory / 'normalization.json'
        self.path.write_text('{}')
        os.chmod(self.path, 0o600)
        self.parent = {'phase': 'finished', 'pending': None, 'rows': 174086, 'cursor': 132032597,
            'stop_reason': 'next_complete_batch_requires_vacuum_review',
            'last': {'blocks': [12], 'block_rows': [{'block': 12, 'rows': 1}], 'expected_rows': 1,
                     'baseline': {'statuses': [{'migration_name': 'duplicate', 'status': 'completed'}]}},
            'inputs': {'upgrade_state': '/data/upgrade.json', 'runtime_before': {}, 'maintenance_rows_before': 75422,
                       'maintenance_origin': {'space_before': 888 * 1024**3, 'resources': {'fixed': True}},
                       'payload': {'database_identity': {'oid': 123}, 'heap_identity': 456, 'applied_versions': [1]}}}
        patch.object(V.J, 'load', side_effect=lambda *a, **k: deepcopy(self.parent)).start()
        for module in (V.H, V.E):
            patch.object(module, 'load', return_value={'phase': 'held', 'inputs': {'upgrade_state': '/data/upgrade.json', 'runtime_before': {}}}).start()
        self.manifest = {'chain_id': '18417507517'}
        self.api = types.SimpleNamespace(configuration=lambda *a: (self.manifest, {}, {}), inspect_project=lambda *a: {},
            ensure_stable=lambda *a: None, run=lambda *a, **k: '')
        self.upgrade = {'candidate_image': 'sha256:' + 'b' * 64, 'network': 'fixture', 'backend_environment': {}, 'candidate_source_commit': 'c' * 40}
        patch.object(V.P, 'context', return_value=(self.manifest, self.upgrade)).start()
        self.cleanups = patch.object(V, 'cleanup').start()

    def prepare(self):
        return Path(V.prepare(self.api, ROOT, self.path, Path('/metrics'), Path('/empty'))['state'])

    def target(self, state):
        payload = state['inputs']['payload']
        return {'pid': 1234, 'backend_start': '2026-11-04 12:00:00+00', 'application_name': payload['application_name'],
            'query_md5': V.SQL_MD5, 'database_oid': 123, 'role_oid': 10,
            'settings': {'maintenance_work_mem': '512MB', 'vacuum_cost_limit': '200', 'vacuum_cost_delay': '2ms',
                         'lock_timeout': '500ms', 'statement_timeout': '1h', 'client_connection_check_interval': '1s'}}

    def requested(self, mode=None):
        path = self.prepare()
        state = V.load(path)
        state.update(phase='vacuum_requested', target=self.target(state), requested_at_utc=datetime.now(timezone.utc).isoformat(), lease_sequence=1)
        V.R.update_state(path, state)
        if mode:
            result = {'token': state['inputs']['payload']['token'], 'identity': state['target'], 'mode': mode,
                      'reason': 'host_lease_expired', 'command': 'vacuum', 'elapsed_ms': 100, 'index_cleanup_requested': True}
            V.R.write_new(path.parent / 'client.log.private', ('BLOCKSCOUT_VACUUM_RESULT=' + json.dumps(result) + '\n').encode())
        return path

    def test_preparation_is_read_only_and_preserves_normalization_baseline(self):
        path = self.prepare()
        state = V.load(path)
        self.assertEqual(state['inputs']['maintenance_selected_rows'], 249508)
        self.assertEqual(state['inputs']['maintenance_origin'], self.parent['inputs']['maintenance_origin'])
        self.assertEqual(state['inputs']['normalization_cursor'], 132032597)
        self.assertFalse(V.public(path, state)['normalization_gate_reopened'])
        self.assertEqual(self.path.read_text(), '{}')

    def test_pending_or_unfinished_normalization_cannot_prepare_vacuum(self):
        for key, value in (('pending', {'slot': 'raw'}), ('phase', 'write_requested')):
            with self.subTest(key=key):
                original = self.parent[key]
                self.parent[key] = value
                with self.assertRaises(V.U.UpgradeError):
                    self.prepare()
                self.parent[key] = original

    def test_result_reconciliation_never_relaunches_vacuum(self):
        path = self.requested('completed')
        with patch.object(V.subprocess, 'Popen') as launch:
            result = V.execute(self.api, ROOT, path, resume=True)
            again = V.execute(self.api, ROOT, path, resume=True)
        launch.assert_not_called()
        self.assertTrue(result['completion_verified'])
        self.assertEqual(again['status'], 'vacuum_window_finished')
        self.assertFalse(result['normalization_gate_reopened'])
        self.assertEqual(self.path.read_text(), '{}')

    def test_interrupted_receipt_does_not_qualify_completion(self):
        path = self.requested('interrupted')
        result = V.execute(self.api, ROOT, path, resume=True)
        self.assertFalse(result['completion_verified'])
        self.assertEqual(result['stop_reason'], 'host_lease_expired')

    def test_missing_receipt_cannot_reset_window_or_start_again(self):
        path = self.requested()
        with patch.object(V.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(V.U.UpgradeError, 'without a completion receipt'):
                V.execute(self.api, ROOT, path, resume=True)
            self.assertEqual(V.execute(self.api, ROOT, path, resume=True)['status'], 'vacuum_window_finished')
        launch.assert_not_called()

    def test_lease_is_not_renewed_after_health_gap(self):
        path = self.requested()
        state = V.load(path)
        state['last_health_at_utc'] = (datetime.now(timezone.utc) - timedelta(seconds=31)).isoformat()
        with patch.object(V, 'publish') as publish:
            with self.assertRaisesRegex(V.U.UpgradeError, 'health observation gap'):
                V.renew(self.api, path, state)
        publish.assert_not_called()

    def test_other_snapshots_vacuums_and_resource_limits_stop_monitoring(self):
        state = V.load(self.prepare())
        inputs = state['inputs']
        baseline = resources()
        cases = [(['transactions', 'oldest_transaction_seconds'], 301),
                 (['transactions', 'prepared_transactions'], 1), (['transactions', 'slots_with_xmin'], 1),
                 (['vacuum'], [{'pid': 999}]),
                 (['wal', 'bytes_since_stats_reset'], str(1000 + V.LIMITS['cluster_wal_bytes'] + 1)),
                 (['io', 'heap_blocks_read'], 101 + V.LIMITS['relation_read_bytes'] // 8192)]
        for keys, value in cases:
            with self.subTest(keys=keys):
                data = resources()
                current = data
                for key in keys[:-1]:
                    current = current[key]
                current[keys[-1]] = value
                with self.assertRaises(V.U.UpgradeError):
                    V.check_resources(data, inputs, baseline)

    def test_role_pid_start_and_application_must_match_owned_socket(self):
        state = V.load(self.prepare())
        target = self.target(state)
        V.validate_target(target, state['inputs']['payload'])
        data = resources()
        data['target_sessions'] = [{**target, 'same_role': True, 'backend_start': 'changed'}]
        with self.assertRaisesRegex(V.U.UpgradeError, 'backend identity changed'):
            V.validate_activity(data, target)
        data['target_sessions'] = []
        data['observer_role_oid'] = 11
        with self.assertRaisesRegex(V.U.UpgradeError, 'role differs'):
            V.validate_activity(data, target)

    def test_frozen_source_or_limits_cannot_be_changed(self):
        path = self.prepare()
        with (path.parent / 'migrate.exs').open('ab') as stream:
            stream.write(b'\nchanged\n')
        with self.assertRaisesRegex(V.U.UpgradeError, 'Frozen vacuum source'):
            V.load(path)

    def test_completion_requires_an_actual_vacuum_receipt(self):
        path = self.requested('completed')
        result = V.marker(path, 'BLOCKSCOUT_VACUUM_RESULT=')
        result['command'] = 'select'
        with self.assertRaisesRegex(V.U.UpgradeError, 'completion receipt'):
            V.validate_result(result, V.load(path))

    def test_preparation_monitors_absent_migration_records_too(self):
        inputs = V.load(self.prepare())['inputs']
        self.assertIn('empty_internal_transactions_data', inputs['payload']['status_names'])
        self.assertIn('heavy_indexes_create_internal_transactions_pkey', inputs['payload']['status_names'])

    def test_maximum_anchor_does_not_enter_a_command_line_argument(self):
        source = V.observer_source(ROOT)
        payload = {'anchor': {'baseline': {'full_hashes': ['a' * 32] * 1000, 'semantic_hashes': ['b' * 32] * 1000}}}
        captured = {}
        def run(command, **kwargs):
            captured.update(command=command, **kwargs)
            self.assertTrue(all(len(arg.encode()) < 4096 for arg in command))
            self.assertGreater(len(kwargs['input_text'].encode()), 131072)
            return 'BLOCKSCOUT_DEPLOY_CHECK={}\n'
        api = types.SimpleNamespace(require=V.P.require, run=run)
        with patch.object(V, 'observe', new=REAL_OBSERVE):
            self.assertEqual(V.observe(api, source, payload), {'query_diagnostics': []})
        self.assertEqual(captured['command'], ['docker', 'exec', '-i', 'backend', 'sh', '-c', V.RPC_SCRIPT])
        self.assertNotIn('a' * 32, ' '.join(captured['command']))

    def test_pre_vacuum_clients_still_stop_but_later_clients_are_observed(self):
        inputs = V.load(self.prepare())['inputs']
        boundary = '2026-11-04 14:00:00+00'
        data = resources()
        data.update(vacuum_query_started_at=boundary, query_diagnostics=[{'pid': 9, 'query_md5': 'a' * 32}])
        data['transactions'].update(oldest_transaction_seconds=310, oldest_pre_vacuum_transaction_seconds=0,
                                    post_vacuum_client_transactions_over_limit=1)
        scoped = {**inputs, 'vacuum_query_started_at': boundary}
        result = V.check_resources(data, scoped)
        self.assertEqual(result['transactions']['oldest_transaction_seconds'], 310)
        self.assertEqual(result['transactions']['post_vacuum_client_transactions_over_limit'], 1)
        data['transactions']['oldest_pre_vacuum_transaction_seconds'] = 310
        with self.assertRaises(V.ResourceReviewError) as rejected:
            V.check_resources(data, scoped)
        self.assertEqual(V.error_report(rejected.exception)['transaction_review']['query_diagnostics'], data['query_diagnostics'])
        data['transactions']['oldest_pre_vacuum_transaction_seconds'] = 0
        with self.assertRaises(V.U.UpgradeError):
            V.check_resources(data, {**scoped, 'transaction_guard_scope': None})
        with self.assertRaisesRegex(V.U.UpgradeError, 'boundary changed'):
            V.check_resources(data, inputs)

    def test_actual_query_start_is_proved_once_and_not_editable(self):
        path = self.requested()
        state = V.load(path)
        boundary = '2026-11-04 14:00:00+00'
        data = resources()
        data.update(vacuum_query_started_at=boundary,
                    target_sessions=[{**state['target'], 'query_start': boundary, 'state': 'active', 'same_role': True}])
        V.capture_start(path, state, data)
        self.assertEqual(V.load(path)['vacuum_query_started_at'], boundary)
        V.capture_start(path, state, data)
        # A crash after proof fsync but before state publication can recover
        # the same statement without replacing its proof or changing limits.
        recovered = V.load(path)
        recovered.pop('vacuum_query_started_at')
        recovered.pop('vacuum_start_sha256')
        V.R.update_state(path, recovered)
        V.capture_start(path, recovered, data)
        self.assertEqual(V.load(path)['vacuum_query_started_at'], boundary)
        with self.assertRaisesRegex(V.U.UpgradeError, 'query start changed'):
            V.capture_start(path, state, {**data, 'vacuum_query_started_at': '2026-11-04 14:01:00+00'})
        changed = json.loads(V.U.private(path))
        changed['vacuum_query_started_at'] = '2026-11-04 14:01:00+00'
        V.R.update_state(path, changed)
        with self.assertRaisesRegex(V.U.UpgradeError, 'query-start proof changed'):
            V.load(path)

    def test_rejecting_observation_and_query_fingerprint_are_retained(self):
        path = self.requested()
        data = resources()
        data['transactions']['oldest_transaction_seconds'] = 310
        data['query_diagnostics'] = [{'pid': 9, 'query_md5': 'b' * 32}]
        with patch.object(V, 'owned_client', return_value=True), patch.object(V, 'observe', return_value=data):
            with self.assertRaises(V.ResourceReviewError):
                V.execute(self.api, ROOT, path, resume=True)
        saved = json.loads(V.U.private(path.parent / 'resources-latest.json'))
        self.assertEqual(saved['query_diagnostics'], data['query_diagnostics'])
        self.assertEqual(saved['transactions']['oldest_transaction_seconds'], 310)
        self.assertEqual(V.load(path)['phase'], 'finished')

    def test_error_details_preserve_safe_configuration_reason_and_hide_values(self):
        safe = V.error_report(V.D.ConfigError('Docker/Compose read unavailable; raw output suppressed'))
        self.assertEqual(safe['reason'], 'Docker/Compose read unavailable; raw output suppressed')
        for error in (KeyError('private-row-canary'), RuntimeError('private-row-canary')):
            report = V.error_report(error)
            self.assertNotIn('private-row-canary', json.dumps(report))
            self.assertEqual(report['error_type'], type(error).__name__)
        try:
            try:
                raise OSError(7, 'private-row-canary')
            except OSError:
                raise V.D.ConfigError('Docker/Compose read unavailable; raw output suppressed') from None
        except V.D.ConfigError as error:
            report = V.error_report(error)
        self.assertEqual(report['system_errno'], 7)
        self.assertEqual(report['context_type'], 'OSError')
        self.assertNotIn('private-row-canary', json.dumps(report))


if __name__ == '__main__':
    unittest.main()
