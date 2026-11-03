#!/usr/bin/env python3
"""Move the reviewed dev image store in stages, retaining the original store.

--check reads only. --apply stops services and prepares the new store, leaving
applications stopped. --resume starts the recorded services. --abort is only
available before any recorded service has been started on the new store.
"""
import argparse
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
DROPIN = Path('/etc/systemd/system/containerd.service.d/90-blockscout-storage.conf')
DROPIN_DATA = b'[Unit]\nRequiresMountsFor=/data/containerd\n'
DATABASES = ('db', 'stats-db')
CLIENTS = ('redis-db', 'sc-verifier', 'sig-provider', 'backend', 'nft_media_handler',
           'stats', 'user-ops-indexer', 'frontend', 'visualizer', 'proxy')
UNITS = ('containerd.service', 'docker.service', 'docker.socket')
FORMAT = 'blockscout-containerd-execution-v1'
PHASE = 'load_plan'


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / ('deploy/scripts/' + name + '.py'))
    value = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = value
    spec.loader.exec_module(value)
    return value


S = module('containerd_storage')


def require(condition, code):
    S.require(condition, code)


def encoded(value):
    return (json.dumps(value, indent=2, sort_keys=True) + '\n').encode()


def private_file(path):
    require(path.is_file() and not path.is_symlink(), 'private_file_unavailable')
    info = path.stat()
    require(stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == os.geteuid(), 'private_file_permissions')
    require(info.st_size <= S.MAX_BYTES, 'private_file_exceeded_limit')
    return path.read_bytes()


