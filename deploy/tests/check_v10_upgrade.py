#!/usr/bin/env python3
"""Run the exact v10 schema client against disposable PG15 and synthetic v9 data."""
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
import check_upgrade_database as F
import candidate_build as B
import deploy_check as C
import dev_config as D
import v10_upgrade as U

IMAGE = 'deriw-blockscout:v10-nft-local-validation'


def evaluate(image, network, name, token, environment, payload, expected_failure=False):
    command = U.client_command(image, network, name, token)
    # Add fixture ownership alongside the production runner's ownership label.
    command[2:2] = ['--label', F.LABEL + '=' + token]
    data = U.archive(environment, payload, (ROOT / 'deploy/scripts/v10/migrate.exs').read_bytes())
    try:
        result = subprocess.run(command, input=data, capture_output=True, timeout=150)
        if expected_failure:
            assert result.returncode != 0, 'Deliberately rejected schema client unexpectedly succeeded'
            return None
        if result.returncode:
            raise RuntimeError('Schema runner fixture failed: ' + result.stdout.decode(errors='replace')[-5000:] + result.stderr.decode(errors='replace')[-2500:])
        return C.marked_json(D, result.stdout.decode())
    finally:
        F.cleanup_container(name, token)


def startup(image, network, token, environment, directory, api_check=None):
    names = {role: 'v10-startup-' + role + '-' + token for role in ('rpc', 'redis', 'backend', 'worker')}
    rpc = r'''const http=require('http');
const one=q=>({jsonrpc:'2.0',id:q.id,result:({eth_chainId:'0x449c54bbd',net_version:'18417507517',eth_blockNumber:'0x14',eth_gasPrice:'0x1',eth_getBalance:'0x0',eth_getCode:'0x',eth_call:'0x',eth_getLogs:[],eth_getTransactionReceipt:null})[q.method]??null});
http.createServer((req,res)=>{let b='';req.on('data',x=>b+=x);req.on('end',()=>{try{const q=JSON.parse(b);res.setHeader('Content-Type','application/json');res.end(JSON.stringify(Array.isArray(q)?q.map(one):one(q)));}catch(e){res.statusCode=400;res.end('{}');}});}).listen(8545,'0.0.0.0');'''
    def create(role, selected, alias, env, command):
        args = ['docker', 'run', '--rm', '-d', '--pull', 'never', '--name', names[role], '--label', F.LABEL + '=' + token,
                '--network', network, '--network-alias', alias, '--read-only', '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges', '--cpus', '2', '--memory', '1g',
                '--tmpfs', '/tmp:rw,mode=1777,size=64m', '--tmpfs', '/app/dets:rw,mode=1777,size=16m',
                '--tmpfs', '/app/logs:rw,mode=1777,size=16m', '--tmpfs', '/app/temp:rw,mode=1777,size=16m']
        for key, value in env.items():
            args += ['-e', key + '=' + value]
        F.run(args + ['--entrypoint', command[0], selected, *command[1:]])
    try:
        create('rpc', 'node:22-alpine', 'rpc', {}, ['node', '-e', rpc])
        create('redis', 'redis:7-alpine', 'redis', {}, ['redis-server', '--save', '', '--appendonly', 'no'])
        env = {**environment, **B.market_policy().ENVIRONMENT, 'DISABLE_INDEXER': 'false',
               'DISABLE_CATCHUP_INDEXER': 'true', 'DISABLE_REALTIME_INDEXER': 'true',
               'RELEASE_DISTRIBUTION': 'name', 'RELEASE_COOKIE': 'synthetic_v10_startup_cookie',
               'RELEASE_TMP': '/tmp', 'RELEASE_VERSION': '10.2.6', 'PORT': '4000',
               'ACCOUNT_REDIS_URL': 'redis://redis:6379', 'POOL_SIZE_API': '2',
               'NFT_MEDIA_HANDLER_ENABLED': 'true', 'NFT_MEDIA_HANDLER_REMOTE_DISPATCHER_NODE_MODE_ENABLED': 'true',
               'NFT_MEDIA_HANDLER_BUCKET_FOLDER': '/folder_1', 'NFT_MEDIA_HANDLER_BACKFILL_ENABLED': 'false',
               'MIGRATION_BACKGROUND_AUTOSTART_DISABLED': 'true', 'DISABLE_FILE_LOGGING': 'true',
               'INDEXER_UNCATALOGED_TOKEN_TRANSFERS_ENABLED': 'false', 'INDEXER_DISABLE_INTERNAL_TRANSACTIONS_FETCHER': 'true',
               'ETHEREUM_JSONRPC_HTTP_URL': 'http://rpc:8545', 'ETHEREUM_JSONRPC_TRACE_URL': 'http://rpc:8545',
               'INDEXER_ARBITRUM_L1_RPC': 'http://rpc:8545', 'ETHEREUM_JSONRPC_RECEIPTS_BY_BLOCK': 'false',
               'CACHE_TXS_COUNT_APPROXIMATE': 'true', 'INDEXER_CONTRACT_CODE_RETRY_DELAY': '60s',
               'INDEXER_CONTRACT_CODE_CONCURRENCY': '1', 'TRACE_BLOCK_RANGES': '176114083..latest'}
        create('backend', image, 'backend.blockscout.internal', {**env, 'RELEASE_NODE': U.N.PRODUCER}, ['/app/bin/blockscout', 'start'])
        create('worker', image, 'nft-media.blockscout.internal', {**env, 'RELEASE_NODE': U.N.WORKER,
               'NFT_MEDIA_HANDLER_IS_WORKER': 'true', 'NFT_MEDIA_HANDLER_NODES_MAP': json.dumps({U.N.PRODUCER: '/folder_1'})}, ['/app/bin/blockscout', 'start'])
        deadline = time.monotonic() + 90
        while True:
            result = subprocess.run(['docker', 'exec', names['backend'], 'curl', '-fsS', '--max-time', '2',
                                     'http://127.0.0.1:4000/api/health/readiness'], capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and result.stdout.strip() == 'OK':
                break
            if time.monotonic() > deadline:
                raise RuntimeError('Disposable full application did not become ready; logs at ' + str(directory))
            time.sleep(1)
        C.marked_json(D, F.run(['docker', 'exec', names['backend'], '/app/bin/blockscout', 'rpc', U.runtime_expression()]))
        deadline = time.monotonic() + 30
        while True:
            checked = subprocess.run(['docker', 'exec', names['worker'], '/app/bin/blockscout', 'rpc', U.nft_expression()], capture_output=True, text=True, timeout=20)
            if checked.returncode == 0:
                C.marked_json(D, checked.stdout)
                break
            if time.monotonic() > deadline:
                raise RuntimeError('Disposable standalone worker did not connect automatically')
            time.sleep(1)
        paths = ['/api/v2/blocks', '/api/v2/transactions', '/api/v2/tokens',
                 '/api/v2/transactions/0x' + format(120, '064x') + '/internal-transactions']
        for path in paths:
            body = F.run(['docker', 'exec', names['backend'], 'curl', '-fsS', '--max-time', '8', 'http://127.0.0.1:4000' + path])
            assert isinstance(json.loads(body), dict)
        if api_check is not None:
            api_check(names['backend'])
        return {'full_backend_and_standalone_worker_started': True, 'readiness_passed': True,
                'actual_runtime_hold_and_history_policy_checked': True, 'automatic_nft_connection_checked': True,
                'representative_api_routes_passed': paths, 'fixture_block_fetchers_paused': True,
                'external_rpc_and_market_connectivity_tested': False}
    finally:
        for role in ('worker', 'backend', 'redis', 'rpc'):
            row = subprocess.run(['docker', 'logs', names[role]], capture_output=True, text=True)
            (directory / ('startup-' + role + '.log')).write_text(row.stdout + row.stderr)
            F.cleanup_container(names[role], token)


def main():
    token = uuid.uuid4().hex[:16]
    directory = Path(tempfile.mkdtemp(prefix='blockscout-v10-runner-'))
    os.chmod(directory, 0o700)
    network = 'v10-runner-' + token
    pg_name = 'v10-runner-pg-' + token
    created = False
    password = uuid.uuid4().hex
    try:
        plan = D.read_json(ROOT / 'deploy/upgrades/v10.2.6.json')
        a, old = F.image_inventory(D, C, 'deriw-blockscout:v9-upgrade-baseline')
        b, new = F.image_inventory(D, C, IMAGE)
        assert new['migrations'] == plan['target_migration_sha256'] and old['migrations'] == F.baseline_inventory()
        versions = C.migration_versions(D, new)
        versions = {k: versions[k] for k in ('repo', 'account', 'arbitrum')}
        F.run(['docker', 'network', 'create', '--internal', '--label', F.LABEL + '=' + token, network])
        created = True
        F.run(['docker', 'run', '--rm', '-d', '--pull', 'never', '--name', pg_name, '--label', F.LABEL + '=' + token,
               '--network', network, '--network-alias', 'pg', '--read-only', '--cpus', '2', '--memory', '1g',
               '--tmpfs', '/var/lib/postgresql/data:rw,mode=0700,size=768m',
               '--tmpfs', '/var/run/postgresql:rw,mode=1777,size=16m', '--tmpfs', '/tmp:rw,size=16m',
               '-e', 'POSTGRES_USER=fixture', '-e', 'POSTGRES_PASSWORD=' + password, '-e', 'POSTGRES_DB=fixture',
               'postgres:15-alpine'])
        deadline = time.monotonic() + 30
        while subprocess.run(['docker', 'exec', pg_name, 'pg_isready', '-q', '-U', 'fixture', '-d', 'fixture'], capture_output=True).returncode:
            if time.monotonic() > deadline:
                raise RuntimeError('PG15 fixture not ready')
            time.sleep(0.5)
        F.evaluate(a['Id'], 'baseline', token, network, password, versions, directory, pg_name, defer_indexes=True)
        environment = {'DATABASE_URL': 'postgresql://fixture:' + password + '@pg:5432/fixture', 'ECTO_USE_SSL': 'false',
                       'POOL_SIZE': '2', 'ACCOUNT_POOL_SIZE': '2', 'CHAIN_TYPE': 'arbitrum', 'CHAIN_ID': '18417507517',
                       'SECRET_KEY_BASE': 'a' * 64, 'ETHEREUM_JSONRPC_HTTP_URL': 'http://127.0.0.1:1',
                       'DISABLE_INDEXER': 'true', 'DISABLE_MARKET': 'true', 'ERL_FLAGS': '+S 2:2'}
        F.run(['docker', 'exec', pg_name, 'psql', '-U', 'fixture', '-d', 'fixture', '-c',
               "INSERT INTO migrations_status(migration_name,status,inserted_at,updated_at) VALUES ('reindex_duplicated_internal_transactions','completed',NOW(),NOW())"])
        inspected = evaluate(b['Id'], network, 'v10-client-inspect-' + token, token, environment, U.payload(plan))
        payload = U.payload(plan, inspected, mode='migrate')
        for label in ('route', 'inventory'):
            invalid = {**payload, 'mode': 'inspect'}
            if label == 'route':
                invalid['database_identity'] = {**inspected['database_identity'], 'address': '192.0.2.99'}
            else:
                invalid['migration_inventory'] = {**payload['migration_inventory'], next(iter(payload['migration_inventory'])): '0' * 64}
            evaluate(b['Id'], network, 'v10-client-invalid-' + label + '-' + token, token, environment, invalid, expected_failure=True)
        lock_sql = "SET application_name='fixture-v10-account-blocker'; BEGIN; LOCK TABLE account_public_tags_requests IN ACCESS SHARE MODE; SELECT pg_sleep(120); ROLLBACK;"
        blocker_log = (directory / 'account-blocker.log').open('wb')
        blocker = subprocess.Popen(['docker', 'exec', pg_name, 'psql', '-U', 'fixture', '-d', 'fixture', '-c', lock_sql], stdout=blocker_log, stderr=blocker_log)
        try:
            deadline = time.monotonic() + 15
            while F.run(['docker', 'exec', pg_name, 'psql', '-U', 'fixture', '-d', 'fixture', '-Atc', "SELECT count(*) FROM pg_stat_activity WHERE application_name='fixture-v10-account-blocker' AND wait_event='PgSleep'"]).strip() != '1':
                if time.monotonic() > deadline:
                    raise RuntimeError('Account blocker did not acquire its fixture lock')
                time.sleep(0.2)
            evaluate(b['Id'], network, 'v10-client-partial-' + token, token, environment, payload, expected_failure=True)
        finally:
            F.run(['docker', 'exec', pg_name, 'psql', '-U', 'fixture', '-d', 'fixture', '-c', "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name='fixture-v10-account-blocker'"])
            blocker.wait(timeout=10)
            blocker_log.close()
        partial_payload = {**payload, 'mode': 'inspect'}
        partial = evaluate(b['Id'], network, 'v10-client-partial-check-' + token, token, environment, partial_payload)
        assert partial['pending'] == {'repo': sorted(U.R.VERSIONS), 'account': [20250815115250, 20251029115930], 'arbitrum': [20250731001757]}
        changed = evaluate(b['Id'], network, 'v10-client-migrate-' + token, token, environment, payload)
        repeat = evaluate(b['Id'], network, 'v10-client-repeat-' + token, token, environment, payload)
        assert [len(row['versions']) for row in changed['migrated']] == [0, 2, 1]
        assert len(set(changed['applied_versions']) - set(inspected['applied_versions'])) == 29
        assert all(not row['versions'] for row in repeat['migrated'])
        assert changed['heap_identity'] == repeat['heap_identity'] == inspected['heap_identity']
        assert changed['pending'] == {'repo': sorted(U.R.VERSIONS), 'account': [], 'arbitrum': []}
        startup_result = startup(b['Id'], network, token, environment, directory)
        after_startup = evaluate(b['Id'], network, 'v10-client-after-startup-' + token, token, environment, {**payload, 'mode': 'inspect'})
        assert after_startup['background_statuses'] == inspected['background_statuses']
        print(json.dumps({'status': 'passed', 'exact_frozen_schema_runner': True, 'upstream_applied_counts': [26, 2, 1],
                          'repeat_applied_counts': [0, 0, 0], 'partial_failure_after_main_schema_checked': True, 'database_route_and_migration_inventory_drift_rejected': True,
                          'resume_applied_counts': [0, 2, 1], 'heap_identity_preserved': True,
                          'background_status_records_preserved': True, 'deferred_custom_versions': sorted(U.R.VERSIONS),
                          'schema_client_application_started': False, 'application_startup': startup_result,
                          'live_services_accessed': False, 'fixture_logs': str(directory)}))
        return 0
    finally:
        F.cleanup_container(pg_name, token)
        if created:
            F.run(['docker', 'network', 'rm', network])


if __name__ == '__main__':
    raise SystemExit(main())
