#!/usr/bin/env python3
"""Enable bounded row transactions while retaining native block checkpoints."""
import argparse
import json
import os
from pathlib import Path
import re
import sys

import dev_config as D
import release as R
import v10_native_normalization as N
import v10_native_oversized_block_repair as O
import v10_upgrade as U

ROOT = Path(__file__).resolve().parents[2]
FORMAT = 'blockscout-dev-v10-native-row-chunks-v1'
CHUNK_ROWS = 5000
CALLBACK_FILE = 'apps/explorer/lib/explorer/migrator/empty_internal_transactions_data.ex'
CALLBACK_SHA256 = '59b233944726793d276d21a993ce3e29abc79a62e8c181b9ca16e923fc3f6250'
PREPARED = 'native_row_chunk_repair_prepared'


def source(original, root=ROOT):
    # Validate the existing frozen guard without changing that original file.
    O.repair_source(original, 0)
    callback = (root / CALLBACK_FILE).read_bytes()
    O.require(R.digest(callback) == CALLBACK_SHA256, 'Native row callback source differs from the reviewed revision')
    callback = callback.decode()
    replacements = (
        ('defmodule Explorer.Migrator.EmptyInternalTransactionsData do',
         'defmodule BlockscoutNativeNormalization.RowCallback do'),
        ('def update_batch(block_numbers) do', 'def update_batch({block_numbers, row_ctids}) do'),
        ('it.block_number in ^block_numbers and',
         'it.block_number in ^block_numbers and\n'
         '                fragment("?::text = ANY(?::text[])", fragment("?.ctid", it), ^row_ctids) and'),
    )
    for before, after in replacements:
        O.require(callback.count(before) == 1, 'Native row callback template changed')
        callback = callback.replace(before, after)
    entry = '  def update(blocks) do\n'
    O.require(original.count(entry) == 1, 'Native update entrypoint changed')
    worker = original.replace(entry,
        '  def update([block]), do: BlockscoutNativeNormalization.RowChunks.drain(block)\n\n' + entry)
    return callback + '\n' + (root / 'deploy/scripts/v10/native_row_chunks.exs').read_text() + '\n' + worker


def validate_record(path, inputs, repair):
    N.J.P.require(set(repair) == {'format', 'block', 'chunk_rows', 'script_file', 'script_sha256'}
                  and repair['format'] == FORMAT and type(repair['block']) is int
                  and 0 <= repair['block'] < inputs['payload']['start_before_block']
                  and type(repair['chunk_rows']) is int and repair['chunk_rows'] == CHUNK_ROWS
                  and re.fullmatch(r'[a-f0-9]{64}', repair['script_sha256']),
                  'Invalid native row-chunk repair metadata')
    N.J.P.require(repair['script_file'] == f"native-row-chunks-{repair['script_sha256'][:12]}.exs",
                  'Invalid native row-chunk repair filename')
    data = U.private(path.parent / repair['script_file'])
    N.J.P.require(R.digest(data) == repair['script_sha256'], 'Native row-chunk repair source changed')
    N.J.P.require(data == source(U.private(path.parent / 'native.exs').decode()).encode(),
                  'Native row-chunk repair differs from the bounded source template')


def prepare(api, root, path, block):
    state = N.load(path)
    stopped = (state['phase'] == 'needs_attention' and state.get('reason') == 'native_batch_requires_smaller_range')
    rebound = (state['phase'] == 'paused' and (state.get('worker') or {}).get('reason')
               == 'native_batch_requires_smaller_range')
    repeated = (state['phase'] == 'prepared' and state.get('reason') == PREPARED
                and (state.get('worker_repair') or {}).get('block') == block)
    O.require(stopped or rebound or repeated, 'Native job is not stopped on the single-block size guard')
    O.require(state['batch_blocks'] == 1, 'Row-chunk preparation requires a single-block checkpoint')
    O.require(N.owned(api, path, state['inputs']) is None, 'The native worker must be stopped before row-chunk preparation')
    binding = N.runtime_binding(path, state)
    O.require(binding is not None and binding['source_root'] == str(root)
              and N.B.clean_source(api, root) == (binding['tool_source_commit'], '10.2.6'),
              'Rebind the native job to this clean tool source before row-chunk preparation')
    manifest, _, _ = api.configuration(root, 'dev')
    current = api.inspect_project(manifest)
    api.ensure_stable(binding['runtime_before'], current, 'Runtime changed during row-chunk preparation')
    N.K.check_anchor(binding['database_anchor'], N.K.stable_database_anchor(api), progress=True)
    N.Q.verify_holds(api)
    N.current_runtime_health(api, binding)
    O.require(N.J.P.free_bytes() >= N.RESUME_FREE, 'Insufficient free space for native row chunks')
    meta = (state.get('database') or {}).get('native_meta') or {}
    O.require(type(block) is int and 0 <= block < state['inputs']['payload']['start_before_block']
              and meta.get('max_block_number') == block, 'Select the exact stopped native cursor with --block')
    report = O.rpc(api, block, max_rows=20000)
    O.require(report['status'] == 'started' and report['token'] == state['inputs']['payload']['token']
              and report['cursor'] == block, 'Native database checkpoint changed before row-chunk preparation')
    O.require(type(report['all_rows']) is int and 10000 < report['all_rows'] <= 20001
              and type(report['target_rows']) is int and 0 <= report['target_rows'] <= report['all_rows']
              and type(report['all_bytes']) is int and report['all_bytes'] >= 0,
              'Invalid bounded row-chunk inspection')
    data = source(U.private(path.parent / 'native.exs').decode(), root).encode()
    digest = R.digest(data)
    filename = f'native-row-chunks-{digest[:12]}.exs'
    target = path.parent / filename
    api.ensure_stable(current, api.inspect_project(manifest), 'Runtime changed during row-chunk preparation')
    if target.exists():
        O.require(U.private(target) == data, 'An existing native row-chunk repair differs')
    else:
        R.write_new(target, data)
    record = {'format': FORMAT, 'block': block, 'chunk_rows': CHUNK_ROWS,
              'script_file': filename, 'script_sha256': digest}
    previous = state.get('worker_repair')
    if previous and previous != record:
        state.setdefault('worker_repair_history', []).append(previous)
    state['worker_repair'] = record
    N.save(path, state, 'prepared', PREPARED)
    return {'status': PREPARED, 'state': str(path), 'block': block, 'chunk_rows': CHUNK_ROWS,
            'sampled_rows': report['all_rows'], 'sample_limit_reached': report['all_rows'] == 20001,
            'sampled_bytes': report['all_bytes'], 'sampled_target_rows': report['target_rows'],
            'subsequent_single_blocks_use_row_chunks': True, 'database_writes': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--block', type=int, required=True)
    args = parser.parse_args(argv)
    O.require(os.geteuid() == 0 and sys.platform.startswith('linux'), 'Run on the dev Linux server as root')
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
                          else 'Native row-chunk preparation failed; retain private files'}))
        raise SystemExit(2)
