#!/usr/bin/env python3
"""Exercise durable deposit cursors against disposable local PostgreSQL and real Ecto."""
import argparse
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
LABEL = 'org.deriw.bridge-checkpoint-fixture'
CURSOR = 'apps/indexer/lib/indexer/fetcher/arbitrum/utils/db/message_scan_cursor.ex'
TRACKER = 'apps/indexer/lib/indexer/fetcher/arbitrum/tracking_messages_on_l1.ex'


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
        database = '''
for app <- [:crypto, :ssl, :postgrex, :ecto_sql], do: ({:ok, _} = Application.ensure_all_started(app))
Application.put_env(:explorer, :chain_type, :arbitrum)
Application.put_env(:explorer, Explorer.Repo, url: System.fetch_env!("DATABASE_URL"))
{:ok, _} = Explorer.Repo.start_link(url: System.fetch_env!("DATABASE_URL"),
  pool: Ecto.Adapters.SQL.Sandbox, pool_size: 4, log: false)
Ecto.Adapters.SQL.Sandbox.mode(Explorer.Repo, :manual)
'''
        bootstrap = OLD.BOOTSTRAP.replace('Logger.configure(level: :warning)', 'Logger.configure(level: :warning)\n' + database)
        bootstrap = bootstrap.replace('Code.compile_file("/tmp/bridge-cursor/worker.ex")',
            'Code.compile_file("/tmp/bridge-cursor/cursor.ex")\n'
            'Code.compile_file("/tmp/bridge-cursor/tracker.ex")\n'
            'Code.compile_file("/tmp/bridge-cursor/worker.ex")')
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
System.halt(0)
'''
        bootstrap = bootstrap.replace('System.halt(if result.failures == 0, do: 0, else: 1)', handoff)
        sources = dict((('worker.ex', (ROOT / OLD.WORKER).read_bytes()),
                             ('test.exs', (ROOT / OLD.TEST).read_bytes()),
                             ('cursor.ex', (ROOT / CURSOR).read_bytes()),
                             ('tracker.ex', (ROOT / TRACKER).read_bytes()), ('bootstrap.exs', bootstrap.encode())))
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
