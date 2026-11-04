import contextlib
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import v10_normalization_pilot as P

SECRET = 'fixture-private-normalization-value'


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.path = self.base / 'pilot-20261104T040000Z-0123abcd' / 'state.json'
        self.path.parent.mkdir(mode=0o700)
        self.addCleanup(patch.stopall)
        patch.object(P, 'BASE', self.base).start()
        self.manifest = {'chain_id': '18417507517'}
        self.runtime = {'backend': {'Id': 'a' * 64, 'Image': 'sha256:' + 'b' * 64}}
        self.raw = {'mode': 'inspect', 'application_started': False, 'snapshot': {
            'rows': [{'block_index': 1, 'private': SECRET}], 'full_hashes': ['a' * 32], 'semantic_hashes': ['b' * 32],
            'needs_normalization': 1, 'sampled_row_bytes': 324},
            'statuses': [{'migration_name': 'reindex_duplicated_internal_transactions', 'status': 'completed'}]}
        self.normalized = deepcopy(self.raw)
        self.normalized['snapshot'].update(full_hashes=['c' * 32], needs_normalization=0)
        self.normalized.update(mode='migrate', updated_rows=1, elapsed_ms=15, cluster_wal_delta_bytes=8192, already_normalized=False)
        saved = P.baseline(self.raw)
        self.state = {'format': P.FORMAT, 'environment': 'dev', 'chain_id': '18417507517', 'phase': 'prepared',
                      'block': 171635973, 'expected_rows': 1, 'writes_may_have_started': False,
                      'upgrade_state': '/fixture/upgrade/state.json', 'candidate_image': 'sha256:' + 'b' * 64,
                      'candidate_source_commit': 'a' * 40, 'client_name': 'blockscout-v10-schema-' + 'c' * 16,
                      'backend_environment': {'SECRET': SECRET}, 'network': 'fixture', 'runtime_before': self.runtime,
                      'baseline': saved, 'migration_payload': {'mode': 'inspect', 'block': 171635973, 'expected_rows': 1,
                       'wal_limit_bytes': P.WAL_LIMIT, 'baseline': {k: saved[k] for k in ('full_hashes', 'semantic_hashes', 'statuses')}}}
        self.api = types.SimpleNamespace(require=P.D.require, inspect_project=Mock(side_effect=lambda *a, **k: deepcopy(self.runtime)),
                                         ensure_stable=P.D.ensure_stable, run=Mock(return_value=''))
        self.write()

    def write(self):
        script = (ROOT / 'deploy/scripts/v10/normalization_pilot.exs').read_bytes()
        target = self.path.parent / 'migrate.exs'
        target.write_bytes(script)
        target.chmod(0o600)
        self.state['script_sha256'] = P.R.digest(script)
        target = self.path.parent / 'baseline.json'
        target.write_bytes(P.R.encoded(self.state['baseline']))
        target.chmod(0o600)
        self.state['baseline_sha256'] = P.R.digest(target.read_bytes())
        self.path.write_bytes(P.R.encoded(self.state))
        self.path.chmod(0o600)

    @contextlib.contextmanager
    def guarded(self, client):
        with patch.object(P, 'context', return_value=(self.manifest, {})), \
                patch.object(P.B, 'clean_source', return_value=('a' * 40, '10.2.6')), \
                patch.object(P, 'free_bytes', return_value=888 * 1024**3), \
                patch.object(P.U, 'remove_client') as cleanup, \
                patch.object(P, 'client', side_effect=client) as call, contextlib.redirect_stdout(io.StringIO()):
            yield call, cleanup

    def test_writes_are_journaled_before_callback_and_public_result_excludes_original_row(self):
        def client(api, path, state, mode):
            if mode == 'migrate':
                current = json.loads(path.read_text())
                self.assertTrue(current['writes_may_have_started'])
                self.assertEqual(current['phase'], 'write_requested')
                return self.normalized
            return self.raw
        with self.guarded(client) as (call, cleanup):
            result = P.apply(self.api, ROOT, self.path, self.manifest)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(result['updated_rows'], 1)
        self.assertEqual(result['recorded_phase'], 'applied')
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual([c.args[-1] for c in call.call_args_list], ['inspect', 'migrate'])
        self.assertEqual(P.D.read_json(self.path)['phase'], 'applied')
        self.assertTrue(all(c.args[1] == self.path and c.args[2]['client_name'] == self.state['client_name'] for c in cleanup.call_args_list))

    def test_failure_retains_write_boundary_and_resume_reconciles_commit_without_second_update(self):
        def failed(api, path, state, mode):
            if mode == 'migrate':
                raise P.D.ConfigError('simulated response lost after commit')
            return self.raw
        with self.guarded(failed):
            with self.assertRaises(P.D.ConfigError):
                P.apply(self.api, ROOT, self.path, self.manifest)
        self.assertTrue(P.D.read_json(self.path)['writes_may_have_started'])
        with self.guarded(lambda *args: self.normalized) as (call, _):
            result = P.apply(self.api, ROOT, self.path, self.manifest, resume=True)
        self.assertEqual(result['status'], 'verified')
        self.assertTrue(result['result_reconciled_without_second_update'])
        self.assertEqual([c.args[-1] for c in call.call_args_list], ['inspect'])
        self.assertIsNone(result['update_elapsed_ms'])

    def test_committed_metrics_survive_a_subsequent_health_check_failure(self):
        def client(api, path, state, mode):
            return self.normalized if mode == 'migrate' else self.raw
        with self.guarded(client), patch.object(P, 'context', side_effect=[(self.manifest, {}), P.D.ConfigError('health unavailable')]):
            with self.assertRaises(P.D.ConfigError):
                P.apply(self.api, ROOT, self.path, self.manifest)
        state = P.D.read_json(self.path)
        self.assertEqual(state['phase'], 'write_requested')
        self.assertTrue(state['writes_may_have_started'])
        self.assertEqual(state['apply_result']['updated_rows'], 1)

    def test_semantic_full_row_or_migration_state_drift_prevents_an_update(self):
        for mutate in (lambda d: d['snapshot'].update(semantic_hashes=['f' * 32]),
                       lambda d: d['snapshot'].update(full_hashes=['f' * 32]),
                       lambda d: d.update(statuses=[])):
            raw = deepcopy(self.raw)
            mutate(raw)
            with self.guarded(lambda *args: raw) as (call, _):
                with self.assertRaises(P.D.ConfigError):
                    P.apply(self.api, ROOT, self.path, self.manifest)
            self.assertEqual([c.args[-1] for c in call.call_args_list], ['inspect'])
            self.assertFalse(P.D.read_json(self.path)['writes_may_have_started'])

    def test_frozen_script_original_snapshot_and_resource_limit_changes_are_rejected(self):
        for file in ('migrate.exs', 'baseline.json'):
            (self.path.parent / file).write_text('{}')
            with self.assertRaises(P.D.ConfigError):
                P.load(self.api, self.path, self.manifest)
            self.write()
        self.state['migration_payload']['wal_limit_bytes'] = 1000 * P.WAL_LIMIT
        self.write()
        with self.assertRaises(P.D.ConfigError):
            P.load(self.api, self.path, self.manifest)

    def test_status_never_interrupts_an_active_owned_client(self):
        self.api.run.return_value = 'a' * 64
        with patch.object(P, 'context') as context, patch.object(P, 'client') as client:
            result = P.status(self.api, ROOT, self.path, self.manifest)
        self.assertEqual(result['status'], 'client_in_progress')
        context.assert_not_called()
        client.assert_not_called()

    def test_status_observes_both_original_and_normalized_without_writes(self):
        for raw, expected in ((self.raw, 'not_applied'), (self.normalized, 'verified')):
            with patch.object(P, 'context'), patch.object(P, 'client', return_value=raw) as client:
                result = P.status(self.api, ROOT, self.path, self.manifest)
            self.assertEqual(result['status'], expected)
            self.assertEqual(client.call_args.args[-1], 'inspect')
            self.assertFalse(P.D.read_json(self.path)['writes_may_have_started'])

    def test_runtime_changes_block_before_selected_row_read(self):
        self.runtime['backend']['Image'] = 'sha256:' + 'f' * 64
        with self.guarded(lambda *args: self.raw) as (client, _):
            with self.assertRaises(P.D.ConfigError):
                P.apply(self.api, ROOT, self.path, self.manifest)
        client.assert_not_called()

    def test_free_space_floor_and_small_consumption_limit_are_enforced(self):
        for free in (199 * 1024**3, 887 * 1024**3):
            with patch.object(P, 'free_bytes', return_value=free), self.assertRaises(P.D.ConfigError):
                P.monitor({'space_before': 888 * 1024**3})

    def test_positive_preparation_freezes_original_row_and_private_environment(self):
        self.api.read_json = P.D.read_json
        upgrade = {'migration_payload': {'database_identity': {'database': 'fixture'}, 'heap_identity': 12},
                   'applied_result': {'applied_versions': [1, 2]}, 'candidate_image': self.state['candidate_image'],
                   'candidate_source_commit': 'a' * 40, 'network': 'fixture', 'backend_environment': {'SECRET': SECRET}}
        with patch.object(P, 'context', return_value=(self.manifest, upgrade)), \
                patch.object(P.N, 'collect', return_value={'blockers': [], 'selected_block': 171635973, 'sample_rows': 1}), \
                patch.object(P.B, 'clean_source', return_value=('a' * 40, '10.2.6')), \
                patch.object(P, 'client', return_value=self.raw) as client, \
                patch.object(P, 'free_bytes', return_value=888 * 1024**3), contextlib.redirect_stdout(io.StringIO()) as output:
            result = P.prepare(self.api, ROOT, Path('/fixture/upgrade/state.json'), 171635973, 1)
        path = Path(result['state'])
        saved = P.load(self.api, path, self.manifest)
        self.assertFalse(saved['writes_may_have_started'])
        self.assertEqual(saved['phase'], 'prepared')
        self.assertEqual(saved['baseline']['rows'][0]['private'], SECRET)
        self.assertEqual(saved['backend_environment']['SECRET'], SECRET)
        self.assertEqual(client.call_args.args[-1], 'inspect')
        self.assertNotIn(SECRET, output.getvalue())
        self.assertNotIn(SECRET, json.dumps(result))

    def test_run_cli_prepares_and_applies_the_same_saved_state(self):
        argv = ['pilot', '--run', '--upgrade-state', '/fixture/upgrade/state.json', '--block', '171635973', '--expected-rows', '1']
        with patch.object(P.sys, 'argv', argv), patch.object(P.sys, 'platform', 'linux'), \
                patch.object(P.os, 'geteuid', return_value=0), \
                patch.object(P.D, 'configuration', return_value=(self.manifest, {}, {})), \
                patch.object(P.M, 'global_lock', return_value=contextlib.nullcontext()), \
                patch.object(P.B, 'build_lock', return_value=contextlib.nullcontext()), \
                patch.object(P, 'prepare', return_value={'status': 'normalization_pilot_prepared', 'state': str(self.path)}) as prepare, \
                patch.object(P, 'apply', return_value={'status': 'verified', 'updated_rows': 1}) as apply, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            result = P.main()
        self.assertEqual(result, 0)
        self.assertEqual(prepare.call_args.args[-2:], (171635973, 1))
        self.assertEqual(apply.call_args.args[2], self.path)
        self.assertIn('normalization_pilot_prepared', output.getvalue())
        self.assertIn('verified', output.getvalue())

    def test_preparation_rejects_a_changed_selected_workload_before_state_creation(self):
        with patch.object(P, 'context', return_value=(self.manifest, {})), \
                patch.object(P.N, 'collect', return_value={'blockers': [], 'selected_block': 171635973, 'sample_rows': 2}):
            with self.assertRaises(P.D.ConfigError):
                P.prepare(self.api, ROOT, Path('/fixture/upgrade/state.json'), 171635973, 1)
        self.assertEqual(len(list(self.base.glob('pilot-*'))), 1)


if __name__ == '__main__':
    unittest.main()
