#!/usr/bin/env python3
"""Normalize one pinned complete-block sample with the pilot's atomic checks."""
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
import v10_normalization_batch as S
import v10_normalization_pilot as P
import v10_upgrade as U

ROOT = Path(__file__).resolve().parents[2]
STAGE = 'sample_receipt'


def receipt(path, digest, manifest):
    P.require(path.is_absolute() and path.resolve() == path and path.parent == S.BASE
              and re.fullmatch(r'sample-[0-9TZ-]+-[a-f0-9]{8}\.json', path.name), 'Unexpected private sample path')
    P.require(path.parent.stat().st_uid == os.geteuid() and stat.S_IMODE(path.parent.stat().st_mode) == 0o700,
              'Sample directory ownership/permissions changed')
    P.require(isinstance(digest, str) and re.fullmatch(r'[a-f0-9]{64}', digest), 'Provide the exact sample SHA-256')
    content = U.private(path)
    P.require(R.digest(content) == digest, 'Sample SHA-256 changed')
    saved = json.loads(content)
    P.require(saved.get('format') == S.FORMAT and saved.get('environment') == 'dev'
              and saved.get('chain_id') == manifest['chain_id'] and saved.get('read_only') is True
              and saved.get('apply_available') is False and saved.get('automatic_background_migrations_held') is True
              and saved.get('status') == 'normalization_batch_sample_ready' and saved.get('blockers') == [],
              'Sample is not eligible for a bounded update')
    groups = saved.get('selected_blocks')
    P.require(isinstance(groups, list) and all(isinstance(r, dict) for r in groups), 'Missing complete-block manifest')
    targets = [{'block': r['block'], 'rows': r['rows']} for r in groups]
    count = P.block_rows(targets)
    P.require(type(saved.get('selected_rows')) is int and saved['selected_rows'] == count
              and saved.get('selected_block_count') == len(targets) and type(saved.get('before_block')) is int
              and targets[0]['block'] < saved['before_block']
              and saved.get('newest_selected_block') == targets[0]['block']
              and saved.get('oldest_selected_block') == targets[-1]['block'], 'Sample target accounting changed')
    P.require(all(type(r.get('needs')) is int and 0 <= r['needs'] <= r['rows']
                  and type(r.get('row_bytes')) is int and r['row_bytes'] >= 0 for r in groups)
              and sum(r['needs'] for r in groups) == saved['rows_needing_normalization'] > 0
              and sum(r['row_bytes'] for r in groups) == saved['selected_row_bytes'] <= 16 * 1024**2,
              'Sample work/size accounting changed')
    excluded = saved.get('excluded_boundary_block')
    P.require(excluded is None or (type(excluded) is int and 0 <= excluded < targets[-1]['block']),
              'An incomplete boundary block entered the selection')
    return saved, content, targets


