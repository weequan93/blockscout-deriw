#!/usr/bin/env python3
"""Exercise the testnet release with v9, exact f0 v10 and disposable PostgreSQL 15.

Synthetic data only, internal Docker networking, no published ports or host mounts.
The release image must already be built from the qualified application revision.
"""
import hashlib
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
import deploy_testnet_v10 as T
import deploy_check as C
import check_upgrade_database as F
from test_deploy_testnet_v10 import fixture

IMAGE = 'deriw-blockscout:test-v10-fixture-' + T.SOURCE[:12]
V9_TRACKERS = '''
{:ok, _} = Application.ensure_all_started(:ethereum_jsonrpc)
defmodule TestParentTransport do
  def json_rpc(%{method: "eth_chainId"}, _), do: {:ok, "0x539"}
  def json_rpc(%{method: "eth_getLogs", params: [filter]}, _) do
    anchor = if filter.address == "0x" <> String.duplicate("1",40), do: 10000, else: 12000
    first = EthereumJSONRPC.quantity_to_integer(filter.fromBlock)
    last = EthereumJSONRPC.quantity_to_integer(filter.toBlock)
    logs = if first <= anchor and anchor <= last do
      [%{"address" => filter.address, "blockNumber" => EthereumJSONRPC.integer_to_quantity(anchor),
         "blockHash" => "0x" <> String.duplicate("a",64), "transactionHash" => "0x" <> String.duplicate("b",64),
         "logIndex" => "0x0", "topics" => filter.topics, "data" => "0x", "removed" => false}]
    else
      []
    end
    {:ok, logs}
  end
end
args = [transport: TestParentTransport, transport_options: [url: "http://fixture.invalid"]]
rollup = "0x" <> String.duplicate("3",40)
{:ok, _} = Agent.start_link(fn ->
  %{config: %{json_l1_rpc_named_arguments: args, l1_bridge_address: "0x" <> String.duplicate("1",40),
     l1_rollup_address: rollup, recheck_interval: 10000, l1_rpc_block_range: 50},
    data: %{new_msg_to_l2_start_block: 9000}}
end, name: Indexer.Fetcher.Arbitrum.TrackingMessagesOnL1)
{:ok, _} = Agent.start_link(fn ->
  %{callback_module_state: %{config: %{l1_rpc: %{json_rpc_named_arguments: args, logs_block_range: 50},
     l1_outbox_address: "0x" <> String.duplicate("2",40), l1_rollup_address: rollup},
    task_data: %{new_executions: %{start_block: 11000}}, intervals: %{new_executions: 3000}}}
end, name: Indexer.Fetcher.Arbitrum.TrackingBatchesStatuses)
'''


