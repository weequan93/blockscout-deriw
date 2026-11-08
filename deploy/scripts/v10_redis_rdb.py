"""Verify a stopped Redis RDB as its owner, without changing data or permissions."""
from pathlib import Path
import re
import stat
import sys
import uuid

import dev_config as D
import release as R
import v10_infrastructure_checks as K


def check(api, path, inputs, side):
    D.require(inputs['component'] == 'redis' and side in ('forward', 'rollback'),
              'RDB owner review is only for the saved Redis patch')
    source = K.data_mount(inputs['before']['redis-db'], '/data')
    filename = inputs['component_before']['dbfilename']
    D.require(re.fullmatch(r'[A-Za-z0-9_.-]+', filename) and filename not in ('.', '..'),
              'Unexpected saved Redis RDB filename')
    rdb = source / filename
    D.require(rdb.is_file() and not rdb.is_symlink(),
              'The final persisted Redis RDB must be an existing regular file')
    before = rdb.stat()
    image = inputs['image_id'] if side == 'forward' else inputs['before']['redis-db']['Image']
    # A root process with every capability dropped cannot bypass mode 0600 on
    # another user's RDB. Use the existing owner; never chmod/chown live data.
    command = ['docker', 'run', '--rm', '--pull', 'never', '--network', 'none',
               '--read-only', '--cap-drop', 'ALL', '--user', f'{before.st_uid}:{before.st_gid}',
               '--mount', 'type=bind,source=' + str(source) + ',target=/data,readonly',
               '--entrypoint', 'redis-check-rdb', image, '/data/' + filename]
    try:
        api.run(command, timeout=120)
    except D.ConfigError:
        raise D.ConfigError('Redis RDB check failed using the file owner; keep the stopped data and saved recovery journal') from None
    after = rdb.stat()
    fields = ('st_dev', 'st_ino', 'st_uid', 'st_gid', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    D.require(all(getattr(before, key) == getattr(after, key) for key in fields),
              'Redis RDB metadata changed during the read-only check')
    evidence = {'format': 'deriw-redis-rdb-owner-check-v1', 'side': side,
                'image_id': image, 'checker_source_sha256': R.digest(Path(__file__).read_bytes()),
                'uid': before.st_uid, 'gid': before.st_gid, 'mode': oct(stat.S_IMODE(before.st_mode)),
                'bytes': before.st_size, 'verified': True, 'network': 'none',
                'data_mounted_read_only': True, 'capabilities_dropped': 'ALL',
                'data_and_permissions_changed': False}
    R.write_new(path.parent / ('redis-rdb-owner-' + side + '-' + uuid.uuid4().hex[:8] + '.json'), R.encoded(evidence))
    return evidence


def resume_existing(deployment, path, rollback=False):
    """Narrow recovery overlay; keep the original creator's source guard and checks."""
    state, inputs = deployment.load(path)
    D.require(inputs['component'] == 'redis' and state['phase'] in (
        'staged', 'activation_requested', 'active', 'finished',
        'rollback_requested', 'rolled_back', 'rollback_finished'),
        'Use the existing interrupted Redis patch journal')
    D.require(deployment.ROOT == Path(inputs['source_root'])
              and deployment.F.source_review(deployment.D) == inputs['tool_source_commit'],
              'Recover through the unchanged original infrastructure creation checkout')
    recovery_root = Path(__file__).resolve().parents[2]
    commit, version = deployment.B.clean_source(deployment.D, recovery_root)
    D.require(version == '10.2.6', 'Use the reviewed v10 Redis recovery tools')
    changes = deployment.D.run(['git', '-C', str(recovery_root), 'diff', '--name-only',
                                inputs['tool_source_commit'], commit]).splitlines()
    D.require(all(name.startswith(('deploy/', 'docs/')) for name in changes),
              'Recovery overlay changed backend application/configuration')
    evidence = {'format': 'deriw-redis-rdb-owner-recovery-v1',
                'original_source_commit': inputs['tool_source_commit'],
                'recovery_source_commit': commit, 'recovery_source_root': str(recovery_root),
                'checker_source_sha256': R.digest(Path(__file__).read_bytes()),
                'inputs_sha256': state['inputs_sha256'],
                'replacement_function': 'stopped_redis_check',
                'original_inputs_and_snapshots_edited': False}
    R.write_new(path.parent / ('redis-rdb-owner-recovery-' + uuid.uuid4().hex[:8] + '.json'), R.encoded(evidence))
    original, argv = deployment.stopped_redis_check, sys.argv
    deployment.stopped_redis_check = check
    sys.argv = ['v10_infrastructure_deploy.py', '--rollback' if rollback else '--resume', '--state', str(path)]
    try:
        return deployment.main()
    finally:
        deployment.stopped_redis_check, sys.argv = original, argv
