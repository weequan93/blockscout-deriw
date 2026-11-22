#!/usr/bin/env python3
"""Prepare or end one bounded native/autovacuum overlap trial."""
from datetime import datetime, timedelta, timezone
import argparse
import json
import os
from pathlib import Path
import re
import sys

import dev_config as D
import release as R
import v10_upgrade as U

ROOT = Path(__file__).resolve().parents[2]
FORMAT = 'blockscout-dev-native-overlap-trial-v1'
DEFAULT_DEAD_ROWS = 10000000
MIN_START_FREE = 400 * 1024**3
LIMITS = {'max_dead_rows': 20000000, 'duration_seconds': 1800,
          'max_free_drop_bytes': 8 * 1024**3}
ENDED = ('expired', 'space_budget', 'clock_invalid', 'operator_ended')
STOP_FILE = 'overlap-trial-stop.request'
PREPARED = 'native_overlap_trial_prepared'


def now_utc():
    return datetime.now(timezone.utc)


def stamp(value):
    try:
        result = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        raise U.UpgradeError('Invalid overlap-trial timestamp') from None
    U.require(result.tzinfo is not None and result.utcoffset() == timedelta(0),
              'Overlap-trial timestamps must be UTC')
    return result


def load_trial(path, state):
    filename = state.get('overlap_trial_file')
    if filename is None:
        U.require('overlap_trial_sha256' not in state and 'overlap_trial_ended_reason' not in state
                  and not (path.parent / STOP_FILE).exists(), 'Incomplete overlap-trial checkpoint')
        return None
    digest = state.get('overlap_trial_sha256')
    U.require(isinstance(digest, str) and re.fullmatch(r'[a-f0-9]{64}', digest)
              and filename == f'native-overlap-trial-{digest[:12]}.json', 'Invalid overlap-trial filename')
    data = U.private(path.parent / filename)
    U.require(R.digest(data) == digest, 'Frozen overlap trial changed')
    trial = json.loads(data)
    U.require(isinstance(trial, dict) and set(trial) == {'format', 'native_inputs_sha256', 'limits', 'prepared_at_utc',
                            'expires_at_utc', 'free_bytes_at_start', 'cursor_at_start', 'dead_rows_at_start'}
              and trial['format'] == FORMAT and trial['native_inputs_sha256'] == state['inputs_sha256']
              and trial['limits'] == LIMITS
              and all(type(value) is int for value in trial['limits'].values())
              and type(trial['free_bytes_at_start']) is int and trial['free_bytes_at_start'] >= MIN_START_FREE
              and type(trial['cursor_at_start']) is int
              and 0 <= trial['cursor_at_start'] < state['inputs']['payload']['start_before_block']
              and type(trial['dead_rows_at_start']) is int and 0 <= trial['dead_rows_at_start'] <= LIMITS['max_dead_rows']
              and (state.get('worker_repair') or {}).get('format') == 'blockscout-dev-v10-native-row-chunks-v1'
              and state['worker_repair'].get('chunk_rows') == 5000
              and state.get('overlap_trial_ended_reason') in (None, *ENDED),
              'Invalid frozen overlap-trial policy')
    U.require(stamp(trial['expires_at_utc']) - stamp(trial['prepared_at_utc'])
              == timedelta(seconds=LIMITS['duration_seconds']), 'Overlap-trial duration changed')
    return trial


def decision(trial, free, ended=None, at=None):
    if trial is None:
        return {'status': 'not_configured', 'dead_row_pause_threshold': DEFAULT_DEAD_ROWS}
    at = now_utc() if at is None else at
    drop = max(0, trial['free_bytes_at_start'] - free) if type(free) is int else None
    status = ended or (
        'clock_invalid' if at < stamp(trial['prepared_at_utc']) else
        'expired' if at >= stamp(trial['expires_at_utc']) else
        'space_budget' if drop is None or drop >= LIMITS['max_free_drop_bytes'] else 'active')
    return {'status': status,
            'dead_row_pause_threshold': LIMITS['max_dead_rows'] if status == 'active' else DEFAULT_DEAD_ROWS,
            'expires_at_utc': trial['expires_at_utc'], 'data_free_drop_bytes': drop,
            'max_free_drop_bytes': LIMITS['max_free_drop_bytes'],
            'cursor_at_start': trial['cursor_at_start'], 'dead_rows_at_start': trial['dead_rows_at_start']}


def remaining_ms(trial):
    return max(0, int((stamp(trial['expires_at_utc']) - now_utc()).total_seconds() * 1000))


def stop_content(state):
    return R.encoded({'format': FORMAT, 'native_inputs_sha256': state['inputs_sha256'],
                      'trial_sha256': state['overlap_trial_sha256']})


def stop_requested(path, state):
    request = path.parent / STOP_FILE
    if not request.exists():
        return False
    U.require(state.get('overlap_trial_file') is not None
              and U.private(request) == stop_content(state), 'Overlap-trial stop request changed')
    return True


