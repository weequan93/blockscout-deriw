#!/usr/bin/env python3
"""Resolve the three Compose directories without a daemon or live credentials."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
NETWORKS = {'dev': '18417507517', 'test': '12345', 'prod': '67890'}


class ComposeDirectoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('docker'):
            raise unittest.SkipTest('Docker Compose is required; no daemon is used')
        probe = subprocess.run(['docker', 'compose', 'version'], capture_output=True)
        if probe.returncode:
            raise unittest.SkipTest('Docker Compose is unavailable')
        value = 'fixture$literal$$pair'
        fixture = {'services': {'fixture': {'image': 'scratch', 'environment': {
            'VALUE': '${DERIW_COMPOSE_TEST_LITERAL:?required}'}}}}
        environment = dict(os.environ, DERIW_COMPOSE_TEST_LITERAL=value)
        probe = subprocess.run(['docker', 'compose', '-p', 'deriw-config-test',
                                '--env-file', os.devnull, '-f', '-', 'config', '--format', 'json'],
                               input=json.dumps(fixture), env=environment, text=True,
                               capture_output=True, check=True)
        rendered = json.loads(probe.stdout)['services']['fixture']['environment']['VALUE']
        if rendered not in (value, value.replace('$', '$$')):
            raise AssertionError('Unknown Compose config dollar escaping')
        cls.dollar_escaping = rendered != value

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='deriw-compose-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        shutil.copytree(ROOT / 'deploy/compose', self.root / 'deploy/compose')
        self.values = {}
        for network, chain_id in NETWORKS.items():
            directory = self.root / ('docker-compose.' + network)
            shutil.copytree(ROOT / ('docker-compose.' + network), directory)
            self.initialize(directory)
            values = dict(line.split('=', 1) for line in (directory / '.env').read_text().splitlines()
                          if line and not line.startswith('#') and '=' in line)
            for key, value in list(values.items()):
                if value:
                    continue
                if key == 'CHAIN_ID':
                    values[key] = chain_id
                elif key == 'BLOCKSCOUT_VERSION':
                    values[key] = 'a' * 40
                elif key.endswith('_IMAGE'):
                    values[key] = 'example.invalid/' + key.lower() + ':fixture'
                elif key.endswith('_DIR'):
                    path = directory / 'fixture' / key.lower()
                    path.mkdir(parents=True)
                    values[key] = str(path)
            self.values[network] = values
            self.write_values(network)
            with (directory / 'envs/common-blockscout.env').open('a') as stream:
                stream.write("\nFIXTURE_PRIVATE='" + network + "-literal$with$$dollars'\n")
                # Compose is authoritative even if an old runtime file has a stale chain ID.
                stream.write('CHAIN_ID=999999\n')

    def directory(self, network):
        return self.root / ('docker-compose.' + network)

    def initialize(self, directory):
        result = subprocess.run(['bash', str(self.root / 'deploy/compose/setup.sh')],
                                cwd=directory, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def write_values(self, network):
        (self.directory(network) / '.env').write_text(
            ''.join(key + '=' + value + '\n' for key, value in self.values[network].items()))

    def resolve(self, network, success=True):
        environment = {key: value for key, value in os.environ.items()
                       if key not in self.values[network] and not key.startswith('COMPOSE_')}
        result = subprocess.run(['docker', 'compose', 'config', '--format', 'json'],
                                cwd=self.directory(network), env=environment,
                                text=True, capture_output=True, timeout=30)
        if not success:
            self.assertNotEqual(result.returncode, 0)
            return result
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_networks_load_only_their_own_runtime_files_and_paths(self):
        for network, chain_id in NETWORKS.items():
            with self.subTest(network=network):
                model = self.resolve(network)
                backend = model['services']['backend']
                frontend = model['services']['frontend']
                self.assertEqual(model['name'], 'deriw-' + network)
                self.assertEqual(model['networks']['default']['name'], 'deriw-' + network + '_default')
                self.assertEqual(backend['environment']['CHAIN_ID'], chain_id)
                self.assertEqual(frontend['environment']['NEXT_PUBLIC_NETWORK_ID'], chain_id)
                self.assertEqual(frontend['environment']['NEXT_PUBLIC_IS_TESTNET'],
                                 'false' if network == 'prod' else 'true')
                private = network + '-literal$with$$dollars'
                if self.dollar_escaping:
                    private = private.replace('$', '$$')
                self.assertEqual(backend['environment']['FIXTURE_PRIVATE'], private)
                self.assertEqual(Path(backend['build']['context']), self.root)
                self.assertEqual(backend['build']['args']['CHAIN_TYPE'], 'arbitrum')
                self.assertEqual(backend['build']['args']['RELEASE_VERSION'], '10.2.6')
                self.assertEqual(backend['command'], ['bin/blockscout', 'start'])
                self.assertEqual(backend['environment']['MIGRATION_BACKGROUND_AUTOSTART_DISABLED'], 'true')
                self.assertNotIn('build', frontend)
                for service in model['services'].values():
                    for mount in service.get('volumes', []):
                        if mount['type'] == 'bind':
                            self.assertTrue(Path(mount['source']).is_relative_to(self.directory(network)))
                            self.assertFalse(mount['bind']['create_host_path'])

    def test_local_settings_merge_with_shared_services(self):
        (self.directory('test') / 'docker-compose.local.yml').write_text('''services:
  db:
    user: "2000:2000"
    shm_size: 6gb
    command: [postgres, -c, max_connections=80]
  backend:
    container_name: existing-test-backend
    volumes:
      - type: bind
        source: ./fixture/queue
        target: /folder_1
        bind:
          create_host_path: false
''')
        model = self.resolve('test')
        self.assertEqual(model['services']['db']['user'], '2000:2000')
        self.assertEqual(model['services']['db']['command'], ['postgres', '-c', 'max_connections=80'])
        self.assertEqual(model['services']['db']['shm_size'], '6442450944')
        backend = model['services']['backend']
        self.assertEqual(backend['container_name'], 'existing-test-backend')
        self.assertEqual(backend['command'], ['bin/blockscout', 'start'])
        self.assertEqual({mount['target'] for mount in backend['volumes']},
                         {'/app/logs', '/app/dets', '/folder_1'})
        self.assertNotIn('container_name', self.resolve('prod')['services']['backend'])

    def test_missing_network_and_data_selection_fail_before_startup(self):
        for key in ('CHAIN_ID', 'BLOCKSCOUT_DB_DATA_DIR', 'POSTGRES_IMAGE'):
            with self.subTest(key=key):
                value = self.values['prod'][key]
                self.values['prod'][key] = ''
                self.write_values('prod')
                self.assertIn(key, self.resolve('prod', success=False).stderr)
                self.values['prod'][key] = value
        self.write_values('prod')

    def test_setup_preserves_private_files_and_does_not_evaluate_them(self):
        directory = self.directory('test')
        marker = self.root / 'must-not-exist'
        runtime = directory / 'envs/common-blockscout.env'
        with runtime.open('a') as stream:
            stream.write('\nFIXTURE_SHELL=$(touch ' + str(marker) + ')\n')
        paths = [directory / '.env', directory / 'docker-compose.local.yml', runtime]
        before = {path: path.read_bytes() for path in paths}
        self.initialize(directory)
        self.assertEqual(before, {path: path.read_bytes() for path in paths})
        self.assertFalse(marker.exists())
        for path in paths:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def start_with_stub_docker(self, network, fail=False):
        directory = self.root / 'commands'
        directory.mkdir(exist_ok=True)
        log = self.root / ('docker-' + network + '.jsonl')
        stub = directory / 'docker'
        stub.write_text('#!' + sys.executable + '''
import json, os, sys
with open(os.environ['DERIW_TEST_DOCKER_LOG'], 'a') as stream:
    stream.write(json.dumps({'args': sys.argv[1:], 'cwd': os.getcwd()}) + '\\n')
if 'config' in sys.argv and os.environ.get('DERIW_TEST_CONFIG_FAIL') == '1':
    sys.exit(42)
''')
        stub.chmod(0o755)
        environment = dict(os.environ, PATH=str(directory) + os.pathsep + os.environ['PATH'],
                           DERIW_TEST_DOCKER_LOG=str(log), DERIW_TEST_CONFIG_FAIL='1' if fail else '0',
                           COMPOSE_FILE='/must/not/select/an/ambient/file.yml')
        # Invoke from outside the selected directory; no real Docker startup is performed.
        result = subprocess.run([str(self.directory(network) / 'start.sh'), '--build'],
                                cwd=self.root, env=environment, text=True, capture_output=True)
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return result, calls

    def test_start_scripts_validate_then_start_the_selected_directory(self):
        for network in NETWORKS:
            with self.subTest(network=network):
                result, calls = self.start_with_stub_docker(network)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(calls[0]['args'], ['compose', 'version'])
                selection = ['compose', '--project-directory', str(self.directory(network)),
                             '--env-file', str(self.directory(network) / '.env'),
                             '-f', str(self.directory(network) / 'docker-compose.yml')]
                self.assertEqual(calls[1]['args'], selection + ['config', '--quiet'])
                self.assertEqual(calls[2]['args'], selection + ['up', '-d', '--build'])
                self.assertEqual(Path(calls[2]['cwd']), self.directory(network))

    def test_start_refuses_failed_config_or_missing_server_values(self):
        result, calls = self.start_with_stub_docker('test', fail=True)
        self.assertEqual(result.returncode, 42)
        self.assertFalse(any('up' in row['args'] for row in calls))
        (self.directory('prod') / '.env').unlink()
        result, calls = self.start_with_stub_docker('prod')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('setup.sh', result.stderr)
        self.assertEqual(calls, [])

    def test_git_ignores_server_values_but_keeps_definitions_and_commands(self):
        for network in NETWORKS:
            base = 'docker-compose.' + network + '/'
            for relative in ('.env', 'envs/common-blockscout.env', 'docker-compose.local.yml'):
                probe = subprocess.run(['git', 'check-ignore', '--no-index', '-q', base + relative], cwd=ROOT)
                self.assertEqual(probe.returncode, 0, base + relative)
            for relative in ('docker-compose.yml', '.env.sample', 'start.sh', 'envs/.gitignore'):
                probe = subprocess.run(['git', 'check-ignore', '--no-index', '-q', base + relative], cwd=ROOT)
                self.assertEqual(probe.returncode, 1, base + relative)


if __name__ == '__main__':
    unittest.main()
