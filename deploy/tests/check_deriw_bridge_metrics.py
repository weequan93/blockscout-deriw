#!/usr/bin/env python3
"""Test the actual metrics RPC with synthetic PG15 rows in disposable containers.

Uses cached local images, an internal network, tmpfs PostgreSQL, no published
ports, no host mounts, and no live configuration. Never starts the Explorer app.
"""
import argparse
import base64
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
import deriw_bridge_metrics as M

LABEL = 'org.deriw.bridge-metrics-fixture'

SCHEMA = '''
CREATE TYPE arbitrum_messages_op_type AS ENUM ('to_l2','from_l2');
CREATE TYPE arbitrum_messages_status AS ENUM ('initiated','sent','confirmed','relayed');
CREATE TYPE l1_tx_status AS ENUM ('unfinalized','finalized');
CREATE TABLE blocks(number bigint, consensus boolean, timestamp timestamp);
CREATE UNIQUE INDEX blocks_consensus_number ON blocks(number) WHERE consensus;
CREATE TABLE arbitrum_crosslevel_messages(direction arbitrum_messages_op_type, message_id integer,
 status arbitrum_messages_status NOT NULL, origination_timestamp timestamp,
 originating_transaction_hash bytea, completion_transaction_hash bytea, PRIMARY KEY(direction,message_id));
CREATE TABLE arbitrum_lifecycle_l1_transactions(id integer PRIMARY KEY, timestamp timestamp, status l1_tx_status);
CREATE TABLE arbitrum_l1_batches(number integer PRIMARY KEY, end_block integer, commitment_id integer NOT NULL);
CREATE TABLE arbitrum_batch_l2_blocks(block_number integer PRIMARY KEY, batch_number integer NOT NULL, confirmation_id integer);
CREATE INDEX arbitrum_batch_l2_blocks_confirmation_id_index ON arbitrum_batch_l2_blocks(confirmation_id);
INSERT INTO blocks SELECT n,true,to_timestamp(1700000000+n) FROM generate_series(1,2000) n;
INSERT INTO blocks VALUES (9999,false,now());
INSERT INTO arbitrum_lifecycle_l1_transactions VALUES
 (1,to_timestamp(1700000000),'finalized'),(2,to_timestamp(1700000100),'unfinalized'),
 (3,to_timestamp(1700000200),'finalized'),(4,to_timestamp(1700000300),'unfinalized');
INSERT INTO arbitrum_l1_batches VALUES (1,1900,1),(2,1950,2);
INSERT INTO arbitrum_batch_l2_blocks SELECT n,CASE WHEN n<=1900 THEN 1 ELSE 2 END,
 CASE WHEN n<=1500 THEN 3 WHEN n<=1600 THEN 4 ELSE NULL END FROM generate_series(1,1950) n;
INSERT INTO arbitrum_crosslevel_messages VALUES
 ('to_l2',1,'initiated',to_timestamp(1700000000),decode('01','hex'),NULL),
 ('to_l2',2,'relayed',to_timestamp(1700000010),decode('02','hex'),decode('03','hex')),
 ('to_l2',3,'relayed',NULL,NULL,decode('04','hex')),
 ('from_l2',1,'initiated',to_timestamp(1700000100),decode('05','hex'),NULL),
 ('from_l2',2,'sent',to_timestamp(1700000200),decode('06','hex'),NULL),
 ('from_l2',3,'confirmed',to_timestamp(1700000300),decode('07','hex'),NULL),
 ('from_l2',4,'relayed',to_timestamp(1700000400),decode('08','hex'),decode('09','hex'));
ANALYZE;
'''


