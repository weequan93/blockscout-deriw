import contextlib
from copy import deepcopy
import io
import json
from pathlib import Path
import stat
import sys
import tarfile
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import v10_upgrade as U

IMAGE = 'sha256:' + 'b' * 64
SECRET = 'fixture-private-upgrade-d17'


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve() / 'upgrades'
        self.base.mkdir(mode=0o700)
        self.path = self.base / 'upgrade-20261104T120000Z-0123abcd' / 'state.json'
        self.path.parent.mkdir(mode=0o700)
        self.addCleanup(patch.stopall)
        patch.object(U, 'BASE', self.base).start()
        self.before = {name: {'Id': str(i + 1) * 64, 'Image': 'sha256:' + str(i + 1) * 64,
                             'Config': {'Env': ['SECRET=' + SECRET], 'Labels': {}, 'Cmd': U.R.START},
                             'State': {'Running': True}, 'HostConfig': {'RestartPolicy': {'Name': 'always', 'MaximumRetryCount': 0}, 'PortBindings': {}},
                             'Mounts': [], 'NetworkSettings': {'Networks': {'fixture': {'Aliases': [name]}}}, 'RestartCount': 0}
                       for i, name in enumerate((*U.PAIR, 'db', 'frontend'))}
        self.manifest = {'chain_id': '18417507517', 'compose_project': 'fixture', 'baseline_image_ids': {n: r['Image'] for n, r in self.before.items()}}
        self.model = {'services': {n: {'image': r['Image'], 'build': {'context': '/not-used'}, 'command': U.R.START,
                                      'environment': {'SECRET': SECRET, 'ABSENT': None}, 'labels': {}, 'networks': {'fixture': {}}}
                                   for n, r in self.before.items()}}
        self.nft = {'backend_environment': {'NFT_MEDIA_HANDLER_BUCKET_FOLDER': '/folder_1'}}
        self.forward = U.forward_model(self.model, self.before, IMAGE, 'a' * 40, self.path, self.nft)
        self.plan = U.D.read_json(ROOT / 'deploy/upgrades/v10.2.6.json')
        self.state = {'format': U.FORMAT, 'environment': 'dev', 'chain_id': '18417507517', 'compose_project': 'fixture',
                      'phase': 'prepared', 'schema_writes_may_have_started': False, 'snapshot_verified': False,
                      'recovery_policy': U.RECOVERY, 'candidate_image': IMAGE, 'candidate_source_commit': 'a' * 40,
                      'before': self.before, 'source_environment': {n: self.model['services'][n]['environment'] for n in U.PAIR}, 'source_images': {n: s['image'] for n, s in self.model['services'].items()},
                      'image_ids': {n: s['image'] for n, s in self.forward['services'].items()},
                      'client_name': 'blockscout-v10-schema-' + 'a' * 16, 'backend_environment': {'SECRET': SECRET},
                      'network': 'fixture', 'nft_role_plan': self.nft, 'migration_payload': U.payload(self.plan)}
        self.current = deepcopy(self.before)
        self.commands = []
        self.api = types.SimpleNamespace(MANAGED_RELEASES=self.base, require=U.D.require, inspect_project=Mock(side_effect=lambda *a, **k: deepcopy(self.current)),
                                         run=Mock(side_effect=self.api_run), env_map=U.D.env_map, inspect_mounts=U.D.inspect_mounts,
                                         compose_model=Mock(return_value=self.forward), live_environment_differences=Mock(return_value=[]),
                                         rpc_chain=Mock(), load_tool=Mock(), lifecycle_summary=U.D.lifecycle_summary, ConfigError=U.D.ConfigError)
        self.write()

    def write(self):
        hashes = {}
        for name, value in (('previous.compose.json', U.R.encoded(self.model)), ('forward.compose.json', U.R.encoded(self.forward)),
                            ('migrate.exs', (ROOT / 'deploy/scripts/v10/migrate.exs').read_bytes())):
            file = self.path.parent / name
            file.write_bytes(value)
            file.chmod(0o600)
            hashes[name] = U.R.digest(value)
        self.state['file_hashes'] = hashes
        self.path.write_bytes(U.R.encoded(self.state))
        self.path.chmod(0o600)

    def api_run(self, command, **kwargs):
        self.commands.append(command)
        if command[:3] == ['git', '-C', str(ROOT)]:
            return ''
        if command[:2] == ['docker', 'update']:
            for row in self.current.values():
                if row['Id'] == command[-1]:
                    row['HostConfig']['RestartPolicy'] = {'Name': 'no' if command[2] == '--restart=no' else 'always', 'MaximumRetryCount': 0}
            return ''
        if command[:2] == ['docker', 'stop']:
            next(r for r in self.current.values() if r['Id'] == command[-1])['State']['Running'] = False
            return ''
        if command[:2] == ['docker', 'start']:
            next(r for r in self.current.values() if r['Id'] == command[-1])['State']['Running'] = True
            return ''
        if command[:3] == ['docker', 'ps', '-aq']:
            return ''
        raise AssertionError(str(command[:3]))

    def test_forward_snapshot_changes_only_the_pair_and_keeps_private_values(self):
        for name, service in self.forward['services'].items():
            self.assertNotIn('build', service)
            self.assertEqual(service['pull_policy'], 'never')
            self.assertEqual(service['environment']['SECRET'], SECRET)
            self.assertNotIn('ABSENT', service['environment'])
            self.assertEqual(service['image'], IMAGE if name in U.PAIR else self.before[name]['Image'])
        for name in U.PAIR:
            self.assertEqual(self.forward['services'][name]['environment']['RELEASE_VERSION'], '10.2.6')
            self.assertEqual(self.forward['services'][name]['environment'][U.V.HOLD], 'true')
            self.assertEqual(self.forward['services'][name]['command'], U.R.START)
        self.assertEqual(self.forward['services']['backend']['environment']['NFT_MEDIA_HANDLER_BUCKET_FOLDER'], '/folder_1')

    def test_schema_client_has_no_host_mounts_public_ports_or_credentials_in_argv(self):
        command = U.client_command(IMAGE, 'fixture', 'test-client', str(self.path))
        for value in ('--volume', '-v', '--publish', '-p', SECRET):
            self.assertNotIn(value, command)
        self.assertIn('--read-only', command)
        self.assertIn('--pull', command)
        data = U.archive({'SECRET': SECRET, 'SHELL': '$(touch /must-not-exist)'}, U.payload(self.plan), b'fixture')
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            self.assertEqual(set(tar.getnames()), {'environment.json', 'payload.json', 'migrate.exs'})
            env = json.load(tar.extractfile('environment.json'))
            self.assertEqual(env['SECRET'], SECRET)
            self.assertEqual(env['RELEASE_DISTRIBUTION'], 'none')
            self.assertEqual(env[U.V.HOLD], 'true')

    def test_payload_freezes_all_29_versions_and_keeps_custom_versions_outside_sequence(self):
        data = U.payload(self.plan)
        self.assertEqual([len(data['upstream_versions'][d]) for d in ('repo', 'account', 'arbitrum')], [26, 2, 1])
        self.assertFalse(set(sum(data['upstream_versions'].values(), [])) & U.R.VERSIONS)
        self.assertNotIn('IO.puts(', data['presence_probe'])

    def test_saved_file_change_bad_permissions_or_wrong_format_is_rejected(self):
        U.load_state(self.api, self.path, self.manifest)
        (self.path.parent / 'migrate.exs').write_text('tampered')
        with self.assertRaises(U.D.ConfigError):
            U.load_state(self.api, self.path, self.manifest)
        self.write()
        self.path.chmod(0o644)
        with self.assertRaises(U.D.ConfigError):
            U.load_state(self.api, self.path, self.manifest)
        self.state['format'] = U.R.FORMAT
        self.write()
        with self.assertRaises(U.D.ConfigError):
            U.load_state(self.api, self.path, self.manifest)

    def test_abort_after_any_possible_schema_write_is_refused_without_service_changes(self):
        self.state['schema_writes_may_have_started'] = True
        self.state['phase'] = 'schema_write_requested'
        self.write()
        with self.assertRaises(U.D.ConfigError):
            U.abort(self.api, self.path, self.manifest)
        self.assertFalse(self.commands)

    def test_abort_before_schema_writes_restores_original_pair_and_policies(self):
        for name in U.PAIR:
            self.current[name]['State']['Running'] = False
            self.current[name]['HostConfig']['RestartPolicy'] = {'Name': 'no', 'MaximumRetryCount': 0}
        with contextlib.redirect_stdout(io.StringIO()):
            result = U.abort(self.api, self.path, self.manifest)
        self.assertEqual(result['status'], 'aborted_before_schema_writes')
        self.assertTrue(all(self.current[n]['State']['Running'] for n in U.PAIR))
        self.assertEqual(U.D.read_json(self.path)['phase'], 'aborted')
        self.assertFalse(any(self.before[n]['Id'] in c for c in self.commands for n in ('db', 'frontend')))

    def test_free_space_reserve_and_consumption_limit_are_both_enforced(self):
        state = {'space_at_write_start': 900 * U.S.GIB}
        with patch.object(U, 'capacity', return_value=890 * U.S.GIB):
            U.space(state)
        for free in (879 * U.S.GIB, 99 * U.S.GIB):
            with patch.object(U, 'capacity', return_value=free), self.assertRaises(U.D.ConfigError):
                U.space(state)

    def test_unrelated_container_change_stops_recovery(self):
        self.current['frontend']['Image'] = IMAGE
        with self.assertRaises(Exception):
            U.abort(self.api, self.path, self.manifest)
        self.assertFalse(self.commands)

    @contextlib.contextmanager
    def resume_context(self, fail_migration=False, fail_activation=False):
        def client(api, path, state, mode):
            if mode == 'migrate':
                saved = U.D.read_json(path)
                self.assertTrue(saved['schema_writes_may_have_started'])
                self.assertTrue(all(not self.current[n]['State']['Running'] for n in U.PAIR))
                if fail_migration:
                    raise U.D.ConfigError('fixture migration failure')
            return {'schema': {}, 'tables': [], 'mode': mode, 'migrated': []}
        def compose(*args):
            self.assertEqual(U.D.read_json(self.path)['phase'], 'activation_requested')
            if fail_activation:
                raise U.D.ConfigError('fixture activation failure')
        with patch.object(U.B, 'clean_source', return_value=('a' * 40, '10.2.6')), \
                patch.object(U.B, 'image_info', return_value={'Id': IMAGE}), \
                patch.object(U, 'client', side_effect=client) as call, \
                patch.object(U.S, 'budget', return_value={'status': 'eligible_for_initial_schema_phase'}), \
                patch.object(U, 'capacity', return_value=900 * U.S.GIB), \
                patch.object(U, 'compose_pair', side_effect=compose) as activate, \
                patch.object(U, 'verify', return_value={'status': 'verified'}), \
                contextlib.redirect_stdout(io.StringIO()):
            yield call, activate

    def test_migration_failure_leaves_old_pair_stopped_and_prevents_automatic_v9_abort(self):
        with self.resume_context(fail_migration=True) as (call, activate):
            with self.assertRaises(U.D.ConfigError):
                U.resume(self.api, ROOT, self.path, self.manifest, initial=True)
        self.assertEqual([c.args[-1] for c in call.call_args_list], ['inspect', 'migrate'])
        activate.assert_not_called()
        state = U.D.read_json(self.path)
        self.assertTrue(state['schema_writes_may_have_started'])
        self.assertEqual(state['phase'], 'schema_write_requested')
        self.assertTrue(all(not self.current[n]['State']['Running'] for n in U.PAIR))
        with self.assertRaises(U.D.ConfigError):
            U.abort(self.api, self.path, self.manifest)

    def test_apply_sequences_stop_recheck_migration_activation_and_persists_success(self):
        with self.resume_context() as (call, activate):
            result = U.resume(self.api, ROOT, self.path, self.manifest, initial=True)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(U.D.read_json(self.path)['phase'], 'active')
        self.assertEqual([c.args[-1] for c in call.call_args_list], ['inspect', 'migrate'])
        activate.assert_called_once()
        stops = [c[-1] for c in self.commands if c[:2] == ['docker', 'stop']]
        self.assertEqual(stops, [self.before[n]['Id'] for n in reversed(U.PAIR)])
        self.assertTrue(all(c[3] == '300' for c in self.commands if c[:2] == ['docker', 'stop']))

    def test_activation_retry_does_not_rerun_migrations_or_require_original_pair(self):
        self.state['schema_writes_may_have_started'] = True
        self.state['phase'] = 'activation_requested'
        self.write()
        self.current.pop('nft_media_handler')
        self.current['backend']['Id'] = 'f' * 64
        self.current['backend']['Image'] = IMAGE
        with self.resume_context() as (call, activate):
            U.resume(self.api, ROOT, self.path, self.manifest)
        call.assert_not_called()
        activate.assert_called_once()

    def test_space_limit_stops_and_removes_only_the_owned_schema_client(self):
        class Process:
            stdin = io.BytesIO()
            returncode = None
            def poll(self):
                return self.returncode
            def terminate(self):
                self.returncode = -15
            def wait(self, **kwargs):
                return self.returncode
        process = Process()
        with patch.object(U.subprocess, 'Popen', return_value=process) as run, \
                patch.object(U, 'remove_client') as remove, \
                patch.object(U, 'space', side_effect=U.D.ConfigError('space limit')):
            with self.assertRaises(U.D.ConfigError):
                U.client(self.api, self.path, self.state, 'migrate')
        self.assertEqual(process.returncode, -15)
        self.assertGreaterEqual(remove.call_count, 2)
        self.assertNotIn(SECRET, str(run.call_args.args))
        self.assertTrue(all(c.args[1:] == (self.path, self.state) for c in remove.call_args_list))
        self.assertEqual(len(list(self.path.parent.glob('migrate-*.log.private'))), 1)

    def test_preparation_keeps_dev_identity_separate_from_private_backend_environment(self):
        self.api.read_json = U.D.read_json
        self.api.ensure_stable = U.D.ensure_stable
        for row in self.before.values():
            row['NetworkSettings']['Networks']['fixture']['IPAddress'] = '192.0.2.10'
        self.current = deepcopy(self.before)
        evidence = {'database_identity': {'address': '192.0.2.10', 'port': 5432},
                    'applied_versions': [1], 'heap_identity': 12, 'background_statuses': [], 'schema': {}, 'tables': []}
        receipt = {'image_id': IMAGE, 'candidate_tag': 'fixture-candidate', 'source_commit': 'a' * 40}
        prepared = (self.manifest, self.before, receipt, self.model, self.model, self.nft, ('a' * 40, '10.2.6'), 'fixture', {})
        with contextlib.redirect_stdout(io.StringIO()), patch.object(U, 'preflight', return_value=prepared), \
                patch.object(U, 'client', return_value=evidence) as client, \
                patch.object(U.R, 'check_snapshot'), \
                patch.object(U.S, 'budget', return_value={'status': 'eligible_for_initial_schema_phase'}), \
                patch.object(U, 'capacity', return_value=900 * U.S.GIB), \
                patch.object(U.B, 'clean_source', return_value=('a' * 40, '10.2.6')), \
                patch.object(U.B, 'image_info', return_value={'Id': IMAGE}):
            result = U.prepare(self.api, ROOT, self.base / 'private.json', self.base / 'receipt.json')
        state_path = Path(result['state'])
        state = U.load_state(self.api, state_path, self.manifest)
        self.assertEqual(state['environment'], 'dev')
        self.assertEqual(state['backend_environment']['SECRET'], SECRET)
        self.assertFalse(state['schema_writes_may_have_started'])
        self.assertEqual(client.call_args.args[-1], 'inspect')
        self.assertFalse(self.commands)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_partial_pair_can_be_inspected_and_later_source_image_changes_are_visible(self):
        self.current.pop('nft_media_handler')
        result = U.verify(self.api, self.path, self.state, self.manifest)
        self.assertEqual(result['status'], 'needs_attention')
        self.assertNotIn('Node.ping', U.nft_expression())
        model = deepcopy(self.model)
        model['services']['frontend']['image'] = 'fixture-proposed-frontend'
        model['services']['backend']['image'] = 'fixture-proposed-backend'
        model['services']['backend']['environment'][U.V.HOLD] = 'false'
        context = {'state': self.state, 'path': self.path}
        selected = U.active_model(self.api, model, context)
        self.assertEqual(selected['services']['frontend']['image'], 'fixture-proposed-frontend')
        self.assertEqual(selected['services']['backend']['image'], 'fixture-proposed-backend')
        self.assertEqual(selected['services']['backend']['environment'][U.V.HOLD], 'false')

    def healthy_verification_pair(self, failed_stage=None):
        self.state['phase'] = 'active'
        self.manifest['http_rpc'] = 'http://fixture.invalid'
        for name in U.PAIR:
            self.current[name]['Image'] = IMAGE
            self.current[name]['Config']['Labels'].update({U.STATE_LABEL: str(self.path), U.ROLE_LABEL: name})
        def run(command, **kwargs):
            if command == ['docker', 'exec', '-i', 'backend', 'sh', '-c', U.D.RPC_CHAIN_SCRIPT]:
                self.assertEqual(kwargs['input_text'], self.manifest['http_rpc'] + '\n')
                return json.dumps({'result': hex(int(self.manifest['chain_id']))}) + '\nBLOCKSCOUT_RPC_META=200|0.001\nBLOCKSCOUT_RPC_EXIT=0\n'
            if '/api/health/readiness' in command[-1]:
                return 'OK'
            if command[:4] == ['docker', 'exec', 'backend', '/app/bin/blockscout']:
                return U.C.MARKER + json.dumps({'settings_ok': True, 'automatic_migrators_absent': True, 'market_price_ready': True})
            if command[:4] == ['docker', 'exec', 'nft_media_handler', '/app/bin/blockscout']:
                return U.C.MARKER + json.dumps({'v10_queue_protocol_ok': True, 'connected': True})
            for route in ('blocks', 'transactions', 'tokens'):
                if command[-1].endswith('/api/v2/' + route):
                    if failed_stage == 'api_' + route:
                        raise U.D.ConfigError('Docker/Compose read failed or exceeded limit; raw output suppressed')
                    return json.dumps({'items': []})
            raise AssertionError('Unexpected verification probe')
        self.api.run.side_effect = run
        self.api.rpc_chain.side_effect = lambda manifest, runner=None: U.D.rpc_chain(manifest, runner=runner or run)

    def test_failed_api_probe_keeps_its_name_and_previous_check_results(self):
        self.healthy_verification_pair(failed_stage='api_tokens')
        result = U.verify(self.api, self.path, self.state, self.manifest)
        self.assertEqual(result['status'], 'needs_attention')
        self.assertEqual(result['verification_failure']['stage'], 'api_tokens')
        self.assertTrue(result['backend_readiness_ok'])
        self.assertTrue(result['verification_probes']['backend_readiness'])
        self.assertTrue(result['verification_probes']['api_transactions'])
        self.assertFalse(result['verification_probes']['api_tokens'])
        self.assertNotIn(SECRET, json.dumps(result))

    def test_chain_transport_failure_preserves_completed_local_readiness_checks(self):
        self.healthy_verification_pair()
        self.api.rpc_chain.side_effect = U.D.ConfigError('Docker/Compose read failed or exceeded limit; raw output suppressed')
        result = U.verify(self.api, self.path, self.state, self.manifest)
        self.assertEqual(result['status'], 'needs_attention')
        self.assertEqual(result['verification_failure']['stage'], 'l2_chain')
        self.assertTrue(result['backend_readiness_ok'])
        self.assertTrue(result['settings_ok'])
        self.assertTrue(result['v10_queue_protocol_ok'])
        self.assertFalse(result['verification_probes']['l2_chain'])
        self.assertFalse(result['runtime_checks_completed'])

    def test_chain_http_failure_records_redacted_diagnostic_details(self):
        self.healthy_verification_pair()
        self.api.rpc_chain.side_effect = U.D.RpcChainError('L2 RPC transport check failed; raw output suppressed',
            'http_error', {'curl_exit': 22, 'http_status': 503, 'elapsed_seconds': 0.1, 'chain_id_matches': None})
        result = U.verify(self.api, self.path, self.state, self.manifest)
        failure = result['verification_failure']
        self.assertEqual(failure['stage'], 'l2_chain')
        self.assertEqual(failure['kind'], 'http_error')
        self.assertEqual(failure['rpc_probe']['http_status'], 503)
        self.assertTrue(result['backend_readiness_ok'])
        self.assertEqual(result['status'], 'needs_attention')

    def test_failed_local_readiness_is_still_reported_false(self):
        self.healthy_verification_pair()
        original = self.api.run.side_effect
        def run(command, **kwargs):
            if '/api/health/readiness' in command[-1]:
                raise U.D.ConfigError('Docker/Compose read failed or exceeded limit; raw output suppressed')
            return original(command, **kwargs)
        self.api.run.side_effect = run
        result = U.verify(self.api, self.path, self.state, self.manifest)
        self.assertEqual(result['status'], 'needs_attention')
        self.assertFalse(result['backend_readiness_ok'])
        self.assertFalse(result['verification_probes']['backend_readiness'])
        self.assertEqual(result['verification_failure']['stage'], 'backend_readiness')

    def test_invalid_runtime_object_cannot_replace_successful_readiness_or_leak_values(self):
        self.healthy_verification_pair()
        original = self.api.run.side_effect
        def run(command, **kwargs):
            if command[:4] == ['docker', 'exec', 'backend', '/app/bin/blockscout']:
                return U.C.MARKER + json.dumps([SECRET])
            return original(command, **kwargs)
        self.api.run.side_effect = run
        result = U.verify(self.api, self.path, self.state, self.manifest)
        self.assertEqual(result['status'], 'needs_attention')
        self.assertTrue(result['backend_readiness_ok'])
        self.assertEqual(result['verification_failure']['stage'], 'backend_runtime')
        self.assertNotIn(SECRET, json.dumps(result))

    def test_verification_deadline_caps_every_expensive_probe(self):
        self.healthy_verification_pair()
        with patch.object(U.time, 'monotonic', return_value=100):
            result = U.verify(self.api, self.path, self.state, self.manifest, deadline=103)
        self.assertEqual(result['status'], 'verified')
        self.assertGreaterEqual(len(self.api.run.call_args_list), 7)
        self.assertTrue(all(0 < call.kwargs['timeout'] <= 3 for call in self.api.run.call_args_list))
        self.assertTrue(result['verification_probes']['l2_chain'])

    def test_wrong_chain_is_identified_and_expired_deadline_launches_no_probe(self):
        self.healthy_verification_pair()
        self.api.rpc_chain.side_effect = U.D.ConfigError('L2 RPC chain does not match dev manifest')
        result = U.verify(self.api, self.path, self.state, self.manifest)
        self.assertEqual(result['verification_failure']['kind'], 'identity_mismatch')
        self.api.run.reset_mock()
        with patch.object(U.time, 'monotonic', return_value=104):
            result = U.verify(self.api, self.path, self.state, self.manifest, deadline=103)
        self.assertEqual(result['verification_failure']['stage'], 'verification_deadline')
        self.api.run.assert_not_called()

    def test_upgrade_context_uses_new_format_while_old_rollback_cannot_load_it(self):
        self.state['schema_writes_may_have_started'] = True
        self.state['phase'] = 'active'
        self.write()
        backend = deepcopy(self.before['backend'])
        backend['Image'] = IMAGE
        backend['Config']['Labels'] = {U.STATE_LABEL: str(self.path), U.ROLE_LABEL: 'backend',
                                       'com.docker.compose.project.config_files': str(self.path.parent / 'forward.compose.json')}
        self.api.load_tool.return_value = U
        context = U.R.active_context(self.api, backend, self.manifest)
        self.assertTrue(context['upgrade'])
        self.assertEqual(U.active_model(self.api, self.model, context), self.forward)
        with self.assertRaises(U.D.ConfigError):
            U.R.load_state(self.api, self.path, self.manifest)


if __name__ == '__main__':
    unittest.main()
