#!/usr/bin/env python3
"""Run bridge cursor regressions, including durable local PostgreSQL checkpoints."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKER = 'apps/indexer/lib/indexer/fetcher/arbitrum/workers/new_messages_to_l2.ex'
TEST = 'apps/indexer/test/indexer/fetcher/arbitrum/workers/new_messages_to_l2_test.exs'
BOOTSTRAP = '''
ExUnit.start(autorun: false)
{:ok, _} = Application.ensure_all_started(:mox)
Logger.configure(level: :warning)
Application.put_env(:ethereum_jsonrpc, EthereumJSONRPC.Tracer, disabled?: true, adapter: SpandexDatadog.Adapter)
Application.put_env(:ethereum_jsonrpc, EthereumJSONRPC.RequestCoordinator,
  rolling_window_opts: [table: :bridge_cursor_timeouts, window_count: 6, duration: 60_000],
  wait_per_timeout: 0,
  max_jitter: 1
)
{:ok, _} = EthereumJSONRPC.RollingWindow.start_link(
  table: :bridge_cursor_timeouts, window_count: 6, duration: 60_000
)
{:ok, _} = EthereumJSONRPC.Utility.EndpointAvailabilityObserver.start_link([])
Mox.defmock(EthereumJSONRPC.Mox, for: EthereumJSONRPC.Transport)
Code.compiler_options(ignore_module_conflict: true)
test_source = File.read!("/tmp/bridge-cursor/test.exs")
formatted_test = test_source |> Code.format_string!(line_length: 120) |> IO.iodata_to_binary()
if String.trim_trailing(test_source) != formatted_test, do: raise("Bridge cursor tests need formatting")
Code.compile_file("/tmp/bridge-cursor/worker.ex")
Code.require_file("/tmp/bridge-cursor/test.exs")
result = ExUnit.run()
System.halt(if result.failures == 0, do: 0, else: 1)
'''


def main():
    from check_arbitrum_message_checkpoint import main as durable_check
    return durable_check()


if __name__ == '__main__':
    raise SystemExit(main())
