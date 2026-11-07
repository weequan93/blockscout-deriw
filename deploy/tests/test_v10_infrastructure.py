from copy import deepcopy
from contextlib import nullcontext, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import v10_infrastructure_deploy as I
import v10_infrastructure_checks as K
import v10_infrastructure_stage as S
import v10_native_normalization as N


def image(char):
    return 'sha256:' + char * 64


def row(name, char='a'):
    return {'Id': name, 'Image': image(char), 'Config': {'Env': ['PRIVATE_CANARY=do-not-print'],
            'Labels': {}, 'User': '', 'Cmd': ['same'], 'Entrypoint': ['same']},
            'HostConfig': {'PortBindings': {}, 'ShmSize': 67108864}, 'Mounts': [],
            'NetworkSettings': {'Networks': {'dev': {'IPAddress': '172.20.0.2', 'GlobalIPv6Address': ''}}},
            'RestartCount': 0, 'State': {'Running': True, 'StartedAt': 'fixed'}}


def anchor(cursor=20):
    return {'system_identifier': '1234', 'database_identity': {'database': 'fixture', 'oid': 1,
        'address': '172.20.0.2', 'port': 5432, 'postgres_major': 15}, 'heap_identity': 50, 'versions': [1, 2],
        'native_status': {'status': 'started', 'meta': {'v10_native_job': 'a' * 16, 'max_block_number': cursor}}}


class InfrastructureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        self.base = Path(self.temp.name).resolve()
        patch.object(I, 'BASE', self.base).start()
        patch.object(N, 'BASE', self.base).start()
        self.directory = self.base / 'infra-20261107T100000Z-1234abcd'
        self.directory.mkdir(mode=0o700)
        self.path = self.directory / 'state.json'
        ndir = self.base / 'job-20261106T103901Z-59bb4dc6'
        ndir.mkdir(mode=0o700)
        self.native_path = ndir / 'state.json'
        script = b'original frozen native worker'
        self.ninputs = {'min_free_bytes': N.MIN_FREE, 'pause_free_bytes': N.PAUSE_FREE, 'resume_free_bytes': N.RESUME_FREE,
            'cumulative_growth_cap_bytes': None, 'maximum_batch_blocks': 1000, 'script_sha256': N.R.digest(script),
            'client_name': 'blockscout-v10-native-' + 'a' * 16, 'python_executable': '/usr/bin/python3',
            'manual_phase_rows': 1, 'maintenance_selected_rows': 1,
            'payload': {'token': 'a' * 16, 'start_before_block': 21, 'database_identity': anchor()['database_identity']}}
        N.R.write_new(ndir / 'inputs.json', N.R.encoded(self.ninputs))
        N.R.write_new(ndir / 'native.exs', script)
        self.native = {'format': N.FORMAT, 'inputs': self.ninputs, 'inputs_sha256': N.R.digest(N.R.encoded(self.ninputs)),
            'phase': 'paused', 'batch_blocks': 10, 'database': {'native_status': 'started', 'native_meta': anchor()['native_status']['meta']}}
        self.before = {name: row(name) for name in ('backend', 'frontend', 'proxy', 'redis-db', 'db', 'db-init', 'stats-db', 'stats-db-init')}
        self.after = deepcopy(self.before)
        self.after['proxy'] = row('new-proxy', 'b')
        self.bridge_path = self.base / 'bridge.json'
        bridge = {'bridge_patch': True, 'status': 'active', 'normalization_state': str(self.native_path),
            'candidate_source_commit': 'b' * 40, 'source_root': '/data/bridge', 'image_ids': {'forward': {'backend': image('a')}}}
        N.R.write_new(self.bridge_path, N.R.encoded(bridge))
        prior = {'format': 'blockscout-native-bridge-binding-v1', 'native_inputs_sha256': self.native['inputs_sha256'],
            'native_worker_script_sha256': self.ninputs['script_sha256'], 'release_state': str(self.bridge_path),
            'release_state_sha256': N.R.digest(N.U.private(self.bridge_path)), 'source_root': '/data/bridge',
            'tool_source_commit': 'b' * 40, 'runtime_before': self.before}
        N.R.write_new(ndir / 'bridge-runtime-binding.json', N.R.encoded(prior))
        self.native.update(runtime_binding_file='bridge-runtime-binding.json', runtime_binding_sha256=N.R.digest(N.R.encoded(prior)))
        N.R.write_new(self.native_path, N.R.encoded(self.native))
        catalog = (ROOT / 'deploy/infra/targets.json').read_bytes()
        N.R.write_new(self.directory / 'targets.json', catalog)
        N.R.write_new(self.directory / 'native-service-before.private', b'old unit')
        self.inputs = {'format': I.FORMAT, 'component': 'nginx', 'target': K.catalog()['nginx'], 'image_id': image('b'),
            'source_root': str(ROOT), 'tool_source_commit': 'c' * 40, 'normalization_state': str(self.native_path),
            'native_inputs_sha256': self.native['inputs_sha256'], 'prior_binding': {k: self.native[k] for k in ('runtime_binding_file','runtime_binding_sha256')},
            'base_bridge_state': str(self.bridge_path), 'before': self.before, 'snapshots': {}, 'nginx': None,
            'native_service_sha256': N.R.digest(b'old unit'), 'catalog_sha256': N.R.digest(catalog),
            'recovery_policy': N.U.RECOVERY, 'tls_ca_file': None,
            'fingerprint_before': {'version': '1.31.1'}, 'component_before': {}}
        for side in ('forward','rollback'):
            raw = N.R.encoded({'services': {}})
            N.R.write_new(self.directory / (side + '.compose.json'), raw)
            self.inputs['snapshots'][side] = N.R.digest(raw)
        N.R.write_new(self.directory / 'database-before-forward.json', N.R.encoded(anchor()))
        self.state = {'format': I.FORMAT, 'phase': 'active', 'anchor_sha256': {'forward': N.R.digest(N.R.encoded(anchor()))}}
        self.write_inputs()
        self.receipt('forward', self.after)
        patch.object(N.D, 'configuration', return_value=({}, {}, {})).start()
        patch.object(N.R, 'load_state', return_value=bridge).start()
        self.commands = []
        self.api = types.SimpleNamespace(ConfigError=N.D.ConfigError, ensure_stable=N.D.ensure_stable,
            configuration=lambda *_: ({'compose_project': 'fixture'}, {}, {}), inspect_project=lambda *a, **k: deepcopy(self.before),
            run=lambda command, **kwargs: self.commands.append(command) or '')

    def write_inputs(self):
        N.R.update_state(self.directory / 'inputs.json', self.inputs)
        self.state['inputs_sha256'] = N.R.digest(N.U.private(self.directory / 'inputs.json'))
        I.save(self.path, self.state, self.state['phase'])

    def receipt(self, side, runtime):
        N.R.update_state(self.directory / ('receipt-' + side + '.json'), {'format': I.FORMAT, 'side': side,
            'inputs_sha256': self.state['inputs_sha256'], 'runtime_before': runtime,
            'source_root': str(ROOT), 'tool_source_commit': 'c' * 40, 'normalization_state': str(self.native_path),
            'anchor_sha256': self.state['anchor_sha256'].get(side), 'database_after': anchor()})

    def bind(self):
        receipt=json.loads(N.U.private(self.directory/'receipt-forward.json'))
        with patch.object(I, 'verify', return_value=self.after), \
             patch.object(K, 'stable_database_anchor', return_value=receipt['database_after']):
            return I.bind_native(self.api, self.path, 'forward')

    def test_native_handoff_preserves_frozen_inputs_and_cursor(self):
        raw = N.U.private(self.native_path.parent / 'inputs.json')
        script = N.U.private(self.native_path.parent / 'native.exs')
        self.bind()
        native = N.load(self.native_path)
        context = N.runtime_binding(self.native_path, native)
        self.assertEqual(context['infrastructure_release_state'], str(self.path))
        self.assertEqual(context['previous_source_root'], '/data/bridge')
        self.assertEqual(native['database']['native_meta']['max_block_number'], 20)
        self.assertEqual(raw, N.U.private(self.native_path.parent / 'inputs.json'))
        self.assertEqual(script, N.U.private(self.native_path.parent / 'native.exs'))

    def test_completed_native_job_stays_completed(self):
        self.native['phase'] = 'completed'
        self.native['database']['native_meta']['max_block_number'] = -1
        N.R.update_state(self.native_path, self.native)
        receipt=json.loads(N.U.private(self.directory/'receipt-forward.json'))
        receipt['database_after']['native_status']['status']='completed'
        receipt['database_after']['native_status']['meta']['max_block_number']=-1
        N.R.update_state(self.directory/'receipt-forward.json',receipt)
        self.assertTrue(self.bind()['normalization_complete'])
        with patch.object(N, 'install_service', return_value={'service_start_requested': False}) as install:
            I.resume_native(self.api, self.path, 'forward')
        install.assert_called_once_with(self.api, self.native_path, start=False)
        self.assertEqual(N.load(self.native_path)['phase'], 'completed')

    def test_completed_database_checkpoint_updates_a_stale_paused_journal(self):
        receipt=json.loads(N.U.private(self.directory/'receipt-forward.json'))
        receipt['database_after']['native_status']['status']='completed'
        receipt['database_after']['native_status']['meta']['max_block_number']=-1
        N.R.update_state(self.directory/'receipt-forward.json',receipt)
        self.assertTrue(self.bind()['normalization_complete'])
        self.assertEqual(N.load(self.native_path)['phase'],'completed')
        self.assertEqual(N.load(self.native_path)['database']['native_meta']['max_block_number'],-1)

    def test_receipt_tampering_rejected(self):
        self.bind()
        receipt = json.loads(N.U.private(self.directory / 'receipt-forward.json'))
        receipt['runtime_before']['backend']['Image'] = image('f')
        N.R.update_state(self.directory / 'receipt-forward.json', receipt)
        with self.assertRaises(N.D.ConfigError):
            N.load(self.native_path)

    def test_unrelated_service_cannot_change_in_handoff(self):
        self.after['db']['Id'] = 'unreviewed-database'
        self.receipt('forward', self.after)
        with self.assertRaises(N.D.ConfigError):
            self.bind()

    def test_snapshot_tampering_rejected(self):
        (self.directory / 'forward.compose.json').write_text('{}')
        with self.assertRaises(N.D.ConfigError):
            I.load(self.path)

    def test_newer_deployment_ownership_is_not_overwritten(self):
        native = deepcopy(self.native)
        native['runtime_binding_file'] = 'frontend-runtime-binding-other-forward.json'
        with self.assertRaises(N.D.ConfigError):
            I.own_native_binding(native, self.inputs, self.path)

    def test_public_output_suppresses_environment_values(self):
        text = json.dumps(I.public(self.path))
        self.assertNotIn('do-not-print', text)
        self.assertNotIn('PRIVATE_CANARY', text)
        self.assertIn('automatic_data_restore', text)

    def test_database_progress_accepts_newer_cursor_but_not_reset(self):
        K.check_anchor(anchor(20), anchor(19), progress=True)
        with self.assertRaises(N.D.ConfigError):
            K.check_anchor(anchor(19), anchor(20), progress=True)
        with self.assertRaises(N.D.ConfigError):
            K.check_anchor(anchor(20), anchor(19))
        changed = anchor(19)
        changed['native_status']['meta']['v10_native_job'] = 'another job'
        with self.assertRaises(N.D.ConfigError):
            K.check_anchor(anchor(), changed, progress=True)
        done=anchor(-1)
        done['native_status']['status']='completed'
        with self.assertRaises(N.D.ConfigError):
            K.check_anchor(done,anchor(-1),progress=True)

    def test_database_identity_and_endpoint_must_survive_patch(self):
        for key in ('system_identifier','database_identity','heap_identity','versions'):
            altered = anchor()
            altered[key] = 'changed'
            with self.assertRaises(N.D.ConfigError):
                K.check_anchor(anchor(), altered, progress=True)

    def test_postgres_snapshot_preserves_data_user_tuning_and_ip(self):
        before = deepcopy(self.before)
        before['db']['Config']['Entrypoint'] = ['docker-entrypoint.sh']
        model = {'services': {name: {'image': 'old'} for name in before}, 'networks': {'default': {'name': 'dev'}}}
        model['services']['db'].update(user='2000:2000', command=['postgres','-c','shared_buffers=4GB'],
            shm_size=6442450944, volumes=[{'type':'bind','source':'/data/existing','target':'/var/lib/postgresql/data'}], networks={'default':{}})
        model['services']['stats-db-init']['image'] = 'previously-patched-init-declaration'
        new = K.snapshot(model, before, 'postgresql-main', image('b'), self.path, 'forward')
        self.assertEqual(new['services']['db']['entrypoint'], K.PG_GUARD)
        self.assertEqual(new['services']['db']['command'], model['services']['db']['command'])
        self.assertEqual(new['services']['db']['volumes'], model['services']['db']['volumes'])
        self.assertEqual(new['services']['db']['networks']['default']['ipv4_address'], '172.20.0.2')
        self.assertEqual(new['networks']['default'], {'name':'dev','external':True})
        self.assertEqual(new['services']['db-init']['image'], image('b'))
        self.assertEqual(new['services']['stats-db-init']['image'], 'previously-patched-init-declaration')
        self.assertEqual(new['services']['frontend']['image'], before['frontend']['Image'])

    def test_recreated_postgres_never_initializes_an_empty_volume(self):
        self.assertIn('PG_VERSION', K.PG_GUARD[2])
        self.assertIn('global/pg_control', K.PG_GUARD[2])
        self.assertIn('exit 73', K.PG_GUARD[2])

    def test_postgres_extension_collation_replication_review_guards(self):
        good = {'version':150018,'in_recovery':False,'prepared_transactions':0,'replicas':0,'tablespaces':0,
            'slot_plugins':[],'collation_mismatches':0,'database_collation_mismatch':False,'float_gist_indexes':0,'ltree_installed':False}
        K.validate_postgres(good)
        for key, value in [('version',160001),('prepared_transactions',1),('replicas',1),('tablespaces',1),
            ('slot_plugins',['custom_plugin']),('collation_mismatches',1),('database_collation_mismatch',True),
            ('float_gist_indexes',1),('ltree_installed',True)]:
            data = dict(good, **{key:value})
            with self.assertRaises(N.D.ConfigError):
                K.validate_postgres(data)

    def test_os_major_change_rejected(self):
        target = K.catalog()['postgresql']
        before = {'version':'15.18','os':{'ID':'debian','VERSION_ID':'13'}}
        candidate = {'version':'15.19','os':{'ID':'debian','VERSION_ID':'13'}}
        K.validate_target('postgresql', before, candidate, target)
        candidate['os']['VERSION_ID'] = '12'
        with self.assertRaises(N.D.ConfigError):
            K.validate_target('postgresql', before, candidate, target)
        candidate['os']['VERSION_ID'] = '13'
        before['version'] = '15.20'
        with self.assertRaises(N.D.ConfigError):
            K.validate_target('postgresql', before, candidate, target)

    def test_nginx_frozen_config_keeps_original_template_mount(self):
        model = {'services':{'proxy':{'volumes':[{'type':'bind','source':'/data/templates','target':'/etc/nginx/templates'}]}}}
        files = [{'type':'bind','source':'/data/private/nginx.conf','target':'/etc/nginx/nginx.conf','read_only':True}]
        result = S.nginx_snapshot(model, files)
        self.assertEqual(result['services']['proxy']['entrypoint'], ['nginx'])
        self.assertEqual(result['services']['proxy']['volumes'][0], model['services']['proxy']['volumes'][0])
        self.assertTrue(result['services']['proxy']['volumes'][1]['read_only'])

    def test_unverified_tls_or_websocket_is_not_qualified(self):
        good = {name:{'http_status':200,'transport_exit':0} for name in ('deposits','backend_api','deposit_api','stats_api','https')}
        good.update(source_map={'http_status':404,'transport_exit':0}, websocket={'http_status':101,'transport_exit':28})
        S.validate_routes(good)
        for name, status in [('https',0),('websocket',400),('source_map',200)]:
            data = deepcopy(good)
            data[name]['http_status'] = status
            with self.assertRaises(N.D.ConfigError):
                S.validate_routes(data)

    def test_offline_recovery_pauses_owned_client_without_database_rpc(self):
        unit = self.base / 'unit.service'
        unit.write_bytes(b'old unit')
        self.api.run = lambda command, **kw: self.commands.append(command) or ('inactive' if command[:2]==['systemctl','show'] else '')
        with patch.object(N, 'SYSTEMD_DIRECTORY', self.base), patch.object(N, 'UNIT', 'unit.service'), \
             patch.object(N, 'render_service', return_value='new unit'), patch.object(N, 'stop_client') as stop, \
             patch.object(N, 'owned', return_value=None), patch.object(N, 'rpc', side_effect=AssertionError('No DB RPC while down')):
            I.pause(self.api, self.path, 'rollback')
        stop.assert_called_once()
        self.assertEqual(N.load(self.native_path)['phase'], 'paused')
        self.assertTrue((self.native_path.parent / 'pause.request').exists())

    def test_old_anchor_adoption_is_recoverable_before_activation_request(self):
        self.state['anchor_sha256'] = {}
        self.state['phase'] = 'staged'
        I.save(self.path, self.state, 'staged')
        (self.directory / 'receipt-forward.json').unlink()
        I.load(self.path)
        I.save(self.path, self.state, 'activation_requested')
        with self.assertRaises(N.D.ConfigError):
            I.load(self.path)

    def test_paused_anchor_cannot_be_changed_after_recording(self):
        N.R.update_state(self.directory / 'database-before-forward.json', anchor(10))
        with self.assertRaises(N.D.ConfigError):
            I.load(self.path)

    def test_second_component_handoff_authenticates_the_first_receipt(self):
        self.bind()
        first_path = self.path
        self.native = N.load(self.native_path)
        self.inputs['prior_binding'] = {k: self.native[k] for k in ('runtime_binding_file', 'runtime_binding_sha256')}
        self.inputs['component'] = 'redis'
        self.inputs['target'] = K.catalog()['redis']
        self.inputs['before'] = deepcopy(self.after)
        self.directory = self.base / 'infra-20261107T110000Z-8765abcd'
        self.directory.mkdir(mode=0o700)
        self.path = self.directory / 'state.json'
        for name in ('targets.json', 'native-service-before.private', 'forward.compose.json',
                     'rollback.compose.json', 'database-before-forward.json'):
            N.R.write_new(self.directory / name, N.U.private(first_path.parent / name))
        self.after['redis-db'] = row('new-redis', 'b')
        self.write_inputs()
        self.receipt('forward', self.after)
        self.bind()
        self.assertEqual(N.runtime_binding(self.native_path, N.load(self.native_path))['infrastructure_release_state'], str(self.path))
        previous = json.loads(N.U.private(first_path.parent / 'receipt-forward.json'))
        previous['runtime_before']['backend']['Image'] = image('f')
        N.R.update_state(first_path.parent / 'receipt-forward.json', previous)
        with self.assertRaises(N.D.ConfigError):
            N.load(self.native_path)

    def test_completed_service_unit_is_installed_without_starting_processing(self):
        with patch.object(N, 'SYSTEMD_DIRECTORY', self.base), patch.object(N, 'UNIT', 'completed.service'), \
             patch.object(N, 'load', return_value=self.native), \
             patch.object(N, 'runtime_binding', return_value={'tool_source_commit': 'c' * 40}), \
             patch.object(N.B, 'clean_source', return_value=('c' * 40, '10.2.6')), \
             patch.object(N, 'service_text', return_value='owned unit'):
            result = N.install_service(self.api, self.native_path, start=False)
        self.assertFalse(result['service_start_requested'])
        self.assertIn(['systemctl', 'enable', 'completed.service'], self.commands)
        self.assertFalse(any('--now' in c or 'start' in c for c in self.commands))

    def test_receipt_retry_verifies_without_recreating_service(self):
        with patch.object(I.F, 'source_review', return_value='c' * 40), \
             patch.object(I, 'verify', return_value=self.after) as verify, \
             patch.object(I.F, 'private_command', side_effect=AssertionError('No recreation after receipt')):
            receipt = I.activate(self.api, self.path, 'forward')
        self.assertEqual(receipt['runtime_before'], self.after)
        verify.assert_called_once()
        self.assertFalse(self.commands)

    def test_offline_main_database_rollback_preserves_saved_progress(self):
        (self.directory / 'receipt-forward.json').unlink()
        self.inputs['component'] = 'postgresql-main'
        self.inputs['target'] = K.catalog()['postgresql']
        self.inputs['fingerprint_before'] = {'version': '15.18'}
        self.write_inputs()
        self.native['database']['native_meta']['max_block_number'] = 19
        N.R.update_state(self.native_path, self.native)
        runtime = deepcopy(self.before)
        runtime.pop('db')
        self.api.inspect_project = lambda *a, **kw: runtime
        data = self.base / 'pgdata'
        (data / 'global').mkdir(parents=True)
        (data / 'PG_VERSION').write_text('15\n')
        (data / 'global/pg_control').write_bytes(b'existing control data')
        with patch.object(I.F, 'source_review', return_value='c' * 40), \
             patch.object(K, 'stable_database_anchor', side_effect=[N.D.ConfigError('DB offline'), anchor(19)]), \
             patch.object(K, 'data_mount', return_value=data), \
             patch.object(N.J.P, 'free_bytes', return_value=N.RESUME_FREE), \
             patch.object(I.F, 'private_command') as recreate, \
             patch.object(I, 'verify', return_value=self.after):
            receipt = I.activate(self.api, self.path, 'rollback')
        saved = json.loads(N.U.private(self.directory / 'database-before-rollback.json'))
        self.assertEqual(saved['continuity_mode'], 'bounded_progress')
        self.assertEqual(saved['native_status']['meta']['max_block_number'], 19)
        self.assertEqual(receipt['database_after'], anchor(19))
        command = recreate.call_args.args[0]
        self.assertEqual(command[-1], 'db')
        self.assertIn('--no-deps', command)
        self.assertNotIn('db-init', command)
        self.assertNotIn('stats-db-init', command)
        self.assertEqual((data / 'PG_VERSION').read_bytes(), b'15\n')

    def test_activation_failure_never_binds_or_resumes_native_job(self):
        with patch.object(sys, 'argv', ['upgrade', '--resume', '--state', str(self.path)]), \
             patch.object(I.sys, 'platform', 'linux'), patch.object(I.os, 'geteuid', return_value=0), \
             patch.object(I, 'load', return_value=(self.state, self.inputs)), \
             patch.object(I, 'pause') as pause, \
             patch.object(I.M, 'global_lock', return_value=nullcontext()), \
             patch.object(I.B, 'build_lock', return_value=nullcontext()), \
             patch.object(I, 'activate', side_effect=N.D.ConfigError('Patch not healthy')), \
             patch.object(I, 'bind_native') as bind, patch.object(I, 'resume_native') as resume, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(I.main(), 2)
        pause.assert_called_once()
        bind.assert_not_called()
        resume.assert_not_called()


if __name__ == '__main__':
    unittest.main()
