from copy import deepcopy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('containerd_maintenance', ROOT / 'deploy/scripts/containerd_maintenance.py')
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def rows():
    result = {}
    for index, name in enumerate((*M.DATABASES, *M.CLIENTS, 'db-init', 'stats-db-init'), 1):
        running = not name.endswith('-init')
        result[name] = {
            'Id': format(index, '064x'), 'Image': 'sha256:' + 'f' * 64,
            'Config': {'Cmd': ['bin/blockscout', 'start'] if name == 'backend' else ['service'],
                       'Env': ['PASSWORD=private-fixture-value']},
            'HostConfig': {'RestartPolicy': {'Name': 'always' if running else 'no', 'MaximumRetryCount': 0},
                           'NetworkMode': 'dev-network', 'PortBindings': {}},
            'Mounts': [{'Source': '/data/' + name, 'Destination': '/data', 'RW': True}],
            'NetworkSettings': {'Networks': {'dev-network': {'Aliases': [name], 'IPAMConfig': None,
                                                           'IPAddress': '172.20.0.' + str(index)}}},
            'State': {'Running': running, 'Paused': False, 'OOMKilled': False, 'ExitCode': 0,
                      'StartedAt': '2026-11-03T09:27:18.000000000Z'},
        }
    return result


class PreservationTests(unittest.TestCase):
    def test_restart_policies_round_trip_without_losing_retry_limit(self):
        row = rows()['backend']
        row['HostConfig']['RestartPolicy'] = {'Name': 'on-failure', 'MaximumRetryCount': 7}
        self.assertEqual(M.policy(row), 'on-failure:7')
        for value in ({'Name': 'other', 'MaximumRetryCount': 0}, {'Name': 'on-failure', 'MaximumRetryCount': -1}):
            row['HostConfig']['RestartPolicy'] = value
            with self.assertRaises(M.S.PlanError):
                M.policy(row)

    def test_restarts_and_mount_order_can_change_without_hiding_config_drift(self):
        before = rows()
        after = deepcopy(before)
        after['backend']['State']['StartedAt'] = 'later'
        M.validate_containers(before, after, policies='original')
        after['backend']['Config']['Env'] = ['PASSWORD=changed']
        with self.assertRaisesRegex(M.S.PlanError, 'configuration_changed'):
            M.validate_containers(before, after)

    def test_mount_host_image_and_network_changes_are_rejected(self):
        before = rows()
        changes = [
            lambda d: d['backend'].update(Image='sha256:' + 'a' * 64),
            lambda d: d['db']['Mounts'][0].update(Source='/another/database'),
            lambda d: d['proxy']['HostConfig'].update(PortBindings={'80': 'other'}),
            lambda d: d['db']['NetworkSettings']['Networks']['dev-network'].update(Aliases=['other']),
        ]
        for change in changes:
            after = deepcopy(before)
            change(after)
            with self.assertRaises(M.S.PlanError):
                M.validate_containers(before, after)

    def test_static_ip_is_preserved_while_dynamic_ip_changes_are_tolerated(self):
        before = rows()
        after = deepcopy(before)
        after['db']['NetworkSettings']['Networks']['dev-network']['IPAddress'] = '172.20.0.250'
        M.validate_containers(before, after)
        before['db']['NetworkSettings']['Networks']['dev-network']['IPAMConfig'] = {'IPv4Address': '172.20.0.1'}
        after['db']['NetworkSettings']['Networks']['dev-network']['IPAMConfig'] = {'IPv4Address': '172.20.0.1'}
        with self.assertRaisesRegex(M.S.PlanError, 'explicit_ipv4'):
            M.validate_containers(before, after)

    def test_delete_is_only_present_in_checksum_dry_run(self):
        copy = M.rsync_command()
        verify = M.rsync_command(verify=True)
        self.assertNotIn('--delete', copy)
        self.assertNotIn('--dry-run', copy)
        self.assertIn('--delete', verify)
        self.assertIn('--dry-run', verify)
        self.assertIn('--checksum', verify)
        self.assertIn('-aHAXSx', copy)

    def test_copy_capability_check_rejects_disabled_metadata_features(self):
        M.rsync_capable('rsync version 3.2.7 protocol version 31\nCapabilities: hardlinks ACLs xattrs')
        for feature in ('ACLs', 'xattrs', 'hardlinks'):
            with self.assertRaises(M.S.PlanError):
                M.rsync_capable('rsync version 3.2.7\nhardlinks ACLs xattrs no ' + feature)

    def test_remaining_mounts_are_rejected_by_path_boundary(self):
        M.no_store_mounts('40 20 0:1 / /var/lib/containerd-other rw - ext4 root rw\n')
        for point in ('/var/lib/containerd', '/var/lib/containerd/snapshot/mount', '/data/containerd/fs'):
            with self.assertRaisesRegex(M.S.PlanError, 'remaining_mounts'):
                M.no_store_mounts('40 20 0:1 / ' + point + ' rw - overlay overlay rw\n')

    def test_atomic_journal_is_private_and_replaces_complete_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'journal'
            M.write_atomic(path, b'first')
            M.write_atomic(path, b'second')
            self.assertEqual(path.read_bytes(), b'second')
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_host_lock_also_blocks_an_operation_from_a_different_plan(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(M.S, 'PRIVATE', Path(directory)):
            with M.global_lock(True):
                with self.assertRaisesRegex(M.S.PlanError, 'another_host_storage_operation'):
                    with M.global_lock(True):
                        self.fail('second operation acquired the host lock')

    def test_orphan_copy_is_detected_from_selected_rsync_paths(self):
        with patch.object(M, 'process_ids', return_value=[123]), \
             patch.object(M.Path, 'read_bytes', return_value=b'rsync\0--server\0.\0/data/containerd/\0'):
            self.assertTrue(M.active_store_copy())
        with patch.object(M, 'process_ids', return_value=[123]), \
             patch.object(M.Path, 'read_bytes', return_value=b'rsync\0/unrelated/\0/elsewhere/\0'):
            self.assertFalse(M.active_store_copy())


class PlanFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.private = self.base / 'private'
        self.directory = self.private / 'plan-fixture1'
        self.private.mkdir(mode=0o700)
        self.directory.mkdir(mode=0o700)
        self.path = self.directory / 'state.json'
        self.patcher = patch.object(M.S, 'PRIVATE', self.private)
        self.patcher.start()
        self.data = {
            'format': 'blockscout-containerd-maintenance-plan-v1', 'config_existed': True,
            'config_sha256': M.S.digest(b'before'), 'candidate_sha256': M.S.digest(b'after'),
            'config_metadata': {'uid': 0, 'gid': 0, 'mode': 0o644}, 'before': rows(),
            'public_plan': {'source': str(M.S.SOURCE), 'target': str(M.S.TARGET),
                            'runtime_state_preserved': '/run/containerd',
                            'running_services': sorted(name for name, row in rows().items() if row['State']['Running'])},
        }
        M.write_atomic(self.directory / 'containerd.before.toml', b'before')
        M.write_atomic(self.directory / 'containerd.after.toml', b'after')
        self.write()

    def write(self):
        M.write_atomic(self.path, M.encoded(self.data))

    def tearDown(self):
        self.patcher.stop()
        self.temp.cleanup()

    def test_saved_plan_and_snapshot_hashes_are_accepted(self):
        plan, before, after, identity = M.load_plan(self.path)
        self.assertEqual(before, b'before')
        self.assertEqual(after, b'after')
        self.assertEqual(identity, M.S.digest(self.path.read_bytes()))
        self.assertEqual(len(plan['before']), 14)

    def test_changed_candidate_or_original_is_rejected(self):
        for name in ('containerd.before.toml', 'containerd.after.toml'):
            path = self.directory / name
            original = path.read_bytes()
            M.write_atomic(path, b'changed')
            with self.assertRaisesRegex(M.S.PlanError, 'saved_configuration_changed'):
                M.load_plan(self.path)
            M.write_atomic(path, original)

    def test_public_file_permissions_or_redirected_plan_are_rejected(self):
        self.path.chmod(0o644)
        with self.assertRaisesRegex(M.S.PlanError, 'private_file_permissions'):
            M.load_plan(self.path)
        self.path.chmod(0o600)
        alias = self.directory / 'alias.json'
        alias.symlink_to(self.path)
        with self.assertRaises(M.S.PlanError):
            M.load_plan(alias)

    def test_legacy_backend_migration_startup_and_unexpected_services_are_rejected(self):
        self.data['before']['backend']['Config']['Cmd'] = ['sh', '-c', 'create_and_migrate && start']
        self.write()
        with self.assertRaisesRegex(M.S.PlanError, 'saved_backend_startup'):
            M.load_plan(self.path)
        self.data['before'] = rows()
        self.data['before']['other'] = deepcopy(self.data['before']['backend'])
        self.write()
        with self.assertRaisesRegex(M.S.PlanError, 'saved_service_inventory'):
            M.load_plan(self.path)

    def test_world_writable_saved_config_metadata_is_rejected(self):
        self.data['config_metadata']['mode'] = 0o666
        self.write()
        with self.assertRaisesRegex(M.S.PlanError, 'saved_config_ownership'):
            M.load_plan(self.path)

    def test_corrupted_or_inconsistent_execution_flags_are_rejected_before_host_reads(self):
        base = {'format': M.FORMAT, 'plan_sha256': M.S.digest(self.path.read_bytes()),
                'copy_verified': False, 'services_started_on_target': False, 'status': 'needs_attention'}
        for changes in ({'copy_verified': 'true'}, {'status': 'active'}, {'status': 'store_ready'},
                        {'copy_verified': True}):
            M.write_atomic(self.directory / 'execution.json', M.encoded({**base, **changes}))
            with self.assertRaises(M.S.PlanError):
                M.Maintenance(self.path)


class FakeMaintenance(M.Maintenance):
    """Deterministic host model; never calls Docker, systemd, signals or /proc."""
    def __init__(self, directory):
        self.directory = directory
        self.path = directory / 'state.json'
        self.journal_path = directory / 'execution.json'
        self.plan_hash = 'fixture'
        self.original = b'original-config'
        self.candidate = b'target-config'
        self.live = rows()
        self.plan = {'before': deepcopy(self.live),
                     'public_plan': {'running_services': sorted(name for name, row in self.live.items() if row['State']['Running'])},
                     'source_identity': {'device': M.S.SOURCE.stat().st_dev, 'inode': M.S.SOURCE.stat().st_ino},
                     'config_metadata': {'mode': 0o600, 'uid': os.geteuid(), 'gid': os.getegid()}}
        self.state = None
        self.units = {name: 'active' for name in M.UNITS}
        self.images = [('fixture', 'v9', '<none>', 'sha256:' + 'f' * 64)]
        self.events = []
        self.corrupt_copy = False
        self.dirty_database = False
        self.fail_target_start = False
        self.healthy = True
        self.copied = 0

    def read(self, arguments, **_options):
        if arguments[:2] == ['docker', 'version']:
            return '29.4.0'
        if arguments[:2] == ['docker', 'update']:
            value, identity = arguments[2].split('=', 1)[1], arguments[3]
            for row in self.live.values():
                if row['Id'] == identity:
                    row['HostConfig']['RestartPolicy'] = {'Name': value, 'MaximumRetryCount': 0}
            self.events.append(tuple(arguments))
            return ''
        if arguments[:3] == ['docker', 'image', 'ls']:
            return '\n'.join(json.dumps(dict(zip(('Repository', 'Tag', 'Digest', 'ID'), value))) for value in self.images)
        if arguments[:2] == ['docker', 'exec']:
            return ''
        if arguments[:2] == ['systemctl', 'daemon-reload']:
            return ''
        raise AssertionError('Unmodeled command: ' + repr(arguments))

    def rows(self):
        return deepcopy(self.live)

    def unit_state(self, name):
        return self.units[name]

    def precheck(self):
        M.require(not M.S.TARGET.exists(), 'unowned_destination_exists')
        return self.plan['public_plan'], deepcopy(self.units), list(self.images)

    def ensure_daemons(self):
        if self.fail_target_start and self.config_side() == 'target':
            self.units = {'containerd.service': 'failed', 'docker.service': 'inactive', 'docker.socket': 'inactive'}
            raise M.S.PlanError('fixture_target_daemon_failed')
        self.units = {name: 'active' for name in M.UNITS}

    def cold(self):
        M.require(all(value == 'inactive' for value in self.units.values()), 'daemons_not_fully_stopped')
        self.identities()

    def long(self, arguments, **_options):
        self.events.append(tuple(arguments))
        log = self.directory / ('log-' + str(len(self.events)))
        log.write_bytes(b'')
        if arguments[:2] == ['docker', 'stop']:
            M.require('--timeout' in arguments and arguments[arguments.index('--timeout') + 1] == '-1', 'forced_kill_forbidden')
            identities = set(arguments[arguments.index('-1') + 1:])
            for name, row in self.live.items():
                if row['Id'] in identities:
                    if name in M.DATABASES:
                        M.require('--signal' in arguments and arguments[arguments.index('--signal') + 1] == 'SIGINT', 'database_signal_incorrect')
                    row['State']['Running'] = False
                    if self.dirty_database and name == 'db':
                        row['State']['ExitCode'] = 137
        elif arguments[:2] == ['docker', 'start']:
            for row in self.live.values():
                if row['Id'] == arguments[2]:
                    row['State']['Running'] = True
                    row['State']['StartedAt'] = 'fixture-start-' + str(len(self.events))
        elif arguments[:2] == ['systemctl', 'stop']:
            for name in arguments[2:]:
                self.units[name] = 'inactive'
        elif arguments[0] == 'rsync':
            if '--dry-run' in arguments:
                if (M.S.TARGET / 'payload').read_bytes() != (M.S.SOURCE / 'payload').read_bytes():
                    log.write_bytes(b'>fcs.......\n')
            else:
                self.copied += 1
                shutil.copytree(M.S.SOURCE, M.S.TARGET, dirs_exist_ok=True)
                if self.corrupt_copy:
                    (M.S.TARGET / 'payload').write_bytes(b'corrupted')
        else:
            raise AssertionError('Unmodeled mutation: ' + repr(arguments))
        return log

    def verify_store(self):
        self.target_configuration()
        M.validate_containers(self.plan['before'], self.rows(), policies='disabled', running=set())
        self.not_started_since_copy(self.rows())
        self.events.append(('verified_target_store',))

    def target_configuration(self):
        M.require(self.config_side() == 'target' and M.DROPIN.read_bytes() == M.DROPIN_DATA,
                  'target_configuration_not_installed')

    def cleanup_probe(self):
        pass

    def health(self, wait=False):
        return {'backend_readiness_ok': self.healthy, 'nft_connection_and_queue_ok': self.healthy}

    def verify_backend_path(self, root=None):
        self.events.append(('verified_target_backend_path',))


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name).resolve()
        source, data, private = base / 'source', base / 'data', base / 'private'
        source.mkdir()
        data.mkdir()
        private.mkdir(mode=0o700)
        (source / 'payload').write_bytes(b'original-store')
        config = base / 'config.toml'
        config.write_bytes(b'original-config')
        self.patches = [patch.object(M.S, 'SOURCE', source), patch.object(M.S, 'TARGET', data / 'containerd'),
                        patch.object(M.S, 'CONFIG', config), patch.object(M, 'DROPIN', base / 'units' / 'storage.conf'),
                        patch.object(M.S, 'workload_inventory', return_value=[]),
                        patch.object(M, 'active_store_copy', return_value=False), patch('sys.stdout', io.StringIO())]
        for value in self.patches:
            value.start()
        self.host = FakeMaintenance(private)

    def tearDown(self):
        for value in reversed(self.patches):
            value.stop()
        self.temp.cleanup()

    def test_apply_copies_verifies_and_leaves_services_stopped_then_resume_restores_exact_set(self):
        result = self.host.apply()
        self.assertEqual(result['status'], 'store_ready')
        self.assertTrue(result['copy_verified'])
        self.assertFalse(any(row['State']['Running'] for row in self.host.live.values()))
        self.assertEqual((M.S.SOURCE / 'payload').read_bytes(), b'original-store')
        self.assertEqual((M.S.TARGET / 'payload').read_bytes(), b'original-store')
        stops = [event for event in self.host.events if event[:2] == ('docker', 'stop')]
        database_ids = {self.host.plan['before'][name]['Id'] for name in M.DATABASES}
        self.assertTrue(database_ids.isdisjoint(stops[0]))
        self.assertTrue(all('--signal' in event for event in stops[1:]))
        self.host.resume()
        M.validate_containers(self.host.plan['before'], self.host.rows(), policies='original',
                              running=set(self.host.plan['public_plan']['running_services']))
        self.assertEqual(self.host.state['status'], 'active')
        starts = [event[2] for event in self.host.events if event[:2] == ('docker', 'start')]
        self.assertEqual(starts[:2], [self.host.plan['before'][name]['Id'] for name in M.DATABASES])
        self.assertNotIn(self.host.plan['before']['db-init']['Id'], starts)
        self.assertNotIn(self.host.plan['before']['stats-db-init']['Id'], starts)

    def test_copy_corruption_never_installs_candidate_and_abort_restores_services(self):
        self.host.corrupt_copy = True
        with self.assertRaisesRegex(M.S.PlanError, 'cold_copy_verification'):
            self.host.apply()
        self.assertEqual(M.S.CONFIG.read_bytes(), b'original-config')
        self.assertFalse(self.host.state['copy_verified'])
        self.host.abort()
        self.assertEqual(self.host.state['status'], 'aborted')
        self.assertEqual((M.S.SOURCE / 'payload').read_bytes(), b'original-store')
        self.assertEqual((M.S.TARGET / 'payload').read_bytes(), b'corrupted')

    def test_unclean_database_exit_stops_before_daemon_stop_or_copy(self):
        self.host.dirty_database = True
        with self.assertRaisesRegex(M.S.PlanError, 'database_clean_shutdown'):
            self.host.apply()
        self.assertFalse(M.S.TARGET.exists())
        self.assertEqual(self.host.units['containerd.service'], 'active')
        self.assertEqual(M.S.CONFIG.read_bytes(), b'original-config')

    def test_abort_of_verified_store_restores_original_config_and_policies(self):
        self.host.apply()
        self.host.abort()
        self.assertEqual(M.S.CONFIG.read_bytes(), b'original-config')
        self.assertFalse(M.DROPIN.exists())
        M.validate_containers(self.host.plan['before'], self.host.rows(), policies='original',
                              running=set(self.host.plan['public_plan']['running_services']))

    def test_failed_target_daemon_can_be_aborted_without_starting_it_again(self):
        self.host.fail_target_start = True
        with self.assertRaisesRegex(M.S.PlanError, 'fixture_target_daemon_failed'):
            self.host.apply()
        self.host.abort()
        self.assertEqual(self.host.state['status'], 'aborted')
        self.assertEqual(self.host.config_side(), 'source')

    def test_after_resume_started_abort_is_refused_and_retry_never_recopies_old_store(self):
        self.host.apply()
        self.host.healthy = False
        with self.assertRaisesRegex(M.S.PlanError, 'readiness_needs_attention'):
            self.host.resume()
        (M.S.TARGET / 'payload').write_bytes(b'newer-writes')
        with self.assertRaisesRegex(M.S.PlanError, 'abort_unavailable_after_services_started'):
            self.host.abort()
        self.host.healthy = True
        self.host.resume()
        self.assertEqual((M.S.TARGET / 'payload').read_bytes(), b'newer-writes')
        self.assertEqual(self.host.copied, 1)

    def test_unexpected_target_service_start_blocks_abort(self):
        self.host.apply()
        self.host.live['backend']['State']['Running'] = True
        with self.assertRaisesRegex(M.S.PlanError, 'target_services_started_outside_executor'):
            self.host.abort()
        self.assertEqual(self.host.config_side(), 'target')

    def test_external_config_edit_is_not_overwritten(self):
        self.host.apply()
        M.S.CONFIG.write_bytes(b'external-config')
        with self.assertRaisesRegex(M.S.PlanError, 'installed_config_changed_outside'):
            self.host.abort()
        self.assertEqual(M.S.CONFIG.read_bytes(), b'external-config')

    def test_manually_started_then_stopped_target_container_blocks_abort(self):
        self.host.apply()
        self.host.live['backend']['State']['StartedAt'] = 'another-start'
        with self.assertRaisesRegex(M.S.PlanError, 'containers_started_after_cold_copy'):
            self.host.abort()
        self.assertEqual(self.host.config_side(), 'target')

    def test_target_directory_replacement_is_not_adopted(self):
        self.host.apply()
        M.S.TARGET.rename(M.S.TARGET.with_name('preserved'))
        M.S.TARGET.mkdir()
        with self.assertRaisesRegex(M.S.PlanError, 'target_identity_changed'):
            self.host.resume()

    def test_aborted_execution_does_not_repeat_the_outage(self):
        self.host.apply()
        self.host.abort()
        events = len(self.host.events)
        self.host.abort()
        self.assertEqual(len(self.host.events), events)

    def test_status_distinguishes_recorded_success_from_live_readiness(self):
        self.host.apply()
        self.assertEqual(self.host.status()['status'], 'verified')
        self.host.resume()
        self.assertEqual(self.host.status()['recorded_status'], 'active')
        self.assertEqual(self.host.status()['status'], 'verified')
        self.host.healthy = False
        self.assertEqual(self.host.status()['status'], 'needs_attention')
        self.assertEqual(self.host.state['status'], 'active')

    def test_status_reports_partial_resume_without_assuming_all_services_stopped(self):
        self.host.apply()
        self.host.state['status'] = 'needs_attention'
        self.host.state['services_started_on_target'] = True
        self.host.live['db']['State']['Running'] = True
        result = self.host.status()
        self.assertEqual(result['status'], 'needs_attention')
        self.assertEqual(result['observed_running_services'], ['db'])


if __name__ == '__main__':
    unittest.main()
