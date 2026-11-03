#!/usr/bin/env python3
"""Reclaim root space by clearing the verified, archived original store only.

--check reads metadata only. --apply revalidates the archive, then removes the
contents of /var/lib/containerd while retaining that directory's identity.
The archive, active store, databases and services are preserved.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import sys

import archive_containerd_source as A

M = A.M
FORMAT = 'blockscout-original-store-retirement-v1'


def active_removal():
    for pid in M.process_ids('find', exact=True):
        try:
            arguments = Path('/proc', str(pid), 'cmdline').read_bytes().decode().rstrip('\0').split('\0')
            if '-delete' in arguments and str(M.S.SOURCE) in arguments:
                return True
        except FileNotFoundError:
            pass
    return False


def removal_command():
    # GNU find defaults to -P: it never follows directory symlinks. Keep the
    # source directory itself, preserve its inode for the maintenance audit,
    # and refuse traversal into a different filesystem.
    return ['find', '-P', str(M.S.SOURCE), '-xdev', '-mindepth', '1', '-depth', '-delete']


def receipt(maintenance):
    destination = maintenance.directory / 'original-store-archive'
    raw = M.private_file(maintenance.directory / 'original-store-archive.json')
    saved = json.loads(raw)
    M.require(saved['format'] == A.FORMAT and saved['plan_sha256'] == maintenance.plan_hash
              and saved['source_identity'] == maintenance.plan['source_identity']
              and saved['archive_identity'] == A.identity(destination)
              and saved['copy_verified'] is True, 'verified_archive_required_before_source_removal')
    M.require(destination.stat().st_dev == M.S.TARGET.stat().st_dev
              and destination.stat().st_dev != M.S.SOURCE.stat().st_dev,
              'archive_must_be_on_active_data_filesystem')
    return saved, M.S.digest(raw), destination


def retire(maintenance, apply=False):
    A.active_check(maintenance)
    M.require(not active_removal() and not M.active_store_copy(), 'previous_source_operation_is_running')
    saved, archive_hash, destination = receipt(maintenance)
    record = maintenance.source_retirement()
    result = {'status': 'source_retirement_ready', 'source': str(M.S.SOURCE),
              'archive_directory': str(destination), 'active_store': str(M.S.TARGET),
              'services_changed': False, 'application_upgrade_performed': False,
              'effect': 'Apply removes only the archived old-store contents. The original directory remains empty; the archive and active store remain intact.'}
    if record and record['phase'] == 'complete':
        return {**result, 'status': 'source_retirement_verified', 'source_contents_removed': True}
    if not apply:
        return {**result, 'resume_partial_removal': bool(record and record['phase'] == 'removing')}
    M.require(M.S.capacity(maintenance.directory)['available_bytes'] >= M.S.RESERVE, 'data_space_reserve_unavailable')
    path = maintenance.directory / 'original-store-retirement.json'
    if record is None or record['phase'] == 'prepared':
        M.PHASE = 'rechecking_original_store_archive'
        print(json.dumps({'phase': M.PHASE}), flush=True)
        log = maintenance.long(A.commands(destination, verify=True), timeout=28800, reserve=M.S.RESERVE)
        M.require(log.stat().st_size == 0, 'archive_changed_source_removal_refused')
        maintenance.long(['sync', '-f', str(destination)], timeout=300)
        record = {'format': FORMAT, 'plan_sha256': maintenance.plan_hash,
                  'source_identity': maintenance.plan['source_identity'], 'archive_identity': saved['archive_identity'],
                  'archive_receipt_sha256': archive_hash, 'phase': 'prepared'}
        M.write_atomic(path, M.encoded(record))
    # A partial deletion can be retried using the durable verification record;
    # do not compare or copy its incomplete source back over the full archive.
    A.active_check(maintenance)
    M.require(receipt(maintenance)[1] == record['archive_receipt_sha256'], 'archive_receipt_changed')
    record['phase'] = 'removing'
    M.write_atomic(path, M.encoded(record))
    M.PHASE = 'clearing_archived_original_store'
    print(json.dumps({'phase': M.PHASE}), flush=True)
    maintenance.long(removal_command(), timeout=28800)
    M.require(not any(M.S.SOURCE.iterdir()), 'original_store_removal_incomplete')
    maintenance.long(['sync', '-f', str(M.S.SOURCE)], timeout=300)
    A.active_check(maintenance)
    record['phase'] = 'complete'
    M.write_atomic(path, M.encoded(record))
    status = maintenance.status()
    M.require(status['status'] == 'verified', 'post_cleanup_status_needs_attention')
    return {**result, 'status': 'source_retirement_verified', 'source_contents_removed': True,
            'archive_retained': True, 'root_capacity': M.S.capacity(M.S.SOURCE),
            'data_capacity': M.S.capacity(maintenance.directory),
            'backend_readiness_ok': status['backend_readiness_ok'],
            'nft_connection_and_queue_ok': status['nft_connection_and_queue_ok']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--apply', action='store_true')
    parser.add_argument('--state', type=Path, required=True)
    args = parser.parse_args()
    try:
        M.require(sys.platform.startswith('linux') and os.geteuid() == 0, 'linux_root_required')
        if args.apply:
            signal.signal(signal.SIGHUP, M.interrupted)
            signal.signal(signal.SIGTERM, M.interrupted)
        M.load_plan(args.state)
        with M.global_lock(args.apply), open(args.state, 'rb') as lock:
            fcntl.flock(lock, (fcntl.LOCK_EX if args.apply else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            result = retire(M.Maintenance(args.state), apply=args.apply)
        print(json.dumps(result, indent=2))
        return 0
    except BaseException as error:
        if isinstance(error, SystemExit):
            raise
        reason = str(error) if isinstance(error, M.S.PlanError) else 'retirement_failed_raw_values_suppressed'
        print(json.dumps({'status': 'stopped', 'stage': M.PHASE, 'reason_code': reason,
                          'services_changed': False, 'raw_values_suppressed': True,
                          'recovery': 'Keep the archive intact. Run maintenance --status. Apply can retry an interrupted removal using its saved record; never rerun the archive copy after removal begins.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
