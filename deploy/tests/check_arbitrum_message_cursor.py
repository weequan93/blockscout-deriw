#!/usr/bin/env python3
"""Run bridge cursor regressions in an existing Elixir image without network or DB access."""
import argparse
import io
from pathlib import Path
import subprocess
import tarfile

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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='deriw-blockscout:v10-nft-compile')
    args = parser.parse_args()
    image = subprocess.check_output(
        ['docker', 'image', 'inspect', '--format', '{{.Id}}', args.image], text=True
    ).strip()
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w') as tar:
        for name, content in (
            ('worker.ex', (ROOT / WORKER).read_bytes()),
            ('test.exs', (ROOT / TEST).read_bytes()),
            ('bootstrap.exs', BOOTSTRAP.encode()),
        ):
            entry = tarfile.TarInfo(name)
            entry.mode = 0o600
            entry.size = len(content)
            tar.addfile(entry, io.BytesIO(content))
    result = subprocess.run([
        'docker', 'run', '--rm', '-i', '--pull', 'never', '--network', 'none',
        '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
        '--tmpfs', '/tmp:rw,size=128m', '--cpus', '2', '--memory', '1g',
        '--env', 'ERL_FLAGS=+S 2:2', '--env', 'ERL_LIBS=/app/_build/prod/lib',
        '--entrypoint', '/bin/sh', image, '-c',
        'mkdir /tmp/bridge-cursor && tar -xf - -C /tmp/bridge-cursor '
        '&& exec elixir /tmp/bridge-cursor/bootstrap.exs',
    ], input=archive.getvalue(), timeout=120)
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
