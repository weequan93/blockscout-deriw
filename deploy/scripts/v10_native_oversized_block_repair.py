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
REPAIR_FORMAT = 'blockscout-dev-v10-oversized-block-repair-v1'
REPAIR_FILE = 'native-oversized-block.exs'


def require(condition, message):
    if not condition:
        raise D.ConfigError(message)


def repair_source(original, block):
    first = 'unless rows <= 10000 and bytes <= 16777216, do: fail!("native_batch_requires_smaller_range")'
    second = 'unless updated <= 10000, do: fail!("native_batch_requires_smaller_range")'
    require(original.count(first) == 1 and original.count(second) == 1,
            'Native worker source does not match the reviewed repair template')
    source = original.replace(
        first,
        f'limit = if blocks == [{block}], do: {MAX_ROWS}, else: 10000\n'
        '      unless rows <= limit and bytes <= 16777216, do: fail!("native_batch_requires_smaller_range")')
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
  [[status, token, cursor]] = Explorer.Repo.query!("""
    SELECT status, meta->>'v10_native_job', (meta->>'max_block_number')::bigint
    FROM public.migrations_status
    WHERE migration_name='empty_internal_transactions_data'
  """, [], log: false).rows
  [[all_rows, all_bytes]] = Explorer.Repo.query!("""
    SELECT count(*), COALESCE(sum(pg_column_size(it)), 0)::bigint
    FROM public.internal_transactions it WHERE it.block_number=$1
  """, [block], log: false).rows
  [[target_rows, target_bytes]] = Explorer.Repo.query!("""
    SELECT count(*), COALESCE(sum(pg_column_size(it)), 0)::bigint
    FROM public.internal_transactions it
    WHERE it.block_number=$1 AND
      (it.trace_address IS NOT NULL OR it.value=0 OR
       (it.call_type_enum IS NULL AND it.call_type IS NOT NULL) OR
       it.error IS NOT NULL OR
       (it.created_contract_address_hash IS NOT NULL AND it.to_address_hash IS NULL)
      )
  """, [block], log: false).rows
  %{{status: status, token: token, cursor: cursor, all_rows: all_rows, all_bytes: all_bytes,
     target_rows: target_rows, target_bytes: target_bytes}}
end, timeout: 12000)
IO.puts("BLOCKSCOUT_DEPLOY_CHECK=" <> Jason.encode!(result))
'''
    return N.V.C.marked_json(api, api.run(['docker', 'exec', 'backend', 'bin/blockscout', 'rpc', code], timeout=20))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    args = parser.parse_args(argv)
    require(os.geteuid() == 0 and sys.platform.startswith('linux'), 'Run on the dev Linux server as root')
    path = args.state.resolve(strict=True)
    state = N.load(path)
    require(state['phase'] == 'needs_attention' and state.get('reason') == 'native_batch_requires_smaller_range',
            'Native job is not stopped on the expected oversized-block guard')
    require(N.owned(D, path, state['inputs']) is None, 'The native worker must be stopped before repair preparation')
    binding = N.runtime_binding(path, state)
    manifest, _, _ = D.configuration(ROOT, 'dev')
    current = D.inspect_project(manifest)
    D.ensure_stable(binding['runtime_before'], current, 'Runtime changed during oversized-block repair preparation')
    native = (state.get('database') or {}).get('native_meta') or {}
    block = native.get('max_block_number')
    require(type(block) is int and block >= 0, 'Native cursor is unavailable for oversized-block repair')
    report = rpc(D, block)
    require(report['status'] == 'started' and report['token'] == state['inputs']['payload']['token']
            and report['cursor'] == block, 'Native database checkpoint changed before repair')
    require(type(report['target_rows']) is int and 10000 < report['target_rows'] <= MAX_ROWS
            and type(report['target_bytes']) is int and report['target_bytes'] <= 16777216
            and report['target_rows'] == report['all_rows'],
            'The cursor block does not match the bounded oversized-block repair')
    original = U.private(path.parent / 'native.exs').decode()
    data = repair_source(original, block).encode()
    repair_path = path.parent / REPAIR_FILE
    if repair_path.exists():
        require(U.private(repair_path) == data, 'An existing oversized-block repair differs')
    else:
        R.write_new(repair_path, data)
    state['worker_repair'] = {'format': REPAIR_FORMAT, 'block': block, 'max_rows': MAX_ROWS,
                              'script_file': REPAIR_FILE, 'script_sha256': R.digest(data)}
    N.save(path, state, 'prepared', 'oversized_block_repair_prepared')
    print(json.dumps({'status': 'oversized_block_repair_prepared', 'state': str(path),
                      'block': block, 'target_rows': report['target_rows'],
                      'target_bytes': report['target_bytes'], 'max_rows': MAX_ROWS}, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'status': 'stopped', 'raw_values_suppressed': True,
                          'reason': str(error) if isinstance(error, D.ConfigError)
                          else 'Oversized-block repair preparation failed'}))
        raise SystemExit(2)
