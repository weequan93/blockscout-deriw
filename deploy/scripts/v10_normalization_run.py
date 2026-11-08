#!/usr/bin/env python3
"""Run a finite normalization pilot with durable checkpoints and shared budgets."""
from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid

import candidate_build as B
import containerd_maintenance as M
import dev_config as D
import release as R
import v10_normalization_batch as S
import v10_normalization_batch_update as C
import v10_normalization_pilot as P
import v10_upgrade as U

ROOT = Path(__file__).resolve().parents[2]
BASE = Path('/data/blockscout-dev-private/v10-normalization-runs')
FORMAT = 'blockscout-dev-v10-normalization-run-v1'
WAL_BUDGET = 128 * 1024**2
NEXT_BATCH_WAL_RESERVE = 16 * 1024**2
MAX_BATCHES = 25
MAX_ROWS = 25000
START_WINDOW_SECONDS = 600
PHASES = {'prepared', 'sampling', 'sampled', 'preparing_batch', 'batch_prepared', 'applying_batch', 'running', 'finished'}
STAGE = 'preflight'


class RunLimit(U.UpgradeError):
    pass


def totals(state):
    entries = state['batches']
    return sum(e['rows'] for e in entries), sum(e['charged_wal_bytes'] for e in entries)


def public(path, state, status):
    rows, charged = totals(state)
    available = P.free_bytes()
    pending = state.get('pending') or {}
    measurements = [e['observed_wal_bytes'] for e in state['batches']]
    updates = [e['updated_rows'] for e in state['batches']]
    return {'status': status, 'state': str(path), 'environment': 'dev', 'recorded_phase': state['phase'],
            'stop_reason': state.get('stop_reason'), 'maximum_batches': state['inputs']['max_batches'],
            'maximum_selected_rows': state['inputs']['max_rows'], 'completed_batches': len(state['batches']),
            'selected_rows_verified': rows, 'updated_rows_observed': sum(updates) if all(v is not None for v in updates) else None,
            'observed_chunk_wal_sum_bytes': sum(v for v in measurements if v is not None),
            'wal_measurements_complete': all(v is not None for v in measurements),
            'charged_chunk_wal_bytes': charged, 'chunk_wal_budget_bytes': WAL_BUDGET,
            'minimum_next_batch_wal_bytes': state['inputs'].get('minimum_next_batch_wal_bytes', 0),
            'new_batch_start_window_seconds': START_WINDOW_SECONDS,
            'starting_data_free_bytes': state['inputs']['space_before'], 'observed_data_free_bytes': available,
            'observed_net_data_consumption_bytes': max(0, state['inputs']['space_before'] - available),
            'batch_results': [{k: e[k] for k in ('batch_state', 'rows', 'oldest_block', 'updated_rows', 'elapsed_ms',
                                                'observed_wal_bytes', 'charged_wal_bytes')} for e in state['batches']],
            'last_verified_batch_state': state['after_state'],
            'next_before_block': state['batches'][-1]['oldest_block'] if state['batches'] else state['inputs']['start_before_block'],
            'pending_batch_state': pending.get('batch_state'), 'pending_selected_rows': pending.get('rows'),
            'writes_may_have_started': state['writes_may_have_started'],
            **({'blocked_sample': state['blocked_sample']} if state.get('blocked_sample') else {}),
            'automatic_background_migrations_held': True, 'migration_completion_records_unchanged': True,
            'application_restarted': False, 'historical_rpc_used': False, 'global_normalization_complete': False,
            'scope': 'Finite stored-row pilot. Each batch commits separately; the cursor advances only after verification. Chunk WAL includes concurrent writers and excludes final commit records and gaps between chunks. Unknown committed measurements consume the remaining chunk budget. No global worker, heavy index, PK change or completion record.'}


def save(path, state, phase):
    global STAGE
    STAGE = phase
    state['phase'] = phase
    R.update_state(path, state)
    print(json.dumps({'phase': phase, 'state': str(path), 'completed_batches': len(state['batches']),
                      'pending_batch_state': (state.get('pending') or {}).get('batch_state')}), flush=True)


def limit_reason(state, next_rows=0):
    rows, charged = totals(state)
    if len(state['batches']) >= state['inputs']['max_batches']:
        return 'batch_limit_reached'
    if rows >= state['inputs']['max_rows']:
        return 'row_limit_reached'
    if rows + next_rows > state['inputs']['max_rows']:
        return 'next_batch_exceeds_remaining_rows'
    if charged >= WAL_BUDGET:
        return 'chunk_wal_budget_exhausted'
    if WAL_BUDGET - charged < state['inputs'].get('minimum_next_batch_wal_bytes', 0):
        return 'next_batch_wal_reserve_reached'
    if time.time() - state['inputs']['created_epoch'] >= START_WINDOW_SECONDS:
        return 'new_batch_start_window_expired'
    return None