def write_atomic(path, data, mode=0o600, uid=None, gid=None):
    temporary = path.parent / ('.storage-' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            if uid is not None or gid is not None:
                os.fchown(stream.fileno(), uid if uid is not None else -1, gid if gid is not None else -1)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_plan(path):
    require(path.is_absolute() and path.name == 'state.json' and path.resolve() == path,
            'expected_original_private_plan_path')
    require(path.parent.parent == S.PRIVATE and bool(re.fullmatch(r'plan-[a-z0-9_-]{6,40}', path.parent.name)),
            'unexpected_private_plan_directory')
    require(stat.S_IMODE(S.PRIVATE.stat().st_mode) == 0o700 and S.PRIVATE.stat().st_uid == os.geteuid(),
            'private_storage_directory_permissions')
    require(stat.S_IMODE(path.parent.stat().st_mode) == 0o700 and path.parent.stat().st_uid == os.geteuid(),
            'private_plan_directory_permissions')
    raw = private_file(path)
    plan = json.loads(raw)
    public = plan['public_plan']
    require(plan['format'] == 'blockscout-containerd-maintenance-plan-v1'
            and public['source'] == str(S.SOURCE) and public['target'] == str(S.TARGET)
            and public['runtime_state_preserved'] == '/run/containerd', 'unexpected_storage_plan')
    require(type(plan['config_existed']) is bool, 'invalid_saved_config_existence')
    before = private_file(path.parent / 'containerd.before.toml') if plan['config_existed'] else None
    after = private_file(path.parent / 'containerd.after.toml')
    require((S.digest(before) if before is not None else None) == plan['config_sha256']
            and S.digest(after) == plan['candidate_sha256'], 'saved_configuration_changed')
    if plan['config_existed']:
        meta = plan['config_metadata']
        require(meta['uid'] == 0 and type(meta['gid']) is int and meta['gid'] >= 0
                and type(meta['mode']) is int and 0 <= meta['mode'] <= 0o777 and not meta['mode'] & 0o022,
                'saved_config_ownership_requires_review')
    rows = plan['before']
    require(set(rows) == set(CLIENTS) | set(DATABASES) | {'db-init', 'stats-db-init'}, 'saved_service_inventory_invalid')
    for row in rows.values():
        require(bool(re.fullmatch(r'[a-f0-9]{64}', row.get('Id', '')))
                and bool(re.fullmatch(r'sha256:[a-f0-9]{64}', row.get('Image', ''))), 'saved_container_identity_invalid')
    require(sorted(name for name, row in rows.items() if row['State']['Running']) == public['running_services'],
            'saved_running_service_inventory_invalid')
    require(all(rows[name]['State']['Running'] for name in DATABASES), 'databases_not_in_saved_running_set')
    require(not rows['db-init']['State']['Running'] and not rows['stats-db-init']['State']['Running'],
            'initialization_containers_must_be_stopped')
    require(rows['backend']['Config'].get('Cmd') == ['bin/blockscout', 'start'], 'saved_backend_startup_requires_review')
    return plan, before, after, S.digest(raw)


def policy(row):
    value = row['HostConfig']['RestartPolicy']
    name, retries = value.get('Name'), value.get('MaximumRetryCount', 0)
    require(name in ('', 'no', 'always', 'unless-stopped', 'on-failure')
            and type(retries) is int and retries >= 0, 'unexpected_restart_policy')
    return ('on-failure:' + str(retries)) if name == 'on-failure' and retries else (name or 'no')


@contextmanager
def global_lock(mutation):
    path = S.PRIVATE / 'operation.lock'
    if not mutation and not path.exists():
        yield
        return
    fd = os.open(path, (os.O_RDWR | os.O_CREAT if mutation else os.O_RDONLY) | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o600, 'global_lock_permissions')
        try:
            fcntl.flock(stream, (fcntl.LOCK_EX if mutation else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise S.PlanError('another_host_storage_operation_is_running') from None
        yield


def network_definition(row):
    return {name: {'Aliases': sorted(value.get('Aliases') or []), 'IPAMConfig': value.get('IPAMConfig'),
                   'Links': sorted(value.get('Links') or []), 'DriverOpts': value.get('DriverOpts')}
            for name, value in (row.get('NetworkSettings', {}).get('Networks') or {}).items()}


def host_definition(row):
    values = dict(row['HostConfig'])
    # Docker's readHostConfig calls InitDNSHostConfig on daemon startup,
    # replacing nil DNS slices with empty slices. Normalize only these three
    # null values; preserve missing keys, nonempty lists and every other field.
    for key in ('Dns', 'DnsOptions', 'DnsSearch'):
        if key in values and values[key] is None:
            values[key] = []
    return values


def validate_containers(before, after, policies='either', running=None):
    require(set(before) == set(after), 'container_service_set_changed')
    for name, old in before.items():
        new = after[name]
        require(old['Id'] == new['Id'] and old['Image'] == new['Image'] and old['Config'] == new['Config'],
                'container_identity_image_or_configuration_changed')
        require(sorted_mounts(old) == sorted_mounts(new), 'container_mounts_changed')
        a, b = host_definition(old), host_definition(new)
        saved_policy, actual_policy = a.pop('RestartPolicy'), b.pop('RestartPolicy')
        require(a == b and network_definition(old) == network_definition(new), 'container_host_or_network_definition_changed')
        disabled = {'Name': 'no', 'MaximumRetryCount': 0}
        allowed = [saved_policy] if policies == 'original' else [disabled] if policies == 'disabled' else [saved_policy, disabled]
        require(actual_policy in allowed, 'container_restart_policy_changed')
        for network, values in (old.get('NetworkSettings', {}).get('Networks') or {}).items():
            assigned = new['NetworkSettings']['Networks'][network]
            explicit = values.get('IPAMConfig') or {}
            require(not explicit.get('IPv4Address') or assigned.get('IPAddress') == values.get('IPAddress'),
                    'explicit_ipv4_assignment_changed')
            require(not explicit.get('IPv6Address') or assigned.get('GlobalIPv6Address') == values.get('GlobalIPv6Address'),
                    'explicit_ipv6_assignment_changed')
        if running is not None:
            require(new['State']['Running'] == (name in running), 'container_running_set_changed')
        require(not new['State'].get('Paused'), 'paused_container_requires_review')


def sorted_mounts(row):
    return sorted(row.get('Mounts') or [], key=lambda item: json.dumps(item, sort_keys=True))


def image_bindings(read):
    values = read(['docker', 'image', 'ls', '--all', '--digests', '--no-trunc', '--format', '{{json .}}'])
    rows = [json.loads(line) for line in values.splitlines() if line.strip()]
    return sorted(set(tuple(row[key] for key in ('Repository', 'Tag', 'Digest', 'ID')) for row in rows))


def rsync_command(verify=False):
    arguments = ['rsync', '-aHAXSx', '--numeric-ids']
    if verify:
        arguments += ['--dry-run', '--checksum', '--delete', '--itemize-changes', '--out-format=%i']
    return arguments + [str(S.SOURCE) + '/', str(S.TARGET) + '/']


def rsync_capable(text):
    require(bool(re.search(r'\brsync\s+version\s+3\.', text)), 'rsync_3_required')
    for feature in ('ACLs', 'xattrs', 'hardlinks'):
        require(feature in text and not re.search(r'\bno\s+' + re.escape(feature) + r'\b', text),
                'rsync_must_support_acls_xattrs_and_hardlinks')


def no_store_mounts(mountinfo):
    for line in mountinfo.splitlines():
        fields = line.split(' - ', 1)[0].split()
        if len(fields) < 5:
            continue
        mount = fields[4]
        require(not any(mount == str(path) or mount.startswith(str(path) + '/') for path in (S.SOURCE, S.TARGET)),
                'store_has_remaining_mounts')


def process_ids(prefix, exact=False):
    result = []
    for process in Path('/proc').iterdir():
        if process.name.isdigit():
            try:
                name = process.joinpath('comm').read_text().strip()
                matches = name == prefix if exact else name.startswith(prefix)
                if matches:
                    result.append(int(process.name))
            except FileNotFoundError:
                pass
    return result


def active_store_copy():
    paths = {str(S.SOURCE), str(S.TARGET)}
    for identity in process_ids('rsync'):
        try:
            arguments = Path('/proc', str(identity), 'cmdline').read_bytes().decode().rstrip('\x00').split('\x00')
            if any(value.rstrip('/') in paths for value in arguments):
                return True
        except FileNotFoundError:
            pass
    return False


def interrupted(_signal, _frame):
    raise S.PlanError('maintenance_interrupted')


class Maintenance:
    def __init__(self, path):
        self.path = path
        self.plan, self.original, self.candidate, self.plan_hash = load_plan(path)
        self.directory = path.parent
        self.journal_path = self.directory / 'execution.json'
        self.state = json.loads(private_file(self.journal_path)) if self.journal_path.exists() else None
        if self.state is not None:
            require(self.state['format'] == FORMAT and self.state['plan_sha256'] == self.plan_hash,
                    'execution_journal_does_not_match_plan')
            require(type(self.state.get('copy_verified')) is bool and type(self.state.get('services_started_on_target')) is bool
                    and self.state.get('status') in ('in_progress', 'needs_attention', 'store_ready', 'active', 'aborted'),
                    'unrecognized_execution_state')
            require(not self.state['services_started_on_target'] or self.state['copy_verified'], 'unrecognized_execution_state')
            require(self.state['status'] != 'active' or self.state['services_started_on_target'], 'unrecognized_active_state')
            require(self.state['status'] != 'store_ready'
                    or (self.state['copy_verified'] and not self.state['services_started_on_target']), 'unrecognized_ready_state')
            require(self.state['status'] != 'aborted' or not self.state['services_started_on_target'], 'unrecognized_aborted_state')
            if self.state['copy_verified']:
                require(isinstance(self.state.get('target_identity'), dict)
                        and set(self.state.get('cold_started_at', {})) == set(self.plan['before'])
                        and all(isinstance(value, str) and value for value in self.state['cold_started_at'].values()),
                        'unrecognized_verified_copy_state')
        self.api = module('dev_config')
        self.manifest, _, _ = self.api.configuration(ROOT, 'dev')
        self.api.run = self.read
        self.checker = module('deploy_check')

    def read(self, arguments, **options):
        return S.command(arguments, **options)

    def phase(self, value):
        global PHASE
        PHASE = value
        if self.state is not None:
            self.state['phase'] = value
            self.state['updated_utc'] = datetime.now(timezone.utc).isoformat()
            self.save()
        print(json.dumps({'phase': value}), flush=True)

    def save(self):
        write_atomic(self.journal_path, encoded(self.state))

    def unit_state(self, unit):
        return self.read(['systemctl', 'show', unit, '--property=ActiveState', '--value']).strip()

    def config_side(self):
        require(not S.CONFIG.is_symlink(), 'installed_config_redirected')
        data = S.CONFIG.read_bytes() if S.CONFIG.exists() else None
        if data == self.original:
            return 'source'
        if data == self.candidate:
            return 'target'
        raise S.PlanError('installed_config_changed_outside_maintenance')

    def rows(self):
        rows = self.api.inspect_project(self.manifest, require_running=False)
        ids = set(self.read(['docker', 'container', 'ls', '--all', '--no-trunc', '--quiet']).split())
        require(ids == {row['Id'] for row in rows.values()}, 'other_host_containers_require_review')
        return rows

    def identities(self):
        require(S.SOURCE.is_dir() and S.SOURCE.resolve() == S.SOURCE, 'source_directory_changed')
        require({'device': S.SOURCE.stat().st_dev, 'inode': S.SOURCE.stat().st_ino} == self.plan['source_identity'],
                'source_identity_changed')
        if self.state and self.state.get('target_identity'):
            target = S.TARGET
            if not os.path.lexists(target) and self.state.get('target_staging'):
                target = Path(self.state['target_staging'])
                require(target.parent == self.directory and target.name.startswith('store-stage-'),
                        'target_staging_path_changed')
            require(target.is_dir() and target.resolve() == target, 'target_directory_changed')
            require({'device': target.stat().st_dev, 'inode': target.stat().st_ino} == self.state['target_identity'],
                    'target_identity_changed')
        else:
            require(not os.path.lexists(S.TARGET), 'unowned_destination_exists')

    def precheck(self):
        global PHASE
        PHASE = 'preflight'
        require(self.state is None, 'execution_exists_use_status_or_recovery')
        report, fresh, original, candidate = S.collect()
        rsync_capable(self.read(['rsync', '--version']))
        self.identities()
        require(self.directory.stat().st_dev == S.TARGET.parent.stat().st_dev, 'journal_and_target_must_share_data_filesystem')
        require(original == self.original and candidate == self.candidate
                and fresh['effective_before_sha256'] == self.plan['effective_before_sha256']
                and fresh['config_metadata'] == self.plan['config_metadata'], 'plan_configuration_drift')
        validate_containers(self.plan['before'], fresh['before'], policies='original',
                            running=set(self.plan['public_plan']['running_services']))
        require(all(row['State']['Running'] or policy(row) != 'always' for row in self.plan['before'].values()),
                'stopped_always_restart_container_requires_review')
        require(not os.path.lexists(DROPIN) and DROPIN.parent.resolve() == DROPIN.parent,
                'storage_dropin_already_exists_or_redirected')
        require(S.CONFIG.parent.is_dir() and S.CONFIG.parent.resolve() == S.CONFIG.parent,
                'containerd_config_parent_requires_review')
        require(self.read(['findmnt', '--list', '--noheadings', '--target', '/data', '--output', 'TARGET']).strip() == '/data',
                'data_mount_requires_review')
        require(self.read(['findmnt', '--fstab', '--list', '--noheadings', '--mountpoint', '/data', '--output', 'TARGET']).strip() == '/data',
                'persistent_data_mount_requires_review')
        filesystem = self.read(['findmnt', '--list', '--noheadings', '--target', '/data', '--output', 'FSTYPE']).strip()
        require(filesystem in ('ext4', 'xfs'), 'destination_filesystem_requires_review')
        if filesystem == 'xfs':
            require(S.shutil.which('xfs_info') is not None, 'xfs_info_required_for_destination')
            require(bool(re.search(r'\bftype=1\b', self.read(['xfs_info', '/data']))), 'xfs_directory_type_support_required')
        units = {}
        for unit in UNITS:
            require(self.read(['systemctl', 'show', unit, '--property=LoadState', '--value']).strip() == 'loaded',
                    'required_systemd_unit_unavailable')
            units[unit] = self.unit_state(unit)
        require(all(units[unit] == 'active' for unit in ('containerd.service', 'docker.service')), 'daemon_not_active')
        require(len(process_ids('containerd', exact=True)) == 1,
                'other_containerd_daemons_require_review')
        images = image_bindings(self.read)
        try:
            health = self.checker.live_health(self.api, fresh['before'])
        except Exception:
            raise S.PlanError('source_health_read_unavailable') from None
        require(all(health.values()), 'source_application_not_ready_for_maintenance')
        return report, units, images

    def long(self, arguments, timeout=900, reserve=None):
        name = self.directory / (PHASE + '-' + uuid.uuid4().hex[:8] + '.log.private')
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        process = None
        try:
            with os.fdopen(fd, 'wb') as log:
                process = subprocess.Popen(arguments, stdout=log, stderr=log, start_new_session=True)
                started = time.monotonic()
                next_update = started + 20
                while process.poll() is None:
                    require(time.monotonic() - started < timeout, 'maintenance_command_timed_out')
                    if reserve is not None:
                        require(S.capacity(S.TARGET.parent)['available_bytes'] >= reserve, 'copy_free_space_reserve_reached')
                    if time.monotonic() >= next_update:
                        print(json.dumps({'phase': PHASE, 'elapsed_seconds': int(time.monotonic() - started)}), flush=True)
                        next_update = time.monotonic() + 20
                    time.sleep(1)
                require(process.returncode == 0, 'maintenance_command_failed_inspect_private_log')
                if reserve is not None:
                    require(S.capacity(S.TARGET.parent)['available_bytes'] >= reserve, 'copy_free_space_reserve_reached')
        finally:
            if process is not None and process.poll() is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)
        return name

    def policies(self, original):
        for name in sorted(self.plan['before']):
            row = self.plan['before'][name]
            self.read(['docker', 'update', '--restart=' + (policy(row) if original else 'no'), row['Id']])

    def ensure_daemons(self):
        if self.state is not None:
            require(self.read(['dockerd', '--version']).strip().startswith('Docker version ' + self.state['docker_version'] + ','),
                    'docker_binary_version_changed_during_maintenance')
            require(self.read(['containerd', '--version']).strip().startswith('containerd containerd.io v2.2.4 '),
                    'containerd_binary_version_changed_during_maintenance')
        self.long(['systemctl', 'start', 'containerd.service', 'docker.service'], timeout=180)

    def quiesce(self):
        self.phase('disabling_restart_policies')
        rows = self.rows()
        validate_containers(self.plan['before'], rows)
        require(all(not row['State']['Running'] or name in self.plan['public_plan']['running_services']
                    for name, row in rows.items()), 'unexpected_service_started')
        self.policies(False)
        self.phase('stopping_clients')
        clients = [row['Id'] for name, row in rows.items() if row['State']['Running'] and name not in DATABASES]
        if clients:
            self.long(['docker', 'stop', '--timeout', '-1', *clients], timeout=900)
        self.phase('stopping_databases')
        rows = self.rows()
        for name in DATABASES:
            if rows[name]['State']['Running']:
                self.long(['docker', 'stop', '--signal', 'SIGINT', '--timeout', '-1', rows[name]['Id']], timeout=1800)
        rows = self.rows()
        validate_containers(self.plan['before'], rows, policies='disabled', running=set())
        require(all(rows[name]['State']['ExitCode'] == 0 and not rows[name]['State']['OOMKilled'] for name in DATABASES),
                'database_clean_shutdown_not_confirmed')
        if self.config_side() == 'source' and not self.state['copy_verified']:
            self.state['cold_started_at'] = {name: row['State'].get('StartedAt') for name, row in rows.items()}
            require(all(isinstance(value, str) and value for value in self.state['cold_started_at'].values()),
                    'cold_container_lifecycle_metadata_unavailable')
            self.save()
        S.workload_inventory({row['Id'] for row in rows.values()}, set(), self.read)
        self.phase('stopping_daemons')
        self.long(['systemctl', 'stop', 'docker.socket', 'docker.service'], timeout=180)
        self.long(['systemctl', 'stop', 'containerd.service'], timeout=180)
        self.cold()

    def cold(self):
        require(all(self.unit_state(unit) == 'inactive' for unit in UNITS), 'daemons_not_fully_stopped')
        deadline = time.monotonic() + 30
        while True:
            clear = not process_ids('containerd')
            try:
                no_store_mounts(Path('/proc/self/mountinfo').read_text())
            except S.PlanError:
                clear = False
            if clear:
                break
            require(time.monotonic() < deadline, 'containerd_processes_or_store_mounts_still_exist')
            time.sleep(1)
        self.identities()

    def install_config(self, forward):
        require(self.config_side() in ('source', 'target'), 'installed_config_changed_outside_maintenance')
        data = self.candidate if forward else self.original
        if data is None:
            if S.CONFIG.exists():
                require(self.config_side() == 'target', 'unexpected_missing_config_restore')
                S.CONFIG.unlink()
        else:
            meta = self.plan['config_metadata'] or {'mode': 0o644, 'uid': 0, 'gid': 0}
            write_atomic(S.CONFIG, data, **meta)
        if forward:
            DROPIN.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
            require(not DROPIN.is_symlink() and (not DROPIN.exists() or DROPIN.read_bytes() == DROPIN_DATA),
                    'storage_dropin_changed')
            write_atomic(DROPIN, DROPIN_DATA, mode=0o644)
        elif DROPIN.exists():
            require(not DROPIN.is_symlink() and DROPIN.read_bytes() == DROPIN_DATA, 'storage_dropin_changed')
            DROPIN.unlink()
        self.read(['systemctl', 'daemon-reload'])

    def target_configuration(self):
        require(self.config_side() == 'target' and not DROPIN.is_symlink() and DROPIN.read_bytes() == DROPIN_DATA,
                'target_configuration_not_installed')
        pid = self.read(['systemctl', 'show', 'containerd.service', '--property=MainPID', '--value']).strip()
        require(pid.isdigit() and int(pid) > 1, 'target_containerd_not_running')
        process = Path('/proc', pid)
        flags = S.storage_arguments(process.joinpath('cmdline').read_bytes().decode().rstrip('\x00').split('\x00'))
        require(flags.get('config', str(S.CONFIG)) == str(S.CONFIG) and not ({'root', 'state', 'address'} & flags.keys()),
                'containerd_storage_arguments_changed')
        dump = self.read([str(process.joinpath('exe').resolve(strict=True)), '--config', str(S.CONFIG), 'config', 'dump'])
        configured = S.top_fields(dump)
        require(configured.get('root') == str(S.TARGET) and configured.get('state') == '/run/containerd', 'unexpected_active_storage_configuration')
        S.validate_imports(configured)
        require(str(S.SOURCE) not in S.replace_root(dump, S.TARGET), 'active_plugin_retains_old_storage_path')
        require(str(S.TARGET) in self.read(['systemctl', 'show', 'containerd.service', '--property=RequiresMountsFor', '--value']).split(),
                'data_mount_dependency_missing')

    def verify_store(self):
        self.target_configuration()
        rows = self.rows()
        require(self.read(['docker', 'version', '--format', '{{.Server.Version}}']).strip() == self.state['docker_version'],
                'docker_version_changed_during_maintenance')
        validate_containers(self.plan['before'], rows, policies='disabled', running=set())
        self.not_started_since_copy(rows)
        require(image_bindings(self.read) == [tuple(value) for value in self.state['images_before']], 'image_bindings_changed')
        probe = self.state['probe_name']
        try:
            self.long(['docker', 'run', '--rm', '--pull', 'never', '--name', probe,
                       '--label', 'org.deriw.storage-plan=' + str(self.path), '--network', 'none', '--read-only',
                       '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--no-healthcheck',
                       '--cpus', '1', '--memory', '128m', '--entrypoint', '/bin/sh',
                       self.plan['before']['backend']['Image'], '-c', 'exit 0'], timeout=90)
        finally:
            self.cleanup_probe()
        validate_containers(self.plan['before'], self.rows(), policies='disabled', running=set())

    def cleanup_probe(self):
        ids = self.read(['docker', 'container', 'ls', '--all', '--no-trunc', '--quiet',
                         '--filter', 'name=^/' + self.state['probe_name'] + '$']).split()
        for identity in ids:
            row = json.loads(self.read(['docker', 'inspect', identity]))[0]
            require(row['Name'] == '/' + self.state['probe_name']
                    and row['Config'].get('Labels', {}).get('org.deriw.storage-plan') == str(self.path),
                    'probe_ownership_changed')
            self.read(['docker', 'rm', '--force', identity])

    def not_started_since_copy(self, rows):
        require(self.state.get('cold_started_at') == {name: row['State'].get('StartedAt') for name, row in rows.items()},
                'containers_started_after_cold_copy_require_review')

    def apply(self):
        if self.state is None:
            report, units, images = self.precheck()
            docker_version = self.read(['docker', 'version', '--format', '{{.Server.Version}}']).strip()
            require(bool(re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9_.+-]{0,80}', docker_version)),
                    'unrecognized_docker_server_version')
            self.state = {'format': FORMAT, 'plan_sha256': self.plan_hash, 'status': 'in_progress', 'phase': 'prepared',
                          'copy_verified': False, 'services_started_on_target': False, 'target_identity': None,
                          'images_before': images, 'units_before': units, 'docker_version': docker_version,
                          'probe_name': 'storage-probe-' + uuid.uuid4().hex[:12]}
            self.save()
        require(not self.state['services_started_on_target'] and self.state['status'] not in ('active', 'aborted'),
                'use_resume_or_status_for_this_execution')
        self.identities()
        side = self.config_side()
        require(side == 'source' or self.state['copy_verified'], 'unverified_target_configuration')
        self.ensure_daemons()
        self.cleanup_probe()
        if self.state['copy_verified']:
            self.not_started_since_copy(self.rows())
        if side == 'target':
            require(all(not row['State']['Running'] for row in self.rows().values()),
                    'target_services_started_outside_executor')
        require(image_bindings(self.read) == [tuple(value) for value in self.state['images_before']], 'image_bindings_changed')
        self.quiesce()
        if not self.state['copy_verified']:
            require(not active_store_copy(), 'previous_store_copy_process_is_running')
            self.phase('copying_store')
            if self.state['target_identity'] is None:
                staging = Path(tempfile.mkdtemp(prefix='store-stage-', dir=self.directory))
                self.state['target_identity'] = {'device': staging.stat().st_dev, 'inode': staging.stat().st_ino}
                self.state['target_staging'] = str(staging)
                self.save()
            if not os.path.lexists(S.TARGET):
                self.identities()
                os.rename(self.state['target_staging'], S.TARGET)
            self.long(rsync_command(), timeout=28800, reserve=S.RESERVE)
            self.phase('verifying_cold_copy')
            log = self.long(rsync_command(verify=True), timeout=28800, reserve=S.RESERVE)
            require(log.stat().st_size == 0, 'cold_copy_verification_has_differences')
            self.state['copy_verified'] = True
            self.save()
        self.cold()
        self.phase('installing_target_configuration')
        self.install_config(True)
        self.phase('starting_daemons')
        self.ensure_daemons()
        self.phase('verifying_new_store')
        self.verify_store()
        self.state['status'] = 'store_ready'
        self.phase('store_ready_services_stopped')
        return self.summary()

    def start_previous(self):
        self.phase('starting_databases')
        rows = self.rows()
        for name in DATABASES:
            if not rows[name]['State']['Running']:
                self.long(['docker', 'start', rows[name]['Id']], timeout=180)
        deadline = time.monotonic() + 300
        while True:
            ready = True
            for name in DATABASES:
                try:
                    self.read(['docker', 'exec', self.plan['before'][name]['Id'], 'sh', '-c',
                               'exec pg_isready -q -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-${POSTGRES_USER:-postgres}}"'], timeout=10)
                except Exception:
                    ready = False
            if ready:
                break
            require(time.monotonic() < deadline, 'databases_not_ready')
            print(json.dumps({'phase': 'waiting_for_databases'}), flush=True)
            time.sleep(5)
        self.phase('starting_recorded_clients')
        for name in CLIENTS:
            if name in self.plan['public_plan']['running_services'] and not self.rows()[name]['State']['Running']:
                self.long(['docker', 'start', self.plan['before'][name]['Id']], timeout=180)
        self.phase('restoring_restart_policies')
        self.policies(True)

    def health(self, wait=False):
        deadline = time.monotonic() + (180 if wait else 0)
        while True:
            rows = self.rows()
            validate_containers(self.plan['before'], rows, policies='original')
            wanted = set(self.plan['public_plan']['running_services'])
            require(all(not row['State']['Running'] or name in wanted for name, row in rows.items()),
                    'unexpected_service_started')
            running_ok = all(row['State']['Running'] == (name in wanted) for name, row in rows.items())
            try:
                health = self.checker.live_health(self.api, rows)
            except Exception:
                health = {'backend_readiness_ok': False, 'nft_connection_and_queue_ok': False}
            if not running_ok:
                health = {key: False for key in health}
            if all(health.values()) or time.monotonic() >= deadline:
                return health
            print(json.dumps({'phase': 'waiting_for_backend_and_nft_readiness'}), flush=True)
            time.sleep(5)

    def resume(self):
        require(self.state is not None and self.state['copy_verified']
                and self.state['status'] in ('store_ready', 'needs_attention', 'active'), 'new_store_not_ready_for_resume')
        self.identities()
        require(self.config_side() == 'target', 'target_configuration_not_installed')
        self.ensure_daemons()
        if not self.state['services_started_on_target']:
            self.verify_store()
        else:
            validate_containers(self.plan['before'], self.rows())
        self.state['services_started_on_target'] = True
        self.state['status'] = 'needs_attention'
        self.save()
        self.start_previous()
        self.phase('checking_application_readiness')
        health = self.health(wait=True)
        require(all(health.values()), 'application_readiness_needs_attention_use_resume')
        self.verify_backend_path()
        require(image_bindings(self.read) == [tuple(value) for value in self.state['images_before']], 'image_bindings_changed')
        self.state['status'] = 'active'
        self.phase('active_on_new_store')
        return {**self.summary(), **health}

    def verify_backend_path(self, root=None):
        root = S.TARGET if root is None else root
        backend_pid = self.rows()['backend']['State']['Pid']
        upperdir = S.root_upperdir(Path('/proc', str(backend_pid), 'mountinfo').read_text(), root=root)
        require(Path(upperdir).stat().st_dev == root.stat().st_dev, 'backend_not_using_expected_filesystem')

    def abort(self):
        require(self.state is not None and not self.state['services_started_on_target'],
                'abort_unavailable_after_services_started_use_resume')
        self.identities()
        side = self.config_side()
        if self.state['status'] == 'aborted':
            require(side == 'source', 'original_configuration_not_installed')
            return self.summary()
        if self.unit_state('docker.service') == 'active':
            self.cleanup_probe()
            before_images = {tuple(value) for value in self.state['images_before']}
            require(set(image_bindings(self.read)) <= before_images, 'new_image_bindings_require_review')
            if side == 'target':
                require(all(not row['State']['Running'] for row in self.rows().values()),
                        'target_services_started_outside_executor')
                self.not_started_since_copy(self.rows())
            self.quiesce()
        else:
            if self.unit_state('containerd.service') == 'active':
                S.workload_inventory({row['Id'] for row in self.plan['before'].values()}, set(), self.read)
            self.phase('stopping_inactive_or_failed_daemons')
            self.long(['systemctl', 'stop', 'docker.socket', 'docker.service', 'containerd.service'], timeout=180)
            self.cold()
        self.phase('restoring_original_configuration')
        if self.config_side() == 'target':
            self.install_config(False)
        elif DROPIN.exists():
            require(DROPIN.read_bytes() == DROPIN_DATA, 'storage_dropin_changed')
            DROPIN.unlink()
            self.read(['systemctl', 'daemon-reload'])
        self.ensure_daemons()
        self.cleanup_probe()
        validate_containers(self.plan['before'], self.rows(), policies='disabled', running=set())
        require(image_bindings(self.read) == [tuple(value) for value in self.state['images_before']], 'source_image_bindings_changed')
        self.start_previous()
        health = self.health(wait=True)
        require(all(health.values()), 'original_store_services_need_attention')
        self.state['status'] = 'aborted'
        self.phase('restored_original_store')
        return {**self.summary(), **health}

    def summary(self):
        retained = (S.SOURCE.is_dir() and S.SOURCE.resolve() == S.SOURCE
                    and {'device': S.SOURCE.stat().st_dev, 'inode': S.SOURCE.stat().st_ino} == self.plan['source_identity'])
        return {'status': self.state['status'] if self.state else 'not_started', 'private_plan': str(self.path),
                'recorded_phase': self.state.get('phase') if self.state else None,
                'copy_verified': self.state.get('copy_verified', False) if self.state else False,
                'services_started_on_target': self.state.get('services_started_on_target', False) if self.state else False,
                'source_retained': retained, 'application_upgrade_performed': False,
                'original_store_cleanup_performed': False,
                'note': 'The original store remains on the root filesystem. Abort is available only before services resume on the target. Resume retries use the target store; they never copy the old store over newer writes.'}

    def status(self):
        self.identities()
        result = self.summary()
        result['recorded_status'] = result['status']
        result['observed_config_side'] = self.config_side()
        result['observed_units'] = {unit: self.unit_state(unit) for unit in UNITS}
        if self.state is None:
            return result
        result['status'] = 'needs_attention'
        if any(result['observed_units'][unit] != 'active' for unit in ('docker.service', 'containerd.service')):
            return result
        if result['observed_config_side'] == 'target':
            self.target_configuration()
        rows = self.rows()
        completed = self.state['status'] in ('active', 'aborted')
        ready = self.state['status'] == 'store_ready'
        expected_running = set(self.plan['public_plan']['running_services']) if completed else set() if ready else None
        validate_containers(self.plan['before'], rows,
                            policies='original' if completed else 'disabled' if ready else 'either')
        result['observed_running_services'] = sorted(name for name, row in rows.items() if row['State']['Running'])
        if expected_running is not None and set(result['observed_running_services']) != expected_running:
            return result
        if ready:
            self.not_started_since_copy(rows)
        require(image_bindings(self.read) == [tuple(value) for value in self.state['images_before']], 'image_bindings_changed')
        if self.state['status'] in ('active', 'aborted'):
            self.verify_backend_path(S.TARGET if self.state['status'] == 'active' else S.SOURCE)
            result.update(self.health())
            if not result['backend_readiness_ok'] or not result['nft_connection_and_queue_ok']:
                return result
        if ((self.state['status'] in ('store_ready', 'active') and result['observed_config_side'] == 'target')
                or (self.state['status'] == 'aborted' and result['observed_config_side'] == 'source')):
            result['status'] = 'verified'
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    for name in ('check', 'apply', 'resume', 'abort', 'status'):
        mode.add_argument('--' + name, action='store_true')
    parser.add_argument('--state', type=Path, required=True)
    args = parser.parse_args()
    maintenance = None
    try:
        require(sys.platform.startswith('linux') and os.geteuid() == 0, 'linux_root_required')
        if args.apply or args.resume or args.abort:
            signal.signal(signal.SIGHUP, interrupted)
            signal.signal(signal.SIGTERM, interrupted)
        # The immutable plan file is also the lock inode. No lock file is
        # created by --check or --status; journal replacements cannot unlock it.
        load_plan(args.state)
        fd = os.open(args.state, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as lock, global_lock(args.apply or args.resume or args.abort):
            try:
                fcntl.flock(lock, (fcntl.LOCK_SH if args.check or args.status else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            except BlockingIOError:
                raise S.PlanError('another_storage_operation_holds_this_plan') from None
            maintenance = Maintenance(args.state)
            if args.apply or args.resume or args.abort:
                require(not active_store_copy(), 'previous_store_copy_process_is_running')
            if args.check:
                report, units, images = maintenance.precheck()
                result = {'status': 'storage_maintenance_ready', 'private_plan': str(args.state),
                          'running_services': report['running_services'], 'image_bindings': len(images),
                          'units': units, 'source': str(S.SOURCE), 'target': str(S.TARGET),
                          'interruption_required': True, 'services_changed': False,
                          'effect': '--apply stops the recorded running services, copies and verifies the store, switches containerd, and leaves applications stopped. --resume starts the recorded services. --abort can restore the original store before --resume.'}
            elif args.status:
                result = maintenance.status()
            elif args.apply:
                result = maintenance.apply()
            elif args.resume:
                result = maintenance.resume()
            else:
                result = maintenance.abort()
            print(json.dumps(result, indent=2))
            return 0
    except BaseException as error:
        if isinstance(error, SystemExit):
            raise
        if maintenance is not None and maintenance.state is not None and not args.status and not args.check:
            maintenance.state['status'] = 'needs_attention'
            try:
                maintenance.save()
            except Exception:
                pass
        code = str(error) if isinstance(error, S.PlanError) else 'maintenance_failed_raw_values_suppressed'
        print(json.dumps({'status': 'stopped', 'stage': PHASE, 'reason_code': code,
                          'private_plan': str(args.state), 'raw_values_suppressed': True,
                          'recovery': 'Use --status. Before target services started, --abort restores the source; otherwise retry --resume. Never delete either store to recover.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
