import contextlib
from copy import deepcopy
import io
import json
from pathlib import Path
import re
import sys
from unittest.mock import patch
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import v10_normalization_batch_update as B
import test_v10_normalization_pilot as F


class BatchUpdateTests(unittest.TestCase):
    def setUp(self):
        self.f = F.PilotTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.addCleanup(patch.stopall)
        self.sample_base = self.f.base / 'samples'
        self.sample_base.mkdir(mode=0o700)
        patch.object(B.S, 'BASE', self.sample_base).start()
        self.sample_path = self.sample_base / 'sample-20261104T044127Z-0123abcd.json'
        self.targets = [{'block': 20, 'rows': 1}, {'block': 10, 'rows': 1}]
        self.sample = {'format': B.S.FORMAT, 'environment': 'dev', 'chain_id': '18417507517', 'read_only': True,
                       'apply_available': False, 'automatic_background_migrations_held': True,
                       'status': 'normalization_batch_sample_ready', 'blockers': [], 'before_block': 21,
                       'selected_blocks': [{'block': 20, 'rows': 1, 'needs': 1, 'row_bytes': 324},
                                           {'block': 10, 'rows': 1, 'needs': 1, 'row_bytes': 324}],
                       'selected_rows': 2, 'selected_block_count': 2, 'newest_selected_block': 20,
                       'oldest_selected_block': 10, 'rows_needing_normalization': 2, 'selected_row_bytes': 648,
                       'excluded_boundary_block': 9, 'upgrade_state': '/fixture/upgrade/state.json',
                       'candidate_image': self.f.state['candidate_image'], 'previous_pilot_state': '/fixture/pilot/state.json'}
        self.f.path = self.f.base / 'batch-20261104T050000Z-0123abcd' / 'state.json'
        self.f.path.parent.mkdir(mode=0o700)
        self.f.raw['snapshot'].update(rows=[{'block_number': 20, 'private': F.SECRET}, {'block_number': 10}],
                                      full_hashes=['a' * 32, 'c' * 32], semantic_hashes=['b' * 32, 'd' * 32], needs_normalization=2)
        self.f.normalized = deepcopy(self.f.raw)
        self.f.normalized['snapshot'].update(full_hashes=['e' * 32, 'f' * 32], needs_normalization=0)
        self.f.normalized.update(mode='migrate', updated_rows=2, elapsed_ms=100, cluster_wal_delta_bytes=8192, already_normalized=False)
        self.f.state.update(format=B.P.BATCH_FORMAT, block_rows=self.targets, expected_rows=2, baseline=B.P.baseline(self.f.raw))
        self.f.state.pop('block')
        self.f.state['migration_payload'].pop('block')
        self.f.state['migration_payload'].update(blocks=[20, 10], block_rows=self.targets, expected_rows=2,
            baseline={k: self.f.state['baseline'][k] for k in ('full_hashes', 'semantic_hashes', 'statuses')})
        self.write()

    def write(self):
        content = B.R.encoded(self.sample)
        self.sample_path.write_bytes(content)
        self.sample_path.chmod(0o600)
        frozen = self.f.path.parent / 'sample.json'
        frozen.write_bytes(content)
        frozen.chmod(0o600)
        self.digest = B.R.digest(content)
        self.f.state['sample_sha256'] = self.digest
        self.f.write()

    def test_receipt_rejects_wrong_digest_permissions_boundary_and_total(self):
        B.receipt(self.sample_path, self.digest, self.f.manifest)
        with self.assertRaises(B.U.UpgradeError):
            B.receipt(self.sample_path, '0' * 64, self.f.manifest)
        self.sample_path.chmod(0o644)
        with self.assertRaises(B.U.UpgradeError):
            B.receipt(self.sample_path, self.digest, self.f.manifest)
        for key, value in (('selected_rows', 3), ('excluded_boundary_block', 10), ('blockers', ['review'])):
            old = self.sample[key]
            self.sample[key] = value
            self.write()
            with self.assertRaises(B.U.UpgradeError):
                B.receipt(self.sample_path, self.digest, self.f.manifest)
            self.sample[key] = old
        self.write()

    def test_count_cap_order_duplicates_and_invalid_blocks_are_rejected(self):
        for targets in ([{'block': 20, 'rows': 1000}, {'block': 10, 'rows': 1}], self.targets[::-1],
                        [self.targets[0], self.targets[0]], [{'block': True, 'rows': 1}], []):
            with self.assertRaises(B.U.UpgradeError):
                B.P.block_rows(targets)

    def test_atomic_batch_uses_original_journal_and_omits_private_rows_from_output(self):
        def client(api, path, state, mode):
            self.assertEqual(state['migration_payload']['blocks'], [20, 10])
            if mode == 'migrate':
                self.assertEqual(json.loads(path.read_text())['phase'], 'write_requested')
                return self.f.normalized
            return self.f.raw
        with self.f.guarded(client):
            result = B.P.apply(self.f.api, ROOT, self.f.path, self.f.manifest)
        self.assertEqual(result['updated_rows'], 2)
        self.assertEqual(result['selected_block_count'], 2)
        self.assertEqual(result['rows_needing_normalization'], 0)
        self.assertNotIn(F.SECRET, json.dumps(result))

    def test_same_total_with_a_different_per_block_count_cannot_write(self):
        raw = deepcopy(self.f.raw)
        raw['snapshot']['rows'][1]['block_number'] = 20
        with self.f.guarded(lambda *a: raw) as (call, _), self.assertRaises(B.U.UpgradeError):
            B.P.apply(self.f.api, ROOT, self.f.path, self.f.manifest)
        self.assertEqual([c.args[-1] for c in call.call_args_list], ['inspect'])
        self.assertFalse(json.loads(self.f.path.read_text())['writes_may_have_started'])

    def test_lost_commit_response_is_reconciled_without_a_second_callback(self):
        def lost(*args):
            if args[-1] == 'migrate':
                raise B.D.ConfigError('response lost')
            return self.f.raw
        with self.f.guarded(lost), self.assertRaises(B.D.ConfigError):
            B.P.apply(self.f.api, ROOT, self.f.path, self.f.manifest)
        with self.f.guarded(lambda *a: self.f.normalized) as (call, _):
            result = B.P.apply(self.f.api, ROOT, self.f.path, self.f.manifest, resume=True)
        self.assertTrue(result['result_reconciled_without_second_update'])
        self.assertEqual([c.args[-1] for c in call.call_args_list], ['inspect'])

    def test_frozen_sample_or_payload_target_drift_prevents_any_client(self):
        (self.f.path.parent / 'sample.json').write_text('{}')
        with self.f.guarded(lambda *a: self.f.raw) as (call, _), self.assertRaises(B.U.UpgradeError):
            B.P.apply(self.f.api, ROOT, self.f.path, self.f.manifest)
        call.assert_not_called()
        self.write()
        self.f.state['migration_payload']['blocks'] = [20, 11]
        self.f.write()
        with self.assertRaises(B.U.UpgradeError):
            B.P.load(self.f.api, self.f.path, self.f.manifest)

    def test_preparation_freezes_exact_sample_and_snapshot_without_data_writes(self):
        self.f.api.configuration = lambda *a: (self.f.manifest, {}, {})
        upgrade = {'candidate_image': self.f.state['candidate_image'], 'candidate_source_commit': 'a' * 40,
                   'network': 'fixture', 'backend_environment': {'SECRET': F.SECRET},
                   'migration_payload': {'database_identity': {}, 'heap_identity': 12},
                   'applied_result': {'applied_versions': [1, 2]}}
        pilot = {'format': B.P.FORMAT, 'phase': 'applied', 'writes_may_have_started': True,
                 'upgrade_state': self.sample['upgrade_state'], 'block': 21}
        with patch.object(B.P, 'context', return_value=(self.f.manifest, upgrade)), \
                patch.object(B.P, 'load', return_value=pilot), \
                patch.object(B.P, 'status', return_value={'status': 'verified', 'rows_needing_normalization': 0}), \
                patch.object(B.P, 'client', return_value=self.f.raw) as client, \
                patch.object(B.B, 'clean_source', return_value=('a' * 40, '10.2.6')), \
                patch.object(B.P, 'free_bytes', return_value=888 * 1024**3), contextlib.redirect_stdout(io.StringIO()):
            result = B.prepare(self.f.api, ROOT, self.sample_path, self.digest)
        state = B.P.load(self.f.api, Path(result['state']), self.f.manifest)
        self.assertEqual(state['phase'], 'prepared')
        self.assertEqual(state['block_rows'], self.targets)
        self.assertFalse(state['writes_may_have_started'])
        self.assertEqual(client.call_args.args[-1], 'inspect')
        self.assertEqual((Path(result['state']).parent / 'sample.json').read_bytes(), self.sample_path.read_bytes())

    def test_run_cli_applies_only_the_journal_prepared_from_the_pinned_sample(self):
        argv = ['batch', '--run', '--sample-file', str(self.sample_path), '--sample-sha256', self.digest]
        with patch.object(B.sys, 'argv', argv), patch.object(B.sys, 'platform', 'linux'), \
                patch.object(B.os, 'geteuid', return_value=0), \
                patch.object(B.D, 'configuration', return_value=(self.f.manifest, {}, {})), \
                patch.object(B.M, 'global_lock', return_value=contextlib.nullcontext()), \
                patch.object(B.B, 'build_lock', return_value=contextlib.nullcontext()), \
                patch.object(B, 'prepare', return_value={'status': 'normalization_batch_prepared', 'state': str(self.f.path)}) as prepare, \
                patch.object(B.P, 'apply', return_value={'status': 'verified', 'updated_rows': 2}) as apply, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(B.main(), 0)
        self.assertEqual(prepare.call_args.args[-2:], (self.sample_path, self.digest))
        self.assertEqual(apply.call_args.args[2], self.f.path)
        self.assertNotIn(F.SECRET, output.getvalue())

    def retry_fixture(self, raw=None, status='not_applied'):
        self.f.state.update(phase='write_requested', writes_may_have_started=True)
        self.f.write()
        self.f.api.configuration = lambda *a: (self.f.manifest, {}, {})
        inspected = deepcopy(raw or self.f.raw)
        inspected['callback_plan'] = {'access': 'Index Scan', 'index': 'block_number_idx', 'planned_rows': 2, 'estimated_total_cost': 10.0}
        with self.f.guarded(lambda *a: inspected), \
                patch.object(B.P, 'status', return_value={'status': status}), contextlib.redirect_stdout(io.StringIO()):
            return B.prepare_retry(self.f.api, ROOT, self.f.path)

    def test_retry_preserves_parent_bytes_and_original_rows_with_a_new_frozen_client(self):
        result = self.retry_fixture()
        path = Path(result['state'])
        state = B.P.load(self.f.api, path, self.f.manifest)
        self.assertNotEqual(path, self.f.path)
        self.assertEqual(state['phase'], 'prepared')
        self.assertFalse(state['writes_may_have_started'])
        self.assertEqual(state['baseline'], self.f.state['baseline'])
        self.assertEqual(state['migration_payload'], self.f.state['migration_payload'])
        self.assertEqual((path.parent / 'parent-state.json').read_bytes(), self.f.path.read_bytes())
        self.assertEqual(state['retry_parent_sha256'], B.R.digest(self.f.path.read_bytes()))
        self.assertNotEqual(state['client_name'], self.f.state['client_name'])
        self.assertEqual(state['callback_plan']['index'], 'block_number_idx')
        self.assertNotIn(F.SECRET, json.dumps(result))

    def test_retry_rejects_a_committed_batch_or_a_changed_original_row(self):
        with self.assertRaises(B.U.UpgradeError):
            self.retry_fixture(status='verified')
        changed = deepcopy(self.f.raw)
        changed['snapshot']['full_hashes'][0] = 'f' * 32
        with self.assertRaises(B.U.UpgradeError):
            self.retry_fixture(raw=changed)
        self.assertEqual(json.loads(self.f.path.read_text())['phase'], 'write_requested')

    def test_parent_journal_change_blocks_a_prepared_retry_before_any_client(self):
        result = self.retry_fixture()
        self.f.state['phase'] = 'applied'
        self.f.write()
        with self.f.guarded(lambda *a: self.f.raw) as (call, _), self.assertRaises(B.U.UpgradeError):
            B.P.apply(self.f.api, ROOT, Path(result['state']), self.f.manifest)
        call.assert_not_called()

    def test_retry_cli_applies_only_the_new_child_journal(self):
        child = self.f.base / 'batch-20261104T060000Z-0123abcd' / 'state.json'
        argv = ['batch', '--retry', '--state', str(self.f.path)]
        with patch.object(B.sys, 'argv', argv), patch.object(B.sys, 'platform', 'linux'), \
                patch.object(B.os, 'geteuid', return_value=0), \
                patch.object(B.D, 'configuration', return_value=(self.f.manifest, {}, {})), \
                patch.object(B.M, 'global_lock', return_value=contextlib.nullcontext()), \
                patch.object(B.B, 'build_lock', return_value=contextlib.nullcontext()), \
                patch.object(B, 'prepare_retry', return_value={'status': 'normalization_batch_retry_prepared', 'state': str(child)}) as prepare, \
                patch.object(B.P, 'apply', return_value={'status': 'verified', 'updated_rows': 2}) as apply, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(B.main(), 0)
        self.assertEqual(prepare.call_args.args[-1], self.f.path)
        self.assertEqual(apply.call_args.args[2], child)

    def test_planner_guard_uses_the_same_lock_query_as_the_pinned_callback(self):
        source = (ROOT / 'apps/explorer/lib/explorer/migrator/empty_internal_transactions_data.ex').read_text()
        client = (ROOT / 'deploy/scripts/v10/normalization_pilot.exs').read_text()
        original = re.search(r'lock_query =(.+?)\n\n        extract_error_query', source, re.S)[1]
        guarded = re.search(r'lock_query =(.+?)\n\n          \{lock_sql', client, re.S)[1]
        self.assertEqual(re.sub(r'\s+', '', original.replace('block_numbers', 'blocks')), re.sub(r'\s+', '', guarded))

    def test_client_failure_reports_only_an_allowlisted_stage_from_private_logs(self):
        def fail(*args, **kwargs):
            path = self.f.path.parent / 'migrate-0123abcd.log.private'
            B.R.write_new(path, ('BLOCKSCOUT_NORMALIZATION_STAGE=callback_update\n' + F.SECRET + '\n').encode())
            raise B.U.UpgradeError('Schema client failed')
        with patch.object(B.P.U, 'client', side_effect=fail), self.assertRaises(B.U.UpgradeError) as error:
            B.P.client(self.f.api, self.f.path, self.f.state, 'migrate')
        self.assertIn('callback_update', str(error.exception))
        self.assertNotIn(F.SECRET, str(error.exception))


if __name__ == '__main__':
    unittest.main()
