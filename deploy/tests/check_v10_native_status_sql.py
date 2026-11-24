#!/usr/bin/env python3
"""Check the controller's actual read-only status SQL in isolated local PG15."""
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import v10_native_normalization as N


def command(args, sql=None, timeout=20):
    return subprocess.run(args, input=sql, text=True, capture_output=True, check=True, timeout=timeout).stdout


def main():
    name = 'blockscout-native-status-fixture-' + uuid.uuid4().hex[:12]
    container = None
    try:
        container = command(['docker', 'run', '--detach', '--rm', '--pull=never', '--name', name,
            '--network', 'none', '--tmpfs', '/var/lib/postgresql/data',
            '--env', 'POSTGRES_HOST_AUTH_METHOD=trust', 'postgres:15-alpine']).strip()
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            # The image's bootstrap server accepts Unix sockets before restart.
            # TCP becomes available only on its final server.
            ready = subprocess.run(['docker', 'exec', container, 'pg_isready', '-h', '127.0.0.1', '-U', 'postgres'],
                                   capture_output=True, timeout=5)
            if ready.returncode == 0:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError('Isolated PG15 fixture did not become ready')
        psql = ['docker', 'exec', '-i', container, 'psql', '-h', '127.0.0.1', '-U', 'postgres',
                '-v', 'ON_ERROR_STOP=1', '-tA']
        version = int(command(psql, "SHOW server_version_num;"))
        assert 150000 <= version < 160000, 'Fixture must use PostgreSQL 15'
        command(psql, """
CREATE TABLE public.migrations_status (migration_name text, status text, meta jsonb);
CREATE TABLE public.internal_transactions (block_number bigint);
INSERT INTO public.migrations_status VALUES
  ('empty_internal_transactions_data', 'started', '{"max_block_number": 123, "v10_native_job": "fixture"}');
""")
        query = N.METADATA.split('[[result]] = q.(~S"""', 1)[1].split('""", [NATIVE_APPLICATION_NAME])', 1)[0]
        output = command(psql, "BEGIN READ ONLY;\nSET LOCAL statement_timeout='1500ms';\n"
                         + 'PREPARE native_status(text) AS ' + query + ';\n'
                         + "EXECUTE native_status('native-status-fixture');\nROLLBACK;\n")
        reports = [json.loads(line) for line in output.splitlines() if line.startswith('{')]
        assert len(reports) == 1, 'Expected one status report'
        report = reports[0]
        assert report['native_status'] == 'started'
        assert report['native_meta']['max_block_number'] == 123
        assert report['autovacuum_enabled'] is True
        assert report['autovacuum_running'] == 0 and report['vacuum_progress'] == []
        assert report['long_client_transactions'] == 0
        print(json.dumps({'status': 'native_status_sql_verified', 'postgres_version_num': version,
                          'read_only_transaction': True, 'host_mounts': False, 'published_ports': False}))
    finally:
        if container:
            command(['docker', 'rm', '--force', container])


if __name__ == '__main__':
    main()
