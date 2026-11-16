from copy import deepcopy
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import testnet_v10_layout as L
from test_deploy_testnet_v10 import fixture
T = L.T


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True, stderr=subprocess.PIPE).strip()


def git_fixture(root):
    repo = root / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q', '-b', 'deriw-pre')
    git(repo, 'config', 'user.email', 'fixture@example.invalid')
    git(repo, 'config', 'user.name', 'Fixture')
    old = repo / 'docker-compose'
    old.mkdir()
    (old / 'docker-compose.yml').write_text('public old baseline')
    (repo / 'app.txt').write_text('application')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'baseline')
    before = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'switch', '-qc', 'deriw-dev')
    (repo / '.gitignore').write_text('/docker-compose.test/\n')
    (old / 'docker-compose.yml').write_text('public dev defaults')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'dev defaults')
    target = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'update-ref', 'refs/remotes/origin/deriw-dev', target)
    git(repo, 'config', 'remote.origin.url', str(repo))
    git(repo, 'config', 'remote.origin.fetch', '+refs/heads/*:refs/remotes/origin/*')
    git(repo, 'switch', '-q', 'deriw-pre')
    (old / 'docker-compose.yml').write_text('server private $configuration\n')
    for name in ('db', 'stats-db', 'redis-db', 'logs'):
        (old / 'services' / name).mkdir(parents=True)
        (old / 'services' / name / 'sentinel').write_text('preserve-' + name)
    return repo, old, before, target


def state_fixture(root):
    repo, old, before, target = git_fixture(root)
    base_model, backend = fixture()
    rows = {}
    for i, name in enumerate(('backend', 'db', 'stats-db', 'redis-db'), 1):
        row = deepcopy(backend)
        row['Id'] = str(i) * 64
        row['Image'] = 'sha256:' + str(i) * 64
        row['Name'] = '/' + name
        source = str(old / 'services' / ('logs' if name == 'backend' else name))
        row['Mounts'][0]['Source'] = source
        row['Config'].update(Cmd=['fixture-command'], Entrypoint=[], WorkingDir='/app')
        row['State'] = {'Running': False, 'Restarting': False, 'ExitCode': 0, 'OOMKilled': False}
        row['NetworkSettings']['Networks']['fixture_default']['IPAddress'] = '172.30.0.' + str(i + 1)
        rows[name] = row
    layout = {'old': str(old), 'new': str(repo / 'docker-compose.test'), 'root_identity': L.identity(old),
              'original_main_sha256': T.R.digest((old/'docker-compose.yml').read_bytes()),
              'bind_identities': {m['Source']: L.identity(m['Source']) for r in rows.values() for m in r['Mounts']},
              'original_branch': 'deriw-pre', 'original_head': before, 'target_head': target, 'local_dev_head': target,
              'directory_moved': False, 'running_services': sorted(rows), 'stopped_init_services': [],
              'preparation_complete': True}
    state = {'repo': str(repo), 'project': 'fixture', 'before': deepcopy(rows), 'layout': layout,
             'data_host_path': str(old / 'services/db'), 'schema_complete': True}
    journal = root / 'journal'
    journal.mkdir()
    return journal / 'state.json', state, rows, base_model


