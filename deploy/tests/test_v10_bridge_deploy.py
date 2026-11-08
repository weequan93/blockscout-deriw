from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import v10_bridge_deploy as P
import v10_native_normalization as N


class BridgeDeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        self.base = Path(self.temp.name).resolve()
        patch.object(N, 'BASE', self.base).start()
        self.directory = self.base / 'job-20261106T103901Z-1234abcd'
        self.directory.mkdir(mode=0o700)
        self.native_path = self.directory / 'state.json'
        script = b'frozen native callback'
        self.inputs = {'tool_source_commit': 'a' * 40, 'chain_id': '18417507517',
            'source_root': '/data/blockscout-v10-rehearsal', 'python_executable': '/usr/bin/python3',
            'candidate_image': 'sha256:' + 'b' * 64, 'upgrade_state': '/private/upgrade/state.json',
            'runtime_before': {'backend': {'Image': 'old'}},
            'min_free_bytes': N.MIN_FREE, 'pause_free_bytes': N.PAUSE_FREE, 'resume_free_bytes': N.RESUME_FREE,
            'cumulative_growth_cap_bytes': None, 'maximum_batch_blocks': 1000,
            'script_sha256': N.R.digest(script), 'client_name': 'blockscout-v10-native-' + 'a' * 16,
            'manual_phase_rows': 4999097, 'maintenance_selected_rows': 11294954,
            'payload': {'token': 'a' * 16, 'application_name': 'blockscout-v10-native-' + 'a' * 16,
                        'start_before_block': 131385496}}
        self.native = {'format': N.FORMAT, 'inputs': self.inputs,
                       'inputs_sha256': N.R.digest(N.R.encoded(self.inputs)), 'phase': 'paused',
                       'batch_blocks': 500, 'reason': 'operator_requested',
                       'database': {'native_meta': {'max_block_number': 131383995}}}
        N.R.write_new(self.directory / 'inputs.json', N.R.encoded(self.inputs))
        N.R.write_new(self.directory / 'native.exs', script)
        N.R.write_new(self.native_path, N.R.encoded(self.native))
        self.release_dir = self.base / 'release-20261106T110000Z-1234abcd'
        self.release_dir.mkdir(mode=0o700)
        self.release_path = self.release_dir / 'state.json'
        self.release = {'status': 'active', 'bridge_patch': True, 'bridge_stage': 'active',
                        'normalization_state': str(self.native_path), 'candidate_source_commit': 'c' * 40,
                        'source_root': str(ROOT), 'before': {'backend': {'Image': 'old'}},
                        'image_ids': {'forward': {'backend': 'new'}, 'rollback': {'backend': 'old'}}}
        N.R.write_new(self.release_path, N.R.encoded(self.release))
        self.runtime = {'backend': {'Image': 'new'}}
        self.api = types.SimpleNamespace(configuration=lambda *a: ({}, {}, {}),
            inspect_project=lambda *a, **k: self.runtime, ensure_stable=lambda *a: None,
            run=lambda *a, **k: '', ConfigError=N.D.ConfigError)

    def binding_context(self):
        stack = __import__('contextlib').ExitStack()
        stack.enter_context(patch.object(P, 'load', return_value=(self.release, {'native_inputs_sha256': self.native['inputs_sha256'], 'before': self.release['before']})))
        stack.enter_context(patch.object(P.Q, 'verify_holds'))
        stack.enter_context(patch.object(P.R, 'unchanged_others'))
        stack.enter_context(patch.object(N.R, 'load_state', return_value=self.release))
        return stack

    def test_source_review_requires_only_the_exact_reviewed_application_change(self):
        calls = []
        def run(args):
            calls.append(args)
            if 'diff' in args:
                return P.WORKER + '\ndeploy/scripts/v10_bridge_deploy.py\n'
            if 'show' in args:
                return 'reviewed worker'
            return ''
        self.api.run = run
        with patch.object(P.B, 'clean_source', return_value=('c' * 40, '10.2.6')):
            self.assertEqual(P.source_review(self.api, ROOT)[0], 'c' * 40)
        self.assertTrue(any('--is-ancestor' in c for c in calls))
        self.api.run = lambda args: 'apps/explorer/lib/unrelated.ex\n' if 'diff' in args else ''
        with patch.object(P.B, 'clean_source', return_value=('c' * 40, '10.2.6')):
            with self.assertRaisesRegex(N.D.ConfigError, 'another application'):
                P.source_review(self.api, ROOT)

    def test_snapshot_preserves_nft_database_and_environment_and_removes_old_ownership(self):
        model = {'services': {name: {'image': 'old-' + name, 'environment': {'SECRET': 'private-canary'},
                         'labels': {N.U.STATE_LABEL: '/old', N.U.ROLE_LABEL: name}}
                         for name in ('backend', 'nft_media_handler', 'db')}}
        rows = {name: {'Image': 'old-' + name, 'Config': {'Env': []}} for name in model['services']}
        result = P.snapshot(model, rows, 'new-backend', 'c' * 40, self.release_path, 'forward')
        for name in ('nft_media_handler', 'db'):
            self.assertEqual(result['services'][name]['image'], model['services'][name]['image'])
            self.assertEqual(result['services'][name]['environment'], model['services'][name]['environment'])
        backend = result['services']['backend']
        self.assertEqual(backend['command'], N.R.START)
        self.assertNotIn(N.U.STATE_LABEL, backend['labels'])
        self.assertEqual(backend['labels'][N.R.STATE_LABEL], str(self.release_path))
        self.assertEqual(backend['environment'][P.V.HOLD], 'true')
        self.assertEqual(backend['environment']['SECRET'], 'private-canary')

    def test_verified_binding_keeps_frozen_inputs_worker_and_database_cursor(self):
        before = {name: (self.directory / name).read_bytes() for name in ('inputs.json', 'native.exs')}
        with self.binding_context():
            result = P.bind_native(self.api, self.release_path)
            native = N.load(self.native_path)
            binding = N.runtime_binding(self.native_path, native)
            P.bind_native(self.api, self.release_path)
        self.assertEqual(result['status'], 'native_normalization_prepared')
        self.assertEqual(native['database']['native_meta']['max_block_number'], 131383995)
        self.assertEqual(binding['runtime_before'], self.runtime)
        self.assertEqual(binding['tool_source_commit'], 'c' * 40)
        self.assertIsNone(native['inputs']['cumulative_growth_cap_bytes'])
        for name, raw in before.items():
            self.assertEqual((self.directory / name).read_bytes(), raw)

    def test_failed_deployment_cannot_create_a_native_runtime_binding(self):
        self.release['status'] = 'activation_requested'
        with self.binding_context(), self.assertRaisesRegex(N.D.ConfigError, 'verified deployment'):
            P.bind_native(self.api, self.release_path)
        self.assertFalse((self.directory / 'bridge-runtime-binding.json').exists())
        self.assertEqual(N.load(self.native_path)['phase'], 'paused')

    def test_changed_release_or_binding_is_rejected_without_rewinding_progress(self):
        with self.binding_context():
            P.bind_native(self.api, self.release_path)
            N.R.update_state(self.release_path, {**self.release, 'status': 'needs_attention'})
            with self.assertRaisesRegex(N.U.UpgradeError, 'verified bridge deployment'):
                N.load(self.native_path)
        native = json.loads(N.U.private(self.native_path))
        self.assertEqual(native['database']['native_meta']['max_block_number'], 131383995)

    def test_new_controller_uses_verified_runtime_and_preserves_old_job_source(self):
        with self.binding_context():
            P.bind_native(self.api, self.release_path)
            native = N.load(self.native_path)
            binding = N.runtime_binding(self.native_path, native)
        db = {'native_status': 'started', 'native_meta': {'v10_native_job': 'a' * 16, 'max_block_number': 131383495},
              'autovacuum_enabled': True, 'long_client_transactions': 0, 'dead_rows': 0}
        def stop(_):
            N.STOP_REQUESTED = True
        with patch.object(N, 'runtime_binding', return_value=binding), \
             patch.object(N.B, 'clean_source', return_value=('c' * 40, '10.2.6')), \
             patch.object(N, 'reserve_sources'), patch.object(N, 'STOP_REQUESTED', False), \
             patch.object(N.Q, 'verify_holds'), patch.object(N.U, 'load_state') as old_upgrade, \
             patch.object(N.R, 'verify', return_value={'status': 'verified'}) as health, \
             patch.object(N.J.P, 'free_bytes', return_value=800 * 1024**3), \
             patch.object(N, 'rpc', return_value=db), patch.object(N, 'launch'), \
             patch.object(N, 'owned', return_value=None), patch.object(N, 'read_worker', return_value={'phase': 'running'}), \
             patch.object(N, 'lease') as lease, patch.object(N, 'stop_client'), patch.object(N.time, 'sleep', side_effect=stop):
            result = N.run(self.api, ROOT, self.native_path)
        old_upgrade.assert_not_called()
        health.assert_called_once()
        self.assertTrue(lease.call_args.args[2])
        self.assertEqual(result['native_cursor'], 131383495)
        self.assertEqual(json.loads(N.U.private(self.native_path))['inputs']['tool_source_commit'], 'a' * 40)

    def test_service_handoff_replaces_only_its_original_service_definition(self):
        with self.binding_context():
            P.bind_native(self.api, self.release_path)
            native = N.load(self.native_path)
            binding = N.runtime_binding(self.native_path, native)
        systemd = self.base / 'systemd'
        systemd.mkdir()
        target = systemd / N.UNIT
        def render(path, root, executable):
            return str(root) + ' ' + str(executable) + ' ' + str(path)
        original = render(self.native_path, Path(self.inputs['source_root']), self.inputs['python_executable'])
        target.write_text(original)
        with patch.object(N, 'runtime_binding', return_value=binding), \
             patch.object(N, 'SYSTEMD_DIRECTORY', systemd), patch.object(N, 'render_service', side_effect=render), \
             patch.object(N.B, 'clean_source', return_value=('c' * 40, '10.2.6')):
            result = N.install_service(self.api, self.native_path)
            self.assertTrue(result['service_start_requested'])
            self.assertIn(str(ROOT), target.read_text())
            self.assertEqual((self.directory / 'service-before-bridge.private').read_text(), original)
            target.write_text('unrelated service')
            with self.assertRaisesRegex(N.U.UpgradeError, 'Another native service'):
                N.install_service(self.api, self.native_path)

    def test_activation_is_journaled_before_recreation_and_holds_before_resume(self):
        self.release.update(status='prepared', bridge_stage='candidate_verified', deferred_migrations=[])
        inputs = {'source': {'tag': 'candidate'}, 'before': {'backend': {'Id': 'old'}}, 'database_anchor': {'cursor': 10}}
        events = []
        def recreate(*args):
            events.append('recreate')
            saved = json.loads(N.U.private(self.release_path))
            self.assertEqual(saved['status'], 'activation_requested')
        with patch.object(P, 'load', return_value=(self.release, inputs)), \
             patch.object(P.B, 'clean_source', return_value=('c' * 40, '10.2.6')), \
             patch.object(P.R, 'load_state'), patch.object(P.R, 'unchanged_others'), \
             patch.object(P.B, 'image_info', return_value={'Id': 'new'}), \
             patch.object(P, 'database_anchor', return_value={'cursor': 10}), \
             patch.object(P.R, 'recreate', side_effect=recreate), \
             patch.object(P, 'restore_holds', side_effect=lambda *a: events.append('holds')), \
             patch.object(P.N, 'rpc', return_value={'settings_ok': True, 'automatic_migrators_absent': True}), \
             patch.object(P.R, 'verify', return_value={'status': 'verified'}), patch.object(P.Q, 'verify_holds'):
            result = P.apply(self.api, self.release_path)
        self.assertEqual(events, ['recreate', 'holds'])
        self.assertEqual(result['recorded_phase'], 'active')

    def test_changed_database_anchor_keeps_native_writer_paused(self):
        self.release.update(status='prepared', bridge_stage='candidate_verified')
        inputs = {'source': {'tag': 'candidate'}, 'before': {'backend': {'Id': 'old'}}, 'database_anchor': {'cursor': 10}}
        with patch.object(P, 'load', return_value=(self.release, inputs)), \
             patch.object(P.B, 'clean_source', return_value=('c' * 40, '10.2.6')), \
             patch.object(P.R, 'load_state'), patch.object(P.R, 'unchanged_others'), \
             patch.object(P.B, 'image_info', return_value={'Id': 'new'}), \
             patch.object(P, 'database_anchor', side_effect=[{'cursor': 10}, {'cursor': 9}]), \
             patch.object(P.R, 'recreate'), patch.object(P, 'restore_holds'), \
             patch.object(P.R, 'verify') as verify, patch.object(P, 'bind_native') as bind:
            with self.assertRaisesRegex(N.D.ConfigError, 'across bridge deployment'):
                P.apply(self.api, self.release_path)
        verify.assert_not_called()
        bind.assert_not_called()
        self.assertEqual(N.load(self.native_path)['phase'], 'paused')

    def test_changed_migration_inventory_rejects_image_before_release_or_startup(self):
        inputs = {'source': {'tag': 'candidate'}, 'migration_inventory': {'migrations': {'old': 'old'}}}
        with patch.object(P.B, 'image_info', return_value={'Id': 'new'}), \
             patch.object(P.V, 'validate_image'), \
             patch.object(P.C, 'image_inventory', return_value={'migrations': {'new': 'new'}}), \
             patch.object(P.C, 'pending_migrations') as pending, patch.object(P.B, 'smoke_image') as smoke:
            with self.assertRaisesRegex(N.D.ConfigError, 'migration bytes'):
                P.build(self.api, self.release_path, self.release, inputs, {})
        pending.assert_not_called()
        smoke.assert_not_called()

    def test_managed_model_keeps_visibility_of_explicit_source_overrides(self):
        self.release.update(source_upgrade_state='/private/upgrade/state.json', source_images={'backend': 'captured'})
        context = {'state': self.release, 'path': self.release_path, 'side': 'forward', 'image_ids': {'backend': 'new'}}
        upgrade = {'source_environment': {'backend': {'BLOCKSCOUT_VERSION': None}}}
        def model(image, version):
            return {'services': {'backend': {'image': image, 'labels': {N.U.STATE_LABEL: '/old'},
                                             'environment': {'BLOCKSCOUT_VERSION': version}}}}
        with patch.object(P.U, 'load_state', return_value=upgrade), \
             patch.object(P.U, 'active_model', side_effect=lambda api, value, ctx: deepcopy(value)):
            preserved = P.active_model(self.api, model('captured', None), context)
            changed = P.active_model(self.api, model('explicit-image', 'explicit-version'), context)
        self.assertEqual(preserved['services']['backend']['image'], 'new')
        self.assertEqual(preserved['services']['backend']['environment']['BLOCKSCOUT_VERSION'], 'c' * 40)
        self.assertEqual(changed['services']['backend']['image'], 'explicit-image')
        self.assertEqual(changed['services']['backend']['environment']['BLOCKSCOUT_VERSION'], 'explicit-version')

    def test_repeated_run_selects_its_saved_release_without_another_handoff(self):
        self.release['normalization_state'] = str(self.native_path)
        N.R.update_state(self.release_path, self.release)
        with patch.object(P.D, 'MANAGED_RELEASES', self.base):
            self.assertEqual(P.existing_release(self.native_path), self.release_path)

    def test_partial_service_handoff_can_resume_from_the_original_unit(self):
        systemd = self.base / 'systemd'
        systemd.mkdir()
        render = lambda path, root, executable: str(root)
        (systemd / N.UNIT).write_text(self.inputs['source_root'])
        self.native['phase'] = 'prepared'
        self.api.run = lambda *a, **k: 'inactive'
        with patch.object(N, 'SYSTEMD_DIRECTORY', systemd), \
             patch.object(N, 'render_service', side_effect=render), \
             patch.object(N, 'load', return_value=self.native), \
             patch.object(N, 'runtime_binding', return_value={'source_root': '/data/new-release'}), \
             patch.object(N, 'owned', return_value=None), patch.object(N, 'stop_client'), \
             patch.object(N, 'rpc', return_value={'remaining_native_connections': 0}):
            result = P.pause_native(self.api, self.native_path)
        self.assertEqual(result['inputs'], self.inputs)
        self.assertTrue((self.directory / 'pause.request').exists())

    def test_preparation_freezes_the_paused_job_and_existing_release_environment(self):
        original_image = self.inputs['candidate_image']
        before = {'backend': {'Image': original_image}}
        self.inputs['runtime_before'] = before
        self.native['inputs_sha256'] = N.R.digest(N.R.encoded(self.inputs))
        N.R.update_state(self.directory / 'inputs.json', self.inputs)
        N.R.update_state(self.native_path, self.native)
        self.api.inspect_project = lambda *a, **k: before
        self.api.configuration = lambda *a: ({'chain_id': '18417507517', 'compose_project': 'fixture'}, {}, {})
        self.api.env_map = lambda row: {'BLOCKSCOUT_VERSION': P.ORIGINAL, 'PRIVATE': 'fixture-secret'}
        self.api.live_files = lambda *a: []
        self.api.compose_model = lambda *a: {'services': {'backend': {'environment': {}}}}
        self.api.live_environment_differences = lambda *a: []
        self.api.read_json = lambda path: json.loads(path.read_text())
        source = {'source_commit': 'c' * 40, 'release_version': '10.2.6', 'platform': 'linux/amd64',
                  'build_arguments': {}, 'baseline_runtime_user': 'fixture'}
        upgrade = {'candidate_image': original_image, 'source_images': {'backend': 'public-image'}}
        output = self.base / 'releases'
        output.mkdir(mode=0o700)
        with patch.object(P, 'source_review', return_value=('c' * 40, '10.2.6')), \
             patch.object(P.D, 'MANAGED_RELEASES', output), patch.object(P.Q, 'verify_holds'), \
             patch.object(P.U, 'load_state', return_value=upgrade), \
             patch.object(P.B, 'source_plan', return_value=source), patch.object(P.B, 'validate_state_directory'), \
             patch.object(P.N.J.P, 'free_bytes', return_value=800 * 1024**3), \
             patch.object(P.os, 'statvfs', return_value=types.SimpleNamespace(f_bavail=20 * 1024**3, f_frsize=1)), \
             patch.object(P.C, 'image_inventory', return_value={'identity': [1, 1], 'migrations': {}}), \
             patch.object(P, 'database_anchor', return_value={'cursor': 131383995}):
            path = P.prepare(self.api, self.native_path)
            state, inputs = P.load(self.api, path)
        self.assertEqual(state['bridge_stage'], 'building')
        self.assertEqual(inputs['native_inputs_sha256'], self.native['inputs_sha256'])
        self.assertEqual(inputs['backend_environment']['PRIVATE'], 'fixture-secret')
        self.assertEqual(inputs['source']['build_arguments'][P.V.HOLD], 'true')
        self.assertFalse(state['startup_migrations'])

    def test_built_snapshot_round_trip_loads_as_a_managed_release(self):
        old, new = 'sha256:' + 'b' * 64, 'sha256:' + 'c' * 64
        before = {'backend': {'Image': old, 'Config': {'Env': []}}}
        manifest = {'chain_id': '18417507517', 'compose_project': 'fixture', 'baseline_image_ids': {'backend': old}}
        source = {'tag': 'candidate', 'source_commit': 'c' * 40}
        inventory = {'identity': (1000, 1000, (1000,)), 'migrations': {'repo/migrations/1_fixture.exs': 'd' * 64}}
        inputs = {'format': P.INPUT_FORMAT, 'source': source, 'source_root': str(ROOT), 'normalization_state': str(self.native_path),
                  'migration_inventory': json.loads(N.R.encoded(inventory)), 'before': before,
                  'current_model': {'services': {'backend': {'image': old, 'environment': {'LITERAL': '$value'}}}},
                  'backend_environment': {}, 'database_anchor': {'cursor': 10}}
        state = {'format': N.R.FORMAT, 'environment': 'dev', 'chain_id': manifest['chain_id'],
                 'compose_project': 'fixture', 'status': 'prepared', 'startup_migrations': False,
                 'bridge_patch': True, 'bridge_stage': 'building', 'source_root': str(ROOT),
                 'normalization_state': str(self.native_path), 'candidate_source_commit': 'c' * 40,
                 'bridge_inputs_sha256': N.R.digest(N.R.encoded(inputs)), 'before': before,
                 'source_images': {'backend': 'public-image'}, 'snapshot_hashes': {}, 'image_ids': {}}
        N.R.write_new(self.release_dir / 'inputs.json', N.R.encoded(inputs))
        N.R.update_state(self.release_path, state)
        self.api.require = N.D.require
        self.api.read_json = N.D.read_json
        self.api.MANAGED_RELEASES = self.base
        self.api.inspect_project = lambda *a, **k: before
        self.api.configuration = lambda *a: (manifest, {}, {})
        self.api.model_differences = N.D.model_differences
        self.api.compose_model = lambda directory, manifest, values, files, managed: json.loads(files[0].read_bytes().replace(b'$$', b'$'))
        pending = [{'status': 'ok', 'candidate_pending_versions': [], 'migration_directory': directory}
                   for directory in ('repo', 'account', 'arbitrum')]
        with patch.object(P.D, 'MANAGED_RELEASES', self.base), \
             patch.object(P.B, 'image_info', return_value={'Id': new}), \
             patch.object(P.V, 'validate_image'), patch.object(P.C, 'image_inventory', return_value=inventory), \
             patch.object(P.C, 'pending_migrations', return_value=pending), patch.object(P.B, 'smoke_image'), \
             patch.object(P, 'database_anchor', return_value={'cursor': 10}):
            P.build(self.api, self.release_path, state, inputs, manifest)
            loaded, frozen = P.load(self.api, self.release_path)
        self.assertEqual(loaded['bridge_stage'], 'candidate_verified')
        self.assertEqual(loaded['image_ids']['forward']['backend'], new)
        self.assertEqual(loaded['image_ids']['rollback']['backend'], old)
        self.assertEqual(frozen, inputs)


if __name__ == '__main__':
    unittest.main()
