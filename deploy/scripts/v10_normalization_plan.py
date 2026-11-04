#!/usr/bin/env python3
"""Select an indexed, bounded v10 normalization pilot candidate; never write data."""
import argparse
import json
import os
from pathlib import Path
import re
import sys

import candidate_build as B
import containerd_maintenance as M
import deploy_check as C
import dev_config as D
import upgrade_plan as H
import v10_upgrade as U

ROOT = Path(__file__).resolve().parents[2]
STAGE = 'upgrade_state'


def project(data, free):
    D.require(isinstance(data, dict), 'Invalid normalization metadata')
    block = data.get('block')
    D.require(type(block) is int and block >= 0, 'Invalid selected block')
    plans = {}
    for name in ('seek_plan', 'batch_plan'):
        plan = data.get(name) or {}
        D.require(plan.get('access') in ('Index Scan', 'Index Only Scan') and isinstance(plan.get('index'), str)
                  and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}', plan['index']), 'Unsafe or missing index access path')
        cost = plan.get('estimated_total_cost')
        D.require(type(cost) in (int, float) and cost >= 0, 'Invalid planner cost')
        plans[name] = {k: plan[k] for k in ('access', 'index', 'estimated_total_cost')}
    counts = ('sample_rows', 'rows_needing_normalization_in_sample', 'sampled_row_bytes', 'maximum_sampled_row_bytes')
    D.require(all(type(data.get(k)) is int and data[k] >= 0 for k in counts), 'Invalid sample sizes')
    D.require(0 < data['sample_rows'] <= 1001 and data['rows_needing_normalization_in_sample'] <= data['sample_rows']
              and type(data.get('row_limit_exceeded')) is bool
              and data['row_limit_exceeded'] == (data['sample_rows'] > 1000), 'Invalid sample row bounds')
    statuses = data.get('statuses')
    D.require(isinstance(statuses, list) and len(statuses) <= len(H.HISTORY_WORKERS), 'Invalid worker status metadata')
    status = {}
    for row in statuses:
        D.require(isinstance(row, list) and len(row) == 2 and row[0] in H.HISTORY_WORKERS and row[0] not in status
                  and row[1] in ('started', 'completed'), 'Unexpected worker state')
        status[row[0]] = row[1]
    stats = data.get('table_update_stats')
    D.require(isinstance(stats, list) and len(stats) == 1 and len(stats[0]) == 4
              and all(type(v) is int and v >= 0 for v in stats[0]), 'Invalid table statistics')
    D.require(isinstance(data.get('wal_bytes'), str) and re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', data['wal_bytes'])
              and isinstance(data.get('wal_stats_reset'), str) and re.fullmatch(r'[0-9:.+ -]{10,64}', data['wal_stats_reset']),
              'Invalid WAL statistics')
    blockers = []
    if data['row_limit_exceeded']:
        blockers.append('selected_block_exceeds_1000_row_pilot_cap')
    if data['sampled_row_bytes'] > 16 * 1024**2:
        blockers.append('selected_block_exceeds_16_mib_row_size_budget')
    if data['rows_needing_normalization_in_sample'] == 0:
        blockers.append('selected_block_has_no_normalization_work')
    if free < 200 * 1024**3:
        blockers.append('less_than_200_gib_data_free')
    if status.get('empty_internal_transactions_data') == 'completed':
        blockers.append('normalization_already_recorded_complete')
    if status.get('reindex_duplicated_internal_transactions') != 'completed':
        blockers.append('historical_reindex_state_needs_review')
    if any(name.startswith('heavy_indexes_') and value == 'started' for name, value in status.items()):
        blockers.append('heavy_index_work_recorded_started')
    return {'status': 'normalization_pilot_candidate_selected' if not blockers else 'normalization_pilot_needs_review',
            'read_only': True, 'apply_available': False, 'blockers': blockers, 'selected_block': block,
            **plans, **{k: data[k] for k in counts}, 'sample_is_complete_block': not data['row_limit_exceeded'],
            'data_available_bytes': free, 'required_data_free_bytes': 200 * 1024**3,
            'worker_states': [{'name': name, 'status': status.get(name, 'not_recorded')} for name in H.HISTORY_WORKERS],
            'table_update_statistics': dict(zip(('updates_since_stats_reset', 'hot_updates_since_stats_reset',
                                               'estimated_dead_rows', 'autovacuum_count'), stats[0])),
            'cluster_wal_statistics': {'bytes_since_stats_reset': data['wal_bytes'], 'stats_reset': data['wal_stats_reset']},
            'scope': 'Read-only indexed newest-block seek and at most 1001 rows from that one block, planner-only EXPLAIN and selected catalog/statistics reads. No normalization, migration-state update, worker/index start, node call or restart. Row sizes are PostgreSQL estimates, not a WAL forecast.'}


def collect(api, root, path):
    global STAGE
    manifest, _, _ = api.configuration(root, 'dev')
    state = U.load_state(api, path, manifest)
    api.require(state['phase'] == 'active', 'The v10 upgrade must be active')
    before = api.inspect_project(manifest)
    api.require(all(before[n]['Image'] == state['candidate_image'] for n in U.PAIR), 'Live paired image differs from the upgrade')
    for name in U.PAIR:
        api.require((before[name]['Config'].get('Labels') or {}).get(U.STATE_LABEL) == str(path), 'Another release owns the pair')
    STAGE = 'bounded_indexed_sample'
    source = B.clean_source(api, root)
    expression = (root / 'deploy/scripts/v10/normalization_plan.exs').read_text()
    data = C.marked_json(api, api.run(['docker', 'exec', 'backend', '/app/bin/blockscout', 'rpc', expression], timeout=15))
    values = os.statvfs('/data')
    report = project(data, values.f_bavail * values.f_frsize)
    api.ensure_stable(before, api.inspect_project(manifest), 'Deployment changed during normalization planning')
    api.require(B.clean_source(api, root) == source, 'Planning source changed during the read')
    return {'environment': 'dev', 'chain_id': manifest['chain_id'], 'upgrade_state': str(path),
            'candidate_image': state['candidate_image'], 'automatic_background_migrations_held': True, **report}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--upgrade-state', type=Path, required=True)
    args = parser.parse_args()
    try:
        D.require(sys.platform.startswith('linux') and os.geteuid() == 0, 'Run on the Linux dev server as root')
        with M.global_lock(False):
            result = collect(D, ROOT, args.upgrade_state)
        print(json.dumps(result, indent=2))
        return 0
    except Exception:
        print(json.dumps({'status': 'stopped', 'stage': STAGE, 'read_only': True, 'apply_available': False,
                          'raw_values_suppressed': True, 'reason': 'Bounded normalization planning failed; no writes or worker start.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
