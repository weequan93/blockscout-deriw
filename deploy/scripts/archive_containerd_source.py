#!/usr/bin/env python3
"""Copy and verify the inactive original store; never remove it or stop services."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import sys

import containerd_maintenance as M

FORMAT = 'blockscout-original-store-archive-v1'


def identity(path):
    M.require(path.is_dir() and path.resolve() == path, 'archive_directory_redirected_or_missing')
    info = path.stat()
    return {'device': info.st_dev, 'inode': info.st_ino}


def commands(destination, verify=False):
    arguments = M.rsync_command(verify)
    # The source is always the original, inactive store. The active target is
    # never an rsync destination for this helper.
    arguments[-1] = str(destination) + '/'
    if not verify:
        arguments.insert(1, '--fsync')
    return arguments


def active_check(maintenance):
    status = maintenance.status()
    M.require(status['status'] == 'verified' and status['recorded_status'] == 'active'
              and status['observed_config_side'] == 'target', 'new_store_must_be_verified_and_active')
    M.require(len(M.process_ids('containerd', exact=True)) == 1, 'other_containerd_daemons_require_review')
    rows = maintenance.rows()
    # Inspect mount options as well as mountpoints, including each application
    # namespace. A remaining overlay can reference the old store indirectly.
    mountinfo = [Path('/proc/self/mountinfo')]
    mountinfo.extend(Path('/proc', str(row['State']['Pid']), 'mountinfo') for row in rows.values()
                     if row['State']['Running'])
    for path in mountinfo:
        M.require(str(M.S.SOURCE) not in path.read_text(), 'original_store_still_referenced_by_mount')


def archive(maintenance, copy=False):
    M.require(not os.path.lexists(maintenance.directory / 'original-store-retirement.json'),
              'source_retirement_recorded_use_maintenance_status')
    destination = maintenance.directory / 'original-store-archive'
    receipt_path = maintenance.directory / 'original-store-archive.json'
    active_check(maintenance)
    M.rsync_capable(maintenance.read(['rsync', '--version']))
    M.require(not M.active_store_copy(), 'previous_store_copy_process_is_running')
    source = M.S.capacity(M.S.SOURCE)
    capacity = M.S.capacity(maintenance.directory)
    M.require(source['filesystem_device'] != capacity['filesystem_device']
              and capacity['filesystem_device'] == M.S.TARGET.stat().st_dev, 'archive_must_use_data_filesystem')
    # The old store occupies only part of this 50 GiB filesystem. Reserve its
    # whole capacity conservatively, without scanning either live data store.
    M.require(capacity['available_bytes'] >= source['total_bytes'] + M.S.RESERVE,
              'archive_space_reserve_unavailable')
    if receipt_path.exists():
        receipt = json.loads(M.private_file(receipt_path))
        M.require(receipt['format'] == FORMAT and receipt['plan_sha256'] == maintenance.plan_hash
                  and receipt['source_identity'] == maintenance.plan['source_identity']
                  and receipt['archive_identity'] == identity(destination)
                  and type(receipt['copy_verified']) is bool, 'archive_receipt_does_not_match')
    else:
        M.require(not os.path.lexists(destination), 'unowned_archive_directory_exists')
        receipt = None
    result = {'status': 'archive_copy_ready', 'archive_directory': str(destination),
              'available_bytes': capacity['available_bytes'], 'reserve_bytes': M.S.RESERVE,
              'services_changed': False, 'source_removed': False, 'application_upgrade_performed': False}
    if not copy:
        return result
    if receipt is None:
        destination.mkdir(mode=0o700)
        receipt = {'format': FORMAT, 'plan_sha256': maintenance.plan_hash,
                   'source_identity': maintenance.plan['source_identity'], 'archive_identity': identity(destination),
                   'copy_verified': False}
    receipt['copy_verified'] = False
    M.write_atomic(receipt_path, M.encoded(receipt))
    M.PHASE = 'archiving_original_store'
    print(json.dumps({'phase': M.PHASE}), flush=True)
    maintenance.long(commands(destination), timeout=28800, reserve=M.S.RESERVE)
    M.PHASE = 'verifying_original_store_archive'
    print(json.dumps({'phase': M.PHASE}), flush=True)
    log = maintenance.long(commands(destination, verify=True), timeout=28800, reserve=M.S.RESERVE)
    M.require(log.stat().st_size == 0, 'archive_checksum_or_metadata_differences')
    maintenance.long(['sync', '-f', str(destination)], timeout=300)
    active_check(maintenance)
    M.require(receipt['archive_identity'] == identity(destination), 'archive_directory_changed')
    receipt['copy_verified'] = True
    M.write_atomic(receipt_path, M.encoded(receipt))
    return {**result, 'status': 'original_store_archive_verified', 'receipt': str(receipt_path),
            'available_bytes': M.S.capacity(maintenance.directory)['available_bytes'],
            'note': 'The original 30 GiB store remains on the root disk. This verified archive is a stale pre-resume store, not a current database or container backup. Removal is a separate operation.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--copy', action='store_true')
    parser.add_argument('--state', type=Path, required=True)
    args = parser.parse_args()
    try:
        M.require(sys.platform.startswith('linux') and os.geteuid() == 0, 'linux_root_required')
        if args.copy:
            signal.signal(signal.SIGHUP, M.interrupted)
            signal.signal(signal.SIGTERM, M.interrupted)
        M.load_plan(args.state)
        with M.global_lock(args.copy), open(args.state, 'rb') as lock:
            fcntl.flock(lock, (fcntl.LOCK_EX if args.copy else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            result = archive(M.Maintenance(args.state), copy=args.copy)
        print(json.dumps(result, indent=2))
        return 0
    except BaseException as error:
        if isinstance(error, SystemExit):
            raise
        reason = str(error) if isinstance(error, M.S.PlanError) else 'archive_check_failed_raw_values_suppressed'
        print(json.dumps({'status': 'stopped', 'stage': M.PHASE, 'reason_code': reason,
                          'services_changed': False, 'source_removed': False, 'raw_values_suppressed': True}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
