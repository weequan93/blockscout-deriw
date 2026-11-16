import contextlib
from copy import deepcopy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import deploy_testnet_v10 as T


def fixture():
    service = {'container_name': 'backend', 'image': 'old:latest', 'restart': 'always',
               'environment': {'CHAIN_ID': '2885', 'CHAIN_TYPE': 'arbitrum', 'NFT_MEDIA_HANDLER_ENABLED': 'false',
                               'PRIVATE': 'literal$with$$pair\nand newline'},
               'volumes': [{'type': 'bind', 'source': '/private/test/logs', 'target': '/app/logs'}],
               'networks': {'default': {'aliases': ['backend']}},
               'extra_hosts': {'host.docker.internal': 'host-gateway'}}
    row = {'Id': '1' * 64, 'Name': '/backend', 'Image': 'sha256:' + '2' * 64,
           'State': {'Running': True},
           'Config': {'Env': [k + '=' + v for k, v in {**service['environment'], 'IMAGE_ONLY': 'keep-me'}.items()],
                      'Labels': {}, 'WorkingDir': '/app', 'Entrypoint': None},
           'HostConfig': {'PortBindings': {}, 'RestartPolicy': {'Name': 'always', 'MaximumRetryCount': 0},
                          'ExtraHosts': ['host.docker.internal:host-gateway']},
           'Mounts': [{'Type': 'bind', 'Source': '/private/test/logs', 'Destination': '/app/logs', 'RW': True}],
           'NetworkSettings': {'Networks': {'fixture_default': {'Aliases': ['backend'], 'IPAMConfig': None}}}}
    model = {'name': 'fixture', 'services': {'backend': service, 'db': {'image': 'postgres:15'}},
             'networks': {'default': {'name': 'fixture_default'}}}
    return model, row


