#!/usr/bin/env python3
"""Verify native /metrics with cached Elixir dependencies and disposable PG15.

Runs ExUnit and the real bounded queries, compares native series to the legacy
exporter, and checks database deadlines and read-only enforcement. No live
configuration, host bind mounts, published ports or application startup.
"""
import argparse
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

from check_deriw_bridge_metrics import SCHEMA, run

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import deriw_bridge_metrics as legacy

LABEL = 'org.deriw.native-bridge-metrics-fixture'
BOOT = '''
Path.wildcard("/app/_build/prod/lib/*/ebin") |> Enum.each(&Code.prepend_path/1)
for app <- [:logger, :prometheus, :prometheus_ex, :prometheus_plugs, :postgrex, :ecto_sql],
  do: {:ok, _} = Application.ensure_all_started(app)
Application.put_env(:plug, :validate_header_keys_during_test, true)
Application.put_env(:block_scout_web, :disable_api?, false)
Code.compiler_options(ignore_module_conflict: true)
files = Path.wildcard("/tmp/native/apps/indexer/lib/indexer/prometheus/**/*.ex") ++
  ["/tmp/native/apps/block_scout_web/lib/block_scout_web/application.ex"]
{:ok, _, %{compile_warnings: [], runtime_warnings: []}} =
  Kernel.ParallelCompiler.compile(files, return_diagnostics: true)
'''
OBSERVE = '''
alias Indexer.Prometheus.ArbitrumBridgeMetrics
alias Indexer.Prometheus.ArbitrumBridgeMetrics.{Collector, Source}
Application.put_env(:explorer, Explorer.Repo,
  url: System.fetch_env!("DATABASE_URL"), pool_size: 2, log: false)
{:ok, _} = Explorer.Repo.start_link()
data = Source.collect()
:ok = Source.validate!(data)
Application.put_env(:explorer, :chain_type, :arbitrum)
Application.put_env(:nft_media_handler, :standalone_media_worker?, false)
Application.put_env(:indexer, ArbitrumBridgeMetrics, enabled: true)
{:ok, worker} = ArbitrumBridgeMetrics.start_link(sampler: fn -> data end)
:sys.get_state(worker)
BlockScoutWeb.Prometheus.Exporter.setup()
conn = Plug.Test.conn("GET", "/metrics")
  |> BlockScoutWeb.Prometheus.Exporter.call(BlockScoutWeb.Prometheus.Exporter.init([]))
200 = conn.status
failed = %{
  write: Source.sample("INSERT INTO blocks VALUES(3000,true,now()) RETURNING '{}'::jsonb"),
  timeout: Source.sample("SELECT '{}'::jsonb FROM pg_sleep(3)"),
  budget: Source.sample("SELECT to_jsonb(x) FROM generate_series(1,100000000) x ORDER BY x DESC LIMIT 1"),
  missing: Source.sample("SELECT missing_fixture_table FROM fixture_does_not_exist")
}
IO.puts("NATIVE_BRIDGE_METRICS=" <> Jason.encode!(%{data: data, text: conn.resp_body, failed: failed}))
'''


def archive(script):
    content = io.BytesIO()
    files = [
        *ROOT.glob('apps/indexer/lib/indexer/prometheus/arbitrum_bridge_metrics/**/*.ex'),
        ROOT / 'apps/indexer/lib/indexer/prometheus/arbitrum_bridge_metrics.ex',
        ROOT / 'apps/indexer/priv/arbitrum_bridge_metrics_queries.json',
        ROOT / 'apps/indexer/test/indexer/prometheus/arbitrum_bridge_metrics_test.exs',
        ROOT / 'apps/indexer/test/indexer/prometheus/fixtures/arbitrum_bridge_metrics.json',
        ROOT / 'apps/block_scout_web/lib/block_scout_web/application.ex',
        ROOT / 'apps/block_scout_web/test/block_scout_web/prometheus/arbitrum_bridge_metrics_test.exs',
    ]
    with tarfile.open(fileobj=content, mode='w') as tar:
        for path in files:
            tar.add(path, arcname=str(path.relative_to(ROOT)))
        data = (BOOT + script).encode()
        entry = tarfile.TarInfo('run.exs')
        entry.size = len(data)
        tar.addfile(entry, io.BytesIO(data))
    return content.getvalue()


