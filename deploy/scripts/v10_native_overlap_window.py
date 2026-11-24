#!/usr/bin/env python3
"""Prepare or end one four-hour follow-up to the immutable short overlap trial."""
from datetime import timedelta
import argparse
import json
import os
from pathlib import Path
import re
import sys

import dev_config as D
import release as R
import v10_native_overlap_trial as O
import v10_upgrade as U

ROOT = Path(__file__).resolve().parents[2]
FORMAT = 'blockscout-dev-native-overlap-window-v1'
MIN_FREE = 400 * 1024**3
LIMITS = {'max_dead_rows': 20000000, 'duration_seconds': 14400,
          'max_free_drop_bytes': 16 * 1024**3, 'minimum_free_bytes': MIN_FREE}
ENDED = (*O.ENDED, 'free_space_floor', 'normalization_completed')
STOP_FILE = 'overlap-window-stop.request'
PREPARED = 'native_overlap_window_prepared'


def now_utc():
    return O.now_utc()


def load_window(path, state):
    filename = state.get('overlap_window_file')
    if filename is None:
        U.require('overlap_window_sha256' not in state and 'overlap_window_ended_reason' not in state
                  and not (path.parent / STOP_FILE).exists(), 'Incomplete overlap-window checkpoint')
        return None
    digest = state.get('overlap_window_sha256')
    U.require(isinstance(digest, str) and re.fullmatch(r'[a-f0-9]{64}', digest)
              and filename == f'native-overlap-window-{digest[:12]}.json', 'Invalid overlap-window filename')
    data = U.private(path.parent / filename)
    U.require(R.digest(data) == digest, 'Frozen overlap window changed')
    window = json.loads(data)
    previous = O.load_trial(path, state)
    U.require(previous is not None and state.get('overlap_trial_ended_reason') in O.ENDED,
              'The original short overlap trial must have a latched end')
    U.require(isinstance(window, dict) and set(window) == {
        'format', 'native_inputs_sha256', 'previous_trial_sha256', 'worker_sha256', 'limits',
        'prepared_at_utc', 'expires_at_utc', 'free_bytes_at_start', 'cursor_at_start', 'dead_rows_at_start'}
        and window['format'] == FORMAT and window['native_inputs_sha256'] == state['inputs_sha256']
        and window['previous_trial_sha256'] == state['overlap_trial_sha256']
        and window['worker_sha256'] == (state.get('worker_repair') or {}).get('script_sha256')
        and window['limits'] == LIMITS
        and all(type(value) is int for value in window['limits'].values())
        and type(window['free_bytes_at_start']) is int and window['free_bytes_at_start'] >= MIN_FREE
        and type(window['cursor_at_start']) is int
        and 0 <= window['cursor_at_start'] < state['inputs']['payload']['start_before_block']
        and type(window['dead_rows_at_start']) is int
        and 0 <= window['dead_rows_at_start'] <= LIMITS['max_dead_rows']
        and state.get('overlap_window_ended_reason') in (None, *ENDED),
        'Invalid frozen overlap-window policy')
    U.require(O.stamp(window['expires_at_utc']) - O.stamp(window['prepared_at_utc'])
              == timedelta(seconds=LIMITS['duration_seconds'])
              and O.stamp(window['prepared_at_utc']) >= O.stamp(previous['prepared_at_utc']),
              'Overlap-window duration or ordering changed')
    return window


def decision(window, free, ended=None, at=None):
    if window is None:
        return {'status': 'not_configured', 'dead_row_pause_threshold': O.DEFAULT_DEAD_ROWS}
    at = now_utc() if at is None else at
    drop = max(0, window['free_bytes_at_start'] - free) if type(free) is int else None
    status = ended or (
        'clock_invalid' if at < O.stamp(window['prepared_at_utc']) else
        'expired' if at >= O.stamp(window['expires_at_utc']) else
        'free_space_floor' if type(free) is not int or free < MIN_FREE else
        'space_budget' if drop >= LIMITS['max_free_drop_bytes'] else 'active')
    return {'status': status,
            'dead_row_pause_threshold': LIMITS['max_dead_rows'] if status == 'active' else O.DEFAULT_DEAD_ROWS,
            'expires_at_utc': window['expires_at_utc'], 'data_free_drop_bytes': drop,
            'max_free_drop_bytes': LIMITS['max_free_drop_bytes'], 'minimum_free_bytes': MIN_FREE,
            'cursor_at_start': window['cursor_at_start'], 'dead_rows_at_start': window['dead_rows_at_start']}


def remaining_ms(window):
    return max(0, int((O.stamp(window['expires_at_utc']) - now_utc()).total_seconds() * 1000))


def stop_content(state):
    return R.encoded({'format': FORMAT, 'native_inputs_sha256': state['inputs_sha256'],
                      'window_sha256': state['overlap_window_sha256']})


def stop_requested(path, state):
    request = path.parent / STOP_FILE
    if not request.exists():
        return False
    U.require(state.get('overlap_window_file') is not None
              and U.private(request) == stop_content(state), 'Overlap-window stop request changed')
    return True