def monitor(state):
    P.monitor({'space_before': state['inputs']['space_before']})


def load(api, path, manifest):
    P.require(path.is_absolute() and path.resolve() == path and path.name == 'state.json'
              and path.parent.parent == BASE and re.fullmatch(r'run-[0-9TZ-]+-[a-f0-9]{8}', path.parent.name), 'Unexpected private run path')
    P.require(path.parent.stat().st_uid == os.geteuid() and stat.S_IMODE(path.parent.stat().st_mode) == 0o700,
              'Run directory ownership/permissions changed')
    state = json.loads(U.private(path))
    frozen = U.private(path.parent / 'inputs.json')
    inputs = json.loads(frozen)
    P.require(R.digest(frozen) == state['inputs_sha256'] and inputs == state['inputs'], 'Frozen run inputs changed')
    P.require(state.get('format') == FORMAT and inputs.get('environment') == 'dev' and inputs.get('chain_id') == manifest['chain_id']
              and state.get('phase') in PHASES and type(state.get('writes_may_have_started')) is bool,
              'Invalid bounded run state')
    P.require(type(inputs.get('max_batches')) is int and 1 <= inputs['max_batches'] <= MAX_BATCHES
              and type(inputs.get('max_rows')) is int and 1 <= inputs['max_rows'] <= MAX_ROWS
              and inputs.get('wal_budget_bytes') == WAL_BUDGET and inputs.get('start_window_seconds') == START_WINDOW_SECONDS
              and type(inputs.get('minimum_next_batch_wal_bytes', 0)) is int
              and inputs.get('minimum_next_batch_wal_bytes', 0) in (0, NEXT_BATCH_WAL_RESERVE)
              and type(inputs.get('created_epoch')) in (int, float) and inputs['created_epoch'] > 0
              and type(inputs.get('space_before')) is int and inputs['space_before'] >= 200 * 1024**3,
              'Run limits changed or exceed the finite pilot caps')
    previous_path = Path(inputs['start_state'])
    previous = P.load(api, previous_path, manifest)
    P.require(R.digest(U.private(previous_path)) == inputs['start_state_sha256']
              and P.completed_cursor(previous) == inputs['start_before_block'] and previous['candidate_image'] == inputs['candidate_image'],
              'Starting normalization journal changed')
    P.require(isinstance(state.get('batches'), list) and len(state['batches']) <= inputs['max_batches'], 'Invalid batch ledger')
    rows, charged, cursor = 0, 0, inputs['start_before_block']
    for entry in state['batches']:
        child_path = Path(entry['batch_state'])
        child = P.load(api, child_path, manifest)
        sample = json.loads(U.private(child_path.parent / 'sample.json'))
        P.require(child['phase'] == 'applied' and child.get('controller_state') == str(path)
                  and child['candidate_image'] == inputs['candidate_image'] and child['script_sha256'] == inputs['script_sha256']
                  and child.get('space_before') == inputs['space_before'] and P.wal_limit(child) == WAL_BUDGET - charged
                  and sample['previous_pilot_state'] == str(previous_path) and sample['before_block'] == cursor
                  and child['expected_rows'] == entry['rows'] and child['baseline']['needs_normalization'] == entry['needed_rows']
                  and P.completed_cursor(child) == entry['oldest_block'] < cursor, 'Batch ledger target, order or budget changed')
        metrics = child.get('apply_result') or {}
        observed = metrics.get('cluster_wal_delta_bytes')
        P.require(observed is None or (type(observed) is int and 0 <= observed <= P.wal_limit(child)), 'Invalid observed chunk WAL')
        P.require(entry['observed_wal_bytes'] == observed and entry['charged_wal_bytes'] == (observed if observed is not None else P.wal_limit(child))
                  and entry['updated_rows'] == metrics.get('updated_rows') and entry['elapsed_ms'] == metrics.get('elapsed_ms'),
                  'Batch ledger measurements changed')
        rows += entry['rows']
        charged += entry['charged_wal_bytes']
        previous_path, cursor = child_path, entry['oldest_block']
    P.require(rows <= inputs['max_rows'] and charged <= WAL_BUDGET and state['after_state'] == str(previous_path), 'Run cursor or accounting changed')
    pending = state.get('pending')
    P.require(pending is None or (isinstance(pending, dict) and set(pending) == {'sample_file', 'sample_sha256', 'rows', 'needed_rows', 'batch_state'}),
              'Invalid pending batch checkpoint')
    if pending:
        sample, _, _ = C.receipt(Path(pending['sample_file']), pending['sample_sha256'], manifest)
        P.require(sample['previous_pilot_state'] == state['after_state'] and sample['before_block'] == cursor
                  and sample['selected_rows'] == pending['rows'] and sample['rows_needing_normalization'] == pending['needed_rows'],
                  'Pending sample no longer matches the verified cursor')
        if pending.get('batch_state'):
            child = P.load(api, Path(pending['batch_state']), manifest)
            P.require(child['phase'] in ('prepared', 'write_requested', 'applied') and child.get('controller_state') == str(path)
                      and child['sample_sha256'] == pending['sample_sha256'] and child['expected_rows'] == pending['rows']
                      and child['candidate_image'] == inputs['candidate_image'] and child['script_sha256'] == inputs['script_sha256']
                      and child.get('space_before') == inputs['space_before'] and P.wal_limit(child) == WAL_BUDGET - charged
                      and rows + pending['rows'] <= inputs['max_rows'] and len(state['batches']) < inputs['max_batches'],
                      'Pending batch target, ownership or budget changed')
    return state


