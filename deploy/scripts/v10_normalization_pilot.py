#!/usr/bin/env python3
"""Prepare/apply one bounded v10 normalization block without starting a worker."""
from copy import deepcopy
from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid

import candidate_build as B
import containerd_maintenance as M
import dev_config as D
import release as R
import v10_normalization_plan as N
import v10_upgrade as U

ROOT = Path(__file__).resolve().parents[2]
BASE = Path('/data/blockscout-dev-private/v10-normalization')
FORMAT = 'blockscout-dev-v10-normalization-pilot-v1'
BATCH_FORMAT = 'blockscout-dev-v10-normalization-batch-v1'
WAL_LIMIT = 256 * 1024**2
STAGE = 'preflight'


def require(ok, reason):
    U.require(ok, reason)


def free_bytes():
    stats = os.statvfs('/data')
    return stats.f_bavail * stats.f_frsize


def monitor(state):
    free = free_bytes()
    require(free >= 200 * 1024**3 and state.get('space_before', free) - free <= 512 * 1024**2,
            'Pilot free-space/consumption limit reached')


def baseline(result):
    snapshot = result.get('snapshot') or {}
    rows = snapshot.get('rows')
    require(isinstance(rows, list) and 0 < len(rows) <= 1000 and all(isinstance(r, dict) for r in rows), 'Invalid bounded snapshot')
    require(len(R.encoded(rows)) <= 16 * 1024**2, 'Snapshot exceeds the pilot size limit')
    for name in ('full_hashes', 'semantic_hashes'):
        values = snapshot.get(name)
        require(isinstance(values, list) and len(values) == len(rows)
                and all(isinstance(v, str) and re.fullmatch(r'[a-f0-9]{32}', v) for v in values), 'Snapshot hashes unavailable')
    require(type(snapshot.get('needs_normalization')) is int and 0 <= snapshot['needs_normalization'] <= len(rows),
            'Invalid normalization count')
    return {**snapshot, 'statuses': result['statuses']}


def block_rows(value):
    """Validate a descending manifest of complete blocks under the row cap."""
    require(isinstance(value, list) and 0 < len(value) <= 1000, 'Invalid bounded block manifest')
    previous, total = 2**63, 0
    for row in value:
        require(isinstance(row, dict) and set(row) == {'block', 'rows'}
                and type(row['block']) is int and 0 <= row['block'] < previous
                and type(row['rows']) is int and 0 < row['rows'] <= 1000, 'Invalid complete block/count')
        previous = row['block']
        total += row['rows']
    require(total <= 1000, 'Batch exceeds 1000 rows')
    return total


def check_block_rows(state, snapshot):
    if state.get('format') == BATCH_FORMAT:
        counts = {}
        for row in snapshot['rows']:
            height = row.get('block_number')
            require(type(height) is int, 'Snapshot block identity missing')
            counts[height] = counts.get(height, 0) + 1
        require(counts == {r['block']: r['rows'] for r in state['block_rows']}, 'Selected per-block counts changed')


def callback_plan(result):
    plan = result.get('callback_plan')
    if plan is None:
        return None
    require(isinstance(plan, dict) and set(plan) == {'access', 'index', 'planned_rows', 'estimated_total_cost'}
            and plan['access'] in ('Index Scan', 'Index Only Scan', 'Bitmap Heap Scan')
            and isinstance(plan['index'], str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}', plan['index'])
            and type(plan['planned_rows']) is int and plan['planned_rows'] >= 0
            and type(plan['estimated_total_cost']) in (int, float) and plan['estimated_total_cost'] >= 0,
            'Invalid callback plan metadata')
    return dict(plan)


