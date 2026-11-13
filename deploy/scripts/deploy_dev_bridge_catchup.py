#!/usr/bin/env python3
"""Deploy bounded bridge catchup with a live, durable cursor handoff on dev.

The dev RPC was probed successfully at 10,000 blocks/request and four sequential
requests/second. Keep the existing polling intervals; only widen deposit and live
withdrawal log reads. Rebuild committed source and recreate backend only.
"""
import json
from pathlib import Path
import sys
import time

import deploy_dev_bridge_metrics as M

RANGE = '10000'
DEPOSITS = 'INDEXER_ARBITRUM_DEPOSITS_L1_BLOCKS_RANGE'
WITHDRAWALS = 'INDEXER_ARBITRUM_WITHDRAWALS_L1_BLOCKS_RANGE'
MARKER = 'BRIDGE_CATCHUP_CHECK='


def prepare_model(before, revision, nft_image):
    model = M.prepare_model(before, revision, nft_image)
    backend = model['services']['backend']
    backend['image'] = 'deriw-blockscout:dev-bridge-catchup-' + revision[:12]
    backend['environment'].update({DEPOSITS: RANGE, WITHDRAWALS: RANGE})
    return model


def seed_expression(version):
    return '''
true = Explorer.Migrator.StartupControl.disabled?()
''' + json.dumps(version) + ''' = Application.get_env(:block_scout_web, :version)
"18417507517" = System.fetch_env!("CHAIN_ID")
common_range = Application.get_env(:indexer, Indexer.Fetcher.Arbitrum)[:l1_rpc_block_range]
rows = for {name, module, task, prefix} <- [
  {:deposits, Indexer.Fetcher.Arbitrum.TrackingMessagesOnL1, :check_new, "arbitrum_to_l2_scan_v1:"},
  {:withdrawals, Indexer.Fetcher.Arbitrum.TrackingBatchesStatuses, :new_executions, "arbitrum_to_l1_execution_scan_v1:"}
] do
  pid = Process.whereis(module)
  true = is_pid(pid)
  state = :sys.get_state(pid, 5000).callback_module_state
  config = state.config
  next = get_in(state, [:task_data, task, :start_block])
  true = is_integer(next) and next >= 0
  {args, contract} = if name == :deposits,
    do: {config.json_l1_rpc_named_arguments, config.l1_bridge_address},
    else: {config.l1_rpc.json_rpc_named_arguments, config.l1_outbox_address}
  {:ok, 421614} = EthereumJSONRPC.fetch_chain_id(args)
  "0xddf009b4879eaffa6a23782f9aa30f86cd2c64e6" = String.downcase(config.l1_rollup_address)
  true = Regex.match?(~r/^0x[0-9a-fA-F]{40}$/, contract)
  identity = Enum.join([421614, String.downcase(config.l1_rollup_address), String.downcase(contract)], ":")
  key = prefix <> Base.encode16(:crypto.hash(:sha256, identity), case: :lower)
  if name == :deposits, do: (true = config.l1_messages_scan_cursor_key == key)
  [[value]] = Explorer.Repo.query!(~S"""
  INSERT INTO last_fetched_counters(counter_type,value,inserted_at,updated_at)
  VALUES($1,$2,timezone('UTC',now()),timezone('UTC',now()))
  ON CONFLICT(counter_type) DO UPDATE SET value=GREATEST(last_fetched_counters.value,EXCLUDED.value),updated_at=EXCLUDED.updated_at
  RETURNING value
  """, [key, Decimal.new(next)], log: false, timeout: 5000).rows
  saved = Decimal.to_integer(value)
  true = saved >= next and Decimal.equal?(value, Decimal.new(saved))
  %{tracker: name, counter_key: key, live_next_parent_block: next,
    saved_next_parent_block: saved, interval_ms: state.intervals[task]}
end
IO.puts("''' + MARKER + '''" <> Jason.encode!(%{common_range: common_range, cursors: rows}))
'''


def rpc(expression):
    raw = M.run(['docker', 'exec', 'backend', 'bin/blockscout', 'rpc', expression], timeout=30)
    lines = [line[len(MARKER):] for line in raw.splitlines() if line.startswith(MARKER)]
    M.require(len(lines) == 1, 'The backend did not return a verified bridge cursor snapshot.')
    return json.loads(lines[0])


