import contextlib
from copy import deepcopy
import io
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
import v10_normalization_session as J


def resources():
    return {'settings': [['autovacuum', 'on', None], ['track_counts', 'on', None],
                         ['autovacuum_vacuum_threshold', '50', None], ['autovacuum_vacuum_scale_factor', '0.2', None]],
            'table': {'options': [], 'estimated_rows': 1776221184, 'dead_rows': 25000,
                      'total_bytes': 1598640365568, 'stats_reset': 'fixture'},
            'wal': {'directory_bytes': 1024**3, 'retained_slot_bytes': 0, 'stats_reset': 'fixture'},
            'transactions': {'oldest_transaction_seconds': 1}}


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        os.chmod(self.base, 0o700)
        self.addCleanup(patch.stopall)
        patch.object(J, 'BASE', self.base).start()
        patch.dict(J.LIMITS, max_batches=3, max_rows=6).start()
        patch.object(J.B, 'clean_source', return_value=('a' * 40, '10.2.6')).start()
        patch.object(J.P, 'free_bytes', return_value=888 * 1024**3).start()
        patch.object(J.P, 'context', return_value=({}, {})).start()
        self.manifest = {'chain_id': '18417507517'}
        self.api = types.SimpleNamespace(inspect_project=lambda *a: {}, ensure_stable=lambda *a: None,
                                        run=lambda *a, **k: '')
        self.normalized = {100}
        self.migrates = 0
        self.lose_response = self.fail_before = self.drift = False
        self.raw_resources = resources()
        case = self
        class Client:
            def __init__(self, api, path, inputs):
                self.path = path
                self.name = 'blockscout-v10-schema-' + 'a' * 16
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def request(self, command, payload=None):
                if command == 'resources':
                    return deepcopy(case.raw_resources)
                if command == 'sample':
                    cursor = payload['before_block']
                    return {'block_rows': [{'block': cursor - n, 'rows': 1} for n in (1, 2)],
                            'selected_rows': 2, 'selected_bytes': 648, 'excluded_boundary_block': cursor - 3}
                if command == 'migrate':
                    state = J.load(self.path, case.manifest)
                    case.assertEqual(state['phase'], 'write_requested')
                    case.assertIsNotNone(state['pending'])
                    raw = json.loads(J.U.private(self.path.parent / state['pending']['slot']))
                    case.assertEqual(raw['blocks'], payload['blocks'])
                    if case.fail_before:
                        raise J.U.UpgradeError('simulated rollback')
                    case.normalized.update(payload['blocks'])
                    case.migrates += 1
                    if case.lose_response:
                        case.lose_response = False
                        raise J.U.UpgradeError('simulated lost response')
                result = case.result(payload['blocks'])
                if case.drift:
                    result['snapshot']['semantic_hashes'][0] = 'f' * 32
                return {**result, 'updated_rows': len(payload['blocks']), 'elapsed_ms': 10, 'cluster_wal_delta_bytes': 1000}
        patch.object(J, 'Client', Client).start()

    def result(self, blocks):
        rows = [{'block_number': b, 'value': None if b in self.normalized else 0, 'private': 'private-test-value'} for b in blocks]
        return {'snapshot': {'rows': rows, 'full_hashes': [J.R.digest(J.R.encoded(r))[:32] for r in rows],
                             'semantic_hashes': [J.R.digest(str(b).encode())[:32] for b in blocks],
                             'needs_normalization': sum(b not in self.normalized for b in blocks), 'sampled_row_bytes': 324 * len(blocks)},
                'statuses': []}

    def prepare(self):
        directory = self.base / 'session-20261104T080000Z-1234abcd'
        directory.mkdir(mode=0o700)
        anchor = {'blocks': [100], 'block_rows': [{'block': 100, 'rows': 1}], 'expected_rows': 1,
                  'baseline': J.compact(J.P.baseline(self.result([100])))}
        script = b'frozen session script'
        inputs = {'environment': 'dev', 'chain_id': self.manifest['chain_id'], 'limits': deepcopy(J.LIMITS),
                  'script_sha256': J.R.digest(script), 'start_cursor': 100, 'tool_source_commit': 'a' * 40,
                  'upgrade_state': '/private/upgrade/state.json', 'runtime_before': {},
                  'anchor': anchor, 'space_before': 888 * 1024**3, 'created_epoch': J.time.time()}
        state = {'format': J.FORMAT, 'inputs': inputs, 'inputs_sha256': J.R.digest(J.R.encoded(inputs)), 'phase': 'prepared',
                 'batches': 0, 'rows': 0, 'updated_rows': 0, 'charged_wal_bytes': 0, 'wal_measurements_complete': True,
                 'cursor': 100, 'pending': None, 'last': None, 'last_sha256': None, 'recent': [],
                 'checkpoint_hash': J.R.digest(J.R.encoded(inputs)), 'resources_before': resources()}
        state['checkpoint_sha256'] = J.checkpoint_digest(state)
        J.R.write_new(directory / 'migrate.exs', script)
        J.R.write_new(directory / 'inputs.json', J.R.encoded(inputs))
        path = directory / 'state.json'
        J.R.write_new(path, J.R.encoded(state))
        return path

    def execute(self, path):
        with contextlib.redirect_stdout(io.StringIO()):
            return J.execute(self.api, ROOT, path, self.manifest)

    def test_rotated_snapshots_keep_pending_target_and_finish_cannot_reset_budgets(self):
        path = self.prepare()
        result = self.execute(path)
        self.assertEqual(result['selected_rows_verified'], 6)
        self.assertEqual(result['next_before_block'], 94)
        self.assertEqual(result['charged_chunk_wal_bytes'], 3000)
        self.assertEqual(len(list(path.parent.glob('raw-*.json'))), 2)
        self.assertFalse(result['global_normalization_complete'])
        self.assertNotIn('private-test-value', json.dumps(result))
        self.execute(path)
        self.assertEqual(self.migrates, 3)

    def test_lost_response_reconciles_without_repeating_update_and_charges_unknown_wal(self):
        path = self.prepare()
        self.lose_response = True
        with self.assertRaises(J.U.UpgradeError):
            self.execute(path)
        state = J.load(path, self.manifest)
        self.assertEqual(state['cursor'], 100)
        self.assertIsNotNone(state['pending'])
        result = self.execute(path)
        self.assertEqual(self.migrates, 1)
        self.assertEqual(result['next_before_block'], 98)
        self.assertEqual(result['charged_chunk_wal_bytes'], J.LIMITS['wal_bytes'])
        self.assertFalse(result['wal_measurements_complete'])

    def test_failed_transaction_is_retried_from_same_original_snapshot(self):
        path = self.prepare()
        self.fail_before = True
        with self.assertRaises(J.U.UpgradeError):
            self.execute(path)
        pending = J.load(path, self.manifest)['pending']
        self.fail_before = False
        self.execute(path)
        state = J.load(path, self.manifest)
        self.assertEqual(state['recent'][0]['snapshot_sha256'], pending['sha256'])
        self.assertEqual(self.migrates, 3)

    def test_durable_commit_metrics_survive_a_lost_cursor_checkpoint(self):
        path = self.prepare()
        sync = J.sync_json
        failed = [False]
        def interrupted(target, value):
            if target == path and value.get('batches') == 1 and value.get('pending') is None and not failed[0]:
                failed[0] = True
                raise J.U.UpgradeError('simulated lost checkpoint')
            sync(target, value)
        with patch.object(J, 'sync_json', interrupted), self.assertRaises(J.U.UpgradeError):
            self.execute(path)
        saved = J.load(path, self.manifest)
        self.assertEqual(saved['cursor'], 100)
        self.assertEqual(saved['pending']['result']['cluster_wal_delta_bytes'], 1000)
        result = self.execute(path)
        self.assertEqual(result['charged_chunk_wal_bytes'], 3000)
        self.assertEqual(self.migrates, 3)
        self.assertTrue(result['wal_measurements_complete'])

    def test_changed_committed_totals_cannot_reuse_wal_or_row_allowance(self):
        path = self.prepare()
        self.execute(path)
        state = J.load(path, self.manifest)
        state['charged_wal_bytes'] = 0
        J.sync_json(path, state)
        with self.assertRaises(J.U.UpgradeError):
            self.execute(path)

    def test_retention_inventory_never_offers_pending_or_newest_sessions(self):
        path = self.prepare()
        self.fail_before = True
        with self.assertRaises(J.U.UpgradeError):
            self.execute(path)
        report = J.retention_plan(self.api, self.manifest)
        self.assertFalse(report['deletion_performed'])
        self.assertTrue(report['sessions'][0]['pending'])
        self.assertFalse(report['sessions'][0]['archive_candidate'])

    def test_expired_original_pending_can_finish_without_cursor_advance(self):
        path = self.prepare()
        self.fail_before = True
        with self.assertRaises(J.U.UpgradeError):
            self.execute(path)
        created = J.load(path, self.manifest)['inputs']['created_epoch']
        self.fail_before = False
        with patch.object(J.time, 'time', return_value=created + 601):
            result = self.execute(path)
        self.assertEqual(result['completed_batches'], 0)
        self.assertEqual(result['next_before_block'], 100)
        self.assertFalse(result['pending_transaction'])
        self.assertIsNotNone(J.load(path, self.manifest)['abandoned'])
        self.assertEqual(self.migrates, 0)

    def test_semantic_drift_and_pending_snapshot_tampering_cannot_resume(self):
        path = self.prepare()
        self.fail_before = True
        with self.assertRaises(J.U.UpgradeError):
            self.execute(path)
        self.drift = True
        self.fail_before = False
        with self.assertRaises(J.U.UpgradeError):
            self.execute(path)
        self.assertEqual(self.migrates, 0)
        state = J.load(path, self.manifest)
        J.sync_json(path.parent / state['pending']['slot'], {})
        with self.assertRaises(J.U.UpgradeError):
            J.load(path, self.manifest)

    def test_storage_vacuum_and_long_transaction_guards_stop_before_writes(self):
        path = self.prepare()
        for mutate in (
            lambda r: r['table'].update(dead_rows=1000001),
            lambda r: r['transactions'].update(oldest_transaction_seconds=301),
            lambda r: r['table'].update(stats_reset='changed'),
            lambda r: r['wal'].update(directory_bytes=2 * 1024**3),
        ):
            self.raw_resources = resources()
            mutate(self.raw_resources)
            with self.assertRaises(J.U.UpgradeError):
                self.execute(path)
        self.assertEqual(self.migrates, 0)

    def test_effective_table_overrides_and_statistics_are_not_mistaken_for_empty_table(self):
        report = J.resource_report(resources())
        self.assertAlmostEqual(report['effective_update_delete_vacuum_trigger_estimate'], 355244286.8)
        self.assertFalse(report['sustained_ready'])
        data = resources()
        data['table']['options'] = ['autovacuum_vacuum_scale_factor=0', 'autovacuum_vacuum_threshold=1000000']
        self.assertEqual(J.resource_report(data)['effective_update_delete_vacuum_trigger_estimate'], 1000000)
        data['table']['options'].append('autovacuum_enabled=false')
        with self.assertRaises(J.U.UpgradeError):
            J.resource_guard(data)


if __name__ == '__main__':
    unittest.main()