def public(path, state, result=None):
    observed = baseline(result) if result else state['baseline']
    metrics = state.get('apply_result') or {}
    original = state['baseline']['rows']
    transforms = {
        'legacy_trace_paths_to_clear': sum(r.get('trace_address') is not None for r in original),
        'zero_values_to_store_as_null': sum(r.get('value') == 0 for r in original),
        'legacy_call_types_to_convert': sum(r.get('call_type_enum') is None and r.get('call_type') is not None for r in original),
        'legacy_error_messages_to_relocate': sum(r.get('error') is not None for r in original),
        'created_contract_destinations_to_fill': sum(r.get('created_contract_address_hash') is not None and r.get('to_address_hash') is None for r in original),
    }
    batch = state.get('format') == BATCH_FORMAT
    target = ({'selected_block_count': len(state['block_rows']), 'selected_rows': state['expected_rows'],
               'newest_selected_block': state['block_rows'][0]['block'], 'oldest_selected_block': state['block_rows'][-1]['block'],
               'sample_sha256': state['sample_sha256']} if batch else
              {'block': state['block'], 'rows_in_block': state['expected_rows']})
    plan = callback_plan(result or state.get('apply_result') or state)
    return {'state': str(path), 'environment': 'dev', 'planned_transform_counts': transforms, **target,
            **({'callback_plan': plan} if plan else {}),
            'rows_needing_normalization': observed['needs_normalization'], 'recorded_phase': state['phase'],
            'writes_may_have_started': state['writes_may_have_started'],
            'updated_rows': metrics.get('updated_rows'), 'update_elapsed_ms': metrics.get('elapsed_ms'),
            'observed_cluster_wal_delta_bytes': metrics.get('cluster_wal_delta_bytes'), 'wal_limit_bytes': WAL_LIMIT,
            'automatic_background_migrations_held': True, 'migration_completion_records_unchanged': True,
            'application_restarted': False, 'historical_rpc_used': False,
            'scope': ('Saved complete-block sample only. ' if batch else 'One selected stored block. ') +
                     'Cluster WAL delta includes concurrent writers and excludes the final commit record; it is not a per-row or full-table forecast. Global normalization/index/PK work remains held.'}


def write_state(path, state, phase):
    global STAGE
    STAGE = phase
    state['phase'] = phase
    R.update_state(path, state)
    print(json.dumps({'phase': phase, 'state': str(path)}), flush=True)


def load(api, path, manifest):
    require(path.is_absolute() and path.resolve() == path and path.name == 'state.json' and path.parent.parent == BASE
            and re.fullmatch(r'(pilot|batch)-[0-9TZ-]+-[a-f0-9]{8}', path.parent.name), 'Unexpected private normalization path')
    require(path.parent.stat().st_uid == os.geteuid() and stat.S_IMODE(path.parent.stat().st_mode) == 0o700,
            'Pilot directory ownership/permissions changed')
    state = json.loads(U.private(path))
    require(state.get('format') in (FORMAT, BATCH_FORMAT) and state.get('environment') == 'dev' and state.get('chain_id') == manifest['chain_id']
            and state.get('phase') in ('preparing', 'prepared', 'write_requested', 'applied')
            and type(state.get('writes_may_have_started')) is bool, 'Unexpected pilot state')
    require(type(state.get('expected_rows')) is int and state['expected_rows'] in range(1, 1001), 'Invalid normalization row limit')
    if state['format'] == BATCH_FORMAT:
        require(path.parent.name.startswith('batch-') and block_rows(state.get('block_rows')) == state['expected_rows'], 'Invalid batch target')
        sample = U.private(path.parent / 'sample.json')
        require(R.digest(sample) == state['sample_sha256'], 'Frozen sample changed')
        sample = json.loads(sample)
        require(sample['upgrade_state'] == state['upgrade_state'] and sample['candidate_image'] == state['candidate_image']
                and sample['selected_rows'] == state['expected_rows'] and sample['blockers'] == []
                and [{'block': r['block'], 'rows': r['rows']} for r in sample['selected_blocks']] == state['block_rows'],
                'Frozen sample target changed')
        require(state['migration_payload']['blocks'] == [r['block'] for r in state['block_rows']]
                and state['migration_payload']['block_rows'] == state['block_rows'], 'Batch payload target changed')
        if state.get('retry_of'):
            previous_path = Path(state['retry_of'])
            require(previous_path.is_absolute() and previous_path.resolve() == previous_path and previous_path != path
                    and previous_path.parent.parent == BASE and previous_path.name == 'state.json'
                    and re.fullmatch(r'batch-[0-9TZ-]+-[a-f0-9]{8}', previous_path.parent.name), 'Unexpected parent batch journal')
            previous_bytes = U.private(path.parent / 'parent-state.json')
            require(R.digest(previous_bytes) == state['retry_parent_sha256']
                    and R.digest(U.private(previous_path)) == state['retry_parent_sha256'], 'Previous retry journal changed')
            previous = json.loads(previous_bytes)
            require(previous['format'] == BATCH_FORMAT and previous['phase'] == 'write_requested'
                    and previous['writes_may_have_started'] is True
                    and all(previous[k] == state[k] for k in ('block_rows', 'expected_rows', 'sample_sha256', 'baseline_sha256',
                                                             'baseline', 'migration_payload', 'candidate_image', 'candidate_source_commit',
                                                             'upgrade_state', 'network', 'backend_environment')),
                    'Retry changed original rows, targets or release inputs')
    else:
        require(path.parent.name.startswith('pilot-') and type(state.get('block')) is int and 0 <= state['block'] < 2**63
                and state['migration_payload']['block'] == state['block'] and 'blocks' not in state['migration_payload'], 'Invalid pilot target')
    require(re.fullmatch(r'sha256:[a-f0-9]{64}', state['candidate_image'])
            and re.fullmatch(r'blockscout-v10-schema-[a-f0-9]{16}', state['client_name']), 'Invalid pilot client/image')
    require(R.digest(U.private(path.parent / 'migrate.exs')) == state['script_sha256'], 'Frozen pilot script changed')
    if state['phase'] != 'preparing':
        saved = json.loads(U.private(path.parent / 'baseline.json'))
        require(R.digest(R.encoded(saved)) == state['baseline_sha256'] and state['baseline'] == saved,
                'Saved row snapshot changed')
        check_block_rows(state, saved)
        expected = {k: saved[k] for k in ('full_hashes', 'semantic_hashes', 'statuses')}
        require(state['migration_payload']['baseline'] == expected, 'Pilot baseline payload changed')
    require(state['migration_payload']['expected_rows'] == state['expected_rows']
            and state['migration_payload']['wal_limit_bytes'] == WAL_LIMIT, 'Pilot resource/target limits changed')
    return state