def prepare(api, root, sample_path, digest, limits=None):
    global STAGE
    STAGE = 'sample_receipt'
    manifest, _, _ = api.configuration(root, 'dev')
    sample, content, targets = receipt(sample_path, digest, manifest)
    upgrade_path = Path(sample['upgrade_state'])
    STAGE = 'active_upgrade'
    _, upgrade = P.context(api, root, upgrade_path)
    P.require(sample['candidate_image'] == upgrade['candidate_image'], 'The sample image differs from the active upgrade')
    pilot_path = Path(sample['previous_pilot_state'])
    previous = P.load(api, pilot_path, manifest)
    P.require(previous['upgrade_state'] == str(upgrade_path) and P.completed_cursor(previous) == sample['before_block'],
              'Previous normalization/cursor changed')
    verified = P.status(api, root, pilot_path, manifest)
    P.require(verified['status'] == 'verified' and verified['rows_needing_normalization'] == 0, 'Previous normalization no longer verifies')
    before = api.inspect_project(manifest)
    source = B.clean_source(api, root)
    P.require(source[1] == '10.2.6', 'Normalization source version changed')
    B.validate_state_directory(api, P.BASE, root)
    P.BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = P.BASE / ('batch-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8])
    directory.mkdir(mode=0o700)
    path = directory / 'state.json'
    script = (root / 'deploy/scripts/v10/normalization_pilot.exs').read_bytes()
    R.write_new(directory / 'migrate.exs', script)
    R.write_new(directory / 'sample.json', content)
    payload = {'mode': 'inspect', 'blocks': [r['block'] for r in targets], 'block_rows': targets,
               'expected_rows': sample['selected_rows'], 'wal_limit_bytes': P.WAL_LIMIT,
               'database_identity': upgrade['migration_payload']['database_identity'],
               'heap_identity': upgrade['migration_payload']['heap_identity'],
               'applied_versions': upgrade['applied_result']['applied_versions']}
    state = {'format': P.BATCH_FORMAT, 'environment': 'dev', 'chain_id': manifest['chain_id'], 'phase': 'preparing',
             'writes_may_have_started': False, 'upgrade_state': str(upgrade_path), 'block_rows': targets,
             'expected_rows': sample['selected_rows'], 'sample_sha256': digest, 'sample_file': str(sample_path),
             'candidate_image': upgrade['candidate_image'], 'candidate_source_commit': upgrade['candidate_source_commit'],
             'tool_source_commit': source[0], 'network': upgrade['network'], 'backend_environment': upgrade['backend_environment'],
             'client_name': 'blockscout-v10-schema-' + uuid.uuid4().hex[:16], 'migration_payload': payload,
             'script_sha256': R.digest(script), 'runtime_before': before}
    if limits is not None:
        P.require(set(limits) == {'wal_limit_bytes', 'space_before', 'controller_state'}
                  and type(limits['space_before']) is int and limits['space_before'] >= 200 * 1024**3
                  and Path(limits['controller_state']).is_absolute(), 'Invalid bounded-run limits')
        state.update(limits)
        payload['wal_limit_bytes'] = P.wal_limit(state)
    R.write_new(path, R.encoded(state))
    print(json.dumps({'phase': 'batch_inputs_saved', 'state': str(path), 'database_writes': False}), flush=True)
    STAGE = 'private_bounded_snapshot'
    inspected = P.client(api, path, state, 'inspect')
    saved = P.baseline(inspected)
    if P.callback_plan(inspected):
        state['callback_plan'] = P.callback_plan(inspected)
    P.check_block_rows(state, saved)
    P.require(len(saved['rows']) == sample['selected_rows'] and saved['needs_normalization'] == sample['rows_needing_normalization'],
              'Sample row count or normalization work changed; repeat read-only sampling')
    state['baseline'] = saved
    R.write_new(directory / 'baseline.json', R.encoded(saved))
    state['baseline_sha256'] = R.digest(R.encoded(saved))
    state['migration_payload']['baseline'] = {k: saved[k] for k in ('full_hashes', 'semantic_hashes', 'statuses')}
    api.ensure_stable(before, api.inspect_project(manifest), 'Runtime changed while preparing the sampled batch')
    P.require(B.clean_source(api, root) == source, 'Tool source changed during preparation')
    P.monitor(state)
    P.write_state(path, state, 'prepared')
    return {'status': 'normalization_batch_prepared', **P.public(path, state),
            'note': 'Complete original rows, sample and client saved privately. Prepared only; no data update or worker start.'}


def prepare_retry(api, root, previous_path):
    """Freeze a new client while preserving the failed attempt and original rows."""
    global STAGE
    STAGE = 'retry_original_snapshot'
    manifest, _, _ = api.configuration(root, 'dev')
    previous = P.load(api, previous_path, manifest)
    P.require(previous['format'] == P.BATCH_FORMAT and previous['phase'] == 'write_requested'
              and previous['writes_may_have_started'], 'Retry requires a saved attempted batch')
    previous_bytes = U.private(previous_path)
    observed = P.status(api, root, previous_path, manifest)
    P.require(observed['status'] == 'not_applied', 'Reconcile the previous batch; only unchanged original rows may use a new client')
    before = api.inspect_project(manifest)
    api.ensure_stable(previous['runtime_before'], before, 'Runtime changed since the original batch')
    source = B.clean_source(api, root)
    P.require(source[1] == '10.2.6', 'Normalization source version changed')
    B.validate_state_directory(api, P.BASE, root)
    directory = P.BASE / ('batch-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8])
    directory.mkdir(mode=0o700)
    path = directory / 'state.json'
    script = (root / 'deploy/scripts/v10/normalization_pilot.exs').read_bytes()
    R.write_new(directory / 'migrate.exs', script)
    for name in ('sample.json', 'baseline.json'):
        R.write_new(directory / name, U.private(previous_path.parent / name))
    R.write_new(directory / 'parent-state.json', previous_bytes)
    state = deepcopy(previous)
    for name in ('apply_result', 'space_before', 'callback_plan'):
        state.pop(name, None)
    state.update(phase='preparing', writes_may_have_started=False, retry_of=str(previous_path),
                 retry_parent_sha256=R.digest(previous_bytes), tool_source_commit=source[0],
                 client_name='blockscout-v10-schema-' + uuid.uuid4().hex[:16],
                 script_sha256=R.digest(script), runtime_before=before)
    R.write_new(path, R.encoded(state))
    print(json.dumps({'phase': 'retry_inputs_saved', 'state': str(path), 'retry_of': str(previous_path), 'database_writes': False}), flush=True)
    STAGE = 'retry_bounded_callback_plan'
    inspected = P.client(api, path, state, 'inspect')
    P.require(P.reconcile(state, inspected) == 'original', 'The retry must retain every original row and migration record')
    plan = P.callback_plan(inspected)
    P.require(plan is not None, 'The new retry client must verify its bounded callback plan')
    state['callback_plan'] = plan
    P.require(R.digest(U.private(previous_path)) == state['retry_parent_sha256'], 'Previous journal changed during retry preparation')
    api.ensure_stable(before, api.inspect_project(manifest), 'Runtime changed while preparing the retry')
    P.require(B.clean_source(api, root) == source, 'Tool source changed during retry preparation')
    P.monitor(state)
    P.write_state(path, state, 'prepared')
    return {'status': 'normalization_batch_retry_prepared', 'retry_of': str(previous_path), **P.public(path, state),
            'note': 'Original journal, sample and row snapshot preserved. The new client passed read-only callback planning; no data update yet.'}


def main():
    global STAGE
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    for name in ('prepare', 'run', 'apply', 'resume', 'status', 'prepare-retry', 'retry'):
        modes.add_argument('--' + name, action='store_true')
    parser.add_argument('--sample-file', type=Path)
    parser.add_argument('--sample-sha256')
    parser.add_argument('--state', type=Path)
    args = parser.parse_args()
    try:
        P.require(sys.platform.startswith('linux') and os.geteuid() == 0, 'Run on the Linux dev server as root')
        manifest, _, _ = D.configuration(ROOT, 'dev')
        with M.global_lock(not args.status), B.build_lock(D, D.MANAGED_RELEASES, ROOT):
            if args.prepare_retry or args.retry:
                P.require(args.state is not None and args.sample_file is None and args.sample_sha256 is None,
                          'Retry requires only the previous batch state')
                result = prepare_retry(D, ROOT, args.state)
                if args.retry:
                    print(json.dumps(result, indent=2), flush=True)
                    args.state = Path(result['state'])
                    STAGE = 'bounded_retry_apply'
                    result = P.apply(D, ROOT, args.state, manifest)
            elif args.prepare or args.run:
                P.require(args.state is None and args.sample_file is not None and args.sample_sha256 is not None,
                          'Prepare requires the private sample and its exact SHA-256')
                result = prepare(D, ROOT, args.sample_file, args.sample_sha256)
                if args.run:
                    print(json.dumps(result, indent=2), flush=True)
                    args.state = Path(result['state'])
                    STAGE = 'bounded_batch_apply'
                    result = P.apply(D, ROOT, args.state, manifest)
            else:
                P.require(args.state is not None and args.sample_file is None and args.sample_sha256 is None,
                          'Use only the saved batch state for apply, resume or status')
                P.require(P.load(D, args.state, manifest)['format'] == P.BATCH_FORMAT, 'Provide a saved batch state')
                STAGE = 'bounded_batch_' + ('status' if args.status else 'apply')
                result = P.status(D, ROOT, args.state, manifest) if args.status else P.apply(D, ROOT, args.state, manifest, resume=args.resume)
        print(json.dumps(result, indent=2))
        return 0
    except Exception as error:
        print(json.dumps({'status': 'stopped', 'stage': STAGE, 'client_stage': P.STAGE,
                          'state': str(args.state) if args.state else None, 'raw_values_suppressed': True,
                          'reason': str(error) if isinstance(error, U.UpgradeError) else 'Batch check failed; inspect the server-only journal/logs',
                          'recovery': 'Use --status with the saved batch state. Use --resume to reconcile a possible commit before retrying. Global normalization and index workers remain held.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