class TestnetUpgradeTests(unittest.TestCase):
    def linked(self):
        model, backend = fixture()
        model['services']['backend']['links'] = ['db:database']
        database = deepcopy(backend)
        database.update(Id='3'*64, Name='/fixture-db-1')
        database['Config']['Labels']['com.docker.compose.project'] = 'fixture'
        database['NetworkSettings']['Networks']['fixture_default']['Aliases'] = ['db','fixture-db-1']
        backend['NetworkSettings']['Networks']['fixture_default']['Links'] = [
            'fixture-db-1:database','fixture-db-1:db-1','fixture-db-1:fixture-db-1']
        return model, {'backend':backend,'db':database}

    def test_internal_database_link_is_verified_from_real_endpoint_metadata(self):
        model, rows = self.linked()
        T.validate_topology(model,rows['backend'],rows=rows)
        rows['backend']['HostConfig']['Links'] = ['/fixture-db-1:/backend/database']
        rows['backend']['NetworkSettings']['Networks']['fixture_default']['Links'] = []
        T.validate_topology(model,rows['backend'],rows=rows)

    def test_custom_container_names_keep_generated_compose_link_aliases(self):
        model,rows = self.linked()
        rows['db']['Name'] = '/custom-db'
        rows['db']['Config']['Labels']['com.docker.compose.container-number'] = '1'
        rows['backend']['NetworkSettings']['Networks']['fixture_default']['Links'] = [
            'custom-db:database','custom-db:db-1','custom-db:fixture-db-1']
        T.validate_topology(model,rows['backend'],rows=rows)

    def test_missing_alias_foreign_peer_and_undeclared_links_are_rejected(self):
        for cause in ('missing','foreign','undeclared'):
            model,rows = self.linked()
            if cause == 'missing':
                rows['backend']['NetworkSettings']['Networks']['fixture_default']['Links'] = ['fixture-db-1:db-1']
            if cause == 'foreign':
                rows['backend']['NetworkSettings']['Networks']['fixture_default']['Links'] = ['foreign-db:database']
            if cause == 'undeclared':
                model['services']['backend'].pop('links')
            with self.subTest(cause=cause), self.assertRaises(T.P.PreflightError):
                T.validate_topology(model,rows['backend'],rows=rows)

    def test_unsupported_field_error_names_the_actual_service_and_field(self):
        model,row = fixture()
        model['services']['db']['network_mode'] = 'host'
        with self.assertRaisesRegex(T.P.PreflightError,'Service db uses unsupported topology fields: network_mode'):
            T.validate_topology(model,row,'db')

    def test_topology_rejects_stale_runtime_mount_network_and_port(self):
        model, row = fixture()
        T.validate_topology(model, row)
        for kind in ('mount', 'network', 'port', 'restart', 'host'):
            changed = deepcopy(row)
            if kind == 'mount': changed['Mounts'][0]['Source'] = '/other'
            if kind == 'network': changed['NetworkSettings']['Networks']['unreviewed'] = {}
            if kind == 'port': changed['HostConfig']['PortBindings']['4000/tcp'] = [{'HostIp': '', 'HostPort': '4000'}]
            if kind == 'restart': changed['HostConfig']['RestartPolicy']['Name'] = 'no'
            if kind == 'host': changed['HostConfig']['Privileged'] = True
            with self.subTest(kind=kind), self.assertRaises(T.P.PreflightError):
                T.validate_topology(model, changed)

    def test_test_configuration_and_other_services_survive_candidate(self):
        model, row = fixture()
        before = deepcopy(model)
        result = T.candidate_model(model, {'backend': row}, 'test:pinned', Path('/private/state.json'), True)
        self.assertEqual(model, before)
        self.assertEqual(result['services']['db'], model['services']['db'])
        backend = result['services']['backend']
        self.assertEqual(backend['environment']['PRIVATE'], model['services']['backend']['environment']['PRIVATE'])
        self.assertEqual(backend['environment']['IMAGE_ONLY'], 'keep-me')
        self.assertEqual(backend['environment']['CHAIN_ID'], '2885')
        self.assertEqual(backend['environment']['INDEXER_ARBITRUM_DEPOSITS_L1_BLOCKS_RANGE'], '10000')
        self.assertNotIn('TRACE_BLOCK_RANGES', backend['environment'])
        self.assertNotIn('INDEXER_DISABLE_INTERNAL_TRANSACTIONS_FETCHER', backend['environment'])
        self.assertEqual(backend['command'], ['bin/blockscout', 'start'])
        self.assertIn('$$', T.R.snapshot_bytes(result).decode())
        for key, value in T.HOLDS.items(): self.assertEqual(backend['environment'][key], value)

    def test_wrong_chain_and_unreviewed_nft_topology_fail(self):
        for bad in ('chain', 'nft'):
            model, row = fixture()
            if bad == 'chain': model['services']['backend']['environment']['CHAIN_ID'] = '1'
            else: row['Config']['Env'] = [s.replace('NFT_MEDIA_HANDLER_ENABLED=false', 'NFT_MEDIA_HANDLER_ENABLED=true') for s in row['Config']['Env']]
            with self.assertRaises(T.P.PreflightError):
                T.candidate_model(model, {'backend': row}, 'test:pinned', Path('/private/state.json'), False)

    def plan(self, directory):
        model, backend = fixture()
        before = {'backend': backend, 'db': deepcopy(backend)}
        candidate = T.candidate_model(model, before, 'test:pinned', Path(directory) / 'state.json', True)
        path = Path(directory) / 'state.json'
        selected = path.parent / 'candidate.compose.json'
        T.R.write_new(selected, T.R.snapshot_bytes(candidate))
        state = {'project': 'fixture', 'preparation_complete': True, 'before': before,
                 'repo': directory, 'compose_directory': directory + '/private-compose',
                 'image_id': 'sha256:fixture', 'image_tag': 'test:pinned', 'schema_complete': False,
                 'candidate_sha256': T.R.digest(selected.read_bytes())}
        return path, state, before

    def test_failed_cursor_handoff_cannot_stop_or_migrate(self):
        with tempfile.TemporaryDirectory() as directory:
            path, state, rows = self.plan(directory)
            commands = []
            def run(args, **kwargs):
                commands.append(args)
                self.assertEqual(args[:3], ['docker', 'image', 'inspect'])
                return json.dumps([{'Id': state['image_id'], 'Config': {'Labels': {'org.opencontainers.image.revision': T.SOURCE}}}])
            with patch.object(T, 'current', return_value=rows), patch.object(T.P, 'run', side_effect=run), \
                 patch.object(T, 'ensure_private_config'), patch.object(T, 'rpc', side_effect=T.P.PreflightError('handoff failed')), \
                 patch.object(T, 'client') as client, patch.object(T, 'change') as change:
                with self.assertRaises(T.P.PreflightError): T.apply(path, state)
            client.assert_not_called()
            change.assert_not_called()
            self.assertEqual(len(commands), 1)

    def test_incomplete_preparation_cannot_activate(self):
        with patch.object(T, 'current') as current:
            with self.assertRaises(T.P.PreflightError): T.apply(Path('/private/state.json'), {'preparation_complete': False})
            current.assert_not_called()

    def test_preparation_failure_is_journaled(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            path = base / 'upgrade-fixture' / 'state.json'
            path.parent.mkdir()
            base.chmod(0o700)
            state = {'phase': 'preparing'}
            def fail(repo, preflight, backup, journal):
                journal.update(path=path, state=state)
                raise T.P.PreflightError('deliberate build failure')
            with patch.object(T, 'BASE', base), patch.object(T, 'check_host'), patch.object(T, 'prepare', side_effect=fail), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(T.backend_only_main(['--preflight', directory, '--existing-backup']), 1)
            saved = json.loads(path.read_text())
            self.assertEqual(saved['phase'], 'needs_attention')
            self.assertEqual(saved['failed_phase'], 'preparing')
            self.assertTrue(list(path.parent.glob('failure-*.private.log')))

    def test_activation_recreates_only_backend_and_preserves_successful_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            path, state, rows = self.plan(directory)
            Path(state['compose_directory']).mkdir()
            state.update(schema_complete=True, handoff={'cursors': []}, wide_reads_verified=True)
            rows['backend']['Image'] = state['image_id']
            commands = []
            metrics = 'deriw_bridge_exporter_up 1\n' + ''.join(
                'deriw_bridge_query_success{section="' + k + '"} 1\n' for k in ('deposits','withdrawals','rollup','published','confirmed'))
            metrics += ''.join('deriw_bridge_verification_success{section="' + k + '"} 1\n'
                               for k in ('batch_head','batches','deposits','parent_finalized','parent_latest','parent_safe','withdrawals'))
            def run(args, **kwargs):
                commands.append(args)
                if args[:3] == ['docker', 'image', 'inspect']:
                    return json.dumps([{'Id': state['image_id'], 'Config': {'Labels': {'org.opencontainers.image.revision': T.SOURCE}}}])
                if args[1] == 'compose': return ''
                return metrics if args[-1].endswith('/metrics') else 'OK'
            def change(args, *a, **kw):
                commands.append(args)
            with patch.object(T, 'current', return_value=rows), patch.object(T.P, 'run', side_effect=run), \
                 patch.object(T, 'ensure_private_config'), patch.object(T, 'rpc', return_value={'version': '10.2.6'}), \
                 patch.object(T, 'client') as client, patch.object(T, 'change', side_effect=change), \
                 contextlib.redirect_stdout(io.StringIO()):
                T.apply(path, state)
            client.assert_not_called()
            mutation = [c for c in commands if c[1] == 'compose']
            self.assertEqual(len(mutation), 1)
            self.assertEqual(mutation[0][-7:], ['up','-d','--no-deps','--no-build','--pull','never','backend'])
            self.assertEqual(state['phase'], 'active')


if __name__ == '__main__':
    unittest.main()