def prepare(api, root, path):
    import v10_native_normalization as N
    state = N.load(path)
    U.require(state['phase'] == 'paused' or (state['phase'] == 'prepared' and state.get('reason') == PREPARED),
              'Pause the native controller before overlap-trial preparation')
    U.require(N.owned(api, path, state['inputs']) is None, 'The native worker must be stopped before trial preparation')
    U.require((state.get('worker_repair') or {}).get('format') == 'blockscout-dev-v10-native-row-chunks-v1',
              'Enable the reviewed 5000-row chunk worker before this trial')
    binding = N.runtime_binding(path, state)
    U.require(binding is not None and binding['source_root'] == str(root)
              and N.B.clean_source(api, root) == (binding['tool_source_commit'], '10.2.6'),
              'Rebind the job to this clean trial tool source')
    # Existing trials are immutable and cannot be renewed by repeating this command.
    existing = load_trial(path, state)
    if existing is not None:
        return {'status': 'native_overlap_trial_already_prepared', 'state': str(path),
                'overlap_trial': decision(existing, N.J.P.free_bytes(), state.get('overlap_trial_ended_reason'))}
    manifest, _, _ = api.configuration(root, 'dev')
    current = api.inspect_project(manifest)
    api.ensure_stable(binding['runtime_before'], current, 'Runtime changed during overlap-trial preparation')
    N.K.check_anchor(binding['database_anchor'], N.K.stable_database_anchor(api), progress=True)
    N.Q.verify_holds(api)
    N.current_runtime_health(api, binding)
    free = N.J.P.free_bytes()
    U.require(free >= MIN_START_FREE, 'Overlap trial requires at least 400 GiB free data space')
    code = N.METADATA.replace('NATIVE_APPLICATION_NAME', json.dumps(state['inputs']['payload']['application_name']))
    report = N.rpc(api, code)
    meta = report.get('native_meta') or {}
    saved_meta = (state.get('database') or {}).get('native_meta') or {}
    U.require(report.get('native_status') == 'started' and meta.get('v10_native_job') == state['inputs']['payload']['token']
              and type(meta.get('max_block_number')) is int and 0 <= meta['max_block_number']
              < state['inputs']['payload']['start_before_block']
              and meta.get('max_block_number') == saved_meta.get('max_block_number'),
              'Native database checkpoint changed before trial preparation')
    U.require(report.get('autovacuum_enabled') is True and type(report.get('dead_rows')) is int
              and 0 <= report['dead_rows'] <= LIMITS['max_dead_rows']
              and type(report.get('autovacuum_running')) is int and report['autovacuum_running'] >= 0,
              'Autovacuum or dead-row evidence does not qualify for the bounded trial')
    if report['dead_rows'] <= DEFAULT_DEAD_ROWS or report['autovacuum_running'] == 0:
        return {'status': 'native_overlap_trial_not_needed_now', 'state': str(path),
                'database_writes': False, 'dead_row_pause_threshold': DEFAULT_DEAD_ROWS,
                'estimated_dead_rows': report['dead_rows'],
                'vacuum_workers': report['autovacuum_running']}
    started = now_utc()
    trial = {'format': FORMAT, 'native_inputs_sha256': state['inputs_sha256'], 'limits': dict(LIMITS),
             'prepared_at_utc': started.isoformat(),
             'expires_at_utc': (started + timedelta(seconds=LIMITS['duration_seconds'])).isoformat(),
             'free_bytes_at_start': free, 'cursor_at_start': meta['max_block_number'],
             'dead_rows_at_start': report['dead_rows']}
    data = R.encoded(trial)
    digest = R.digest(data)
    filename = f'native-overlap-trial-{digest[:12]}.json'
    api.ensure_stable(current, api.inspect_project(manifest), 'Runtime changed during trial preparation')
    target = path.parent / filename
    if target.exists():
        U.require(U.private(target) == data, 'An existing overlap-trial receipt differs')
    else:
        R.write_new(target, data)
    state.update(overlap_trial_file=filename, overlap_trial_sha256=digest, free_bytes=free)
    N.save(path, state, 'prepared', PREPARED)
    return {'status': PREPARED, 'state': str(path), 'database_writes': False,
            'overlap_trial': decision(trial, free)}


def request_end(path):
    import v10_native_normalization as N
    state = N.load(path)
    U.require(load_trial(path, state) is not None, 'No overlap trial is configured')
    N.require_service(path)
    request = path.parent / STOP_FILE
    try:
        R.write_new(request, stop_content(state))
    except FileExistsError:
        pass
    stop_requested(path, state)
    return {'status': 'native_overlap_trial_stop_requested', 'state': str(path),
            'default_dead_row_pause_threshold': DEFAULT_DEAD_ROWS}


def main(argv=None):
    import v10_native_normalization as N
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--prepare', action='store_true')
    mode.add_argument('--end', action='store_true')
    parser.add_argument('--state', required=True, type=Path)
    args = parser.parse_args(argv)
    U.require(os.geteuid() == 0 and sys.platform.startswith('linux'), 'Run on the dev Linux server as root')
    path = args.state.resolve(strict=True)
    if args.prepare:
        with N.M.global_lock(True), N.B.build_lock(D, D.MANAGED_RELEASES, ROOT):
            result = prepare(D, ROOT, path)
    else:
        # Do not wait on the running controller's global lock or overwrite its state.
        result = request_end(path)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'status': 'stopped', 'raw_values_suppressed': True,
                          'reason': str(error) if isinstance(error, D.ConfigError)
                          else 'Overlap-trial preparation failed; retain private files'}))
        raise SystemExit(2)