def main():
    os.umask(0o077)
    token = uuid.uuid4().hex[:16]
    directory = Path(tempfile.mkdtemp(prefix='testnet-v10-upgrade-'))
    path = directory / 'state.json'
    network, pg = 'test-v10-net-' + token, 'test-v10-pg-' + token
    password = uuid.uuid4().hex
    created = False
    auxiliary = []
    environment = {'DATABASE_URL': 'postgresql://fixture:' + password + '@pg:5432/fixture',
                   'ECTO_USE_SSL': 'false', 'POOL_SIZE': '2', 'ACCOUNT_POOL_SIZE': '2',
                   'CHAIN_TYPE': 'arbitrum', 'CHAIN_ID': '2885', 'SECRET_KEY_BASE': 'a' * 64,
                   'ETHEREUM_JSONRPC_HTTP_URL': 'http://127.0.0.1:1', 'DISABLE_MARKET': 'true',
                   'DISABLE_INDEXER': 'true', 'ERL_FLAGS': '+S 2:2', 'NFT_MEDIA_HANDLER_ENABLED': 'false',
                   **T.HOLDS, 'BLOCKSCOUT_VERSION': T.SOURCE}

    def sql(query):
        return F.run(['docker', 'exec', '-i', pg, 'psql', '-XAt', '-v', 'ON_ERROR_STOP=1',
                      '-U', 'fixture', '-d', 'fixture'], input=query)

    def eval_image(image, code):
        name = 'test-v10-eval-' + uuid.uuid4().hex[:12]
        command = ['docker', 'run', '--rm', '-i', '--pull', 'never', '--name', name,
                   '--label', F.LABEL + '=' + token, '--network', network, '--read-only',
                   '--cap-drop', 'ALL', '--memory', '1g', '--cpus', '2', '--tmpfs', '/tmp:rw,mode=1777']
        for key, value in {**environment, 'RELEASE_DISTRIBUTION': 'none', 'RELEASE_TMP': '/tmp', 'DISABLE_FILE_LOGGING': 'true'}.items():
            command += ['-e', key + '=' + value]
        boot = '''
for app <- [:crypto, :ssl, :postgrex, :ecto_sql], do: ({:ok, _} = Application.ensure_all_started(app))
{:ok, _} = Explorer.Repo.start_link(pool_size: 2)
'''
        try:
            result = subprocess.run(command + ['--entrypoint', 'sh', image, '-c',
                'cat > /tmp/check.exs && exec /app/bin/blockscout eval \'Code.eval_file("/tmp/check.exs")\''],
                input=boot + code, capture_output=True, text=True, timeout=150)
            T.P.private_write(directory / ('eval-' + uuid.uuid4().hex[:8] + '.private.log'), result.stdout + result.stderr)
            assert result.returncode == 0, 'Release eval failed; logs: ' + str(directory)
            return T.observation(result.stdout)
        finally:
            F.cleanup_container(name, token)

    try:
        assert F.run(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}']).strip().startswith('unix://')
        old, _ = F.image_inventory(T.D, C, 'deriw-blockscout:v9-upgrade-baseline')
        new, inventory = F.image_inventory(T.D, C, IMAGE)
        assert new['Config']['Labels']['org.opencontainers.image.revision'] == T.SOURCE
        plan = json.loads(T.git_source(ROOT, 'deploy/upgrades/v10.2.6.json'))
        assert inventory['migrations'] == plan['target_migration_sha256']
        versions = C.migration_versions(T.D, inventory)
        versions = {k: versions[k] for k in ('repo', 'account', 'arbitrum')}
        F.run(['docker', 'network', 'create', '--internal', '--label', F.LABEL + '=' + token, network])
        created = True
        F.run(['docker', 'run', '--rm', '-d', '--pull', 'never', '--name', pg, '--label', F.LABEL + '=' + token,
               '--network', network, '--network-alias', 'pg', '--read-only', '--cpus', '2', '--memory', '1g',
               '--tmpfs', '/var/lib/postgresql/data:rw,mode=0700,size=768m',
               '--tmpfs', '/var/run/postgresql:rw,mode=1777,size=16m', '--tmpfs', '/tmp:rw,size=16m',
               '-e', 'POSTGRES_USER=fixture', '-e', 'POSTGRES_PASSWORD=' + password,
               '-e', 'POSTGRES_DB=fixture', 'postgres:15-alpine'])
        deadline = time.monotonic() + 30
        while subprocess.run(['docker', 'exec', pg, 'pg_isready', '-q', '-U', 'fixture', '-d', 'fixture'], capture_output=True).returncode:
            assert time.monotonic() < deadline
            time.sleep(0.2)
        emit = lambda phase: print(json.dumps({'phase': phase, 'private_logs': str(directory)}), flush=True)
        emit('synthetic_v9_schema')
        F.evaluate(old['Id'], 'baseline', token, network, password, versions, directory, pg, defer_indexes=True)
        sql("INSERT INTO migrations_status(migration_name,status,inserted_at,updated_at) VALUES ('reindex_duplicated_internal_transactions','completed',NOW(),NOW());"
            "INSERT INTO arbitrum_crosslevel_messages(direction,message_id,originating_transaction_block_number,status,inserted_at,updated_at) VALUES('to_l2',1,10000,'relayed',NOW(),NOW());"
            "INSERT INTO arbitrum_lifecycle_l1_transactions(id,hash,block_number,timestamp,status,inserted_at,updated_at) VALUES(1,decode(repeat('12',32),'hex'),12000,NOW(),'finalized',NOW(),NOW());"
            "INSERT INTO arbitrum_l1_executions(message_id,execution_id,inserted_at,updated_at) VALUES(1,1,NOW(),NOW());")
        emit('legacy_cursor_and_rpc_probe')
        handoff = eval_image(old['Id'], V9_TRACKERS + T.cursor_expression())
        again = eval_image(old['Id'], V9_TRACKERS.replace('start_block: 11000', 'start_block: 10000') + T.cursor_expression())
        for row, repeated, contract in zip(handoff['cursors'], again['cursors'], ('1','2')):
            identity = '1337:0x' + '3'*40 + ':0x' + contract*40
            prefix = 'arbitrum_to_l2_scan_v1:' if row['tracker'] == 'deposits' else 'arbitrum_to_l1_execution_scan_v1:'
            assert row['counter_key'] == prefix + hashlib.sha256(identity.encode()).hexdigest()
            assert repeated['saved_next_parent_block'] == row['saved_next_parent_block']
        assert eval_image(old['Id'], V9_TRACKERS + T.probe_expression(ROOT))['passed']
        state = {'repo': str(ROOT), 'environment': environment, 'image_id': new['Id'], 'network': network,
                 'data_host_path': str(directory)}
        before = T.client(state, path, 'inspect')
        wrong = {**state, 'environment': {**environment, 'CHAIN_ID': '1'}}
        try:
            T.client(wrong, path, 'migrate', before)
            raise AssertionError('Wrong-chain migration was accepted')
        except T.P.PreflightError:
            pass
        assert T.client(state, path, 'inspect', before)['applied_versions'] == before['applied_versions']
        emit('partial_schema_failure_and_resume')
        blocker = subprocess.Popen(['docker', 'exec', pg, 'psql', '-U', 'fixture', '-d', 'fixture', '-c',
            "SET application_name='fixture-test-v10-blocker'; BEGIN; LOCK TABLE account_public_tags_requests IN ACCESS SHARE MODE; SELECT pg_sleep(90); ROLLBACK;"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 15
            while sql("SELECT count(*) FROM pg_stat_activity WHERE application_name='fixture-test-v10-blocker' AND wait_event='PgSleep';").strip() != '1':
                assert time.monotonic() < deadline
                time.sleep(0.2)
            try:
                T.client(state, path, 'migrate', before)
                raise AssertionError('Expected bounded account lock failure')
            except T.P.PreflightError:
                pass
        finally:
            sql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name='fixture-test-v10-blocker';")
            blocker.wait(timeout=10)
        partial = T.client(state, path, 'inspect', before)
        assert partial['pending'] == {'repo': sorted(T.R.VERSIONS), 'account': [20250815115250,20251029115930], 'arbitrum': [20250731001757]}
        changed = T.client(state, path, 'migrate', before)
        repeat = T.client(state, path, 'migrate', before)
        assert len(set(changed['applied_versions']) - set(before['applied_versions'])) == 29
        assert all(not row['versions'] for row in repeat['migrated'])
        assert before['heap_identity'] == changed['heap_identity'] == repeat['heap_identity']
        assert before['background_statuses'] == changed['background_statuses']
        assert changed['pending'] == {'repo': sorted(T.R.VERSIONS), 'account': [], 'arbitrum': []}
        cursor_check = '''
module = Indexer.Fetcher.Arbitrum.Utils.Db.MessageScanCursor
''' + 'rows = Jason.decode!(' + F.elixir_string(json.dumps(handoff['cursors'])) + ')\n' + '''
for row <- rows do
  contract = if row["tracker"] == "deposits", do: "1", else: "2"
  key = if contract == "1", do: module.key(1337, "0x" <> String.duplicate("3",40), "0x" <> String.duplicate(contract,40)),
    else: module.execution_key(1337, "0x" <> String.duplicate("3",40), "0x" <> String.duplicate(contract,40))
  true = key == row["counter_key"]
  true = module.resume(key,0) == row["saved_next_parent_block"]
end
IO.puts("TEST_V10_CHECK={\\"continued\\":true}")
'''
        assert eval_image(new['Id'], cursor_check)['continued']
        model, backend = fixture()
        original = directory / 'original.compose.json'
        T.R.write_new(original, T.R.snapshot_bytes(model))
        model = T.D.compose_model(directory, {'compose_project': 'fixture'}, {}, [original], True)
        candidate = T.candidate_model(model, {'backend': backend}, IMAGE, path, True)
        selected = directory / 'candidate.compose.json'
        T.R.write_new(selected, T.R.snapshot_bytes(candidate))
        resolved = T.D.compose_model(directory, {'compose_project': 'fixture'}, {}, [selected], True)
        assert not T.D.model_differences(candidate, resolved)
        emit('test_backend_startup')
        startup(new['Id'], network, token, environment, directory, auxiliary)
        after_start = T.client(state, path, 'inspect', before)
        assert after_start['background_statuses'] == before['background_statuses']
        assert after_start['heap_identity'] == before['heap_identity']
        assert after_start['pending'] == changed['pending']
        print(json.dumps({'status': 'passed', 'chain_id': 2885, 'source_revision': T.SOURCE,
                          'legacy_v9_cursor_handoff': True, 'monotonic_scoped_checkpoints': True,
                          'v9_rpc_probe_expression': True, 'wrong_chain_rejected': True,
                          'partial_schema_failure_resumed': True, 'initial_schema_versions': 29,
                          'historical_heap_and_migrator_status_preserved': True, 'deferred_indexes_held': True,
                          'test_backend_readiness_and_native_metrics': True, 'literal_compose_values_preserved': True,
                          'live_services_accessed': False, 'full_database_rehearsal': False, 'private_logs': str(directory)}))
    finally:
        T.remove_clients(path)
        for name in reversed(auxiliary):
            raw = subprocess.run(['docker', 'logs', name], capture_output=True)
            T.P.private_write(directory / (name + '.private.log'), raw.stdout + raw.stderr)
            F.cleanup_container(name, token)
        F.cleanup_container(pg, token)
        if created:
            F.run(['docker', 'network', 'rm', network])


def startup(image, network, token, environment, directory, auxiliary):
    rpc = '''const http=require('http');
const one=q=>({jsonrpc:'2.0',id:q.id,result:({eth_chainId:'0xb45',net_version:'2885',eth_blockNumber:'0x14',eth_gasPrice:'0x1',eth_getBalance:'0x0',eth_getCode:'0x',eth_call:'0x',eth_getLogs:[],eth_getTransactionReceipt:null})[q.method]??null});
http.createServer((req,res)=>{let b='';req.on('data',x=>b+=x);req.on('end',()=>{const q=JSON.parse(b);res.setHeader('Content-Type','application/json');res.end(JSON.stringify(Array.isArray(q)?q.map(one):one(q)));});}).listen(8545,'0.0.0.0');'''
    def create(role, selected, env, command):
        name = 'test-v10-' + role + '-' + token
        auxiliary.append(name)
        args = ['docker', 'run', '-d', '--pull', 'never', '--name', name, '--label', F.LABEL + '=' + token,
                '--network', network, '--network-alias', role, '--read-only', '--cap-drop', 'ALL',
                '--memory', '1g', '--cpus', '2', '--tmpfs', '/tmp:rw,mode=1777,size=64m',
                '--tmpfs', '/app/dets:rw,mode=1777,size=16m', '--tmpfs', '/app/logs:rw,mode=1777,size=16m',
                '--tmpfs', '/app/temp:rw,mode=1777,size=16m']
        for key, value in env.items(): args += ['-e', key + '=' + value]
        F.run(args + ['--entrypoint', command[0], selected, *command[1:]])
        return name
    create('rpc', 'node:22-alpine', {}, ['node', '-e', rpc])
    create('redis', 'redis:7-alpine', {}, ['redis-server','--save','','--appendonly','no'])
    env = {**environment, 'RELEASE_DISTRIBUTION': 'name', 'RELEASE_COOKIE': 'synthetic_test_v10_cookie',
           'RELEASE_NODE': 'blockscout@127.0.0.1',
           'RELEASE_TMP': '/tmp', 'DISABLE_INDEXER': 'false', 'DISABLE_CATCHUP_INDEXER': 'true',
           'DISABLE_REALTIME_INDEXER': 'true', 'INDEXER_DISABLE_INTERNAL_TRANSACTIONS_FETCHER': 'true',
           'INDEXER_ARBITRUM_BRIDGE_MESSAGES_TRACKING_ENABLED': 'false', 'INDEXER_ARBITRUM_BATCHES_TRACKING_ENABLED': 'false',
           'ACCOUNT_REDIS_URL': 'redis://redis:6379', 'ETHEREUM_JSONRPC_HTTP_URL': 'http://rpc:8545',
           'ETHEREUM_JSONRPC_TRACE_URL': 'http://rpc:8545', 'DISABLE_FILE_LOGGING': 'true', 'POOL_SIZE_API': '2',
           'ETHEREUM_JSONRPC_RECEIPTS_BY_BLOCK': 'false', 'CACHE_TXS_COUNT_APPROXIMATE': 'true'}
    backend = create('backend', image, env, ['/app/bin/blockscout','start'])
    deadline = time.monotonic() + 120
    while True:
        ready = subprocess.run(['docker','exec',backend,'curl','-fsS','--max-time','3','http://127.0.0.1:4000/api/health/readiness'],capture_output=True,text=True,timeout=6)
        if ready.returncode == 0 and ready.stdout.strip() == 'OK': break
        assert time.monotonic() < deadline, 'Test fixture backend not ready; logs: ' + str(directory)
        time.sleep(1)
    check = '''
"10.2.6" = to_string(Application.spec(:explorer,:vsn))
"2885" = Application.get_env(:block_scout_web,:chain_id)
true = Explorer.Migrator.StartupControl.disabled?()
false = Application.get_env(:indexer,Indexer.Prometheus.Metrics)[:enabled]
true = Application.get_env(:indexer,Indexer.Prometheus.ArbitrumBridgeMetrics)[:enabled]
true = Enum.all?(Supervisor.which_children(Explorer.Supervisor),fn {id,_,_,_} ->
  not (is_atom(id) and String.starts_with?(Atom.to_string(id),"Elixir.Explorer.Migrator.")) and
    id != Explorer.TokenInstanceOwnerAddressMigration.Supervisor
end)
IO.puts("TEST_V10_CHECK={\\"ready\\":true}")
'''
    assert T.observation(F.run(['docker','exec',backend,'bin/blockscout','rpc',check]))['ready']
    for route in ('/api/v2/blocks','/api/v2/transactions','/api/v2/transactions/0x'+format(120,'064x')+'/internal-transactions'):
        assert isinstance(json.loads(F.run(['docker','exec',backend,'curl','-fsS','--max-time','8','http://127.0.0.1:4000'+route])),dict)
    deadline = time.monotonic() + 30
    while True:
        metrics = F.run(['docker','exec',backend,'curl','-fsS','--max-time','5','http://127.0.0.1:4000/metrics'])
        if all('deriw_bridge_query_success{section="'+s+'"} 1' in metrics for s in ('deposits','withdrawals','rollup','published','confirmed')): break
        assert time.monotonic() < deadline
        time.sleep(1)


if __name__ == '__main__':
    main()
