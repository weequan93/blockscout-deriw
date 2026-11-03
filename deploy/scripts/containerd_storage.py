#!/usr/bin/env python3
"""Prepare a private dev containerd storage-maintenance plan; never stop services."""
import argparse
import ast
from datetime import datetime, timezone
import hashlib
import glob
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path('/var/lib/containerd')
TARGET = Path('/data/containerd')
CONFIG = Path('/etc/containerd/config.toml')
PRIVATE = Path('/data/blockscout-dev-private/containerd-storage')
RESERVE = 100 * 1024**3
MAX_BYTES = 16 * 1024**2
PHASE = 'initial'


class PlanError(Exception):
    pass


def require(condition, code):
    if not condition:
        raise PlanError(code)


def phase(value):
    global PHASE
    PHASE = value


def command(arguments, *, input_text=None, timeout=15):
    try:
        result = subprocess.run(arguments, input=input_text, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        raise PlanError('command_unavailable_or_timed_out') from None
    require(result.returncode == 0 and len(result.stdout) <= MAX_BYTES, 'command_failed_or_exceeded_limit')
    return result.stdout


def storage_arguments(arguments):
    """Interpret only public storage flags; never export raw process arguments."""
    values = {}
    aliases = {'-c': 'config', '--config': 'config', '--root': 'root', '--state': 'state',
               '-a': 'address', '--address': 'address'}
    index = 1
    while index < len(arguments):
        flag, separator, value = arguments[index].partition('=')
        if flag in aliases:
            if not separator:
                index += 1
                require(index < len(arguments), 'incomplete_storage_argument')
                value = arguments[index]
            key = aliases[flag]
            require(key not in values, 'duplicate_storage_argument')
            values[key] = value
        index += 1
    return values


def top_fields(text):
    """Read scalar storage fields from containerd's canonical config dump."""
    values = {}
    for line in text.splitlines():
        if line.lstrip().startswith('['):
            break
        match = re.fullmatch(r'\s*(version|root|state|imports)\s*=\s*(.*?)\s*', line)
        if match:
            require(match[1] not in values, 'duplicate_top_level_storage_setting')
            try:
                values[match[1]] = ast.literal_eval(match[2])
            except (ValueError, SyntaxError):
                raise PlanError('unsupported_top_level_storage_setting') from None
    return values


def replace_root(text, target):
    """Preserve source version, plugins and comments; change one top-level key."""
    lines = text.splitlines(keepends=True)
    count = 0
    for index, line in enumerate(lines):
        if line.lstrip().startswith('['):
            break
        if re.match(r'^\s*root\s*=', line):
            count += 1
            lines[index] = 'root = ' + json.dumps(str(target)) + '\n'
    require(count <= 1, 'duplicate_top_level_storage_setting')
    if count == 0:
        lines.insert(0, 'root = ' + json.dumps(str(target)) + '\n')
    return ''.join(lines)


def validate_imports(values):
    # v2.2.4 includes this glob in its defaults even when it matches no files.
    # Actual imported files need their own snapshot/override review.
    imports = values.get('imports', [])
    require(isinstance(imports, list), 'unsupported_import_registry')
    require(all(value == '/etc/containerd/conf.d/*.toml' and not glob.glob(value) for value in imports),
            'imported_configuration_requires_review')


def validate_candidate(before, after):
    a, b = top_fields(before), top_fields(after)
    require(a.get('version') == b.get('version') == 3, 'unexpected_effective_config_version')
    require(a.get('root') == str(SOURCE) and b.get('root') == str(TARGET), 'unexpected_storage_root')
    require(a.get('state') == b.get('state') == '/run/containerd', 'unexpected_runtime_state_path')
    validate_imports(a)
    validate_imports(b)
    require(replace_root(before, TARGET) == replace_root(after, TARGET), 'candidate_changes_other_settings')
    require(str(SOURCE) not in replace_root(after, TARGET), 'plugin_retains_old_storage_path')


def root_upperdir(mountinfo, root=None):
    root = SOURCE if root is None else root
    for line in mountinfo.splitlines():
        left, separator, right = line.partition(' - ')
        fields, data = left.split(), right.split()
        if separator and len(fields) >= 5 and fields[4] == '/' and len(data) == 3 and data[0] == 'overlay':
            for option in data[2].split(','):
                if option.startswith('upperdir='):
                    value = option[len('upperdir='):]
                    require('\\' not in value and value.startswith(str(root) + '/'), 'unexpected_active_snapshot_path')
                    return value
    raise PlanError('active_snapshot_path_unavailable')


def capacity(path):
    path = path.resolve(strict=True)
    data, fs = path.stat(), os.statvfs(path)
    return {'path': str(path), 'filesystem_device': data.st_dev,
            'total_bytes': fs.f_blocks * fs.f_frsize, 'available_bytes': fs.f_bavail * fs.f_frsize}


def digest(value):
    return hashlib.sha256(value).hexdigest()


def workload_inventory(docker_ids, running_ids, read=None):
    read = command if read is None else read
    prefix = ['ctr', '--address', '/run/containerd/containerd.sock']
    namespaces = read(prefix + ['namespaces', 'list', '--quiet'], timeout=5).split()
    allowed = {'moby', 'plugins.moby', 'default', 'moby_history'}
    require('moby' in namespaces and len(namespaces) == len(set(namespaces))
            and set(namespaces) <= allowed, 'other_containerd_namespaces_require_review')
    result = []
    for namespace in sorted(namespaces):
        containers = read(prefix + ['--namespace', namespace, 'containers', 'list', '--quiet'], timeout=5).split()
        tasks = read(prefix + ['--namespace', namespace, 'tasks', 'list', '--quiet'], timeout=5).split()
        if namespace == 'moby':
            require(set(containers) <= docker_ids and set(tasks) <= set(containers)
                    and set(tasks) == running_ids, 'other_containerd_workloads_require_review')
        else:
            # The operator observed moby_history with no containers or tasks.
            # Inspect both on every run; its name alone grants no exception.
            require(not containers and not tasks, 'other_containerd_workloads_require_review')
        result.append({'namespace': namespace, 'containers': len(containers), 'tasks': len(tasks)})
    return result


def collect():
    phase('host_prerequisites')
    require(sys.platform.startswith('linux') and os.geteuid() == 0, 'linux_root_required')
    require(shutil.which('rsync') is not None, 'rsync_not_installed')
    spec = importlib.util.spec_from_file_location('storage_dev_config', ROOT / 'deploy/scripts/dev_config.py')
    api = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = api
    spec.loader.exec_module(api)
    api.run = command
    phase('docker_inventory')
    manifest, _, _ = api.configuration(ROOT, 'dev')
    rows = api.inspect_project(manifest)
    ids = set(command(['docker', 'container', 'ls', '--all', '--no-trunc', '--quiet']).split())
    require(ids == {row['Id'] for row in rows.values()}, 'other_host_containers_require_review')
    require(all(not row.get('State', {}).get('Paused') and not row.get('State', {}).get('Restarting')
                for row in rows.values()), 'containers_must_be_stable')
    require(rows['backend']['Config'].get('Cmd') == ['bin/blockscout', 'start'], 'backend_startup_requires_review')
    info = json.loads(command(['docker', 'info', '--format', '{{json .}}']))
    require(info.get('DockerRootDir') == '/data/docker' and info.get('Driver') == 'overlayfs'
            and ['driver-type', 'io.containerd.snapshotter.v1'] in info.get('DriverStatus', []), 'unexpected_docker_storage')
    require(info.get('Swarm', {}).get('LocalNodeState') == 'inactive', 'swarm_requires_separate_maintenance')
    require(not command(['docker', 'plugin', 'ls', '--quiet']).strip(), 'managed_plugins_require_review')
    phase('containerd_process')
    pid_text = command(['systemctl', 'show', 'containerd', '--property=MainPID', '--value']).strip()
    require(pid_text.isdigit() and int(pid_text) > 1, 'containerd_service_not_running')
    process = Path('/proc') / pid_text
    arguments = process.joinpath('cmdline').read_bytes().decode().rstrip('\x00').split('\x00')
    require(Path(arguments[0]).name == 'containerd', 'unexpected_containerd_process')
    flags = storage_arguments(arguments)
    require(flags.get('config', str(CONFIG)) == str(CONFIG), 'custom_config_path_requires_review')
    require(not ({'root', 'state', 'address'} & flags.keys()), 'command_line_storage_override_requires_review')
    executable = process.joinpath('exe').resolve(strict=True)
    version = command([str(executable), '--version']).strip()
    require(bool(re.fullmatch(r'containerd containerd\.io v2\.2\.4 [a-f0-9]{40}', version)), 'unreviewed_containerd_version')
    require(not CONFIG.is_symlink(), 'config_symlink_requires_review')
    phase('containerd_configuration')
    exists = CONFIG.exists()
    if exists:
        require(CONFIG.is_file() and CONFIG.stat().st_size <= 2 * 1024**2, 'config_file_unavailable_or_oversize')
    before = command([str(executable), '--config', str(CONFIG), 'config', 'dump'])
    validate_imports(top_fields(before))
    original = CONFIG.read_bytes() if exists else None
    candidate = replace_root(original.decode() if exists else before, TARGET)
    after = command([str(executable), '--config', '/dev/stdin', 'config', 'dump'], input_text=candidate)
    validate_candidate(before, after)
    phase('active_storage_paths')
    require(SOURCE.is_dir() and SOURCE.resolve() == SOURCE, 'source_directory_requires_review')
    require(TARGET.parent.resolve() == Path('/data') and not os.path.lexists(TARGET), 'destination_already_exists_or_redirected')
    backend_pid = rows['backend']['State']['Pid']
    require(type(backend_pid) is int and backend_pid > 1, 'backend_process_unavailable')
    upperdir = root_upperdir(Path('/proc', str(backend_pid), 'mountinfo').read_text())
    require(Path(upperdir).is_dir() and Path(upperdir).stat().st_dev == SOURCE.stat().st_dev, 'active_snapshot_on_unexpected_filesystem')
    phase('containerd_workloads')
    running_ids = {row['Id'] for row in rows.values() if row['State']['Running']}
    workloads = workload_inventory(ids, running_ids)
    phase('storage_capacity')
    source_space, target_space = capacity(SOURCE), capacity(TARGET.parent)
    require(source_space['filesystem_device'] != target_space['filesystem_device'], 'destination_must_use_data_filesystem')
    require(target_space['available_bytes'] >= 2 * source_space['total_bytes'] + RESERVE, 'insufficient_preliminary_copy_headroom')
    phase('stability')
    api.ensure_stable(rows, api.inspect_project(manifest), 'Deployment changed during storage planning')
    require(process.exists() and command(['systemctl', 'show', 'containerd', '--property=MainPID', '--value']).strip() == pid_text,
            'containerd_changed_during_planning')
    require(CONFIG.exists() == exists and (CONFIG.read_bytes() if exists else None) == original,
            'configuration_changed_during_planning')
    validate_imports(top_fields(before))
    require(workload_inventory(ids, running_ids) == workloads, 'containerd_workloads_changed_during_planning')
    report = {'status': 'containerd_storage_plan_ready', 'read_only_services': True,
              'source': str(SOURCE), 'target': str(TARGET), 'runtime_state_preserved': '/run/containerd',
              'containerd_version': '2.2.4', 'effective_config_version': 3,
              'active_backend_snapshot_path_verified': True, 'source_capacity': source_space,
              'containerd_workloads': workloads,
              'destination_capacity': target_space, 'copy_free_space_reserve_bytes': RESERVE,
              'services': len(rows), 'running_services': sorted(name for name, row in rows.items() if row['State']['Running']),
              'maintenance_required': True, 'application_upgrade_performed': False, 'apply_available': False,
              'steps': ['Save private metadata and candidate configuration',
                        'Temporarily disable restart policies; stop clients, then PostgreSQL cleanly',
                        'Confirm no remaining containerd tasks or nested mounts; stop Docker/socket and containerd',
                        'Copy the store with ownership, hard links, ACLs, xattrs and sparse files preserved',
                        'Verify the cold copy and remaining free space before installing the candidate config',
                        'Restart the daemons; verify the original image and container inventory',
                        'Start only previously running services; restore policies and verify readiness',
                        'Retain the original store until a separate cleanup review'],
              'note': 'Capacity is a preliminary guard, not a measured copy-size or duration estimate. No directory scan, copy, config write or service change in --check mode.'}
    private = {'format': 'blockscout-containerd-maintenance-plan-v1', 'created_utc': datetime.now(timezone.utc).isoformat(),
               'config_existed': exists, 'config_sha256': digest(original) if exists else None,
               'candidate_sha256': digest(candidate.encode()), 'effective_before_sha256': digest(before.encode()),
               'source_identity': {'device': SOURCE.stat().st_dev, 'inode': SOURCE.stat().st_ino},
               'config_metadata': {'mode': stat.S_IMODE(CONFIG.stat().st_mode), 'uid': CONFIG.stat().st_uid,
                                   'gid': CONFIG.stat().st_gid} if exists else None,
               'before': rows, 'public_plan': report}
    return report, private, original, candidate.encode()


def prepare(report, private, original, candidate):
    phase('private_plan_write')
    require(not PRIVATE.is_symlink() and PRIVATE.resolve() == PRIVATE, 'private_directory_redirected')
    PRIVATE.mkdir(parents=True, mode=0o700, exist_ok=True)
    require(PRIVATE.is_dir() and stat.S_IMODE(PRIVATE.stat().st_mode) == 0o700, 'private_directory_permissions')
    directory = Path(tempfile.mkdtemp(prefix='plan-', dir=PRIVATE))
    files = {'state.json': (json.dumps(private, indent=2) + '\n').encode(), 'containerd.after.toml': candidate}
    if original is not None:
        files['containerd.before.toml'] = original
    for name, data in files.items():
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    return {**report, 'status': 'containerd_storage_plan_saved', 'private_plan': str(directory / 'state.json'),
            'note': 'Private files may contain credentials. Keep this directory on the server. No copy, installed config change, service stop or application upgrade was performed.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--check', action='store_true')
    modes.add_argument('--prepare', action='store_true', help='Save private before-state and validated candidate config only')
    args = parser.parse_args()
    try:
        report, private, original, candidate = collect()
        if args.prepare:
            report = prepare(report, private, original, candidate)
        print(json.dumps(report, indent=2))
        return 0
    except PlanError as error:
        # PlanError codes are constants in this file; arbitrary external errors
        # and raw Docker/config/process outputs must never cross this boundary.
        print(json.dumps({'status': 'stopped', 'reason_code': str(error), 'raw_values_suppressed': True,
                          'stage': PHASE, 'services_changed': False}))
    except Exception:
        print(json.dumps({'status': 'stopped', 'reason_code': 'bounded_storage_plan_check_failed',
                          'stage': PHASE, 'raw_values_suppressed': True, 'services_changed': False}))
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