def series(text):
    result = {}
    for line in text.splitlines():
        if not line.startswith(('deriw_bridge_', 'deriw_rollup_')):
            continue
        key, value = line.rsplit(' ', 1)
        name = key.split('{', 1)[0]
        labels = tuple(sorted(re.findall(r'(\w+)="([^"]*)"', key)))
        identity = (name, labels)
        assert identity not in result, 'Duplicate metric series'
        result[identity] = float(value)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres-image', default='postgres:15-alpine')
    parser.add_argument('--app-image', default='deriw-blockscout:v10-nft-compile')
    args = parser.parse_args()
    assert json.loads((ROOT / 'apps/indexer/priv/arbitrum_bridge_metrics_queries.json').read_text()) == \
        json.loads((ROOT / 'deploy/scripts/v10/bridge_metrics_queries.json').read_text())
    host = run(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}']).strip()
    assert host.startswith('unix://'), 'Fixture requires a local Docker socket'
    token = uuid.uuid4().hex[:16]
    network, pg = 'native-bridge-' + token, 'native-bridge-pg-' + token
    password = uuid.uuid4().hex
    image = json.loads(run(['docker', 'image', 'inspect', args.app_image]))[0]['Id']
    pg_image = json.loads(run(['docker', 'image', 'inspect', args.postgres_image]))[0]['Id']
    created = False

    def execute(script, network_name='none', database=False):
        command = ['docker', 'run', '--rm', '--pull', 'never', '-i', '--label', LABEL + '=' + token,
                   '--network', network_name, '--read-only', '--memory', '1g', '--cpus', '2',
                   '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                   '--tmpfs', '/tmp:rw,mode=1777,size=256m', '-e', 'ERL_FLAGS=+S 2:2']
        if database:
            command += ['-e', 'DATABASE_URL=postgresql://fixture:' + password + '@pg:5432/fixture',
                        '-e', 'ECTO_USE_SSL=false']
        command += ['--entrypoint', 'sh', image, '-c',
                    'mkdir /tmp/native && tar --no-same-owner -xf - -C /tmp/native && elixir /tmp/native/run.exs']
        completed = subprocess.run(command, input=archive(script), capture_output=True, timeout=60)
        if completed.returncode:
            with tempfile.NamedTemporaryFile(prefix='native-bridge-', suffix='.log.private', delete=False) as log:
                log.write(completed.stdout + completed.stderr)
            raise RuntimeError('Native metrics fixture failed; private details: ' + log.name)
        return completed.stdout.decode()

    unit_output = execute('ExUnit.start()\nCode.require_file("/tmp/native/apps/indexer/test/indexer/prometheus/arbitrum_bridge_metrics_test.exs")\nCode.require_file("/tmp/native/apps/block_scout_web/test/block_scout_web/prometheus/arbitrum_bridge_metrics_test.exs")\n')
    print(unit_output.strip(), flush=True)
    try:
        run(['docker', 'network', 'create', '--internal', '--label', LABEL + '=' + token, network])
        created = True
        run(['docker', 'run', '--pull', 'never', '-d', '--name', pg, '--label', LABEL + '=' + token,
             '--network', network, '--network-alias', 'pg', '--read-only', '--memory', '512m',
             '--tmpfs', '/var/lib/postgresql/data:rw,size=256m', '--tmpfs', '/var/run/postgresql:rw',
             '--tmpfs', '/tmp:rw', '-e', 'POSTGRES_USER=fixture', '-e', 'POSTGRES_PASSWORD=' + password,
             '-e', 'POSTGRES_DB=fixture', pg_image])
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            ready = subprocess.run(['docker', 'exec', pg, 'pg_isready', '-h', '127.0.0.1', '-U', 'fixture', '-d', 'fixture'], capture_output=True)
            if ready.returncode == 0:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError('Disposable PostgreSQL did not start')

        def sql(text):
            return run(['docker', 'exec', '-i', pg, 'psql', '-X', '-v', 'ON_ERROR_STOP=1',
                        '-At', '-U', 'fixture', '-d', 'fixture'], input=text)

        sql(SCHEMA)
        for truncated in (False, True):
            if truncated:
                sql("INSERT INTO arbitrum_crosslevel_messages SELECT 'to_l2',n,'initiated',to_timestamp(1700000000),decode('01','hex'),NULL FROM generate_series(4,10002) n; UPDATE arbitrum_batch_l2_blocks SET confirmation_id=NULL WHERE block_number>1500; UPDATE arbitrum_lifecycle_l1_transactions SET status='finalized' WHERE id=2;")
            output = execute(OBSERVE, network, True)
            result = json.loads(next(line.split('=', 1)[1] for line in output.splitlines()
                                     if line.startswith('NATIVE_BRIDGE_METRICS=')))
            data = result['data']
            legacy.validate(data)
            assert all(s['ok'] for s in data['sections'].values()), data['sections']
            expected = legacy.render(data, data['observed_at_unix'], True, now=data['observed_at_unix'])
            # Collection and registration can straddle a Unix second.
            actual = series(result['text'])
            expected_series = series(expected)
            timestamp = ('deriw_bridge_exporter_last_success_timestamp_seconds', ())
            actual[timestamp] = expected_series[timestamp]
            assert actual == expected_series, 'Native endpoint differs from the legacy metric series'
            assert data['sections']['deposits']['data']['complete'] is not truncated
            assert data['sections']['confirmed']['data']['block_number'] == (1500 if truncated else 1600)
            assert result['failed']['write']['reason'] == 'read_only_denied'
            assert result['failed']['timeout']['reason'] == 'statement_timeout'
            assert result['failed']['budget']['reason'] == 'plan_budget'
            assert result['failed']['missing']['reason'] == 'query_failed'
            assert sql('SELECT count(*) FROM blocks WHERE number=3000;').strip() == '0'
        print(json.dumps({'status': 'passed', 'postgres_major': 15,
                          'native_endpoint_matches_legacy_series': True, 'read_only_and_query_budgets': True,
                          'sample_truncation_and_reorg_regression': True,
                          'live_services_accessed': False, 'backend_application_started': False}, indent=2))
    finally:
        for identity in run(['docker', 'ps', '-aq', '--filter', 'label=' + LABEL + '=' + token]).split():
            metadata = json.loads(run(['docker', 'inspect', identity]))[0]
            assert metadata['Config']['Labels'][LABEL] == token
            run(['docker', 'rm', '-f', identity])
        if created:
            run(['docker', 'network', 'rm', network])


if __name__ == '__main__':
    main()
