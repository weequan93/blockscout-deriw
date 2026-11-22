#!/usr/bin/env python3
"""Prepare one bounded repair for a native block exceeding the normal row cap."""
import argparse
import json
import os
from pathlib import Path
import sys

import dev_config as D
import release as R
import v10_native_normalization as N
import v10_upgrade as U

ROOT = Path(__file__).resolve().parents[2]
MAX_ROWS = 12000
REPAIR_FORMAT = 'blockscout-dev-v10-oversized-block-repair-v2'


def require(condition, message):
    if not condition:
        raise D.ConfigError(message)


def repair_source(original, block):
    require(type(block) is int and 0 <= block < 2**63, 'Invalid oversized-block identity')
    sample = '''      [[rows, bytes]] = Explorer.Repo.query!(~S"""
      SELECT count(*),COALESCE(sum(pg_column_size(it)),0)::bigint
      FROM (SELECT it.* FROM public.internal_transactions it WHERE block_number=ANY($1::bigint[]) LIMIT 10001) it
      """, [blocks], log: false, timeout: 6000).rows'''
    first = 'unless rows <= 10000 and bytes <= 16777216, do: fail!("native_batch_requires_smaller_range")'
    second = 'unless updated <= 10000, do: fail!("native_batch_requires_smaller_range")'
    require(original.count(sample) == original.count(first) == original.count(second) == 1,
            'Native worker source does not match the reviewed repair template')
    # Read one extra row beyond the active cap before allowing the native update.
    # The previous repair kept LIMIT 10001 even when its cap was 12000.
    expanded_sample = sample.replace('LIMIT 10001', 'LIMIT $2').replace('[blocks],', '[blocks, limit + 1],')
    source = original.replace(sample, f'      limit = if blocks == [{block}], do: {MAX_ROWS}, else: 10000\n' + expanded_sample)
    source = source.replace(first, 'unless rows <= limit and bytes <= 16777216, do: fail!("native_batch_requires_smaller_range")')
    source = source.replace(second, 'unless updated <= limit, do: fail!("native_batch_requires_smaller_range")')
    return source


def rpc(api, block):
    code = f'''
true = Explorer.Migrator.StartupControl.disabled?()
"10.2.6" = to_string(Application.spec(:explorer, :vsn))
block = {block}
{{:ok, result}} = Explorer.Repo.transaction(fn ->
  Explorer.Repo.query!("SET TRANSACTION READ ONLY", [], log: false)
  Explorer.Repo.query!("SET LOCAL statement_timeout='10s'", [], log: false)
  Explorer.Repo.query!("SET LOCAL lock_timeout='500ms'", [], log: false)
  [[status, token, cursor]] = Explorer.Repo.query!("""
    SELECT status, meta->>'v10_native_job', (meta->>'max_block_number')::bigint
    FROM public.migrations_status
    WHERE migration_name='empty_internal_transactions_data'
  """, [], log: false).rows
  [[all_rows, all_bytes, target_rows]] = Explorer.Repo.query!("""
    SELECT count(*), COALESCE(sum(pg_column_size(it)), 0)::bigint,
      count(*) FILTER (WHERE
      (it.trace_address IS NOT NULL OR it.value=0 OR
       (it.call_type_enum IS NULL AND it.call_type IS NOT NULL) OR
       it.error IS NOT NULL OR
       (it.created_contract_address_hash IS NOT NULL AND it.to_address_hash IS NULL)
      ))
    FROM (SELECT * FROM public.internal_transactions WHERE block_number=$1 LIMIT $2) it
  """, [block, {MAX_ROWS + 1}], log: false, timeout: 11000).rows
  %{{status: status, token: token, cursor: cursor, all_rows: all_rows, all_bytes: all_bytes,
     target_rows: target_rows, target_bytes: all_bytes}}
end, timeout: 12000)
IO.puts("BLOCKSCOUT_DEPLOY_CHECK=" <> Jason.encode!(result))
'''
    return N.V.C.marked_json(api, api.run(['docker', 'exec', 'backend', 'bin/blockscout', 'rpc', code], timeout=20))


