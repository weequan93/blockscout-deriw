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
REAL_CLIENT = J.Client


def resources():
    return {'settings': [['autovacuum', 'on', None], ['track_counts', 'on', None],
                         ['autovacuum_vacuum_threshold', '50', None], ['autovacuum_vacuum_scale_factor', '0.2', None]],
            'table': {'options': [], 'estimated_rows': 1776221184, 'dead_rows': 25000,
                      'total_bytes': 1598640365568, 'stats_reset': 'fixture',
                      'vacuum_count': 0, 'autovacuum_count': 0},
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
        self.migrate_limits = []
        self.requests = []
        self.lose_response = self.fail_before = self.drift = False
        self.raw_resources = resources()
        case = self
        class Client:
            def __init__(self, api, path, inputs, read_only=False):
                self.path = path
                self.read_only = read_only
                self.name = 'blockscout-v10-schema-' + 'a' * 16
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def request(self, command, payload=None):
                case.requests.append(command)
                case.assertFalse(self.read_only and command == 'migrate')
                if command == 'prepare_batch':
                    cursor = payload['before_block']
                    sample = {'block_rows': [{'block': cursor - n, 'rows': 1} for n in (1, 2)],
                              'selected_rows': 2, 'selected_bytes': 648, 'excluded_boundary_block': cursor - 3}
                    return {'resources': deepcopy(case.raw_resources), 'sample': sample,
                            'inspection': case.result([cursor - 1, cursor - 2])}
                if command == 'resources':
                    return deepcopy(case.raw_resources)
                if command == 'sample':
                    cursor = payload['before_block']
                    return {'block_rows': [{'block': cursor - n, 'rows': 1} for n in (1, 2)],
                            'selected_rows': 2, 'selected_bytes': 648, 'excluded_boundary_block': cursor - 3}
                if command == 'migrate':
                    case.migrate_limits.append(payload['wal_limit_bytes'])
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

    def prepare(self, execution_profile=None):
        directory = self.base / 'session-20261104T080000Z-1234abcd'
        directory.mkdir(mode=0o700)
        anchor = {'blocks': [100], 'block_rows': [{'block': 100, 'rows': 1}], 'expected_rows': 1,
                  'baseline': J.compact(J.P.baseline(self.result([100])))}
        script = b'frozen session script'
        inputs = {'environment': 'dev', 'chain_id': self.manifest['chain_id'], 'limits': deepcopy(J.LIMITS),
                  'script_sha256': J.R.digest(script), 'start_cursor': 100, 'tool_source_commit': 'a' * 40,
                  'upgrade_state': '/private/upgrade/state.json', 'runtime_before': {},
                  'candidate_image': 'sha256:' + 'a' * 64,
                  'anchor': anchor, 'space_before': 888 * 1024**3, 'created_epoch': J.time.time()}
        if execution_profile:
            inputs['execution_profile'] = execution_profile
            inputs['payload'] = {'execution_profile': execution_profile}
        state = {'format': J.FORMAT, 'inputs': inputs, 'inputs_sha256': J.R.digest(J.R.encoded(inputs)), 'phase': 'prepared',
                 'batches': 0, 'rows': 0, 'updated_rows': 0, 'charged_wal_bytes': 0, 'wal_measurements_complete': True,
                 'cursor': 100, 'pending': None, 'last': None, 'last_sha256': None, 'recent': [],
                 'checkpoint_hash': J.R.digest(J.R.encoded(inputs)), 'resources_before': resources(),
                 'preparation_complete': True}
        state['checkpoint_sha256'] = J.checkpoint_digest(state)
        J.R.write_new(directory / 'migrate.exs', script)
        J.R.write_new(directory / 'inputs.json', J.R.encoded(inputs))
        path = directory / 'state.json'
        J.R.write_new(path, J.R.encoded(state))
        return path

    def execute(self, path):
        with contextlib.redirect_stdout(io.StringIO()):
            return J.execute(self.api, ROOT, path, self.manifest)

    def continuation(self, path, profile='observation-v1', execution_profile=None):
        upgrade = {'candidate_image': 'sha256:' + 'a' * 64, 'candidate_source_commit': 'a' * 40,
                   'network': 'fixture', 'backend_environment': {},
                   'migration_payload': {'database_identity': {}, 'heap_identity': 1},
                   'applied_result': {'applied_versions': [1]}}
        self.api.configuration = lambda *a: (self.manifest, {}, {})
        with patch.object(J.P, 'context', return_value=(self.manifest, upgrade)), \
                patch.object(J.B, 'validate_state_directory'), contextlib.redirect_stdout(io.StringIO()):
            prepared = J.prepare(self.api, ROOT, path, after_session=True, profile=profile, execution_profile=execution_profile)
        return Path(prepared['state'])

    def prepare_reuse(self):
        from test_v10_normalization_reuse import review
        patch.dict(J.OBSERVATION_LIMITS, maintenance_max_rows=4, max_batches=2, max_rows=10).start()
        patch.dict(J.REUSE_LIMITS, max_batches=3, max_rows=6).start()
        previous = self.prepare()
        self.execute(previous)
        parent = self.continuation(previous)
        self.assertEqual(self.execute(parent)['stop_reason'], 'vacuum_review_required')
        state = J.load(parent, self.manifest)
        proof = review(parent, state)
        patch.object(J.Q, 'prepare_review', return_value=proof).start()
        patch.object(J.Q, 'reserve_review').start()
        self.holds = patch.object(J.Q, 'verify_holds').start()
        patch.object(J.P, 'free_bytes', return_value=proof['resources_after']['data_free_bytes']).start()
        self.raw_resources = deepcopy(proof['resources_after'])
        upgrade = {'candidate_image': state['inputs']['candidate_image'], 'candidate_source_commit': 'a' * 40,
                   'network': 'fixture', 'backend_environment': {},
                   'migration_payload': {'database_identity': {}, 'heap_identity': 1},
                   'applied_result': {'applied_versions': [1]}}
        with patch.object(J.P, 'context', return_value=(self.manifest, upgrade)), \
             patch.object(J.B, 'validate_state_directory'), contextlib.redirect_stdout(io.StringIO()):
            result = J.prepare(self.api, ROOT, parent, after_session=True, profile=J.Q.PROFILE,
                               after_vacuum=Path('/private/vacuum/state.json'))
        return parent, Path(result['state'])

    def test_reuse_retains_cumulative_origin_and_grants_one_finite_phase(self):
        parent, path = self.prepare_reuse()
        old = J.load(parent, self.manifest, check_resources=False)
        before = J.U.private(parent)
        inputs = J.load(path, self.manifest)['inputs']
        self.assertEqual(inputs['maintenance_origin'], old['inputs']['maintenance_origin'])
        self.assertEqual(inputs['space_before'], old['inputs']['space_before'])
        self.assertEqual(inputs['maintenance_rows_before'], 4)
        result = self.execute(path)
        self.assertEqual(result['selected_rows_verified'], 6)
        self.assertEqual(result['maintenance_selected_rows'], 10)
        self.assertEqual(result['stop_reason'], 'reuse_review_required')
        self.assertEqual(result['execution_profile'], 'lean-v1')
        self.assertFalse(result['sustained_full_table_run_enabled'])
        self.assertEqual(J.U.private(parent), before)
        self.holds.assert_called()
        for profile in ('pilot-v1', 'observation-v1', J.Q.PROFILE):
            with self.assertRaises(J.U.UpgradeError):
                self.continuation(path, profile=profile)

    def test_reuse_enforces_both_storage_baselines_and_reports_their_deltas(self):
        _, path = self.prepare_reuse()
        state = J.load(path, self.manifest)
        inputs = state['inputs']
        self.assertEqual(J.public(path, state)['reuse_review']['cumulative_space_limit_bytes'], 8 * 1024**3)
        after_free = inputs['reuse_review']['resources_after']['data_free_bytes']
        with patch.object(J.P, 'free_bytes', return_value=after_free - J.REUSE_LIMITS['space_bytes'] - 1):
            with self.assertRaisesRegex(J.U.UpgradeError, 'Post-vacuum'):
                self.execute(path)
        with patch.object(J.P, 'free_bytes', return_value=inputs['space_before'] - J.Q.TOTAL_SPACE_BYTES - 1):
            with self.assertRaisesRegex(J.U.UpgradeError, 'consumption'):
                self.execute(path)
        self.assertEqual(J.load(path, self.manifest)['rows'], 0)
        self.raw_resources['table']['total_bytes'] += J.REUSE_LIMITS['space_bytes'] + 1
        with self.assertRaisesRegex(J.U.UpgradeError, 'growth'):
            self.execute(path)
        self.assertIsNone(J.load(path, self.manifest)['pending'])

    def test_reuse_lost_commit_reconciles_once_and_never_resets_wal(self):
        _, path = self.prepare_reuse()
        count = self.migrates
        self.lose_response = True
        with self.assertRaises(J.U.UpgradeError):
            self.execute(path)
        self.requests.clear()
        result = self.execute(path)
        self.assertEqual(self.migrates, count + 1)
        self.assertEqual(self.requests[:2], ['inspect', 'inspect'])
        self.assertNotIn('migrate', self.requests)
        self.assertFalse(result['wal_measurements_complete'])
        self.assertEqual(result['charged_chunk_wal_bytes'], J.REUSE_LIMITS['wal_bytes'])
        self.assertFalse(result['pending_transaction'])

    def test_reuse_inputs_cannot_rebase_the_phase_or_drop_the_review(self):
        _, path = self.prepare_reuse()
        inputs = J.load(path, self.manifest)['inputs']
        for mutate in (lambda x: x.pop('reuse_review'), lambda x: x.update(maintenance_rows_before=0),
                       lambda x: x.update(space_before=883 * 1024**3),
                       lambda x: x['reuse_review'].update(completion_verified=False)):
            changed = deepcopy(inputs)
            mutate(changed)
            with self.assertRaises(J.U.UpgradeError):
                J.Q.validate_inputs(changed)

    def test_reuse_runtime_anchor_ignores_health_logs_and_detects_restarts(self):
        _, path = self.prepare_reuse()
        inputs = J.load(path, self.manifest)['inputs']
        runtime = {'backend': {'Id': 'a' * 64, 'Image': inputs['candidate_image'],
                              'State': {'StartedAt': 'fixed', 'Health': {'Log': ['old']}}, 'RestartCount': 0}}
        inputs['runtime_before'] = runtime
        inputs['reuse_review']['runtime_before_sha256'] = J.R.digest(J.R.encoded(J.D.stable(runtime)))
        runtime['backend']['State']['Health']['Log'] = ['new']
        J.Q.validate_inputs(inputs)
        runtime['backend']['RestartCount'] = 1
        with self.assertRaises(J.U.UpgradeError):
            J.Q.validate_inputs(inputs)

    def prepare_capacity(self):
        _, reuse = self.prepare_reuse()
        self.execute(reuse)
        patch.object(J.Q, 'CAPACITY_MIN_REUSE_ROWS', 6).start()
        patch.dict(J.CAPACITY_LIMITS, max_batches=4, max_rows=8).start()
        return reuse, self.continuation(reuse, profile=J.Q.CAPACITY_PROFILE)

    def test_capacity_preserves_the_completed_reuse_and_original_accounting(self):
        reuse, path = self.prepare_capacity()
        before = J.U.private(reuse)
        parent = J.load(reuse, self.manifest, check_resources=False)
        state = J.load(path, self.manifest)
        self.assertEqual(state['inputs']['maintenance_rows_before'], 10)
        self.assertEqual(state['inputs']['maintenance_origin'], parent['inputs']['maintenance_origin'])
        self.assertEqual(state['inputs']['space_before'], parent['inputs']['space_before'])
        self.assertEqual(J.profile_limits(state['inputs'])['session_request_limit'], 6000)
        self.assertEqual(state['inputs']['reuse_review'], parent['inputs']['reuse_review'])
        result = self.execute(path)
        self.assertEqual(result['selected_rows_verified'], 8)
        self.assertEqual(result['maintenance_selected_rows'], 18)
        self.assertEqual(result['stop_reason'], 'capacity_review_required')
        self.assertEqual(J.U.private(reuse), before)
        reservation = json.loads(J.U.private(reuse.parent / 'normalization-capacity.json'))
        self.assertEqual(reservation['state'], str(path))
        with self.assertRaisesRegex(J.U.UpgradeError, 'already reserved'):
            self.continuation(reuse, profile=J.Q.CAPACITY_PROFILE)
        for profile in (None, 'pilot-v1', 'observation-v1', J.Q.CAPACITY_PROFILE):
            with self.assertRaises(J.U.UpgradeError):
                self.continuation(path, profile=profile)

    def test_capacity_growth_allowance_is_frozen_and_checks_both_original_baselines(self):
        _, path = self.prepare_capacity()
        state = J.load(path, self.manifest)
        inputs = state['inputs']
        post_free = inputs['reuse_review']['resources_after']['data_free_bytes']
        with patch.object(J.P, 'free_bytes', return_value=post_free - 3 * 1024**3):
            J.monitor(path, inputs)
        with patch.object(J.P, 'free_bytes', return_value=post_free - 4 * 1024**3 - 1):
            with self.assertRaisesRegex(J.U.UpgradeError, 'Post-vacuum'):
                self.execute(path)
        with patch.object(J.P, 'free_bytes', return_value=inputs['space_before'] - 10 * 1024**3 - 1):
            with self.assertRaisesRegex(J.U.UpgradeError, 'consumption'):
                self.execute(path)
        raw = deepcopy(self.raw_resources)
        raw['table']['dead_rows'] = 1250000
        J.session_resources(state, raw)
        raw['table']['total_bytes'] += 4 * 1024**3 + 1
        with self.assertRaisesRegex(J.U.UpgradeError, 'growth'):
            J.session_resources(state, raw)
        self.assertIsNone(J.load(path, self.manifest)['pending'])

    def test_capacity_lost_reply_reconciles_without_repeating_updates(self):
        _, path = self.prepare_capacity()
        starting = self.migrates
        self.lose_response = True
        with self.assertRaises(J.U.UpgradeError):
            self.execute(path)
        self.requests.clear()
        result = self.execute(path)
        self.assertEqual(self.migrates, starting + 1)
        self.assertNotIn('migrate', self.requests)
        self.assertEqual(result['charged_chunk_wal_bytes'], 8 * 1024**3)
        self.assertFalse(result['wal_measurements_complete'])
        self.assertFalse(result['pending_transaction'])

    def test_capacity_requires_complete_reuse_measurements_and_cannot_rebase(self):
        _, path = self.prepare_capacity()
        original = J.load(path, self.manifest)['inputs']
        mutations = [lambda x: x['capacity_review'].update(wal_measurements_complete=False),
                     lambda x: x['capacity_review'].update(rows=5),
                     lambda x: x['capacity_review']['resources_after']['wal'].update(stats_reset='changed'),
                     lambda x: x.update(maintenance_rows_before=0),
                     lambda x: x['capacity_review'].update(vacuum_review_sha256='0' * 64)]
        for mutate in mutations:
            data = deepcopy(original)
            mutate(data)
            with self.assertRaises(J.U.UpgradeError):
                J.Q.validate_inputs(data)
        data = deepcopy(original)
        data['limits']['session_request_limit'] = 6001
        with self.assertRaises(J.U.UpgradeError):
            J.profile_limits(data)

    def test_capacity_window_expiring_during_prechecks_does_not_start_an_update(self):
        _, path = self.prepare_capacity()
        state = J.load(path, self.manifest)
        offset = [0]
        before_writes = self.migrates
        def inspection(*args):
            offset[0] = J.CAPACITY_LIMITS['start_seconds'] + 1
            return {}
        self.api.inspect_project = inspection
        with patch.object(J.time, 'time', side_effect=lambda: state['inputs']['created_epoch'] + offset[0]):
            result = self.execute(path)
        self.assertEqual(result['stop_reason'], 'new_write_window_expired')
        self.assertEqual(result['selected_rows_verified'], 0)
        self.assertFalse(result['pending_transaction'])
        self.assertEqual(self.migrates, before_writes)

    def test_lean_batches_reduce_requests_without_changing_limits_or_recovery_slots(self):
        previous = self.prepare()
        self.requests.clear()
        original = self.execute(previous)
        self.assertEqual(len(self.requests), 3 * 7 + 2)  # anchor, seven per batch, final resources
        path = self.continuation(previous, profile='pilot-v1', execution_profile='lean-v1')
        self.requests.clear()
        result = self.execute(path)
        self.assertEqual(self.requests, ['inspect'] + ['prepare_batch', 'resources', 'migrate', 'resources'] * 3 + ['resources'])
        self.assertEqual(result['execution_profile'], 'lean-v1')
        self.assertEqual(result['limits'], original['limits'])
        self.assertEqual(result['selected_rows_verified'], 6)
        self.assertEqual(result['timing_totals']['transaction_measurements'], 3)
        self.assertEqual(len(list(path.parent.glob('raw-*.json'))), 2)
        self.assertFalse(result['sustained_full_table_run_enabled'])

    def test_lean_lost_commit_reconciles_pending_rows_before_any_new_update(self):
        path = self.prepare(execution_profile='lean-v1')
        self.lose_response = True
        with self.assertRaises(J.U.UpgradeError):
            self.execute(path)
        state = J.load(path, self.manifest)
        self.assertEqual(state['cursor'], 100)
        self.assertIsNotNone(state['pending'])
        self.requests.clear()
        result = self.execute(path)
        self.assertEqual(self.migrates, 1)
        self.assertEqual(self.requests[:2], ['inspect', 'inspect'])  # anchor, uncertain target
        self.assertNotIn('prepare_batch', self.requests)
        self.assertNotIn('migrate', self.requests)
        self.assertEqual(result['charged_chunk_wal_bytes'], J.LIMITS['wal_bytes'])
        self.assertFalse(result['wal_measurements_complete'])
        self.assertFalse(result['pending_transaction'])

    def test_lean_preparation_cannot_reopen_the_vacuum_review_gate(self):
        previous = self.prepare()
        self.execute(previous)
        with patch.dict(J.OBSERVATION_LIMITS, max_batches=2, max_rows=10, maintenance_max_rows=4):
            path = self.continuation(previous, execution_profile='lean-v1')
            result = self.execute(path)
            self.assertEqual(result['stop_reason'], 'vacuum_review_required')
            with self.assertRaisesRegex(J.U.UpgradeError, 'vacuum'):
                self.continuation(path, execution_profile='lean-v1')

    def test_lean_resource_guard_still_stops_before_writes(self):
        path = self.prepare(execution_profile='lean-v1')
        self.raw_resources['transactions']['oldest_transaction_seconds'] = 301
        with self.assertRaises(J.U.UpgradeError):
            self.execute(path)
        self.assertEqual(self.migrates, 0)
        self.assertIsNone(J.load(path, self.manifest)['pending'])

    def test_execution_policy_is_frozen_and_combined_request_requires_opt_in(self):
        inputs = J.load(self.prepare(), self.manifest)['inputs']
        self.assertFalse(J.lean_execution(inputs))
        for policy, payload in [('unknown', 'unknown'), ('lean-v1', None), (None, 'lean-v1')]:
            with self.subTest(policy=policy, payload=payload), self.assertRaises(J.U.UpgradeError):
                J.lean_execution({**inputs, 'execution_profile': policy, 'payload': {'execution_profile': payload}})
        client = REAL_CLIENT(self.api, Path('/unused'), inputs)
        with patch.object(client, 'transport') as transport:
            with self.assertRaisesRegex(J.U.UpgradeError, 'requires lean-v1'):
                client.request('prepare_batch')
            transport.assert_not_called()

    def test_cli_cannot_change_execution_policy_on_resume(self):
        with patch.object(J.sys, 'platform', 'linux'), patch.object(J.os, 'geteuid', return_value=0), \
             patch.object(J.sys, 'argv', ['session', '--resume', '--state', '/saved/state.json', '--execution-profile', 'lean-v1']), \
             patch.object(J.D, 'configuration') as configure, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(J.main(), 1)
        configure.assert_not_called()

    def test_observation_profile_preserves_legacy_and_caps_each_transaction(self):
        previous = self.prepare()
        self.execute(previous)
        old = J.U.private(previous)
        with patch.dict(J.OBSERVATION_LIMITS, max_batches=5, max_rows=10, maintenance_max_rows=10):
            path = self.continuation(previous)
            self.assertEqual(J.load(path, self.manifest)['format'], J.PROFILE_FORMAT)
            result = self.execute(path)
            self.assertEqual(result['selected_rows_verified'], 10)
            self.assertEqual(result['stop_reason'], 'vacuum_review_required')
            self.assertEqual(result['timing_totals']['transaction_ms'], 50)
            self.assertEqual(result['timing_totals']['transaction_measurements'], 5)
            self.assertIn('finished_at', result['latest_attempt'])
            self.assertEqual(J.U.private(previous), old)
            self.assertTrue(all(n == 128 * 1024**2 for n in self.migrate_limits[-5:]))
            self.assertFalse(result['sustained_full_table_run_enabled'])
            with self.assertRaisesRegex(J.U.UpgradeError, 'vacuum'):
                self.continuation(path)

    def test_successor_inherits_storage_origin_and_row_allowance_even_with_pilot_profile(self):
        previous = self.prepare()
        self.execute(previous)
        with patch.dict(J.OBSERVATION_LIMITS, max_batches=2, max_rows=20, maintenance_max_rows=10):
            path = self.continuation(previous)
            self.execute(path)
            state = J.load(path, self.manifest)
            self.assertEqual(state['rows'], 4)
            next_path = self.continuation(path, profile='pilot-v1')
            next_state = J.load(next_path, self.manifest)
            self.assertEqual(next_state['inputs']['maintenance_rows_before'], 4)
            self.assertEqual(next_state['inputs']['maintenance_origin'], state['inputs']['maintenance_origin'])
            result = self.execute(next_path)
            self.assertEqual(result['maintenance_selected_rows'], 10)
            self.assertEqual(result['stop_reason'], 'vacuum_review_required')
            with self.assertRaisesRegex(J.U.UpgradeError, 'vacuum'):
                self.continuation(next_path, profile='pilot-v1')

    def test_observation_lost_commit_charges_entire_epoch_allowance(self):
        previous = self.prepare()
        self.execute(previous)
        path = self.continuation(previous)
        self.lose_response = True
        with self.assertRaises(J.U.UpgradeError):
            self.execute(path)
        written = self.migrates
        result = self.execute(path)
        self.assertEqual(self.migrates, written)
        self.assertEqual(result['charged_chunk_wal_bytes'], J.OBSERVATION_LIMITS['wal_bytes'])
        self.assertFalse(result['wal_measurements_complete'])
        self.assertEqual(result['timing_totals']['transaction_measurements'], 0)
        self.assertFalse(result['pending_transaction'])

    def test_profiles_refuse_custom_limits_and_finished_observation_survives_space_drift(self):
        previous = self.prepare()
        self.execute(previous)
        with patch.dict(J.OBSERVATION_LIMITS, max_batches=1):
            path = self.continuation(previous)
            self.execute(path)
            written = self.migrates
            with patch.object(J.P, 'free_bytes', return_value=880 * 1024**3):
                result = self.execute(path)
            self.assertEqual(result['completed_batches'], 1)
            self.assertEqual(self.migrates, written)
            with patch.dict(J.OBSERVATION_LIMITS, wal_bytes=8 * 1024**3), self.assertRaises(J.U.UpgradeError):
                J.load(path, self.manifest, check_resources=False)

    def test_observation_space_and_maintenance_origin_are_enforced_before_writes(self):
        previous = self.prepare()
        self.execute(previous)
        path = self.continuation(previous)
        written = self.migrates
        inputs = J.load(path, self.manifest)['inputs']
        with patch.object(J.P, 'free_bytes', return_value=inputs['space_before'] - 2 * 1024**3 - 1), \
                self.assertRaisesRegex(J.U.UpgradeError, 'free-space/consumption'):
            self.execute(path)
        self.raw_resources['table']['total_bytes'] += 2 * 1024**3 + 1
        with self.assertRaisesRegex(J.U.UpgradeError, 'growth'):
            self.execute(path)
        self.assertEqual(self.migrates, written)

    def test_nearly_exhausted_maintenance_allowance_refuses_a_complete_block_batch(self):
        previous = self.prepare()
        self.execute(previous)
        with patch.dict(J.OBSERVATION_LIMITS, maintenance_max_rows=3):
            path = self.continuation(previous)
            result = self.execute(path)
            self.assertEqual(result['selected_rows_verified'], 2)
            self.assertEqual(result['stop_reason'], 'next_complete_batch_requires_vacuum_review')
            self.assertFalse(result['pending_transaction'])
            with self.assertRaisesRegex(J.U.UpgradeError, 'vacuum'):
                self.continuation(path, profile='pilot-v1')

    def test_timing_totals_survive_recent_receipt_rotation(self):
        previous = self.prepare()
        self.execute(previous)
        with patch.dict(J.OBSERVATION_LIMITS, max_batches=30, max_rows=60):
            path = self.continuation(previous)
            result = self.execute(path)
            saved = J.load(path, self.manifest)
            self.assertEqual(len(saved['recent']), 25)
            self.assertEqual(result['timing_totals']['transaction_measurements'], 30)
            self.assertEqual(result['timing_totals']['transaction_ms'], 300)
            saved['timing_totals']['transaction_ms'] = 0
            J.sync_json(path, saved)
            with self.assertRaisesRegex(J.U.UpgradeError, 'checkpoint'):
                J.load(path, self.manifest)

    def test_observation_monitor_uses_frozen_profile_instead_of_legacy_space_budget(self):
        previous = self.prepare()
        self.execute(previous)
        with patch.dict(J.OBSERVATION_LIMITS, max_batches=1):
            path = self.continuation(previous)
            with patch.object(J.P, 'free_bytes', return_value=888 * 1024**3 - 600 * 1024**2):
                result = self.execute(path)
            self.assertEqual(result['completed_batches'], 1)

    def test_health_refresh_reserves_time_for_client_prechecks(self):
        path = self.prepare()
        calls = [0]
        def monotonic():
            calls[0] += 1
            return 0 if calls[0] == 1 else 20
        with patch.dict(J.LIMITS, max_batches=1):
            # Freeze the same smaller profile for this fixture.
            state = json.loads(J.U.private(path))
            state['inputs']['limits'] = deepcopy(J.LIMITS)
            J.sync_json(path.parent / 'inputs.json', state['inputs'])
            state['inputs_sha256'] = J.R.digest(J.R.encoded(state['inputs']))
            state['checkpoint_hash'] = state['inputs_sha256']
            state['checkpoint_sha256'] = J.checkpoint_digest(state)
            J.sync_json(path, state)
            with patch.object(J.time, 'monotonic', side_effect=monotonic), \
                    patch.object(J.P, 'context', return_value=({}, {})) as context:
                result = self.execute(path)
        self.assertEqual(result['completed_batches'], 1)
        # Twenty seconds is below the SQL cutoff but must refresh before DML.
        self.assertGreaterEqual(context.call_count, 3)  # initial, renewal, final

    def test_close_unapplied_verifies_original_rows_and_preserves_accounting_after_space_drift(self):
        path = self.prepare()
        self.fail_before = True
        with self.assertRaises(J.U.UpgradeError):
            self.execute(path)
        before = J.load(path, self.manifest)
        pending = before['pending']
        snapshot = J.U.private(path.parent / pending['slot'])
        frozen = J.U.private(path.parent / 'inputs.json')
        with patch.object(J.P, 'free_bytes', return_value=885 * 1024**3):
            result = J.close_unapplied(self.api, ROOT, path, self.manifest)
        self.assertEqual(result['stop_reason'], 'unchanged_pending_target_closed')
        self.assertFalse(result['pending_transaction'])
        self.assertFalse(result['closure']['database_writes'])
        after = J.load(path, self.manifest, check_resources=False)
        for key in ('rows', 'batches', 'charged_wal_bytes', 'cursor', 'checkpoint_sha256'):
            self.assertEqual(after[key], before[key])
        self.assertEqual(after['latest_attempt'], before['latest_attempt'])
        self.assertEqual(after['abandoned'], pending)
        self.assertEqual(J.U.private(path.parent / pending['slot']), snapshot)
        self.assertEqual(J.U.private(path.parent / 'inputs.json'), frozen)
        self.assertEqual(self.migrates, 0)
        self.assertEqual(J.close_unapplied(self.api, ROOT, path, self.manifest)['status'], 'normalization_session_finished')

    def test_close_unapplied_cannot_discard_a_committed_pending_target(self):
        path = self.prepare()
        self.lose_response = True
        with self.assertRaises(J.U.UpgradeError):
            self.execute(path)
        with self.assertRaisesRegex(J.U.UpgradeError, 'not unchanged originals'):
            J.close_unapplied(self.api, ROOT, path, self.manifest)
        state = J.load(path, self.manifest)
        self.assertTrue(state['pending'])
        self.assertEqual(state['cursor'], 100)
        self.assertEqual(self.migrates, 1)
        result = self.execute(path)
        self.assertEqual(self.migrates, 1)
        self.assertEqual(result['charged_chunk_wal_bytes'], J.LIMITS['wal_bytes'])

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

    def test_rejected_preparation_keeps_its_path_and_resource_evidence_but_cannot_execute(self):
        after_path = self.base / 'previous-pilot.json'
        J.R.write_new(after_path, b'previous verified state')
        previous = {'phase': 'applied', 'writes_may_have_started': True, 'format': J.P.FORMAT,
                    'block': 100, 'expected_rows': 1, 'candidate_image': 'sha256:' + 'a' * 64,
                    'upgrade_state': '/private/upgrade/state.json', 'baseline': J.P.baseline(self.result([100]))}
        upgrade = {'candidate_image': previous['candidate_image'], 'candidate_source_commit': 'a' * 40,
                   'network': 'fixture', 'backend_environment': {},
                   'migration_payload': {'database_identity': {}, 'heap_identity': 1},
                   'applied_result': {'applied_versions': [1]}}
        self.api.configuration = lambda *a: (self.manifest, {}, {})
        self.raw_resources['transactions']['oldest_transaction_seconds'] = 301
        with patch.object(J.P, 'load', return_value=previous), patch.object(J.P, 'status', return_value={'status': 'verified'}), \
                patch.object(J.P, 'context', return_value=(self.manifest, upgrade)), \
                patch.object(J.B, 'validate_state_directory'), contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(J.U.UpgradeError) as raised:
                J.prepare(self.api, ROOT, after_path)
        path = raised.exception.state_path
        self.assertIn(str(path), output.getvalue())
        state = J.load(path, self.manifest)
        report = json.loads(J.U.private(path.parent / 'resources-before.json'))
        self.assertEqual(report['transactions']['oldest_transaction_seconds'], 301)
        self.assertFalse(J.public(path, state)['preparation_complete'])
        with self.assertRaisesRegex(J.U.UpgradeError, 'Preparation was incomplete'):
            self.execute(path)
        self.assertEqual(self.migrates, 0)

    def test_read_only_client_can_observe_after_consumption_drift_but_cannot_send_migrate(self):
        path = self.prepare()
        inputs = J.load(path, self.manifest)['inputs']
        channel = path.parent / 'channel'
        channel.mkdir(mode=0o700)
        log_path = path.parent / 'client.log.private'
        J.R.write_new(log_path, b'')
        client = REAL_CLIENT(self.api, path, inputs, read_only=True)
        client.process = types.SimpleNamespace(poll=lambda: None)
        response = {'observed': True}
        sent = []
        def transport(command, **kwargs):
            sent.append(command)
            request = json.loads(J.U.private(channel / 'request.json'))
            return json.dumps({'id': request['id'], 'ok': True, 'data': response})
        with log_path.open('ab') as client.log, patch.object(J.P, 'free_bytes', return_value=885 * 1024**3), \
                patch.object(client, 'transport', side_effect=transport):
            self.assertEqual(client.request('resources'), response)
            count = len(sent)
            with self.assertRaisesRegex(J.U.UpgradeError, 'cannot request normalization writes'):
                client.request('migrate')
            self.assertEqual(len(sent), count)
            normal = REAL_CLIENT(self.api, path, inputs)
            with patch.object(normal, 'transport') as normal_transport:
                with self.assertRaisesRegex(J.U.UpgradeError, 'free-space/consumption'):
                    normal.request('migrate')
                normal_transport.assert_not_called()

    def test_diagnosis_reads_saved_and_backend_metadata_without_old_budget_or_a_new_client(self):
        path = self.prepare()
        state = J.load(path, self.manifest)
        state['inputs'].update(candidate_image='sha256:' + 'a' * 64, payload={'database_identity': {'oid': 42}})
        frozen = J.R.encoded(state['inputs'])
        J.sync_json(path.parent / 'inputs.json', state['inputs'])
        state['inputs_sha256'] = J.R.digest(frozen)
        state['checkpoint_hash'] = state['inputs_sha256']
        state['checkpoint_sha256'] = J.checkpoint_digest(state)
        state['resources_before']['transactions']['oldest_transaction_seconds'] = 323
        J.sync_json(path, state)
        before = J.U.private(path)
        live = {'observer': {'database_oid': 42, 'role_oid': 10, 'can_read_all_stats': True},
                'oldest_transaction_seconds': 0, 'long_client_sessions': []}
        self.api.require = J.P.require
        self.api.ConfigError = J.D.ConfigError
        self.api.inspect_project = lambda *a: {'backend': {'Image': state['inputs']['candidate_image']}}
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            return 'BLOCKSCOUT_DEPLOY_CHECK=' + json.dumps({'database_identity': {'oid': 42}, 'transactions': live})
        self.api.run = run
        with patch.object(J.P, 'free_bytes', return_value=885 * 1024**3), \
                patch.object(J.P, 'monitor', side_effect=AssertionError('old write budget used')), \
                patch.object(J, 'Client', side_effect=AssertionError('new container started')):
            report = J.diagnose(self.api, ROOT, path, self.manifest)
        self.assertEqual(report['saved_transactions']['oldest_transaction_seconds'], 323)
        self.assertEqual(report['live_backend_transactions'], live)
        self.assertTrue(report['old_session_consumption_limit_exceeded'])
        self.assertTrue(report['current_free_space_floor_met'])
        self.assertFalse(report['new_client_started'])
        self.assertFalse(report['database_writes'])
        self.assertEqual(J.U.private(path), before)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:5], ['docker', 'exec', 'backend', '/app/bin/blockscout', 'rpc'])
        self.assertIn('SET TRANSACTION READ ONLY', calls[0][5])
        self.assertNotIn('pg_terminate_backend', calls[0][5])

        self.api.run = lambda *a, **k: 'BLOCKSCOUT_DEPLOY_CHECK=' + json.dumps({'database_identity': {'oid': 43}, 'transactions': live})
        report = J.diagnose(self.api, ROOT, path, self.manifest)
        self.assertIn('Backend database differs', report['live_backend_error'])
        self.assertNotIn('live_backend_transactions', report)

        self.api.run = lambda *a, **k: (_ for _ in ()).throw(J.D.ConfigError('private connection secret'))
        report = J.diagnose(self.api, ROOT, path, self.manifest)
        self.assertIn('live_backend_error', report)
        self.assertEqual(report['saved_transactions']['oldest_transaction_seconds'], 323)
        self.assertNotIn('private connection secret', json.dumps(report))

    def test_query_diagnosis_maps_services_and_suppresses_addresses_and_unknown_fields(self):
        runtime = {
            'backend': {'NetworkSettings': {'Networks': {'fixture': {'IPAddress': '172.30.0.2'}}}},
            'stats': {'NetworkSettings': {'Networks': {'fixture': {'IPAddress': '172.30.0.3'}}}},
        }
        row = {'pid': 33301, 'client_address': '::ffff:172.30.0.3',
               'query_md5': 'a' * 32, 'source_query_matches': ['blocks_consensus_count'],
               'query': 'private-query-with-parameters', 'application_name': 'private-application'}
        result = J.query_diagnostics([row], runtime)
        self.assertEqual(result[0]['client_service_candidates'], ['stats'])
        self.assertEqual(result[0]['client_service_mapping'], 'unique')
        self.assertEqual(result[0]['source_query_matches'], ['blocks_consensus_count'])
        public = json.dumps(result)
        for private in ('172.30.0.3', 'private-query', 'private-application', 'client_address'):
            self.assertNotIn(private, public)
        runtime['frontend'] = deepcopy(runtime['stats'])
        self.assertEqual(J.query_diagnostics([row], runtime)[0]['client_service_mapping'], 'ambiguous')
        row['client_address'] = '172.30.1.99'
        unmatched = J.query_diagnostics([row], runtime)[0]
        self.assertEqual(unmatched['client_service_mapping'], 'unmapped')
        self.assertEqual(unmatched['client_service_candidates'], [])
        with self.assertRaises(J.U.UpgradeError):
            J.query_diagnostics([row] * 21, runtime)


if __name__ == '__main__':
    unittest.main()
