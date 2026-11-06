#!/usr/bin/env python3
"""Check the exact metadata probe on disposable PG15, using the real v9 release."""
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import check_upgrade_database as F
import deploy_check as C
import dev_config as D
import v10_review as V


def main():
    token = uuid.uuid4().hex[:16]
    network = 'v10-review-' + token
    pg = 'v10-review-pg-' + token
    client = 'v10-review-client-' + token
    password = uuid.uuid4().hex
    created = False
    try:
        plan = D.read_json(ROOT / 'deploy/upgrades/v10.2.6.json')
        inventory = C.image_inventory(D, 'deriw-blockscout:v10-held-local-validation')
        assert inventory['migrations'] == plan['target_migration_sha256']
        F.run(['docker', 'network', 'create', '--internal', '--label', F.LABEL + '=' + token, network])
        created = True
        F.run(['docker', 'run', '--rm', '-d', '--pull', 'never', '--name', pg, '--label', F.LABEL + '=' + token,
               '--network', network, '--network-alias', 'pg', '--read-only', '--cpus', '2', '--memory', '512m',
               '--tmpfs', '/var/lib/postgresql/data:rw,mode=0700,size=256m',
               '--tmpfs', '/var/run/postgresql:rw,mode=1777,size=16m', '--tmpfs', '/tmp:rw,size=16m',
               '-e', 'POSTGRES_USER=fixture', '-e', 'POSTGRES_DB=fixture', '-e', 'POSTGRES_PASSWORD=' + password,
               'postgres:15-alpine'])
        deadline = time.monotonic() + 30
        while subprocess.run(['docker', 'exec', pg, 'pg_isready', '-q', '-h', '127.0.0.1', '-U', 'fixture', '-d', 'fixture'],
                             capture_output=True, timeout=5).returncode:
            if time.monotonic() > deadline:
                raise RuntimeError('Disposable PG15 did not become ready')
            time.sleep(1)
        script = '''
%URI{host: "pg", path: "/fixture", port: 5432} = URI.parse(System.fetch_env!("DATABASE_URL"));
for app <- [:crypto, :ssl, :postgrex, :ecto_sql], do: ({:ok, _} = Application.ensure_all_started(app));
{:ok, _} = Explorer.Repo.start_link(pool_size: 1);
q = fn sql -> Explorer.Repo.query!(sql, [], log: false) end;
[["fixture", version, false]] = q.("SELECT current_database(), current_setting('server_version_num')::integer, pg_is_in_recovery()").rows;
true = version >= 150000 and version < 160000;
probe = fn ->
  {_, binding} = Code.eval_string(PROBE);
  %{status: "ok", tables: rows} = Keyword.fetch!(binding, :data);
  Map.new(rows, &{&1.table, &1})
end;
rows = probe.();
true = Enum.all?(rows, fn {_, row} -> row.exists == false and is_nil(row.has_rows) end);
q.("CREATE TABLE account_public_tags_requests(id bigint)");
q.("CREATE TABLE multichain_search_db_main_export_queue(id bigint)");
%{exists: true, has_rows: false} = probe.()["account_public_tags_requests"];
q.("INSERT INTO account_public_tags_requests VALUES (1)");
rows = probe.();
%{exists: true, has_rows: true} = rows["account_public_tags_requests"];
%{exists: true, has_rows: false} = rows["multichain_search_db_main_export_queue"];
[[nil]] = q.("SELECT to_regclass('public.multichain_search_db_export_balances_queue')::text").rows;
nil = Process.whereis(Explorer.Supervisor);
IO.puts("V10_REVIEW_FIXTURE_OK");
'''.replace('PROBE', F.elixir_string(V.presence_expression()))
        environment = {'DATABASE_URL': 'postgresql://fixture:' + password + '@pg:5432/fixture', 'ECTO_USE_SSL': 'false',
                       'CHAIN_TYPE': 'arbitrum', 'CHAIN_ID': '18417507517', 'SECRET_KEY_BASE': 'a' * 64,
                       'RELEASE_DISTRIBUTION': 'none', 'RELEASE_TMP': '/tmp', 'ERL_FLAGS': '+S 2:2',
                       'DISABLE_INDEXER': 'true', 'DISABLE_MARKET': 'true', 'DISABLE_FILE_LOGGING': 'true',
                       'ETHEREUM_JSONRPC_HTTP_URL': 'http://127.0.0.1:1', 'POOL_SIZE': '1'}
        command = ['docker', 'run', '--rm', '-i', '--pull', 'never', '--name', client, '--label', F.LABEL + '=' + token,
                   '--network', network, '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                   '--cpus', '2', '--memory', '768m', '--tmpfs', '/tmp:rw,mode=1777']
        for key, value in environment.items():
            command += ['-e', key + '=' + value]
        command += ['--entrypoint', 'sh', 'deriw-blockscout:v9-upgrade-baseline', '-c',
                    'cat > /tmp/probe.exs && exec /app/bin/blockscout eval \'Code.eval_file("/tmp/probe.exs")\'']
        result = subprocess.run(command, input=script, capture_output=True, text=True, timeout=90)
        if result.returncode or 'V10_REVIEW_FIXTURE_OK' not in result.stdout.splitlines():
            raise RuntimeError('Read-only fixture failed: ' + result.stdout[-1500:] + result.stderr[-1500:])
        print(json.dumps({'status': 'passed', 'candidate_image_migration_hashes_match': True,
                          'postgres_major': 15, 'actual_v9_release_probe_checked': True,
                          'absent_empty_and_nonempty_tables_checked': True, 'application_started': False,
                          'live_services_accessed': False}))
        return 0
    finally:
        F.cleanup_container(client, token)
        F.cleanup_container(pg, token)
        if created:
            F.run(['docker', 'network', 'rm', network])


if __name__ == '__main__':
    raise SystemExit(main())