def prepare(api, root, path, block):
    state = N.load(path)
    stopped_on_guard = (state['phase'] == 'needs_attention'
                        and state.get('reason') == 'native_batch_requires_smaller_range')
    rebound_paused = (state['phase'] == 'paused' and (state.get('worker') or {}).get('reason')
                      == 'native_batch_requires_smaller_range')
    already_prepared = (state['phase'] == 'prepared' and state.get('reason') == 'oversized_block_repair_prepared'
                        and (state.get('worker_repair') or {}).get('block') == block)
    require(stopped_on_guard or rebound_paused or already_prepared,
            'Native job is not stopped on the expected oversized-block guard')
    require(state['batch_blocks'] == 1, 'Reduce to a single native block before repair preparation')
    require(N.owned(api, path, state['inputs']) is None, 'The native worker must be stopped before repair preparation')
    binding = N.runtime_binding(path, state)
    require(binding is not None and binding['source_root'] == str(root)
            and N.B.clean_source(api, root) == (binding['tool_source_commit'], '10.2.6'),
            'Rebind the native job to this clean tool source before repair preparation')
    manifest, _, _ = api.configuration(root, 'dev')
    current = api.inspect_project(manifest)
    api.ensure_stable(binding['runtime_before'], current, 'Runtime changed during oversized-block repair preparation')
    N.K.check_anchor(binding['database_anchor'], N.K.stable_database_anchor(api), progress=True)
    N.Q.verify_holds(api)
    N.current_runtime_health(api, binding)
    require(N.J.P.free_bytes() >= N.RESUME_FREE, 'Insufficient free space for oversized-block repair')
    native = (state.get('database') or {}).get('native_meta') or {}
    require(type(block) is int and 0 <= block < state['inputs']['payload']['start_before_block']
            and native.get('max_block_number') == block,
            'Select the exact stopped native cursor with --block')
    report = rpc(api, block)
    require(report['status'] == 'started' and report['token'] == state['inputs']['payload']['token']
            and report['cursor'] == block, 'Native database checkpoint changed before repair')
    require(type(report['target_rows']) is int and 10000 < report['target_rows'] <= MAX_ROWS
            and type(report['target_bytes']) is int and report['target_bytes'] <= 16777216
            and report['target_rows'] == report['all_rows']
            and report['target_bytes'] == report['all_bytes'],
            'The cursor block does not match the bounded oversized-block repair')
    original = U.private(path.parent / 'native.exs').decode()
    data = repair_source(original, block).encode()
    filename = f'native-oversized-block-{block}-{R.digest(data)[:12]}.exs'
    repair_path = path.parent / filename
    api.ensure_stable(current, api.inspect_project(manifest), 'Runtime changed during repair preparation')
    if repair_path.exists():
        require(U.private(repair_path) == data, 'An existing oversized-block repair differs')
    else:
        R.write_new(repair_path, data)
    record = {'format': REPAIR_FORMAT, 'block': block, 'max_rows': MAX_ROWS,
              'script_file': filename, 'script_sha256': R.digest(data)}
    previous = state.get('worker_repair')
    if previous and previous != record:
        state.setdefault('worker_repair_history', []).append(previous)
    state['worker_repair'] = record
    N.save(path, state, 'prepared', 'oversized_block_repair_prepared')
    return {'status': 'oversized_block_repair_prepared', 'state': str(path),
            'block': block, 'target_rows': report['target_rows'],
            'target_bytes': report['target_bytes'], 'max_rows': MAX_ROWS}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--block', type=int, required=True)
    args = parser.parse_args(argv)
    require(os.geteuid() == 0 and sys.platform.startswith('linux'), 'Run on the dev Linux server as root')
    path = args.state.resolve(strict=True)
    with N.M.global_lock(True), N.B.build_lock(D, D.MANAGED_RELEASES, ROOT):
        result = prepare(D, ROOT, path, args.block)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'status': 'stopped', 'raw_values_suppressed': True,
                          'reason': str(error) if isinstance(error, D.ConfigError)
                          else 'Oversized-block repair preparation failed'}))
        raise SystemExit(2)