def run(command, input=None, timeout=60):
    result = subprocess.run(command, input=input, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        with tempfile.NamedTemporaryFile(prefix='deriw-bridge-metrics-', suffix='.log.private', mode='w', delete=False) as log:
            log.write(result.stdout + result.stderr)
        raise RuntimeError('Disposable fixture command failed; private log: ' + log.name)
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres-image', default='postgres:15-alpine')
    parser.add_argument('--app-image', default='sha256:c3246a1f283b2d3685b83d3c0458107cbc3932671d52f7bf0c641c19bbfc110f')
    args = parser.parse_args()
    token = uuid.uuid4().hex[:16]
    network, pg = 'bridge-metrics-' + token, 'bridge-metrics-pg-' + token
    password = uuid.uuid4().hex
    created = False
    try:
        # Only cached image identities. A remote Docker context is not accepted.
        host = run(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}']).strip()
        assert host.startswith('unix://')
        pg_image = json.loads(run(['docker', 'image', 'inspect', args.postgres_image]))[0]['Id']
        image = json.loads(run(['docker', 'image', 'inspect', args.app_image]))[0]['Id']
        run(['docker', 'network', 'create', '--internal', '--label', LABEL + '=' + token, network])
        created = True
        run(['docker', 'run', '--pull', 'never', '-d', '--name', pg, '--label', LABEL + '=' + token,
             '--network', network, '--network-alias', 'pg', '--read-only', '--memory', '512m', '--cpus', '2',
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
            raise RuntimeError('Disposable PostgreSQL startup timed out')

        def sql(text):
            return run(['docker', 'exec', '-i', pg, 'psql', '-X', '-v', 'ON_ERROR_STOP=1', '-At', '-U', 'fixture', '-d', 'fixture'], input=text)

        sql(SCHEMA)

        def observe(extra_queries=None):
            queries = json.loads((ROOT / 'deploy/scripts/v10/bridge_metrics_queries.json').read_text())
            queries.update(extra_queries or {})
            encoded = base64.b64encode(json.dumps(queries).encode()).decode()
            script = '''
for app <- [:crypto, :ssl, :postgrex, :ecto_sql] do
  {:ok, _} = Application.ensure_all_started(app)
end
{:ok, _} = Explorer.Repo.start_link(pool_size: 2)
bridge_metric_queries = Jason.decode!(Base.decode64!("''' + encoded + '''"))
''' + (ROOT / 'deploy/scripts/v10/bridge_metrics.exs').read_text()
            command = ['docker', 'run', '--rm', '--pull', 'never', '-i', '--label', LABEL + '=' + token,
                '--network', network, '--read-only', '--memory', '768m', '--cpus', '2', '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges', '--tmpfs', '/tmp:rw,mode=1777',
                '-e', 'CHAIN_TYPE=arbitrum', '-e', 'CHAIN_ID=18417507517', '-e', 'ECTO_USE_SSL=false',
                '-e', 'DATABASE_URL=postgresql://fixture:' + password + '@pg:5432/fixture',
                '-e', 'RELEASE_DISTRIBUTION=none', '-e', 'RELEASE_TMP=/tmp', '-e', 'ERL_FLAGS=+S 2:2',
                '-e', 'DISABLE_FILE_LOGGING=true', '--entrypoint', 'sh', image, '-c',
                'cat > /tmp/observe.exs && exec /app/bin/blockscout eval \'Code.eval_file("/tmp/observe.exs")\'']
            output = run(command, input=script)
            return json.loads(next(line.split('=', 1)[1] for line in output.splitlines() if line.startswith('BLOCKSCOUT_DEPLOY_CHECK=')))

        first = observe()
        M.validate(first)
        assert all(row['ok'] for row in first['sections'].values()), first['sections']
        sections = first['sections']
        assert sections['deposits']['data']['states'] == {'initiated': 1, 'relayed': 2}
        assert sections['deposits']['data']['missing_origination'] == 1
        assert sections['withdrawals']['data']['states'] == {s: 1 for s in M.STATES}
        rollup = sections['rollup']['data']
        assert rollup['highest_block'] == 2000 and rollup['size'] == 1000
        assert {(s['stage'], s['parent_finality']): s['count'] for s in rollup['states']} == {
            ('processed', 'not_applicable'): 50, ('sent', 'unfinalized'): 50,
            ('sent', 'finalized'): 300, ('confirmed', 'unfinalized'): 100, ('confirmed', 'finalized'): 500}
        assert sections['confirmed']['data']['block_number'] == 1600
        assert sections['confirmed']['data']['parent_finality'] == 'unfinalized'
        assert sections['published']['data']['block_number'] == 1950
        # Refuse a write even if a future query change accidentally introduces one;
        # enforce the statement deadline and reject expensive plans before execution.
        failed = observe({'write_check': "INSERT INTO blocks VALUES(3000,true,now()) RETURNING '{}'::jsonb",
                          'timeout_check': "SELECT '{}'::jsonb FROM pg_sleep(3)",
                          'budget_check': 'SELECT to_jsonb(x) FROM generate_series(1,100000000) x ORDER BY x DESC LIMIT 1'})
        assert not any(failed['sections'][key]['ok'] for key in ('write_check', 'timeout_check', 'budget_check'))
        assert failed['sections']['write_check']['reason'] == 'read_only_denied'
        assert failed['sections']['timeout_check']['reason'] == 'statement_timeout'
        assert failed['sections']['budget_check']['reason'] == 'plan_budget'
        assert sql('SELECT count(*) FROM blocks WHERE number=3000;').strip() == '0'
        sql("INSERT INTO arbitrum_crosslevel_messages SELECT 'to_l2',n,'initiated',to_timestamp(1700000000),decode('01','hex'),NULL FROM generate_series(4,10002) n; UPDATE arbitrum_batch_l2_blocks SET confirmation_id=NULL WHERE block_number>1500; UPDATE arbitrum_lifecycle_l1_transactions SET status='finalized' WHERE id=2;")
        second = observe()
        M.validate(second)
        assert second['sections']['deposits']['data']['complete'] is False
        assert second['sections']['deposits']['data']['size'] == 10000
        assert second['sections']['confirmed']['data']['block_number'] == 1500
        assert second['sections']['published']['data']['parent_finality'] == 'finalized'
        output = M.render(second, second['observed_at_unix'], True, now=second['observed_at_unix'])
        assert 'deriw_bridge_sample_complete{direction="l2_to_l3"} 0' in output
        print(json.dumps({'status': 'passed', 'postgres_major': 15,
            'actual_metrics_rpc_executed': True, 'message_states_and_missing_origination_verified': True,
            'settlement_stages_and_parent_finality_verified': True, 'reorg_regression_verified': True,
            'sample_truncation_visible': True, 'read_only_enforced': True,
            'statement_timeout_and_plan_budget_enforced': True,
            'backend_app_started': False, 'live_services_accessed': False}, indent=2))
    finally:
        ids = run(['docker', 'ps', '-aq', '--filter', 'label=' + LABEL + '=' + token]).split()
        for identity in ids:
            metadata = json.loads(run(['docker', 'inspect', identity]))[0]
            assert metadata.get('Config', {}).get('Labels', {}).get(LABEL) == token
            run(['docker', 'rm', '-f', identity])
        if created:
            run(['docker', 'network', 'rm', network])


if __name__ == '__main__':
    main()
