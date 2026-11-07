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
import v10_frontend_deploy as F
import v10_native_normalization as N


class FrontendDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        self.base = Path(self.temp.name).resolve()
        patch.object(F, 'BASE', self.base).start()
        patch.object(N, 'BASE', self.base).start()
        self.directory = self.base / 'frontend-20261107T000000Z-1234abcd'
        self.directory.mkdir(mode=0o700)
        self.path = self.directory / 'state.json'
        self.native_directory = self.base / 'job-20261106T103901Z-1234abcd'
        self.native_directory.mkdir(mode=0o700)
        self.native_path = self.native_directory / 'state.json'
        script = b'original native worker'
        self.native_inputs = {'tool_source_commit': 'a' * 40, 'source_root': '/data/native-original',
            'python_executable': '/usr/bin/python3', 'min_free_bytes': N.MIN_FREE,
            'pause_free_bytes': N.PAUSE_FREE, 'resume_free_bytes': N.RESUME_FREE,
            'cumulative_growth_cap_bytes': None, 'maximum_batch_blocks': 1000,
            'script_sha256': N.R.digest(script), 'client_name': 'blockscout-v10-native-' + 'a' * 16,
            'manual_phase_rows': 4999097, 'maintenance_selected_rows': 11294954,
            'payload': {'token': 'a' * 16, 'start_before_block': 131385496}}
        frozen = N.R.encoded(self.native_inputs)
        self.native = {'format': N.FORMAT, 'inputs': self.native_inputs, 'inputs_sha256': N.R.digest(frozen),
                       'phase': 'paused', 'batch_blocks': 62, 'database': {'native_meta': {'max_block_number': 129878966}}}
        N.R.write_new(self.native_directory / 'inputs.json', frozen)
        N.R.write_new(self.native_directory / 'native.exs', script)
        self.before = {'backend': self.row('backend', 'backend-image'), 'frontend': self.row('old-ui', 'old-ui-image')}
        self.after = deepcopy(self.before)
        self.after['frontend'] = self.row('new-ui', 'new-ui-image')
        self.bridge_path = self.base / 'bridge-state.json'
        self.bridge = {'bridge_patch': True, 'status': 'active', 'normalization_state': str(self.native_path),
                       'candidate_source_commit': 'b' * 40, 'source_root': '/data/bridge-controller',
                       'image_ids': {'forward': {'backend': 'backend-image'}}}
        N.R.write_new(self.bridge_path, N.R.encoded(self.bridge))
        prior = {'format': 'blockscout-native-bridge-binding-v1', 'native_inputs_sha256': self.native['inputs_sha256'],
                 'native_worker_script_sha256': self.native_inputs['script_sha256'], 'release_state': str(self.bridge_path),
                 'release_state_sha256': N.R.digest(N.U.private(self.bridge_path)), 'tool_source_commit': 'b' * 40,
                 'source_root': '/data/bridge-controller', 'runtime_before': self.before}
        N.R.write_new(self.native_directory / 'bridge-runtime-binding.json', N.R.encoded(prior))
        self.native.update(runtime_binding_file='bridge-runtime-binding.json', runtime_binding_sha256=N.R.digest(N.R.encoded(prior)))
        N.R.write_new(self.native_path, N.R.encoded(self.native))
        self.inputs = {'format': F.FORMAT, 'frontend_revision': F.REVISION, 'image_id': 'new-ui-image',
                       'native_inputs_sha256': self.native['inputs_sha256'], 'normalization_state': str(self.native_path),
                       'image_reference': 'quanquanah/blockscout-frontend@sha256:' + 'f' * 64,
                       'tool_source_commit': 'c' * 40, 'source_root': str(ROOT), 'before': self.before,
                       'prior_binding': {k: self.native[k] for k in ('runtime_binding_file', 'runtime_binding_sha256')},
                       'base_bridge_state': str(self.bridge_path), 'snapshots': {}}
        for side in ('forward', 'rollback'):
            raw = N.R.encoded({'services': {'frontend': {'image': side}}})
            N.R.write_new(self.directory / (side + '.compose.json'), raw)
            self.inputs['snapshots'][side] = N.R.digest(raw)
        N.R.write_new(self.directory / 'inputs.json', N.R.encoded(self.inputs))
        self.state = {'format': F.FORMAT, 'phase': 'active', 'inputs_sha256': N.R.digest(N.R.encoded(self.inputs))}
        N.R.write_new(self.path, N.R.encoded(self.state))
        self.receipt('forward', self.after)
        patch.object(N.R, 'load_state', return_value=self.bridge).start()
        patch.object(N.D, 'configuration', return_value=({}, {}, {})).start()
        self.api = types.SimpleNamespace(ensure_stable=N.D.ensure_stable, run=lambda *a, **k: '',
                                         configuration=lambda *a: ({}, {}, {}), ConfigError=N.D.ConfigError)

    @staticmethod
    def row(name, image):
        return {'Id': name, 'Image': image, 'Config': {'Env': ['SECRET=private-canary'], 'Labels': {}},
                'HostConfig': {'PortBindings': {}}, 'Mounts': [], 'NetworkSettings': {'Networks': {'dev': {}}},
                'RestartCount': 0, 'State': {'Running': True, 'StartedAt': 'fixed'}}

    def receipt(self, side, runtime):
        receipt = {'format': F.FORMAT, 'side': side, 'inputs_sha256': self.state['inputs_sha256'],
                   'runtime_before': runtime, 'tool_source_commit': self.inputs['tool_source_commit'],
                   'source_root': self.inputs['source_root'], 'normalization_state': str(self.native_path)}
        N.R.write_new(self.directory / ('receipt-' + side + '.json'), N.R.encoded(receipt))

    def test_environment_migration_preserves_custom_values_and_parent_properties(self):
        env = {'NEXT_PUBLIC_NETWORK_ID': '42', 'NEXT_PUBLIC_ROLLUP_TYPE': 'arbitrum',
               'NEXT_PUBLIC_ROLLUP_PARENT_CHAIN': '{"name":"Parent","id":42161}',
               'NEXT_PUBLIC_ROLLUP_L1_BASE_URL': 'https://parent.example', 'NEXT_PUBLIC_API_SPEC_URL': 'old',
               'NEXT_PUBLIC_DERIW_API_HOST': 'https://trade.example', 'NEXT_PUBLIC_DERIW_USDT_ADDRESS': 'token',
               'NEXT_PUBLIC_NETWORK_NAME': 'Deriw', 'ENVS_PRESET': 'old'}
        result = F.migrate_environment(env, '42')
        self.assertEqual(result['NEXT_PUBLIC_DERIW_API_HOST'], env['NEXT_PUBLIC_DERIW_API_HOST'])
        self.assertEqual(result['NEXT_PUBLIC_DERIW_USDT_ADDRESS'], 'token')
        self.assertEqual(json.loads(result['NEXT_PUBLIC_ROLLUP_PARENT_CHAIN']),
                         {'name': 'Parent', 'id': 42161, 'baseUrl': 'https://parent.example'})
        self.assertEqual(result['NEXT_PUBLIC_ROLLUP_LAYER_NUMBER'], '3')
        self.assertEqual(result['ENVS_PRESET'], 'none')
        self.assertNotIn('NEXT_PUBLIC_ROLLUP_L1_BASE_URL', result)
        self.assertNotIn('NEXT_PUBLIC_API_SPEC_URL', result)
        with self.assertRaises(N.D.ConfigError):
            F.migrate_environment(env, 'wrong-chain')

    def test_effective_environment_reads_preset_overrides_without_executing_code(self):
        env = {'NODE_VERSION': 'old', 'NEXT_PUBLIC_NETWORK_NAME': 'old', 'NEXT_PUBLIC_JSON': '{"name":"same"}'}
        script = 'window.__envs = {\nNEXT_PUBLIC_NETWORK_NAME: "effective",\nNEXT_PUBLIC_JSON: "{\'name\':\'same\'}",\n}\n'
        result = F.effective_environment(env, script)
        self.assertEqual(result['NEXT_PUBLIC_NETWORK_NAME'], 'effective')
        self.assertEqual(result['NEXT_PUBLIC_JSON'], env['NEXT_PUBLIC_JSON'])
        self.assertNotIn('NODE_VERSION', result)
        with self.assertRaises(N.D.ConfigError):
            F.effective_environment(env, 'window.__envs = {\nprocess.exit(0);\n}')

    def test_snapshot_changes_only_frontend_selection_and_retains_backend_ownership(self):
        model = {'services': {'backend': {'image': 'backend', 'command': ['same'],
                         'labels': {'release': 'original'}, 'environment': {'SECRET': 'private-canary'}},
                             'frontend': {'image': 'old-ui', 'build': 'source', 'ports': ['3000:3000']}}}
        result = F.snapshot(model, self.before, 'new-ui-image', {'SETTING': '$literal'}, self.path, 'forward')
        self.assertEqual(result['services']['backend']['labels'], {'release': 'original'})
        self.assertEqual(result['services']['backend']['command'], ['same'])
        self.assertEqual(result['services']['backend']['image'], 'backend-image')
        self.assertEqual(result['services']['backend']['environment'], model['services']['backend']['environment'])
        self.assertEqual(result['services']['frontend']['image'], 'new-ui-image')
        self.assertEqual(result['services']['frontend']['ports'], ['3000:3000'])
        self.assertIn(b'$$literal', N.R.snapshot_bytes(result))
        self.assertNotIn('build', result['services']['frontend'])
        rollback = F.snapshot(model, self.before, 'unused', {}, self.path, 'rollback')
        self.assertEqual(rollback['services']['frontend']['image'], 'old-ui-image')
        self.assertEqual(rollback['services']['frontend']['environment'], {'SECRET': 'private-canary'})

    def test_image_requires_amd64_and_exact_source_version(self):
        image = {'Os': 'linux', 'Architecture': 'amd64', 'Config': {'Labels': {
                 'org.opencontainers.image.version': F.VERSION, 'org.opencontainers.image.revision': F.REVISION}}}
        self.api.run = lambda *a, **k: json.dumps([image])
        self.assertEqual(F.image_info(self.api, 'candidate'), image)
        image['Architecture'] = 'arm64'
        with self.assertRaises(N.D.ConfigError):
            F.image_info(self.api, 'candidate')

    def test_deployment_tool_cannot_include_another_backend_change(self):
        self.api.run = lambda *a, **k: 'deploy/scripts/new_tool.py\ndocs/deployment/frontend.md\n'
        with patch.object(F.B, 'clean_source', return_value=('c' * 40, '10.2.6')):
            self.assertEqual(F.source_review(self.api), 'c' * 40)
            self.api.run = lambda *a, **k: 'apps/explorer/lib/unreviewed.ex\n'
            with self.assertRaisesRegex(N.D.ConfigError, 'Backend application'):
                F.source_review(self.api)

    def test_failed_candidate_startup_cannot_produce_an_activation_receipt(self):
        inputs = {**self.inputs, 'environment': {'NEXT_PUBLIC_NETWORK_ID': '42'}}
        with patch.object(F, 'remove_stage') as cleanup, patch.object(F, 'wait_health', side_effect=N.D.ConfigError('startup failed')):
            with self.assertRaisesRegex(N.D.ConfigError, 'startup failed'):
                F.stage(self.api, self.path, self.state, inputs, {})
        self.assertEqual(cleanup.call_count, 2)
        self.assertFalse((self.directory / 'stage-receipt.json').exists())
        self.assertEqual(N.load(self.native_path)['runtime_binding_file'], 'bridge-runtime-binding.json')

    def test_frozen_snapshot_tampering_stops_before_any_activation(self):
        (self.directory / 'forward.compose.json').write_text('changed')
        with self.assertRaisesRegex(N.D.ConfigError, 'snapshot changed'):
            F.load(self.path)

    def test_handoff_preserves_cursor_worker_inputs_and_original_bridge_proof(self):
        preserved = {p: (self.native_directory / p).read_bytes() for p in ('inputs.json', 'native.exs', 'bridge-runtime-binding.json')}
        with patch.object(F, 'verify', return_value=self.after):
            F.bind_native(self.api, self.path, 'forward')
            F.bind_native(self.api, self.path, 'forward')
        native = N.load(self.native_path)
        context = N.runtime_binding(self.native_path, native)
        self.assertEqual(native['database']['native_meta']['max_block_number'], 129878966)
        self.assertEqual(native['batch_blocks'], 62)
        self.assertEqual(context['runtime_before'], self.after)
        self.assertEqual(context['previous_source_root'], '/data/bridge-controller')
        self.assertIsNone(native['inputs']['cumulative_growth_cap_bytes'])
        for p, raw in preserved.items():
            self.assertEqual((self.native_directory / p).read_bytes(), raw)

    def test_failed_verification_cannot_bind_or_reset_the_native_job(self):
        with patch.object(F, 'verify', side_effect=N.D.ConfigError('not verified')):
            with self.assertRaises(N.D.ConfigError):
                F.bind_native(self.api, self.path, 'forward')
        self.assertEqual(N.load(self.native_path)['runtime_binding_file'], 'bridge-runtime-binding.json')
        self.assertFalse(list(self.native_directory.glob('frontend-runtime-binding-*')))

    def test_receipt_tampering_invalidates_native_authorization(self):
        with patch.object(F, 'verify', return_value=self.after):
            F.bind_native(self.api, self.path, 'forward')
        (self.directory / 'receipt-forward.json').write_text('{}')
        with self.assertRaisesRegex(N.D.ConfigError, 'verified release'):
            N.load(self.native_path)

    def test_rollback_creates_another_binding_without_overwriting_forward_evidence(self):
        with patch.object(F, 'verify', return_value=self.after):
            F.bind_native(self.api, self.path, 'forward')
        forward = self.native_directory / N.load(self.native_path)['runtime_binding_file']
        frozen = forward.read_bytes()
        restored = deepcopy(self.before)
        restored['frontend']['Id'] = 'restored-ui'
        self.receipt('rollback', restored)
        with patch.object(F, 'verify', return_value=restored):
            F.bind_native(self.api, self.path, 'rollback')
        native = N.load(self.native_path)
        self.assertEqual(N.runtime_binding(self.native_path, native)['runtime_before'], restored)
        self.assertEqual(native['database']['native_meta']['max_block_number'], 129878966)
        self.assertEqual(forward.read_bytes(), frozen)
        self.assertTrue(native['runtime_binding_file'].endswith('-rollback.json'))

    def test_frontend_handoff_cannot_authorize_a_changed_backend(self):
        receipt_path = self.directory / 'receipt-forward.json'
        receipt = json.loads(receipt_path.read_text())
        receipt['runtime_before']['backend']['Image'] = 'unreviewed-backend'
        receipt_path.write_bytes(N.R.encoded(receipt))
        with patch.object(F, 'verify', return_value=self.after):
            with self.assertRaisesRegex(N.D.ConfigError, 'Another service changed'):
                F.bind_native(self.api, self.path, 'forward')
        self.assertEqual(N.load(self.native_path)['runtime_binding_file'], 'bridge-runtime-binding.json')

    def test_activation_targets_only_frontend_and_retains_database_anchor(self):
        (self.directory / 'receipt-forward.json').unlink()
        N.R.write_new(self.directory / 'stage-receipt.json', N.R.encoded({
            'inputs_sha256': self.state['inputs_sha256'], 'image_id': 'new-ui-image'}))
        commands = []
        control_commands = []
        self.api.run = lambda command, **kwargs: control_commands.append(command) or ''
        self.api.inspect_project = lambda *a, **k: self.before
        self.api.configuration = lambda *a: ({'compose_project': 'dev'}, {}, {})
        with patch.object(F, 'source_review'), patch.object(F.B, 'clean_source', return_value=('c' * 40, '10.2.6')), \
                patch.object(F, 'image_info', return_value={'Id': 'new-ui-image'}), \
                patch.object(F.P, 'database_anchor', return_value={'cursor': 129878966}), \
                patch.object(F, 'private_command', side_effect=lambda command, *a, **k: commands.append(command)), \
                patch.object(F, 'wait_health'), patch.object(F, 'verify', return_value=self.after):
            F.activate(self.api, self.path, 'forward')
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][-1], 'frontend')
        self.assertIn('--no-deps', commands[0])
        self.assertIn('--no-build', commands[0])
        self.assertIn(['docker', 'exec', 'proxy', 'nginx', '-t'], control_commands)
        self.assertIn(['docker', 'exec', 'proxy', 'nginx', '-s', 'reload'], control_commands)
        self.assertEqual(F.load(self.path)[0]['phase'], 'active')
        self.assertEqual(N.load(self.native_path)['database']['native_meta']['max_block_number'], 129878966)
        self.assertTrue((self.directory / 'receipt-forward.json').exists())

    def test_runtime_handoff_uses_the_new_images_assets_instead_of_old_generated_metadata(self):
        raw = {'NEXT_PUBLIC_NETWORK_ID': '42', 'NEXT_PUBLIC_ROLLUP_TYPE': 'arbitrum',
               'NEXT_PUBLIC_ROLLUP_PARENT_CHAIN': '{"name":"Parent","baseUrl":"https://parent.example"}'}
        script = 'window.__envs = {\nNEXT_PUBLIC_ICON_SPRITE_HASH: "old-build-sprite",\nNEXT_PUBLIC_GIT_COMMIT_SHA: "old-build-commit",\nNEXT_PUBLIC_GIT_TAG: "old-release",\n}\n'
        effective = F.effective_environment(raw, script)
        result = F.migrate_environment(effective, '42')
        self.assertNotIn('NEXT_PUBLIC_ICON_SPRITE_HASH', result)
        self.assertEqual(result['NEXT_PUBLIC_GIT_COMMIT_SHA'], F.REVISION)
        self.assertEqual(result['NEXT_PUBLIC_GIT_TAG'], 'v' + F.VERSION)
        self.assertEqual(effective['NEXT_PUBLIC_ICON_SPRITE_HASH'], 'old-build-sprite')

    def test_startup_diagnostics_do_not_disclose_log_values(self):
        text = ('Downloading external assets...\nhttps://private-canary.invalid/?key=secret-canary\n'
                'For the following environment variables placeholders were not generated at build-time:\n'
                'NEXT_PUBLIC_ICON_SPRITE_HASH\nCongruity check failed')
        report = F.startup_diagnostics(text, {'Running': False, 'ExitCode': 1, 'OOMKilled': False})
        self.assertEqual(report['category'], 'unregistered_runtime_setting')
        self.assertEqual(report['runtime_stage'], 'validation')
        self.assertEqual(report['setting_names'], ['NEXT_PUBLIC_ICON_SPRITE_HASH'])
        self.assertNotIn('private-canary', json.dumps(report))
        self.assertNotIn('secret-canary', json.dumps(report))

    def test_exited_startup_reports_the_configuration_error_without_waiting_five_minutes(self):
        def run(command, **kwargs):
            if command[:2] == ['docker', 'exec']:
                raise N.D.ConfigError('not running')
            if command[:2] == ['docker', 'inspect']:
                return json.dumps({'Running': False, 'ExitCode': 1, 'OOMKilled': False})
            return 'For the following environment variables: NEXT_PUBLIC_ICON_SPRITE_HASH\nCongruity check failed'
        self.api.run = run
        with patch.object(F.time, 'sleep', side_effect=AssertionError('must not sleep')):
            with self.assertRaises(N.D.ConfigError) as caught:
                F.wait_health(self.api, 'candidate')
        self.assertEqual(caught.exception.startup_diagnostics['exit_code'], 1)
        self.assertEqual(caught.exception.startup_diagnostics['setting_names'], ['NEXT_PUBLIC_ICON_SPRITE_HASH'])

    def test_startup_timeout_reports_which_stage_is_waiting(self):
        def run(command, **kwargs):
            if command[:2] == ['docker', 'exec']:
                raise N.D.ConfigError('not ready')
            if command[:2] == ['docker', 'inspect']:
                return json.dumps({'Running': True, 'ExitCode': 0, 'OOMKilled': False})
            return 'Generating favicons...'
        self.api.run = run
        with self.assertRaises(N.D.ConfigError) as caught:
            F.wait_health(self.api, 'candidate', seconds=0)
        self.assertEqual(caught.exception.startup_diagnostics['category'], 'startup_timeout')
        self.assertEqual(caught.exception.startup_diagnostics['runtime_stage'], 'favicon')

    def test_failed_preparation_can_be_replaced_without_editing_its_files_or_native_progress(self):
        (self.directory / 'receipt-forward.json').unlink()
        self.state['phase'] = 'prepared'
        N.R.update_state(self.path, self.state)
        original = {p: p.read_bytes() for p in (self.path, self.directory / 'inputs.json', self.native_path)}
        self.api.inspect_project = lambda *a, **k: self.before
        with patch.object(F, 'prepare', return_value=self.base / 'new-state.json') as prepare:
            self.assertEqual(F.retry_staging(self.api, self.path), self.base / 'new-state.json')
        prepare.assert_called_once_with(self.api, self.native_path, self.inputs['image_reference'], supersedes=self.path)
        for path, data in original.items():
            self.assertEqual(path.read_bytes(), data)

    def test_active_preparation_cannot_be_replaced_by_a_staging_retry(self):
        with patch.object(F, 'prepare') as prepare, self.assertRaisesRegex(N.D.ConfigError, 'before activation'):
            F.retry_staging(self.api, self.path)
        prepare.assert_not_called()

    def test_staging_retry_refuses_a_changed_live_runtime(self):
        (self.directory / 'receipt-forward.json').unlink()
        self.state['phase'] = 'prepared'
        N.R.update_state(self.path, self.state)
        self.api.inspect_project = lambda *a, **k: self.after
        with patch.object(F, 'prepare') as prepare, self.assertRaisesRegex(N.D.ConfigError, 'Runtime changed'):
            F.retry_staging(self.api, self.path)
        prepare.assert_not_called()

    def test_service_handoff_accepts_only_the_same_jobs_previous_controller(self):
        with patch.object(F, 'verify', return_value=self.after):
            F.bind_native(self.api, self.path, 'forward')
        unit_dir = self.base / 'systemd'
        unit_dir.mkdir()
        target = unit_dir / N.UNIT
        real_render = N.render_service
        service_path = Path('/data/native/job-20261106T103901Z-1234abcd/state.json')
        previous = real_render(service_path, Path('/data/bridge-controller'), '/usr/bin/python3')
        target.write_text(previous)
        with patch.object(N, 'SYSTEMD_DIRECTORY', unit_dir), patch.object(N.B, 'clean_source', return_value=('c' * 40, '10.2.6')), \
                patch.object(N, 'render_service', side_effect=lambda path, root, executable: real_render(service_path, root, executable)):
            N.install_service(self.api, self.native_path)
            self.assertIn(str(ROOT), target.read_text())
            target.write_text('another service')
            with self.assertRaisesRegex(N.D.ConfigError, 'Another native service'):
                N.install_service(self.api, self.native_path)


if __name__ == '__main__':
    unittest.main()
