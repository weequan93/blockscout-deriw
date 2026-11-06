import contextlib
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import v10_normalization_run as J
import test_v10_normalization_batch_update as F


class RunTests(unittest.TestCase):
    def setUp(self):
        self.fixture = F.BatchUpdateTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(patch.stopall)
        self.f = self.fixture.f
        self.api, self.manifest = self.f.api, self.f.manifest
        self.api.configuration = lambda *a: (self.manifest, {}, {})
        self.api.run.side_effect = lambda command, **kw: 'owned-client' if self.active and command[:2] == ['docker', 'ps'] else ''
        self.active, self.fail_before, self.lose_after = False, False, False
        self.migrates, self.samples = 0, 0
        self.wal_per_batch = 1000
        self.normalized = {20, 10}
        self.addCleanup(patch.stopall)
        patch.object(J, 'BASE', self.f.base / 'runs').start()
        patch.object(J.B, 'clean_source', return_value=('a' * 40, '10.2.6')).start()
        patch.object(J.P, 'free_bytes', return_value=888 * 1024**3).start()
        self.upgrade = {'candidate_image': self.f.state['candidate_image'], 'candidate_source_commit': 'a' * 40,
                        'network': 'fixture', 'backend_environment': {'SECRET': F.F.SECRET},
                        'migration_payload': {'database_identity': {}, 'heap_identity': 12}, 'applied_result': {'applied_versions': [1, 2]}}
        patch.object(J.P, 'context', return_value=(self.manifest, self.upgrade)).start()
        patch.object(J.P, 'client', side_effect=self.client).start()
        patch.object(J.S, 'sample', side_effect=self.sample).start()
        self.f.state['baseline'] = J.P.baseline(self.result(self.f.state, 'inspect', normalized=set()))
        self.f.state['migration_payload']['baseline'] = {k: self.f.state['baseline'][k] for k in ('full_hashes', 'semantic_hashes', 'statuses')}
        self.f.state.update(phase='applied', writes_may_have_started=True)
        self.f.write()

    def result(self, state, mode, normalized=None):
        normalized = self.normalized if normalized is None else normalized
        rows, semantic, count = [], [], 0
        for target in state['block_rows']:
            block = target['block']
            done = block in normalized
            row = {'block_number': block, 'private': F.F.SECRET, 'trace_address': None if done else [],
                   'value': None if done else 0, 'call_type': None if done else 'call', 'call_type_enum': 'call' if done else None}
            rows.append(row)
            semantic.append(J.R.digest(str(block).encode())[:32])
            count += not done
        return {'mode': mode, 'application_started': False, 'snapshot': {'rows': rows,
                'full_hashes': [J.R.digest(J.R.encoded(row))[:32] for row in rows], 'semantic_hashes': semantic,
                'needs_normalization': count, 'sampled_row_bytes': len(rows) * 324},
                'statuses': [], 'callback_plan': {'access': 'Index Scan', 'index': 'block_number_idx', 'planned_rows': len(rows), 'estimated_total_cost': 10.0}}

    def client(self, api, path, state, mode):
        if mode == 'inspect':
            return self.result(state, mode)
        if self.fail_before:
            raise J.U.UpgradeError('simulated timeout before commit')
        parent = json.loads(Path(state['controller_state']).read_text())
        self.assertEqual(parent['phase'], 'applying_batch')
        self.assertEqual(parent['pending']['batch_state'], str(path))
        count = self.result(state, 'inspect')['snapshot']['needs_normalization']
        self.migrates += 1
        self.normalized.update(r['block'] for r in state['block_rows'])
        if self.lose_after:
            self.lose_after = False
            raise J.U.UpgradeError('simulated lost commit response')
        return {**self.result(state, mode), 'updated_rows': count, 'elapsed_ms': 20,
                'cluster_wal_delta_bytes': self.wal_per_batch, 'already_normalized': False}

    def sample(self, api, root, after_path):
        self.samples += 1
        previous = J.P.load(api, after_path, self.manifest)
        cursor = J.P.completed_cursor(previous)
        record = deepcopy(self.fixture.sample)
        record.update(previous_pilot_state=str(after_path), before_block=cursor,
                      newest_selected_block=cursor - 1, oldest_selected_block=cursor - 2,
                      excluded_boundary_block=cursor - 3,
                      selected_blocks=[{'block': cursor - n, 'rows': 1, 'needs': 1, 'row_bytes': 324} for n in (1, 2)])
        path = J.S.BASE / ('sample-20261104T070000Z-' + uuid.uuid4().hex[:8] + '.json')
        J.R.write_new(path, J.R.encoded(record))
        return {'sample_file': str(path), 'sample_sha256': J.R.digest(J.R.encoded(record)),
                'selected_rows': 2, 'rows_needing_normalization': 2, 'blockers': []}

    def prepare(self, batches=2, rows=4):
        with contextlib.redirect_stdout(io.StringIO()):
            result = J.prepare(self.api, ROOT, self.f.path, batches, rows)
        return Path(result['state'])

    def execute(self, path):
        with contextlib.redirect_stdout(io.StringIO()):
            return J.execute(self.api, ROOT, path, self.manifest)

    def test_finite_batches_checkpoint_each_cursor_and_share_wal_and_space_limits(self):
        path = self.prepare()
        result = self.execute(path)
        self.assertEqual(result['completed_batches'], 2)
        self.assertEqual(result['selected_rows_verified'], 4)
        self.assertEqual(result['updated_rows_observed'], 4)
        self.assertEqual(result['next_before_block'], 6)
        self.assertEqual(result['charged_chunk_wal_bytes'], 2000)
        state = J.load(self.api, path, self.manifest)
        for n, entry in enumerate(state['batches']):
            child = J.P.load(self.api, Path(entry['batch_state']), self.manifest)
            self.assertEqual(J.P.wal_limit(child), J.WAL_BUDGET - n * 1000)
            self.assertEqual(child['space_before'], state['inputs']['space_before'])
        self.assertEqual(self.migrates, 2)
        self.assertFalse(result['global_normalization_complete'])
        self.assertNotIn(F.F.SECRET, json.dumps(result))
        self.execute(path)
        self.assertEqual(self.migrates, 2)

    def test_lost_response_reconciles_once_and_conservatively_charges_the_remaining_wal(self):
        path = self.prepare()
        self.lose_after = True
        with self.assertRaises(J.U.UpgradeError):
            self.execute(path)
        pending = json.loads(path.read_text())['pending']['batch_state']
        self.assertEqual(json.loads(path.read_text())['batches'], [])
        result = self.execute(path)
        self.assertEqual(self.migrates, 1)
        self.assertEqual(result['completed_batches'], 1)
        self.assertEqual(result['last_verified_batch_state'], pending)
        self.assertEqual(result['charged_chunk_wal_bytes'], J.WAL_BUDGET)
        self.assertFalse(result['wal_measurements_complete'])
        self.assertIsNone(result['updated_rows_observed'])

    def test_rollback_keeps_the_pending_child_and_never_moves_the_cursor(self):
        path = self.prepare()
        self.fail_before = True
        with self.assertRaises(J.U.UpgradeError):
            self.execute(path)
        before = json.loads(path.read_text())
        self.assertEqual(before['after_state'], str(self.f.path))
        self.assertEqual(before['batches'], [])
        pending = before['pending']['batch_state']
        self.fail_before = False
        result = self.execute(path)
        state = J.load(self.api, path, self.manifest)
        self.assertEqual(state['batches'][0]['batch_state'], pending)
        self.assertEqual(result['selected_rows_verified'], 4)
        self.assertEqual(self.samples, 2)

    def test_loss_of_parent_checkpoint_after_commit_does_not_repeat_the_child(self):
        path = self.prepare()
        save = J.save
        def interrupted(path, state, phase):
            if phase == 'running':
                raise J.U.UpgradeError('simulated checkpoint response lost')
            return save(path, state, phase)
        with patch.object(J, 'save', side_effect=interrupted), self.assertRaises(J.U.UpgradeError):
            self.execute(path)
        result = self.execute(path)
        self.assertEqual(self.migrates, 2)
        self.assertEqual(result['completed_batches'], 2)
        self.assertEqual(result['charged_chunk_wal_bytes'], 2000)

    def test_next_whole_batch_cannot_exceed_the_remaining_row_allowance(self):
        result = self.execute(self.prepare(batches=5, rows=3))
        self.assertEqual(result['selected_rows_verified'], 2)
        self.assertEqual(result['stop_reason'], 'next_batch_exceeds_remaining_rows')
        self.assertEqual(self.migrates, 1)

    def test_expired_start_window_permits_committed_reconciliation_but_no_fresh_update(self):
        path = self.prepare()
        self.lose_after = True
        with self.assertRaises(J.U.UpgradeError):
            self.execute(path)
        created = json.loads(path.read_text())['inputs']['created_epoch']
        with patch.object(J.time, 'time', return_value=created + 601):
            result = self.execute(path)
        self.assertEqual(result['completed_batches'], 1)
        self.assertEqual(self.migrates, 1)
        other = self.prepare()
        created = json.loads(other.read_text())['inputs']['created_epoch']
        with patch.object(J.time, 'time', return_value=created + 601):
            result = self.execute(other)
        self.assertEqual(result['completed_batches'], 0)
        self.assertEqual(result['stop_reason'], 'new_batch_start_window_expired')

    def test_changed_frozen_limits_or_unverified_history_cannot_resume(self):
        path = self.prepare()
        state = json.loads(path.read_text())
        state['inputs']['max_batches'] = 100
        J.R.update_state(path, state)
        with self.assertRaises(J.U.UpgradeError):
            self.execute(path)
        self.assertEqual(self.migrates, 0)

    def test_active_owned_client_is_observed_without_killing_or_replacing_it(self):
        path = self.prepare()
        self.fail_before = True
        with self.assertRaises(J.U.UpgradeError):
            self.execute(path)
        self.active = True
        self.api.run.reset_mock()
        result = self.execute(path)
        self.assertEqual(result['status'], 'client_in_progress')
        self.assertEqual(self.migrates, 0)
        self.assertFalse(any(c.args[0][:2] == ['docker', 'rm'] for c in self.api.run.call_args_list))

    def test_remaining_wal_reserve_stops_before_sampling_or_preparing_another_batch(self):
        self.wal_per_batch = J.WAL_BUDGET - J.NEXT_BATCH_WAL_RESERVE + 1
        result = self.execute(self.prepare(batches=5, rows=10))
        self.assertEqual(result['stop_reason'], 'next_batch_wal_reserve_reached')
        self.assertEqual(result['completed_batches'], 1)
        self.assertEqual(self.samples, 1)
        self.assertIsNone(result['pending_batch_state'])
        self.assertLess(result['charged_chunk_wal_bytes'], J.WAL_BUDGET)

    def test_expanded_pilot_reaches_25_batches_without_changing_shared_budgets(self):
        targets = [{'block': 200, 'rows': 1}, {'block': 100, 'rows': 1}]
        self.fixture.sample.update(before_block=201, newest_selected_block=200, oldest_selected_block=100,
                                   excluded_boundary_block=99,
                                   selected_blocks=[{'block': r['block'], 'rows': 1, 'needs': 1, 'row_bytes': 324} for r in targets])
        self.f.state['block_rows'] = targets
        self.f.state['migration_payload'].update(blocks=[200, 100], block_rows=targets)
        self.f.state['baseline'] = J.P.baseline(self.result(self.f.state, 'inspect', normalized=set()))
        self.f.state['migration_payload']['baseline'] = {k: self.f.state['baseline'][k] for k in ('full_hashes', 'semantic_hashes', 'statuses')}
        self.normalized = {200, 100}
        self.fixture.write()
        path = self.prepare(batches=25, rows=25000)
        result = self.execute(path)
        self.assertEqual(result['completed_batches'], 25)
        self.assertEqual(result['selected_rows_verified'], 50)
        self.assertEqual(result['next_before_block'], 50)
        self.assertEqual(result['chunk_wal_budget_bytes'], 128 * 1024**2)
        self.assertEqual(result['new_batch_start_window_seconds'], 600)
        self.assertEqual(self.migrates, 25)
        for batches, rows in ((26, 25000), (25, 25001)):
            with self.assertRaises(J.U.UpgradeError):
                self.prepare(batches=batches, rows=rows)

    def test_legacy_frozen_run_inputs_without_the_new_reserve_remain_readable(self):
        path = self.prepare()
        state = json.loads(path.read_text())
        state['inputs'].pop('minimum_next_batch_wal_bytes')
        content = J.R.encoded(state['inputs'])
        (path.parent / 'inputs.json').write_bytes(content)
        state['inputs_sha256'] = J.R.digest(content)
        J.R.update_state(path, state)
        loaded = J.load(self.api, path, self.manifest)
        self.assertEqual(J.public(path, loaded, 'status')['minimum_next_batch_wal_bytes'], 0)
        self.assertIsNone(J.limit_reason(loaded))


if __name__ == '__main__':
    unittest.main()