def prepare(api, root, path):
    import v10_native_normalization as N
    state = N.load(path)
    U.require(state['phase'] == 'paused' or (state['phase'] == 'prepared' and state.get('reason') == PREPARED),
              'Pause the native controller before overlap-window preparation')
    U.require(N.owned(api, path, state['inputs']) is None, 'The native worker must be stopped before window preparation')
    binding = N.runtime_binding(path, state)
    U.require(binding is not None and binding['source_root'] == str(root)
              and N.B.clean_source(api, root) == (binding['tool_source_commit'], '10.2.6'),
              'Rebind the job to this clean window tool source')
    previous = O.load_trial(path, state)
    U.require(previous is not None and state.get('overlap_trial_ended_reason') in O.ENDED,
              'The original short overlap trial must have a latched end')
    existing = load_window(path, state)
    if existing is not None:
        return {'status': 'native_overlap_window_already_prepared', 'state': str(path),
                'overlap_window': decision(existing, N.J.P.free_bytes(), state.get('overlap_window_ended_reason'))}
    manifest, _, _ = api.configuration(root, 'dev')
    current = api.inspect_project(manifest)
    api.ensure_stable(binding['runtime_before'], current, 'Runtime changed during overlap-window preparation')
    N.K.check_anchor(binding['database_anchor'], N.K.stable_database_anchor(api), progress=True)
    N.Q.verify_holds(api)
    N.current_runtime_health(api, binding)
    free = N.J.P.free_bytes()
    U.require(free >= MIN_FREE, 'Overlap window requires at least 400 GiB free data space')
    code = N.METADATA.replace('NATIVE_APPLICATION_NAME', json.dumps(state['inputs']['payload']['application_name']))
    report = N.rpc(api, code)
    meta = report.get('native_meta') or {}
    saved_meta = (state.get('database') or {}).get('native_meta') or {}
    U.require(report.get('native_status') == 'started' and meta.get('v10_native_job') == state['inputs']['payload']['token']
              and type(meta.get('max_block_number')) is int and 0 <= meta['max_block_number']
              < state['inputs']['payload']['start_before_block']
              and meta.get('max_block_number') == saved_meta.get('max_block_number'),
              'Native database checkpoint changed before window preparation')
    U.require(report.get('autovacuum_enabled') is True and type(report.get('dead_rows')) is int
              and 0 <= report['dead_rows'] <= LIMITS['max_dead_rows']
              and type(report.get('autovacuum_running')) is int and report['autovacuum_running'] >= 0,
              'Autovacuum or dead-row evidence does not qualify for the bounded window')
    if report['dead_rows'] <= O.DEFAULT_DEAD_ROWS or report['autovacuum_running'] == 0:
        return {'status': 'native_overlap_window_not_needed_now', 'state': str(path),
                'database_writes': False, 'dead_row_pause_threshold': O.DEFAULT_DEAD_ROWS,
                'estimated_dead_rows': report['dead_rows'], 'vacuum_workers': report['autovacuum_running']}
    started = now_utc()
    U.require(started >= O.stamp(previous['prepared_at_utc']), 'Clock precedes the original overlap trial')
    window = {'format': FORMAT, 'native_inputs_sha256': state['inputs_sha256'],
              'previous_trial_sha256': state['overlap_trial_sha256'],
              'worker_sha256': state['worker_repair']['script_sha256'], 'limits': dict(LIMITS),
              'prepared_at_utc': started.isoformat(),
              'expires_at_utc': (started + timedelta(seconds=LIMITS['duration_seconds'])).isoformat(),
              'free_bytes_at_start': free, 'cursor_at_start': meta['max_block_number'],
              'dead_rows_at_start': report['dead_rows']}
    data = R.encoded(window)
    digest = R.digest(data)
    filename = f'native-overlap-window-{digest[:12]}.json'
    api.ensure_stable(current, api.inspect_project(manifest), 'Runtime changed during window preparation')
    target = path.parent / filename
    if target.exists():
        U.require(U.private(target) == data, 'An existing overlap-window receipt differs')
    else:
        R.write_new(target, data)
    state.update(overlap_window_file=filename, overlap_window_sha256=digest, free_bytes=free)
    N.save(path, state, 'prepared', PREPARED)
    return {'status': PREPARED, 'state': str(path), 'database_writes': False,
            'overlap_window': decision(window, free)}


def request_end(path):
    import v10_native_normalization as N
    state = N.load(path)
    U.require(load_window(path, state) is not None, 'No overlap window is configured')
    N.require_service(path)
    try:
        R.write_new(path.parent / STOP_FILE, stop_content(state))
    except FileExistsError:
        pass
    stop_requested(path, state)
    return {'status': 'native_overlap_window_stop_requested', 'state': str(path),
            'default_dead_row_pause_threshold': O.DEFAULT_DEAD_ROWS}


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
        result = request_end(path)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'status': 'stopped', 'raw_values_suppressed': True,
                          'reason': str(error) if isinstance(error, D.ConfigError)
                          else 'Overlap-window preparation failed; retain private files'}))
        raise SystemExit(2)