def context(api, root, path):
    manifest, _, _ = api.configuration(root, 'dev')
    upgrade = U.load_state(api, path, manifest)
    require(upgrade['phase'] == 'active', 'The v10 upgrade must remain active')
    result = U.verify(api, path, upgrade, manifest)
    require(result['status'] == 'verified', 'Active v10 runtime needs attention before normalization')
    return manifest, upgrade


def client(api, path, state, mode):
    global STAGE
    logs_before = set(path.parent.glob(mode + '-*.log.private'))
    try:
        return U.client(api, path, state, mode, monitor=monitor, progress_phase='checking_selected_block' if mode == 'inspect' else 'normalizing_selected_block', timeout_seconds=60)
    except U.UpgradeError:
        stages = {'table_lock', 'snapshot_plan', 'before_snapshot', 'callback_plan', 'callback_update',
                  'after_snapshot', 'final_checks', 'committed', 'inspected'}
        for log in sorted(set(path.parent.glob(mode + '-*.log.private')) - logs_before):
            markers = re.findall(r'^BLOCKSCOUT_NORMALIZATION_STAGE=([a-z_]+)$', U.private(log).decode(errors='replace'), re.M)
            if markers and markers[-1] in stages:
                STAGE = markers[-1]
                raise U.UpgradeError('Normalization client failed during ' + STAGE + '; inspect its server-only log') from None
        raise