def prepare(api, root, after_path, max_batches, max_rows):
    P.require(type(max_batches) is int and 1 <= max_batches <= MAX_BATCHES and type(max_rows) is int and 1 <= max_rows <= MAX_ROWS,
              'Choose at most 25 batches and 25000 selected rows')
    manifest, _, _ = api.configuration(root, 'dev')
    previous = P.load(api, after_path, manifest)
    cursor = P.completed_cursor(previous)
    verified = P.status(api, root, after_path, manifest)
    P.require(verified['status'] == 'verified' and verified['rows_needing_normalization'] == 0, 'Starting normalization result no longer verifies')
    source = B.clean_source(api, root)
    P.require(source[1] == '10.2.6', 'Run source version changed')
    inputs = {'environment': 'dev', 'chain_id': manifest['chain_id'], 'start_state': str(after_path),
              'start_state_sha256': R.digest(U.private(after_path)), 'start_before_block': cursor,
              'candidate_image': previous['candidate_image'], 'tool_source_commit': source[0],
              'script_sha256': R.digest((root / 'deploy/scripts/v10/normalization_pilot.exs').read_bytes()),
              'max_batches': max_batches, 'max_rows': max_rows, 'wal_budget_bytes': WAL_BUDGET,
              'minimum_next_batch_wal_bytes': NEXT_BATCH_WAL_RESERVE,
              'start_window_seconds': START_WINDOW_SECONDS, 'created_epoch': time.time(), 'space_before': P.free_bytes()}
    P.require(inputs['space_before'] >= 200 * 1024**3, 'Less than 200 GiB data free')
    B.validate_state_directory(api, BASE, root)
    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = BASE / ('run-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8])
    directory.mkdir(mode=0o700)
    path = directory / 'state.json'
    R.write_new(directory / 'inputs.json', R.encoded(inputs))
    state = {'format': FORMAT, 'phase': 'prepared', 'inputs': inputs, 'inputs_sha256': R.digest(R.encoded(inputs)),
             'after_state': str(after_path), 'batches': [], 'pending': None, 'writes_may_have_started': False}
    R.write_new(path, R.encoded(state))
    return public(path, state, 'bounded_normalization_run_prepared')


def finish(path, state, reason):
    state['stop_reason'] = reason
    save(path, state, 'finished')
    return public(path, state, 'needs_review' if reason == 'sample_needs_review' else 'bounded_run_finished')


def execute(api, root, path, manifest):
    state = load(api, path, manifest)
    if state['phase'] == 'finished':
        return status(api, root, path, manifest)
    P.require(B.clean_source(api, root) == (state['inputs']['tool_source_commit'], '10.2.6'), 'Resume from the run creation commit')
    while True:
        pending = state.get('pending')
        if not pending or not pending.get('batch_state'):
            reason = limit_reason(state, pending['rows'] if pending else 0)
            if reason:
                return finish(path, state, reason)
            monitor(state)
            if pending is None:
                save(path, state, 'sampling')
                sampled = S.sample(api, root, Path(state['after_state']))
                if sampled['blockers']:
                    state['blocked_sample'] = {k: sampled[k] for k in ('sample_file', 'sample_sha256', 'blockers', 'older_non_null_range_exhausted')}
                    return finish(path, state, 'sample_needs_review')
                pending = {'sample_file': sampled['sample_file'], 'sample_sha256': sampled['sample_sha256'],
                           'rows': sampled['selected_rows'], 'needed_rows': sampled['rows_needing_normalization'], 'batch_state': None}
                state['pending'] = pending
                save(path, state, 'sampled')
            reason = limit_reason(state, pending['rows'])
            if reason:
                return finish(path, state, reason)
            save(path, state, 'preparing_batch')
            _, charged = totals(state)
            prepared = C.prepare(api, root, Path(pending['sample_file']), pending['sample_sha256'],
                                 limits={'wal_limit_bytes': WAL_BUDGET - charged,
                                         'space_before': state['inputs']['space_before'], 'controller_state': str(path)})
            pending['batch_state'] = prepared['state']
            save(path, state, 'batch_prepared')
            # Validate the durable pointer and frozen child limits before writes.
            state = load(api, path, manifest)
            pending = state['pending']
        child_path = Path(pending['batch_state'])
        child = P.load(api, child_path, manifest)
        active = api.run(['docker', 'ps', '-q', '--filter', 'name=^/' + child['client_name'] + '$']).split()
        if active:
            return public(path, state, 'client_in_progress')
        def before_update():
            reason = limit_reason(state, pending['rows'])
            if reason:
                raise RunLimit(reason)
            monitor(state)
            P.require(B.clean_source(api, root) == (state['inputs']['tool_source_commit'], '10.2.6'), 'Run source changed before a write')
        state['writes_may_have_started'] = True
        save(path, state, 'applying_batch')
        try:
            result = P.apply(api, root, child_path, manifest, resume=True, before_update=before_update)
        except RunLimit as error:
            return finish(path, state, str(error))
        P.require(result['status'] == 'verified' and result['rows_needing_normalization'] == 0, 'Batch verification incomplete; retain the pending journal')
        child = P.load(api, child_path, manifest)
        P.require(child['phase'] == 'applied', 'Record an applied batch before advancing its cursor')
        metrics = child.get('apply_result') or {}
        observed = metrics.get('cluster_wal_delta_bytes')
        state['batches'].append({'batch_state': str(child_path), 'rows': child['expected_rows'],
                                 'needed_rows': child['baseline']['needs_normalization'], 'oldest_block': P.completed_cursor(child),
                                 'observed_wal_bytes': observed, 'charged_wal_bytes': observed if observed is not None else P.wal_limit(child),
                                 'updated_rows': metrics.get('updated_rows'), 'elapsed_ms': metrics.get('elapsed_ms')})
        state['after_state'] = str(child_path)
        state['pending'] = None
        save(path, state, 'running')


def status(api, root, path, manifest):
    state = load(api, path, manifest)
    result = public(path, state, 'bounded_run_status')
    observed = P.status(api, root, Path(state['after_state']), manifest)
    result['last_batch_observed_status'] = observed['status']
    pending_path = (state.get('pending') or {}).get('batch_state')
    if pending_path:
        result['pending_batch_observed_status'] = P.status(api, root, Path(pending_path), manifest)['status']
    if state.get('blocked_sample'):
        result['blocked_sample'] = state['blocked_sample']
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    for name in ('prepare', 'run', 'resume', 'status'):
        modes.add_argument('--' + name, action='store_true')
    parser.add_argument('--after-state', type=Path)
    parser.add_argument('--max-batches', type=int)
    parser.add_argument('--max-rows', type=int)
    parser.add_argument('--state', type=Path)
    args = parser.parse_args()
    try:
        P.require(sys.platform.startswith('linux') and os.geteuid() == 0, 'Run on the Linux dev server as root')
        manifest, _, _ = D.configuration(ROOT, 'dev')
        with M.global_lock(not args.status), B.build_lock(D, D.MANAGED_RELEASES, ROOT):
            if args.prepare or args.run:
                P.require(args.state is None and args.after_state is not None and args.max_batches is not None and args.max_rows is not None,
                          'Provide the verified starting state and explicit batch/row limits')
                result = prepare(D, ROOT, args.after_state, args.max_batches, args.max_rows)
                args.state = Path(result['state'])
                if args.run:
                    print(json.dumps(result, indent=2), flush=True)
                    result = execute(D, ROOT, args.state, manifest)
            else:
                P.require(args.state is not None and args.after_state is None and args.max_batches is None and args.max_rows is None,
                          'Use only the saved run state for status/resume')
                result = status(D, ROOT, args.state, manifest) if args.status else execute(D, ROOT, args.state, manifest)
        print(json.dumps(result, indent=2))
        return 1 if result['status'] in ('needs_review', 'client_in_progress') else 0
    except Exception as error:
        print(json.dumps({'status': 'stopped', 'stage': STAGE, 'batch_stage': C.STAGE, 'client_stage': P.STAGE,
                          'state': str(args.state) if args.state else None, 'raw_values_suppressed': True,
                          'reason': str(error) if isinstance(error, U.UpgradeError) else 'Run check failed; inspect its server-only journal',
                          'recovery': 'Use --status and --resume with this run state. Resume reconciles its pending child before advancing. Retain all journals; global workers remain held.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
