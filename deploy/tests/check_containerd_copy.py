#!/usr/bin/env python3
"""Check cold-copy flags and config parsing in an isolated local container.

The fixture runs no daemon, uses no host bind mounts or privileged mode, and
has no external network. Synthetic files use an automatically removed Docker volume.
It does not rehearse systemd, a full store migration or service restarts.
"""
import importlib.util
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tomllib
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
SPEC = importlib.util.spec_from_file_location('containerd_maintenance', ROOT / 'deploy/scripts/containerd_maintenance.py')
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)
IMAGE = 'deriw-blockscout:containerd-storage-validation'


def isolated(script):
    name = 'storage-copy-fixture-' + uuid.uuid4().hex[:16]
    try:
        result = subprocess.run([
            'docker', 'run', '--rm', '--name', name, '--pull', 'never', '-i', '--network', 'none', '--read-only',
            '--mount', 'type=volume,target=/work', '--tmpfs', '/tmp:rw,size=16m',
            '--tmpfs', '/var/lib/postgresql/data:rw,size=1m',
            '--cpus', '2', '--memory', '256m', IMAGE, '-s',
        ], input=script, capture_output=True, text=True, timeout=90)
    finally:
        subprocess.run(['docker', 'rm', '--force', '--volumes', name], capture_output=True, timeout=15)
    if result.returncode:
        raise RuntimeError(result.stderr[-2000:])
    return result.stdout


def lifecycle():
    token = uuid.uuid4().hex[:16]
    name = 'storage-lifecycle-' + token
    label = 'org.deriw.storage-lifecycle=' + token
    network = 'storage-network-' + token

    def run(arguments):
        return subprocess.run(['docker', *arguments], capture_output=True, text=True, check=True, timeout=30).stdout

    def row():
        return {'fixture': json.loads(run(['inspect', name]))[0]}

    created = False
    try:
        run(['network', 'create', '--internal', '--label', label, network])
        created = True
        run(['run', '-d', '--pull', 'never', '--name', name, '--label', label, '--restart', 'always',
             '--network', network, '--network-alias', 'fixture', '--read-only',
             '--tmpfs', '/var/lib/postgresql/data:rw,size=1m', '--entrypoint', 'sh', IMAGE,
             '-c', "trap 'exit 0' TERM INT; while :; do sleep 1; done"])
        before = row()
        run(['update', '--restart=no', name])
        run(['stop', '--timeout', '-1', name])
        M.validate_containers(before, row(), policies='disabled', running=set())
        run(['start', name])
        run(['update', '--restart=always', name])
        M.validate_containers(before, row(), policies='original', running={'fixture'})
    finally:
        ids = run(['container', 'ls', '--all', '--quiet', '--filter', 'label=' + label]).split()
        if ids:
            run(['rm', '--force', '--volumes', *ids])
        if created:
            run(['network', 'rm', network])


def main():
    rsync_version = isolated('rsync --version\n')
    M.rsync_capable(rsync_version)
    script = (ROOT / 'deploy/tests/containerd_fixture/copy.sh').read_text()
    for token, verify in [('COPY_COMMAND', False), ('VERIFY_COMMAND', True)]:
        arguments = M.rsync_command(verify)
        arguments[-2:] = ['/work/source/', '/work/target/']
        script = script.replace(token, shlex.join(arguments))
    output = isolated(script)
    assert output.splitlines() == ['STORAGE_COPY_FIXTURE_OK'], output
    config = """version = 3
root = '/var/lib/containerd'
state = '/run/containerd'
imports = []
[plugins.'io.containerd.snapshotter.v1.overlayfs']
  sync_remove = true
"""
    candidate = M.S.replace_root(config, M.S.TARGET)
    dumps = []
    for text in (config, candidate):
        dump = isolated("exec containerd --config /dev/stdin config dump <<'CONFIG'\n" + text + '\nCONFIG\n')
        tomllib.loads(dump)
        dumps.append(dump)
    M.S.validate_candidate(*dumps)
    assert tomllib.loads(dumps[1])['plugins']['io.containerd.snapshotter.v1.overlayfs']['sync_remove'] is True
    lifecycle()
    print(json.dumps({'status': 'passed', 'containerd_version': '2.2.4',
                      'rsync_version': rsync_version.splitlines()[0],
                      'copy_and_checksum_verification': True, 'hardlinks_acl_xattrs_sparse_preserved': True,
                      'tampering_and_extra_files_detected': True, 'verification_did_not_delete_extra_files': True,
                      'real_containerd_config_candidate_checked': True,
                      'real_container_lifecycle_and_network_checked': True,
                      'live_services_accessed': False, 'full_host_migration_rehearsal': False}, indent=2))


if __name__ == '__main__':
    main()