def prepare(api, root, upgrade_path, block, expected_rows):
    global STAGE
    STAGE = 'active_upgrade'
    manifest, upgrade = context(api, root, upgrade_path)
    require(type(block) is int and 0 <= block < 2**63 and type(expected_rows) is int and expected_rows in range(1, 1001),
            'Provide a valid block and expected count between 1 and 1000')
    planned = N.collect(api, root, upgrade_path)
    require(not planned['blockers'] and planned['selected_block'] == block and planned['sample_rows'] == expected_rows,
            'The selected workload changed; repeat read-only planning')
    before = api.inspect_project(manifest)
    source = B.clean_source(api, root)
    B.validate_state_directory(api, BASE, root)
    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = BASE / ('pilot-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8])
    directory.mkdir(mode=0o700)
    path = directory / 'state.json'
    script = (root / 'deploy/scripts/v10/normalization_pilot.exs').read_bytes()
    R.write_new(directory / 'migrate.exs', script)
    payload = {'mode': 'inspect', 'block': block, 'expected_rows': expected_rows, 'wal_limit_bytes': WAL_LIMIT,
               'database_identity': upgrade['migration_payload']['database_identity'],
               'heap_identity': upgrade['migration_payload']['heap_identity'],
               'applied_versions': upgrade['applied_result']['applied_versions']}
    state = {'format': FORMAT, 'environment': 'dev', 'chain_id': manifest['chain_id'], 'phase': 'preparing',
             'writes_may_have_started': False, 'upgrade_state': str(upgrade_path), 'block': block, 'expected_rows': expected_rows,
             'candidate_image': upgrade['candidate_image'], 'candidate_source_commit': upgrade['candidate_source_commit'],
             'tool_source_commit': source[0], 'network': upgrade['network'], 'backend_environment': upgrade['backend_environment'],
             'client_name': 'blockscout-v10-schema-' + uuid.uuid4().hex[:16], 'migration_payload': payload,
             'script_sha256': R.digest(script), 'runtime_before': before}
    R.write_new(path, R.encoded(state))
    print(json.dumps({'phase': 'pilot_inputs_saved', 'state': str(path), 'database_writes': False}), flush=True)
    STAGE = 'private_bounded_snapshot'
    raw = client(api, path, state, 'inspect')
    saved = baseline(raw)
    require(len(saved['rows']) == expected_rows and saved['needs_normalization'] > 0, 'Prepared block has no expected work')
    state['baseline'] = saved
    R.write_new(directory / 'baseline.json', R.encoded(saved))
    state['baseline_sha256'] = R.digest(R.encoded(saved))
    state['migration_payload']['baseline'] = {k: saved[k] for k in ('full_hashes', 'semantic_hashes', 'statuses')}
    api.ensure_stable(before, api.inspect_project(manifest), 'Runtime changed while preparing normalization')
    require(B.clean_source(api, root) == source, 'Tool source changed during preparation')
    monitor(state)
    write_state(path, state, 'prepared')
    return {'status': 'normalization_pilot_prepared', **public(path, state),
            'note': 'Original bounded row snapshot and runtime inputs saved privately on this server. No data update, worker start or restart occurred.'}


def reconcile(state, result):
    current = baseline(result)
    check_block_rows(state, current)
    saved = state['baseline']
    require(len(current['rows']) == state['expected_rows'] and current['semantic_hashes'] == saved['semantic_hashes']
            and current['statuses'] == saved['statuses'], 'Selected data semantics, row count or migration records changed')
    if current['needs_normalization'] == 0:
        return 'normalized'
    require(current['full_hashes'] == saved['full_hashes'], 'Original selected rows changed; no overwrite is permitted')
    return 'original'


def apply(api, root, path, manifest, resume=False):
    state = load(api, path, manifest)
    require(state['phase'] != 'preparing' and (resume or state['phase'] == 'prepared'), 'Use resume for a prior pilot attempt')
    context(api, root, Path(state['upgrade_state']))
    source = B.clean_source(api, root)
    require(source[1] == '10.2.6', 'Pilot source release version changed')
    api.run(['git', '-C', str(root), 'merge-base', '--is-ancestor', state['candidate_source_commit'], source[0]])
    changes = api.run(['git', '-C', str(root), 'diff', '--name-only', state['candidate_source_commit'], source[0]]).splitlines()
    require(all(n.startswith(('deploy/', 'docs/upgrades/')) for n in changes), 'Application/config changed after the pinned image')
    before = api.inspect_project(manifest)
    api.ensure_stable(state['runtime_before'], before, 'Runtime changed since pilot preparation')
    U.remove_client(api, path, state)
    inspected = client(api, path, state, 'inspect')
    observed = reconcile(state, inspected)
    if observed == 'original':
        state.setdefault('space_before', free_bytes())
        monitor(state)
        state['writes_may_have_started'] = True
        write_state(path, state, 'write_requested')
        result = client(api, path, state, 'migrate')
        require(reconcile(state, result) == 'normalized', 'Normalization result did not match the prepared semantics')
        require(type(result.get('updated_rows')) is int and 0 <= result['updated_rows'] <= state['expected_rows']
                and type(result.get('elapsed_ms')) is int and 0 <= result['elapsed_ms'] <= 10000
                and type(result.get('cluster_wal_delta_bytes')) is int and 0 <= result['cluster_wal_delta_bytes'] <= WAL_LIMIT,
                'Pilot metrics exceeded the checked bounds')
        state['apply_result'] = {k: result[k] for k in ('updated_rows', 'elapsed_ms', 'cluster_wal_delta_bytes', 'already_normalized')}
        if callback_plan(result):
            state['apply_result']['callback_plan'] = callback_plan(result)
        R.update_state(path, state)
        monitor(state)
    else:
        # A commit may have preceded a lost client response. Observe the result;
        # never execute a second update when the semantic result is already there.
        state['writes_may_have_started'] = True
    api.ensure_stable(before, api.inspect_project(manifest), 'Runtime changed during normalization pilot')
    context(api, root, Path(state['upgrade_state']))
    write_state(path, state, 'applied')
    return {'status': 'verified', **public(path, state, inspected if observed == 'normalized' else result),
            'result_reconciled_without_second_update': observed == 'normalized'}


def status(api, root, path, manifest):
    state = load(api, path, manifest)
    if state['phase'] == 'preparing':
        return {'status': 'preparation_incomplete', 'state': str(path), 'writes_may_have_started': False}
    # Do not interrupt an orphan writer from a status operation.
    names = api.run(['docker', 'ps', '-q', '--filter', 'name=^/' + state['client_name'] + '$']).split()
    if names:
        return {'status': 'client_in_progress', 'state': str(path), 'writes_may_have_started': state['writes_may_have_started']}
    context(api, root, Path(state['upgrade_state']))
    result = client(api, path, state, 'inspect')
    observed = reconcile(state, result)
    return {'status': 'verified' if observed == 'normalized' else 'not_applied', **public(path, state, result)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    for name in ('prepare', 'run', 'apply', 'resume', 'status'):
        modes.add_argument('--' + name, action='store_true')
    parser.add_argument('--upgrade-state', type=Path)
    parser.add_argument('--block', type=int)
    parser.add_argument('--expected-rows', type=int)
    parser.add_argument('--state', type=Path)
    args = parser.parse_args()
    try:
        require(sys.platform.startswith('linux') and os.geteuid() == 0, 'Run on the Linux dev server as root')
        manifest, _, _ = D.configuration(ROOT, 'dev')
        with M.global_lock(not args.status), B.build_lock(D, D.MANAGED_RELEASES, ROOT):
            if args.prepare or args.run:
                require(args.state is None and args.upgrade_state is not None and args.block is not None and args.expected_rows is not None,
                        'Prepare requires upgrade state, block and expected rows')
                result = prepare(D, ROOT, args.upgrade_state, args.block, args.expected_rows)
                if args.run:
                    print(json.dumps(result, indent=2), flush=True)
                    args.state = Path(result['state'])
                    result = apply(D, ROOT, args.state, manifest)
            else:
                require(args.state is not None and args.upgrade_state is None and args.block is None and args.expected_rows is None,
                        'Use the saved pilot state only')
                result = status(D, ROOT, args.state, manifest) if args.status else apply(D, ROOT, args.state, manifest, resume=args.resume)
        print(json.dumps(result, indent=2))
        return 0
    except Exception as error:
        print(json.dumps({'status': 'stopped', 'stage': STAGE, 'state': str(args.state) if args.state else None,
                          'raw_values_suppressed': True, 'reason': str(error) if isinstance(error, U.UpgradeError) else 'Pilot check failed; inspect its private journal/logs',
                          'recovery': 'Use status to observe the saved result. Resume reconciles the selected block before another update. Global normalization and index workers remain held.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