class RuntimeLayoutTests(unittest.TestCase):
    def repair_fixture(self, state, rows, base):
        model = {'services': {}, 'networks': base['networks']}
        for name, row in rows.items():
            service = deepcopy(base['services']['backend'])
            service['image'] = row['Image']
            service['volumes'][0]['source'] = row['Mounts'][0]['Source']
            model['services'][name] = service
        verifier = deepcopy(model['services']['db'])
        verifier['environment'][L.VYPER_LIST_KEY] = 'https://raw.githubusercontent.com/blockscout/solc-bin/main/vyper.list.json'
        verifier['entrypoint'] = []
        model['services']['sc-verifier'] = verifier
        state['before']['sc-verifier'] = deepcopy(rows['db'])
        state['before']['sc-verifier']['Config']['Entrypoint'] = None
        return model

    def test_startup_repair_is_limited_to_vyper_list_and_entrypoint_representation(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, state, rows, base = state_fixture(Path(temporary))
            model = self.repair_fixture(state, rows, base)
            before = deepcopy(model)
            repaired = L.startup_repair_model(model, state['before'])
            self.assertEqual(model, before)
            expected = deepcopy(model)
            expected['services']['sc-verifier']['environment'][L.VYPER_LIST_KEY] = L.VYPER_LIST
            expected['services']['sc-verifier']['entrypoint'] = None
            self.assertEqual(repaired, expected)
            model['services']['sc-verifier']['environment'][L.VYPER_LIST_KEY] = 'https://custom.invalid/list.json'
            with self.assertRaises(T.P.PreflightError): L.startup_repair_model(model, state['before'])

    def test_startup_repair_preserves_original_snapshot_and_resumes_atomic_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, state, rows, base = state_fixture(Path(temporary))
            model = self.repair_fixture(state, rows, base)
            original = path.parent / 'relocated.compose.json'
            original.write_bytes(T.R.snapshot_bytes(model))
            digest = T.R.digest(original.read_bytes())
            state.update(format=L.FORMAT, relocated_sha256=digest)
            state['layout']['directory_moved'] = True
            os.rename(state['layout']['old'], state['layout']['new'])
            target = Path(state['layout']['new']) / 'docker-compose.yml'
            target.write_bytes(original.read_bytes())
            resolved = L.startup_repair_model(model, state['before'])
            with patch.object(T.D, 'compose_model', return_value=resolved), contextlib.redirect_stdout(io.StringIO()):
                L.prepare_startup_repair(path, state)
                # Simulate interruption before replacing the active Compose.
                L.prepare_startup_repair(path, state)
                L.write_config(path, state)
                L.prepare_startup_repair(path, state)
                L.write_config(path, state)
            self.assertEqual(T.R.digest(original.read_bytes()), digest)
            self.assertEqual(state['relocated_sha256'], digest)
            self.assertEqual(target.read_bytes(), T.R.snapshot_bytes(resolved))
            self.assertEqual(state['startup_repair']['sha256'], T.R.digest(target.read_bytes()))
            target.write_text('operator edit')
            with self.assertRaises(T.P.PreflightError): L.prepare_startup_repair(path, state)
            self.assertEqual(target.read_text(), 'operator edit')

    def test_unassigned_restarting_service_is_held_until_static_addresses_are_occupied(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, state, rows, base = state_fixture(Path(temporary))
            model = {'services': {}, 'networks': base['networks']}
            for name, row in rows.items():
                model['services'][name] = {'restart': 'always', 'networks': {'default': {
                    'ipv4_address': row['NetworkSettings']['Networks']['fixture_default']['IPAddress']}}}
                row['State']['Running'] = True
            rows['user-ops-indexer'] = deepcopy(rows['db'])
            rows['user-ops-indexer']['Id'] = '9' * 64
            rows['user-ops-indexer']['Config']['Labels'][L.LABEL] = str(path)
            rows['user-ops-indexer']['HostConfig']['RestartPolicy']['Name'] = 'no'
            state['before']['user-ops-indexer'] = deepcopy(rows['user-ops-indexer'])
            state['layout']['running_services'].append('user-ops-indexer')
            model['services']['user-ops-indexer'] = {'restart': 'always', 'networks': {'default': {}}}
            with patch.object(T, 'current', return_value=rows), patch.object(T, 'change') as change, \
                 patch.object(L, 'up') as up, contextlib.redirect_stdout(io.StringIO()):
                dynamic = L.hold_dynamic_services(path, state, model)
                self.assertEqual(dynamic, {'user-ops-indexer'})
                held = [c.args[0] for c in change.call_args_list]
                self.assertEqual(held[0], ['docker', 'update', '--restart=no', '9' * 64])
                self.assertEqual(held[1], ['docker', 'stop', '-t', '30', '9' * 64])
                L.restore_dynamic_services(path, state, Path('compose'), dynamic, model)
                up.assert_called_once_with(path, state, Path('compose'), dynamic)
                self.assertEqual(change.call_args.args[0], ['docker', 'update', '--restart=always', '9' * 64])

    def test_all_bind_sources_move_but_credentials_commands_and_images_survive(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, state, rows, base = state_fixture(Path(temporary))
            model = {'services': {}, 'networks': base['networks']}
            for name, row in rows.items():
                service = deepcopy(base['services']['backend'])
                service['volumes'][0]['source'] = row['Mounts'][0]['Source']
                service['image'] = 'new-backend' if name == 'backend' else 'mutable:latest'
                model['services'][name] = service
            before = deepcopy(model)
            result = L.relocated_model(model, rows, path, state['layout'])
            self.assertEqual(model, before)
            for name, service in result['services'].items():
                self.assertTrue(service['volumes'][0]['source'].startswith(state['layout']['new'] + '/'))
                self.assertFalse(service['volumes'][0]['bind']['create_host_path'])
                self.assertEqual(service['networks']['default']['ipv4_address'], rows[name]['NetworkSettings']['Networks']['fixture_default']['IPAddress'])
                self.assertEqual(service['environment']['PRIVATE'], 'literal$with$$pair\nand newline')
                if name != 'backend':
                    self.assertEqual(service['image'], rows[name]['Image'])
                    self.assertEqual(service['command'], ['fixture-command'])

    def test_directory_rename_and_branch_switch_preserve_private_files_and_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, state, rows, _ = state_fixture(Path(temporary))
            layout = state['layout']
            old, new = Path(layout['old']), Path(layout['new'])
            with patch.object(T, 'current', return_value=rows), patch.object(T, 'change'), \
                 patch.object(L, 'outside_users', return_value=[]), contextlib.redirect_stdout(io.StringIO()):
                L.move_directory(path, state)
            self.assertFalse(old.exists())
            self.assertEqual(L.identity(new), layout['root_identity'])
            self.assertEqual((new / 'docker-compose.yml').read_text(), 'server private $configuration\n')
            with contextlib.redirect_stdout(io.StringIO()): L.switch_branch(path, state)
            repo = Path(state['repo'])
            self.assertEqual(git(repo, 'branch', '--show-current'), 'deriw-dev')
            self.assertEqual(git(repo, 'rev-parse', 'HEAD'), layout['target_head'])
            self.assertEqual(git(repo, 'rev-parse', 'deriw-pre'), layout['original_head'])
            self.assertEqual((old / 'docker-compose.yml').read_text(), 'public dev defaults')
            self.assertEqual((new / 'services/db/sentinel').read_text(), 'preserve-db')
            self.assertFalse((old / 'services/db').exists())
            self.assertEqual(git(repo, 'status', '--porcelain'), '')

    def test_failed_database_stop_or_foreign_attachment_prevents_rename(self):
        for cause in ('database', 'foreign'):
            with self.subTest(cause=cause), tempfile.TemporaryDirectory() as temporary:
                path, state, rows, _ = state_fixture(Path(temporary))
                if cause == 'database': rows['db']['State']['ExitCode'] = 137
                foreign = [{'name': '/foreign'}] if cause == 'foreign' else []
                with patch.object(T, 'current', return_value=rows), patch.object(T, 'change'), \
                     patch.object(L, 'outside_users', return_value=foreign), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(T.P.PreflightError): L.move_directory(path, state)
                self.assertTrue(Path(state['layout']['old']).is_dir())
                self.assertFalse(Path(state['layout']['new']).exists())

    def test_interrupted_rename_is_recognized_without_stopping_again(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, state, _, _ = state_fixture(Path(temporary))
            layout = state['layout']
            os.rename(layout['old'], layout['new'])
            with patch.object(T, 'current') as current, patch.object(T, 'change') as change, \
                 contextlib.redirect_stdout(io.StringIO()):
                L.move_directory(path, state)
            current.assert_not_called()
            change.assert_not_called()
            self.assertTrue(layout['directory_moved'])
            self.assertEqual(state['data_host_path'], layout['new'] + '/services/db')

    def test_resume_after_branch_selected_but_before_fast_forward(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, state, _, _ = state_fixture(Path(temporary))
            repo, layout = Path(state['repo']), state['layout']
            git(repo, 'update-ref', 'refs/heads/deriw-dev', layout['original_head'])
            layout['local_dev_head'] = layout['original_head']
            os.rename(layout['old'], layout['new'])
            Path(layout['new']).chmod(0o700)
            git(repo, 'restore', '--source=HEAD', '--worktree', '--', 'docker-compose')
            git(repo, 'switch', '-q', 'deriw-dev')
            with contextlib.redirect_stdout(io.StringIO()): L.switch_branch(path, state)
            self.assertEqual(git(repo, 'rev-parse', 'HEAD'), layout['target_head'])
            self.assertEqual((Path(layout['new'])/'services/db/sentinel').read_text(), 'preserve-db')
            self.assertEqual(git(repo, 'status', '--porcelain'), '')

    def test_existing_destination_blocks_apply_before_schema_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, state, _, _ = state_fixture(Path(temporary))
            Path(state['layout']['new']).mkdir()
            state.update(format=L.FORMAT, preparation_complete=True, image_tag='fixture:v10', image_id='sha256:fixture')
            image = {'Id':state['image_id'],'Config':{'Labels':{'org.opencontainers.image.revision':T.SOURCE}}}
            with patch.object(T.P,'run',return_value=json.dumps([image])), patch.object(L,'schema') as schema, \
                 patch.object(T,'current') as current:
                with self.assertRaises(T.P.PreflightError): L.apply(path,state)
            schema.assert_not_called()
            current.assert_not_called()

    def test_resume_preserves_later_runtime_configuration_edits(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, state, _, _ = state_fixture(Path(temporary))
            layout = state['layout']
            os.rename(layout['old'], layout['new'])
            selected = path.parent/'relocated.compose.json'
            selected.write_bytes(b'{"services":{"fixture":{}}}\n')
            state['relocated_sha256'] = T.R.digest(selected.read_bytes())
            target = L.write_config(path,state)
            self.assertEqual(target.read_bytes(),selected.read_bytes())
            self.assertEqual((path.parent/'moved-original-compose.private.yml').read_text(), 'server private $configuration\n')
            L.write_config(path,state)
            target.write_text('operator edit to preserve')
            with self.assertRaises(T.P.PreflightError): L.write_config(path,state)
            self.assertEqual(target.read_text(),'operator edit to preserve')

    def test_failed_handoff_prevents_any_stop_or_schema_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, state, rows, _ = state_fixture(Path(temporary))
            state['schema_complete'] = False
            rows['backend']['State']['Running'] = True
            with patch.object(T, 'rpc', side_effect=T.P.PreflightError('handoff unavailable')), \
                 patch.object(T, 'change') as change, patch.object(T, 'client') as client:
                with self.assertRaises(T.P.PreflightError): L.schema(path, state, rows)
            change.assert_not_called()
            client.assert_not_called()

    def test_diverged_dev_branch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, state, _, _ = state_fixture(Path(temporary))
            repo, layout = Path(state['repo']), state['layout']
            divergent = git(repo, 'commit-tree', layout['target_head'] + '^{tree}',
                            '-p', layout['target_head'], '-m', 'unpublished local commit')
            git(repo, 'update-ref', 'refs/heads/deriw-dev', divergent)
            with patch.object(T, 'SOURCE', layout['original_head']):
                with self.assertRaises(T.P.PreflightError): L.check_git(repo, layout['target_head'])
            self.assertTrue(Path(layout['old']).exists())
            self.assertFalse(Path(layout['new']).exists())

    def test_public_entrypoint_uses_the_relocation_workflow(self):
        with patch.object(L, 'main', return_value=0) as main:
            self.assertEqual(T.main(['--prepare-only']), 0)
        main.assert_called_once_with(['--prepare-only'])

    def test_nested_host_mounts_are_detected_before_renaming_the_parent(self):
        raw = '1 0 0:1 / /data rw - xfs /dev/fixture rw\n'
        raw += '2 1 0:2 / /data/blockscout/docker-compose/services/db rw - xfs /dev/fixture2 rw\n'
        self.assertEqual(L.mounted_children(Path('/data/blockscout/docker-compose'), raw),
                         ['/data/blockscout/docker-compose/services/db'])


if __name__ == '__main__':
    unittest.main()
