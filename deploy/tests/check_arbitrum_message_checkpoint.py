#!/usr/bin/env python3
"""Exercise durable bridge cursors against disposable local PostgreSQL and real Ecto."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

import check_arbitrum_message_cursor as OLD

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import v10_bridge_checkpoint_deploy as DEPLOY
import deploy_dev_bridge_catchup as CATCHUP
LABEL = 'org.deriw.bridge-checkpoint-fixture'
CURSOR = 'apps/indexer/lib/indexer/fetcher/arbitrum/utils/db/message_scan_cursor.ex'
TRACKER = 'apps/indexer/lib/indexer/fetcher/arbitrum/tracking_messages_on_l1.ex'
EXTRA = {
    'rpc.ex': 'apps/indexer/lib/indexer/fetcher/arbitrum/utils/rpc.ex',
    'executions_tracker.ex': 'apps/indexer/lib/indexer/fetcher/arbitrum/tracking_batches_statuses.ex',
    'executions_worker.ex': 'apps/indexer/lib/indexer/fetcher/arbitrum/workers/new_l1_executions.ex',
    'executions_test.exs': 'apps/indexer/test/indexer/fetcher/arbitrum/workers/new_l1_executions_test.exs',
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='deriw-blockscout:v10-nft-compile')
    args = parser.parse_args()
    os.umask(0o077)
    directory = Path(tempfile.mkdtemp(prefix='bridge-checkpoint-', dir='/private/tmp'))
    token = uuid.uuid4().hex[:16]
    network, pg, client = ['bridge-checkpoint-' + name + '-' + token for name in ('network', 'pg', 'client')]
    password = uuid.uuid4().hex
    created = False

    def run(command, data=None, timeout=120):
        result = subprocess.run(command, input=data, capture_output=True, timeout=timeout)
        if result.returncode:
            private = directory / ('error-' + uuid.uuid4().hex[:8] + '.log.private')
            private.write_bytes(result.stdout + result.stderr)
            raise RuntimeError('Local bridge fixture failed; private log: ' + str(private))
        return result.stdout

    def cleanup_container(name):
        ids = run(['docker', 'ps', '-aq', '--filter', 'name=^/' + name + '$']).decode().split()
        for identity in ids:
            row = json.loads(run(['docker', 'inspect', identity]))[0]
            assert (row.get('Config', {}).get('Labels') or {}).get(LABEL) == token
            run(['docker', 'rm', '-f', '--volumes', identity])

    try:
        endpoint = run(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}']).decode().strip()
        assert endpoint.startswith('unix://'), 'A local Docker fixture is required'
        image = run(['docker', 'image', 'inspect', '--format', '{{.Id}}', args.image]).decode().strip()
        postgres = run(['docker', 'image', 'inspect', '--format', '{{.Id}}', 'postgres:15-alpine']).decode().strip()
        run(['docker', 'network', 'create', '--internal', '--label', LABEL + '=' + token, network])
        created = True
        run(['docker', 'run', '-d', '--pull', 'never', '--name', pg, '--label', LABEL + '=' + token,
             '--network', network, '--network-alias', 'pg', '--read-only', '--memory', '512m', '--cpus', '1',
             '--tmpfs', '/var/lib/postgresql/data:rw,size=256m', '--tmpfs', '/var/run/postgresql:rw,mode=1777',
             '--tmpfs', '/tmp:rw,mode=1777', '-e', 'POSTGRES_USER=fixture', '-e', 'POSTGRES_DB=fixture',
             '-e', 'POSTGRES_PASSWORD=' + password, postgres])
        deadline = time.monotonic() + 30
        while subprocess.run(['docker', 'exec', pg, 'pg_isready', '-q', '-h', '127.0.0.1', '-U', 'fixture'], capture_output=True).returncode:
            assert time.monotonic() < deadline, 'Disposable PostgreSQL did not become ready'
            time.sleep(0.2)
        run(['docker', 'exec', pg, 'psql', '-X', '-U', 'fixture', '-d', 'fixture', '-v', 'ON_ERROR_STOP=1', '-c',
             'CREATE TABLE last_fetched_counters(counter_type varchar(255) PRIMARY KEY,value numeric,'
             'inserted_at timestamp NOT NULL,updated_at timestamp NOT NULL)'])
        run(['docker', 'exec', pg, 'psql', '-X', '-U', 'fixture', '-d', 'fixture', '-v', 'ON_ERROR_STOP=1', '-c', '''
CREATE TYPE arbitrum_messages_op_type AS ENUM ('to_l2','from_l2');
CREATE TYPE arbitrum_messages_status AS ENUM ('initiated','sent','confirmed','relayed');
CREATE TYPE l1_tx_status AS ENUM ('unfinalized','finalized');
CREATE TABLE arbitrum_crosslevel_messages(direction arbitrum_messages_op_type, message_id integer,
 status arbitrum_messages_status NOT NULL, originator_address bytea, origination_timestamp timestamp,
 originating_transaction_block_number integer, originating_transaction_hash bytea, completion_transaction_hash bytea,
 inserted_at timestamp NOT NULL, updated_at timestamp NOT NULL, PRIMARY KEY(direction,message_id));
CREATE TABLE arbitrum_lifecycle_l1_transactions(id integer PRIMARY KEY, hash bytea UNIQUE NOT NULL,
 block_number integer NOT NULL, timestamp timestamp NOT NULL, status l1_tx_status NOT NULL,
 inserted_at timestamp NOT NULL, updated_at timestamp NOT NULL);
CREATE TABLE arbitrum_l1_executions(message_id integer PRIMARY KEY,
 execution_id integer NOT NULL REFERENCES arbitrum_lifecycle_l1_transactions(id),
 inserted_at timestamp NOT NULL, updated_at timestamp NOT NULL);
'''])
        database = '''
for app <- [:crypto, :ssl, :postgrex, :ecto_sql, :prometheus_ex], do: ({:ok, _} = Application.ensure_all_started(app))
Prometheus.Metric.Histogram.declare(name: :block_import_stage_runner_duration_microseconds,
  labels: [:stage, :runner, :step], help: "Fixture imports")
Application.put_env(:explorer, :chain_type, :arbitrum)
Application.put_env(:explorer, Explorer.Repo, url: System.fetch_env!("DATABASE_URL"))
{:ok, _} = Explorer.Repo.start_link(url: System.fetch_env!("DATABASE_URL"),
  pool: Ecto.Adapters.SQL.Sandbox, pool_size: 4, log: false)
Ecto.Adapters.SQL.Sandbox.mode(Explorer.Repo, :manual)
'''
        bootstrap = OLD.BOOTSTRAP.replace('Logger.configure(level: :warning)', 'Logger.configure(level: :warning)\n' + database)
        bootstrap = bootstrap.replace('Code.compile_file("/tmp/bridge-cursor/worker.ex")',
            'Code.compile_file("/tmp/bridge-cursor/cursor.ex")\n'
            'Code.compile_file("/tmp/bridge-cursor/rpc.ex")\n'
            'Code.compile_file("/tmp/bridge-cursor/tracker.ex")\n'
            'Code.compile_file("/tmp/bridge-cursor/executions_tracker.ex")\n'
            'Code.compile_file("/tmp/bridge-cursor/executions_worker.ex")\n'
            'Code.compile_file("/tmp/bridge-cursor/worker.ex")')
        bootstrap = bootstrap.replace('result = ExUnit.run()',
            'Code.require_file("/tmp/bridge-cursor/executions_test.exs")\nresult = ExUnit.run()')
        handoff = '''
if result.failures != 0, do: System.halt(1)
System.put_env("MIGRATION_BACKGROUND_AUTOSTART_DISABLED", "true")
Application.put_env(:explorer, :background_migrations_autostart_disabled, true)
Application.put_env(:block_scout_web, :version, "''' + DEPLOY.BASE + '''")
defmodule BridgeCheckpointFixtureTracker do
  use GenServer
  def init(state), do: {:ok, state}
end
{:ok, _} = GenServer.start_link(BridgeCheckpointFixtureTracker, %{callback_module_state: %{
  config: %{l1_rollup_address: "0x0000000000000000000000000000000000000002",
    l1_bridge_address: "0x0000000000000000000000000000000000000001",
    json_l1_rpc_named_arguments: [transport: EthereumJSONRPC.Mox, transport_options: []]},
  task_data: %{check_new: %{start_block: 278500000}}}}, name: Indexer.Fetcher.Arbitrum.TrackingMessagesOnL1)
Mox.expect(EthereumJSONRPC.Mox,:json_rpc,fn %{method: "eth_chainId"},_ -> {:ok,"0x66eee"} end)
Ecto.Adapters.SQL.Sandbox.unboxed_run(Explorer.Repo, fn ->
''' + DEPLOY.seed_expression() + '''
  key = Indexer.Fetcher.Arbitrum.Utils.Db.MessageScanCursor.key(421614,
    "0x0000000000000000000000000000000000000002", "0x0000000000000000000000000000000000000001")
  278500000 = Indexer.Fetcher.Arbitrum.Utils.Db.MessageScanCursor.resume(key,278124097)
end)
Mox.verify!()
System.put_env("CHAIN_ID", "18417507517")
Application.put_env(:indexer, Indexer.Fetcher.Arbitrum, l1_rpc_block_range: 50)
rollup = "0xddf009b4879eaffa6a23782f9aa30f86cd2c64e6"
contract = "0x0000000000000000000000000000000000000001"
args = [transport: EthereumJSONRPC.Mox, transport_options: []]
deposit_key = Indexer.Fetcher.Arbitrum.Utils.Db.MessageScanCursor.key(421614, rollup, contract)
execution_key = Indexer.Fetcher.Arbitrum.Utils.Db.MessageScanCursor.execution_key(421614, rollup, contract)
:sys.replace_state(Process.whereis(Indexer.Fetcher.Arbitrum.TrackingMessagesOnL1), fn _ ->
  %{callback_module_state: %{config: %{l1_rollup_address: rollup, l1_bridge_address: contract,
      l1_messages_scan_cursor_key: deposit_key, json_l1_rpc_named_arguments: args},
    task_data: %{check_new: %{start_block: 280421247}}, intervals: %{check_new: 10000}}}
end)
{:ok, _} = GenServer.start_link(BridgeCheckpointFixtureTracker, %{callback_module_state: %{
  config: %{l1_rollup_address: rollup, l1_outbox_address: contract,
    l1_executions_scan_cursor_key: execution_key, l1_rpc: %{json_rpc_named_arguments: args, logs_block_range: 50}},
  task_data: %{new_executions: %{start_block: 324655961}}, intervals: %{new_executions: 3000}}},
  name: Indexer.Fetcher.Arbitrum.TrackingBatchesStatuses)
Mox.expect(EthereumJSONRPC.Mox, :json_rpc, 2, fn %{method: "eth_chainId"}, _ -> {:ok, "0x66eee"} end)
Ecto.Adapters.SQL.Sandbox.unboxed_run(Explorer.Repo, fn ->
''' + CATCHUP.seed_expression(DEPLOY.BASE) + '''
  324655961 = Indexer.Fetcher.Arbitrum.Utils.Db.MessageScanCursor.resume(execution_key, 319000001)
end)
Mox.verify!()
Application.put_env(:indexer, Indexer.Fetcher.Arbitrum.TrackingMessagesOnL1, l1_rpc_block_range: 10000)
Application.put_env(:indexer, Indexer.Fetcher.Arbitrum.TrackingBatchesStatuses, l1_executions_block_range: 10000)
for {module, field} <- [
  {Indexer.Fetcher.Arbitrum.TrackingMessagesOnL1, :l1_rpc_block_range},
  {Indexer.Fetcher.Arbitrum.TrackingBatchesStatuses, :l1_executions_block_range}
] do
  :sys.replace_state(Process.whereis(module), fn state ->
    put_in(state, [:callback_module_state, :config, field], 10000)
  end)
end
Ecto.Adapters.SQL.Sandbox.unboxed_run(Explorer.Repo, fn ->
''' + CATCHUP.verify_expression({'common_range': 50, 'cursors': [
    {'tracker': 'deposits', 'counter_key': 'arbitrum_to_l2_scan_v1:' + hashlib.sha256(
        b'421614:0xddf009b4879eaffa6a23782f9aa30f86cd2c64e6:0x0000000000000000000000000000000000000001').hexdigest(),
     'saved_next_parent_block': 280421247, 'interval_ms': 10000},
    {'tracker': 'withdrawals', 'counter_key': 'arbitrum_to_l1_execution_scan_v1:' + hashlib.sha256(
        b'421614:0xddf009b4879eaffa6a23782f9aa30f86cd2c64e6:0x0000000000000000000000000000000000000001').hexdigest(),
     'saved_next_parent_block': 324655961, 'interval_ms': 3000}]}) + '''
end)
System.halt(0)
'''
        bootstrap = bootstrap.replace('System.halt(if result.failures == 0, do: 0, else: 1)', handoff)
        sources = dict((('worker.ex', (ROOT / OLD.WORKER).read_bytes()),
                             ('test.exs', (ROOT / OLD.TEST).read_bytes()),
                             ('cursor.ex', (ROOT / CURSOR).read_bytes()),
                             ('tracker.ex', (ROOT / TRACKER).read_bytes()), ('bootstrap.exs', bootstrap.encode())))
        sources.update({name: (ROOT / path).read_bytes() for name, path in EXTRA.items()})
        def archive_sources(values):
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode='w') as tar:
                for name, raw in values.items():
                    entry = tarfile.TarInfo(name)
                    entry.mode, entry.size = 0o600, len(raw)
                    tar.addfile(entry, io.BytesIO(raw))
            return archive.getvalue()
        command = ['docker', 'run', '--rm', '-i', '--pull', 'never', '--name', client,
            '--label', LABEL + '=' + token, '--network', network, '--read-only', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges', '--tmpfs', '/tmp:rw,size=128m', '--cpus', '2', '--memory', '1g',
            '--env', 'ERL_FLAGS=+S 2:2', '--env', 'ERL_LIBS=/app/_build/prod/lib',
            '--env', 'DATABASE_URL=postgresql://fixture:' + password + '@pg:5432/fixture', '--env', 'ECTO_USE_SSL=false',
            '--entrypoint', '/bin/sh', image, '-c',
            'mkdir /tmp/bridge-cursor && tar -xf - -C /tmp/bridge-cursor '
            '&& exec elixir /tmp/bridge-cursor/bootstrap.exs']
        output = run(command, archive_sources(sources))
        (directory / 'exunit.log.private').write_bytes(output)
        restart = '''
for app <- [:crypto, :ssl, :postgrex, :ecto_sql], do: ({:ok, _} = Application.ensure_all_started(app))
Application.put_env(:explorer, Explorer.Repo, url: System.fetch_env!("DATABASE_URL"))
{:ok,_}=Explorer.Repo.start_link(pool: Ecto.Adapters.SQL.Sandbox,pool_size: 2,log: false)
Code.compile_file("/tmp/bridge-cursor/cursor.ex")
Ecto.Adapters.SQL.Sandbox.unboxed_run(Explorer.Repo,fn ->
  key=Indexer.Fetcher.Arbitrum.Utils.Db.MessageScanCursor.key(421614,
    "0x0000000000000000000000000000000000000002", "0x0000000000000000000000000000000000000001")
  278500000=Indexer.Fetcher.Arbitrum.Utils.Db.MessageScanCursor.resume(key,278124097)
  278500050=Indexer.Fetcher.Arbitrum.Utils.Db.MessageScanCursor.advance(key,278500050)
  execution_key=Indexer.Fetcher.Arbitrum.Utils.Db.MessageScanCursor.execution_key(421614,
    "0xddf009b4879eaffa6a23782f9aa30f86cd2c64e6", "0x0000000000000000000000000000000000000001")
  324655961=Indexer.Fetcher.Arbitrum.Utils.Db.MessageScanCursor.resume(execution_key,319000001)
  324656011=Indexer.Fetcher.Arbitrum.Utils.Db.MessageScanCursor.advance(execution_key,324656011)
end)
'''
        (directory / 'fresh-process.log.private').write_bytes(run(command, archive_sources(
            {'cursor.ex': sources['cursor.ex'], 'bootstrap.exs': restart.encode()})))
    finally:
        cleanup_container(client)
        cleanup_container(pg)
        if created:
            metadata = json.loads(run(['docker', 'network', 'inspect', network]))[0]
            assert (metadata.get('Labels') or {}).get(LABEL) == token and not metadata.get('Containers')
            run(['docker', 'network', 'rm', network])
    print(json.dumps({'status': 'passed', 'live_services_accessed': False, 'fixture_resources_removed': True,
                      'real_postgresql_and_ecto': True, 'bridge_cursor_restart_tests': True,
                      'live_cursor_handoff_matches_native_key': True, 'fresh_beam_process_resumes_saved_cursor': True,
                      'private_logs': str(directory)}, indent=2))


if __name__ == '__main__':
    main()