def before_replace(saved, initial):
    import dev_config as D
    version = D.env_map(initial['backend']).get('BLOCKSCOUT_VERSION', '')
    M.require(len(version) == 40 and all(c in '0123456789abcdef' for c in version),
              'Expected a committed version on the running backend.')
    handoff = rpc(seed_expression(version))
    M.require({row['tracker'] for row in handoff['cursors']} == {'deposits', 'withdrawals'},
              'Both bridge cursors are required before replacement.')
    M.private_write(saved / 'bridge-cursor-handoff.json', json.dumps(handoff, indent=2).encode())
    M.emit('bridge_cursors_saved', **handoff)


def verify_expression(handoff):
    return '''
true = Explorer.Migrator.StartupControl.disabled?()
''' + RANGE + ''' = Application.get_env(:indexer, Indexer.Fetcher.Arbitrum.TrackingMessagesOnL1)[:l1_rpc_block_range]
''' + RANGE + ''' = Application.get_env(:indexer, Indexer.Fetcher.Arbitrum.TrackingBatchesStatuses)[:l1_executions_block_range]
''' + str(handoff['common_range']) + ''' = Application.get_env(:indexer, Indexer.Fetcher.Arbitrum)[:l1_rpc_block_range]
handoff = Jason.decode!(~S|''' + json.dumps(handoff, separators=(',', ':')) + '''|)
rows = for row <- handoff["cursors"] do
  {module, task, key_field} = case row["tracker"] do
    "deposits" -> {Indexer.Fetcher.Arbitrum.TrackingMessagesOnL1, :check_new, :l1_messages_scan_cursor_key}
    "withdrawals" -> {Indexer.Fetcher.Arbitrum.TrackingBatchesStatuses, :new_executions, :l1_executions_scan_cursor_key}
  end
  state = :sys.get_state(Process.whereis(module), 5000).callback_module_state
  true = state.config[key_field] == row["counter_key"]
  true = state.intervals[task] == row["interval_ms"]
  range = case row["tracker"] do
    "deposits" -> state.config.l1_rpc_block_range
    "withdrawals" ->
      true = state.config.l1_rpc.logs_block_range == handoff["common_range"]
      state.config.l1_executions_block_range
  end
  ''' + RANGE + ''' = range
  next = get_in(state, [:task_data, task, :start_block])
  true = is_integer(next) and next >= row["saved_next_parent_block"]
  saved = Explorer.Chain.Cache.Counters.LastFetchedCounter.get(row["counter_key"], nullable: true)
  true = Decimal.compare(saved, Decimal.new(row["saved_next_parent_block"])) in [:eq, :gt]
  %{tracker: row["tracker"], next_parent_block: next, durable_next_parent_block: Decimal.to_integer(saved),
    interval_ms: state.intervals[task], blocks_per_request: range}
end
IO.puts("''' + MARKER + '''" <> Jason.encode!(%{cursors: rows, resumed_from_handoff: true}))
'''


def after_ready(saved, initial, after):
    handoff = json.loads((saved / 'bridge-cursor-handoff.json').read_text())
    deadline = time.monotonic() + 90
    while True:
        try:
            result = rpc(verify_expression(handoff))
            break
        except M.DeploymentError:
            M.require(time.monotonic() < deadline, 'Bridge workers did not resume from their saved cursors.')
            time.sleep(3)
    M.private_write(saved / 'bridge-cursor-resumed.json', json.dumps(result, indent=2).encode())
    M.emit('bridge_cursors_resumed', **result)


def main():
    M.PRIVATE = Path('/data/blockscout-dev-private/bridge-catchup-deployments')
    return M.main(
        prepare=prepare_model, before_replace=before_replace, after_ready=after_ready,
        image_check='''
true = Code.ensure_loaded?(Indexer.Fetcher.Arbitrum.Utils.Db.MessageScanCursor)
true = function_exported?(Indexer.Fetcher.Arbitrum.Utils.Db.MessageScanCursor, :execution_key, 3)
true = Code.ensure_loaded?(Indexer.Fetcher.Arbitrum.Workers.NewL1Executions)
''', success_phase='bridge_catchup_deployed')


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except M.DeploymentError as error:
        print('STOP:', error, file=sys.stderr)
        raise SystemExit(1)
