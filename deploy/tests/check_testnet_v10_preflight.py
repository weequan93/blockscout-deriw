#!/usr/bin/env python3
"""Run the test inspection through cached v9.0.1 and disposable PostgreSQL 15."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import testnet_v10_preflight as P

LABEL = 'org.deriw.testnet-preflight-fixture'


def main():
    os.umask(0o077)
    token = uuid.uuid4().hex[:16]
    logs = Path(tempfile.mkdtemp(prefix='testnet-preflight-'))
    network, pg = 'test-preflight-net-' + token, 'test-preflight-pg-' + token
    password = uuid.uuid4().hex
    created = False

    def run(command, data=None, timeout=90):
        result = subprocess.run(command, input=data, text=True, capture_output=True, timeout=timeout)
        if result.returncode:
            (logs / ('error-' + uuid.uuid4().hex[:8] + '.private.log')).write_text(result.stdout + result.stderr)
            raise RuntimeError('Disposable preflight fixture failed; private diagnostics: ' + str(logs))
        return result.stdout

    try:
        assert run(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}']).strip().startswith('unix://')
        old = run(['docker', 'image', 'inspect', '--format', '{{.Id}}', 'deriw-blockscout:v9-upgrade-baseline']).strip()
        postgres = run(['docker', 'image', 'inspect', '--format', '{{.Id}}', 'postgres:15-alpine']).strip()
        run(['docker', 'network', 'create', '--internal', '--label', LABEL + '=' + token, network])
        created = True
        run(['docker', 'run', '-d', '--pull', 'never', '--name', pg, '--label', LABEL + '=' + token,
             '--network', network, '--network-alias', 'pg', '--read-only', '--memory', '512m', '--cpus', '1',
             '--tmpfs', '/var/lib/postgresql/data:rw,size=256m', '--tmpfs', '/var/run/postgresql:rw,mode=1777',
             '--tmpfs', '/tmp:rw,mode=1777', '-e', 'POSTGRES_USER=fixture', '-e', 'POSTGRES_DB=fixture',
             '-e', 'POSTGRES_PASSWORD=' + password, postgres])
        deadline = time.monotonic() + 30
        while subprocess.run(['docker', 'exec', pg, 'pg_isready', '-q', '-h', '127.0.0.1',
                             '-U', 'fixture', '-d', 'fixture'], capture_output=True).returncode:
            assert time.monotonic() < deadline
            time.sleep(0.2)
        def sql(query):
            return run(['docker', 'exec', '-i', pg, 'psql', '-XAt', '-v', 'ON_ERROR_STOP=1',
                        '-U', 'fixture', '-d', 'fixture'], query)
        sql('CREATE TABLE internal_transactions(block_hash bytea, block_index integer, block_number integer,'
            'transaction_index integer, trace_address integer[], value numeric(100,0),'
            'PRIMARY KEY(block_hash,block_index));'
            'CREATE TABLE schema_migrations(version bigint PRIMARY KEY); INSERT INTO schema_migrations VALUES(1);'
            'CREATE TABLE migrations_status(migration_name text PRIMARY KEY,status text);' +
            ''.join('CREATE TABLE ' + table + '(id integer);' for table in P.SMALL if table != 'migrations_status'))
        primitives = P.load_primitives(ROOT)
        expression = P.expression(primitives)
        bootstrap = '''
for app <- [:crypto, :ssl, :postgrex, :ecto_sql], do: ({:ok, _} = Application.ensure_all_started(app))
{:ok, _} = Explorer.Repo.start_link(pool_size: 2)
'''
        def observe(code):
            name = 'test-preflight-eval-' + uuid.uuid4().hex[:12]
            command = ['docker', 'run', '--rm', '-i', '--pull', 'never', '--name', name,
                '--label', LABEL + '=' + token, '--network', network, '--read-only', '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges', '--memory', '768m', '--cpus', '2',
                '--tmpfs', '/tmp:rw,mode=1777', '-e', 'CHAIN_ID=2885', '-e', 'CHAIN_TYPE=arbitrum',
                '-e', 'ECTO_USE_SSL=false', '-e', 'DATABASE_URL=postgresql://fixture:' + password + '@pg:5432/fixture',
                '-e', 'RELEASE_DISTRIBUTION=none', '-e', 'RELEASE_TMP=/tmp', '-e', 'ERL_FLAGS=+S 2:2',
                '-e', 'DISABLE_FILE_LOGGING=true', '--entrypoint', 'sh', old, '-c',
                'cat > /tmp/preflight.exs && exec /app/bin/blockscout eval \'Code.eval_file("/tmp/preflight.exs")\'']
            raw = run(command, bootstrap + code)
            (logs / ('observation-' + uuid.uuid4().hex[:8] + '.private.log')).write_text(raw)
            return json.loads(next(line[len(P.MARKER):] for line in raw.splitlines() if line.startswith(P.MARKER)))
        good = observe(expression)
        assert good['runtime_version'] == '9.0.1'
        assert good['schema']['applied_versions'] == [1]
        assert good['schema']['indexes'][0][4] is True
        assert good['schema']['indexes'][0][5] == ['block_hash', 'block_index']
        public, _, _ = primitives.safe_database(good['metadata'])
        assert public['server_version_num'] // 10000 == 15
        assert all(r['has_rows'] is False for r in good['schema']['presence'])
        write = expression.replace('versions = q.(', 'q.("CREATE TABLE preflight_must_not_write(id int)", []); versions = q.(', 1)
        assert observe(write)['status'] == 'unavailable'
        assert sql("SELECT to_regclass('public.preflight_must_not_write') IS NULL;").strip() == 't'
        slow = expression.replace('versions = q.(', 'q.("SELECT pg_sleep(3)", []); versions = q.(', 1)
        assert observe(slow)['status'] == 'unavailable'
    finally:
        ids = run(['docker', 'ps', '-aq', '--filter', 'label=' + LABEL + '=' + token]).split()
        for identity in ids:
            metadata = json.loads(run(['docker', 'inspect', identity]))[0]
            assert metadata['Config']['Labels'].get(LABEL) == token
            run(['docker', 'rm', '-f', '--volumes', identity])
        if created:
            metadata = json.loads(run(['docker', 'network', 'inspect', network]))[0]
            assert metadata.get('Labels', {}).get(LABEL) == token and not metadata.get('Containers')
            run(['docker', 'network', 'rm', network])
    print(json.dumps({'v9_runtime': '9.0.1', 'postgres_major': 15, 'metadata_read': 'passed',
                      'database_writes_rejected': True, 'statement_timeout_enforced': True,
                      'live_services_accessed': False, 'fixture_resources_removed': True,
                      'private_logs': str(logs)}))


if __name__ == '__main__':
    main()
