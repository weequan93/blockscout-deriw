import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('testnet_preflight', ROOT / 'deploy/scripts/testnet_v10_preflight.py')
P = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(P)
SECRET = 'private-preflight-fixture-secret'


class TestnetPreflightTests(unittest.TestCase):
    def metadata(self):
        primitives = P.load_primitives(ROOT)
        return primitives, {
            'runtime_version': '9.0.1',
            'metadata': {
                'server': [[150019, '/pg', False, 1000, '172.28.0.2', 5432]],
                'settings': [[key, '1', None] for key in primitives.SETTINGS],
                'tablespaces': [], 'replication_slots': [[0, 0]], 'relations': [],
                'repository_routes': [{'repository': name, 'shares_main_configuration': True}
                                      for name in ('Explorer.Repo', 'Explorer.Repo.Account', 'Explorer.Repo.Arbitrum')],
            },
            'schema': {'applied_versions': [1], 'types': [['trace_address', 'integer[]'], ['value', 'numeric(100,0)']],
                       'columns': [], 'indexes': [],
                       'relation_sizes': [['tokens', 1000]],
                       'presence': [{'table': t, 'exists': False, 'has_rows': None} for t in P.EMPTY],
                       'background_statuses': [['empty_internal_transactions_data', 'started']]},
        }

    def test_wrong_chain_stops_before_snapshot_or_database_access(self):
        row = {'Config': {'Env': ['CHAIN_ID=18417507517', 'CHAIN_TYPE=arbitrum']}}
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / 'private'
            with patch.object(P, 'run', return_value=json.dumps([row])) as command:
                with self.assertRaises(P.PreflightError):
                    P.collect(ROOT, 'backend', private)
            self.assertFalse(private.exists())
            command.assert_called_once_with(['docker', 'inspect', 'backend'])

    def test_separate_repository_route_is_reported_as_unknown_pending_versions(self):
        primitives, data = self.metadata()
        plan = json.loads(subprocess.check_output(['git', '-C', str(ROOT), 'show',
                          P.SOURCE + ':deploy/upgrades/v10.2.6.json'], text=True))
        routes = data['metadata']['repository_routes']
        routes[1]['shares_main_configuration'] = False
        result = P.schema_summary(plan, data['schema'], routes)
        self.assertIsNone(result['pending_initial_schema_versions']['account'])
        self.assertTrue(result['pending_initial_schema_versions']['repo'])
        self.assertEqual(result['deferred_index_versions'], [20260721000000, 20260812000000])
        data['schema']['background_statuses'][0][1] = SECRET
        self.assertNotIn(SECRET, json.dumps(P.schema_summary(plan, data['schema'], routes)))

    def test_configuration_snapshot_keeps_literal_values_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compose = root / 'compose.yml'
            compose.write_text(SECRET)
            row = {'Config': {'Env': ['SECRET=' + SECRET], 'Labels': {
                'com.docker.compose.project.config_files': str(compose),
                'com.docker.compose.project.working_dir': str(root)}}}
            model = {'services': {'backend': {'environment': {'SECRET': SECRET}}}}
            with patch.object(P, 'run', return_value=json.dumps(model)):
                result = P.compose_snapshot(root, row, 'fixture', root)
            self.assertTrue(result['captured'])
            self.assertNotIn(SECRET, json.dumps(result))
            snapshot = root / 'compose.private.json'
            self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(snapshot.read_text()), model)

    def test_full_collection_only_observes_services_and_never_exports_private_values(self):
        primitives, observation = self.metadata()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            pg = base / 'pg'
            (pg / 'pg_wal').mkdir(parents=True)
            root = base / 'repo'
            root.mkdir()
            compose = root / 'compose.yml'
            compose.write_text('private configuration ' + SECRET)
            model = {'services': {'backend': {'environment': {'CHAIN_ID': '2885', 'SECRET': SECRET}}}}
            def row(name, number):
                return {'Id': str(number) * 64, 'Image': 'sha256:' + str(number) * 64,
                    'Config': {'Image': 'fixture:' + name, 'Env': ['CHAIN_ID=2885', 'CHAIN_TYPE=arbitrum', 'SECRET=' + SECRET],
                               'Labels': {'com.docker.compose.project': 'fixture', 'com.docker.compose.service': name,
                                'com.docker.compose.project.config_files': str(compose),
                                'com.docker.compose.project.working_dir': str(root)}},
                    'State': {'Running': True, 'Status': 'running'}, 'RestartCount': 0,
                    'Mounts': [{'Type': 'bind', 'Destination': '/pg', 'Source': str(pg)}] if name == 'db' else [],
                    'NetworkSettings': {'Networks': {'fixture': {'IPAddress': '172.28.0.' + str(number)}}}}
            backend, db = row('backend', 1), row('db', 2)
            commands = []
            real_run = P.run
            def execute(command, **kwargs):
                commands.append(command)
                if command[0] == 'git':
                    if command[-1] == '--show-toplevel': return str(root)
                    if 'show' in command: return real_run(['git', '-C', str(ROOT), *command[3:]], **kwargs)
                    return ''
                if command == ['docker', 'inspect', 'backend']: return json.dumps([backend])
                if command[:3] == ['docker', 'ps', '-aq']: return backend['Id'] + '\n' + db['Id']
                if command == ['docker', 'inspect', backend['Id'], db['Id']]: return json.dumps([backend, db])
                if command[:2] == ['docker', 'compose']:
                    self.assertEqual(command[-3:], ['config', '--format', 'json'])
                    return json.dumps(model)
                if command[:3] == ['docker', 'image', 'inspect']: return json.dumps([{'Os': 'linux', 'Architecture': 'amd64'}])
                if command[:4] == ['docker', 'exec', backend['Id'], 'bin/blockscout']:
                    self.assertEqual(command[4], 'rpc')
                    self.assertIn('SET TRANSACTION READ ONLY', command[-1])
                    self.assertNotIn('18417507517', command[-1])
                    return P.MARKER + json.dumps(observation)
                if command[:4] == ['docker', 'exec', db['Id'], 'readlink']:
                    return command[-1]
                self.fail('Unexpected command: ' + str(command[:4]))
            with patch.object(P, 'run', side_effect=execute):
                report = P.collect(root, 'backend', base / 'private')
            rendered = json.dumps(report)
            self.assertNotIn(SECRET, rendered)
            self.assertNotIn('172.28.0.2', rendered)
            self.assertNotIn(str(pg), rendered)
            self.assertFalse(report['deployment_ready'])
            self.assertFalse(report['database_backup_created'])
            self.assertFalse(report['services_restarted'])
            self.assertEqual(len(report['storage']), 2)
            self.assertEqual(compose.read_text(), 'private configuration ' + SECRET)
            self.assertTrue(report['database_route_matches_inspected_container'])


if __name__ == '__main__':
    unittest.main()
