#!/usr/bin/env python3
"""Check real v10 NFT peer discovery/reconnection and queue RPC in isolated VMs."""
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import check_upgrade_database as F
import v10_nft as N

IMAGE = 'deriw-blockscout:v10-nft-local-validation'


def command(name, token, network, environment, detached=False):
    result = ['docker', 'run', '--rm', '--pull', 'never', '-i', '--name', name, '--label', F.LABEL + '=' + token,
              '--network', network, '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
              '--cpus', '2', '--memory', '512m', '--tmpfs', '/tmp:rw,mode=1777',
              '--tmpfs', '/app/dets:rw,mode=1777,size=16m']
    if detached:
        result += ['-d', '--network-alias', 'backend.blockscout.internal']
    else:
        result += ['--network-alias', 'nft-media.blockscout.internal']
    for key, value in environment.items():
        result += ['-e', key + '=' + value]
    return result + ['--entrypoint', 'sh', IMAGE, '-c',
                     'epmd -daemon; cat > /tmp/peer.exs && exec /app/bin/blockscout eval \'Code.eval_file("/tmp/peer.exs")\'']


def main():
    token = uuid.uuid4().hex[:16]
    network = 'v10-nft-peers-' + token
    producer = 'v10-nft-producer-' + token
    worker = 'v10-nft-worker-' + token
    created = False
    environment = {'RELEASE_DISTRIBUTION': 'none', 'RELEASE_TMP': '/tmp', 'ERL_FLAGS': '+S 2:2',
                   'CHAIN_TYPE': 'arbitrum', 'CHAIN_ID': '18417507517', 'APPLICATION_MODE': 'all',
                   'SECRET_KEY_BASE': 'a' * 64, 'NFT_MEDIA_HANDLER_ENABLED': 'true',
                   'NFT_MEDIA_HANDLER_REMOTE_DISPATCHER_NODE_MODE_ENABLED': 'true',
                   'NFT_MEDIA_HANDLER_BUCKET_FOLDER': '/folder_1', 'NFT_MEDIA_HANDLER_BACKFILL_ENABLED': 'false',
                   'DISABLE_FILE_LOGGING': 'true', 'ETHEREUM_JSONRPC_HTTP_URL': 'http://127.0.0.1:1'}
    producer_script = '''
{:ok, _} = Node.start(:"producer@backend.blockscout.internal", :longnames);
true = Node.set_cookie(:v10_nft_fixture_cookie);
{:ok, _} = Indexer.NFTMediaHandler.Queue.start_link([]);
nil = Process.whereis(Explorer.Supervisor);
IO.puts("V10_NFT_PRODUCER_READY");
Process.sleep(:infinity);
'''
    worker_script = '''
{:ok, _} = Node.start(:"worker@nft-media.blockscout.internal", :longnames);
true = Node.set_cookie(:v10_nft_fixture_cookie);
[] = Node.list();
{:ok, _} = Application.ensure_all_started(:libcluster);
{:ok, _} = Cluster.Supervisor.start_link([Application.get_env(:libcluster, :topologies), [name: V10NFTFixtureCluster]]);
producer = :"producer@backend.blockscout.internal";
await = fn ->
  true = Enum.reduce_while(1..120, false, fn _, _ ->
    if producer in Node.list(), do: {:halt, true}, else: (Process.sleep(100); {:cont, false})
  end)
end;
await.();
{:ok, _} = NFTMediaHandler.DispatcherInterface.start_link([]);
{[], ^producer, "/folder_1"} = NFTMediaHandler.DispatcherInterface.get_urls(1);
true = Node.disconnect(producer);
await.();
:ok = :rpc.call(producer, Application, :put_env, [:nft_media_handler, :r2_folder, "/producer_updated"]);
{[], ^producer, "/producer_updated"} = NFTMediaHandler.DispatcherInterface.get_urls(1);
nil = Process.whereis(Explorer.Supervisor);
IO.puts("V10_NFT_PEERS_OK");
'''
    try:
        F.run(['docker', 'network', 'create', '--internal', '--label', F.LABEL + '=' + token, network])
        created = True
        started = subprocess.run(command(producer, token, network, environment, detached=True), input=producer_script,
                                 capture_output=True, text=True, timeout=30)
        if started.returncode:
            raise RuntimeError('Producer fixture failed to start: ' + started.stderr[-1500:])
        deadline = time.monotonic() + 30
        while 'V10_NFT_PRODUCER_READY' not in F.run(['docker', 'logs', producer]):
            if time.monotonic() > deadline:
                raise RuntimeError('Producer fixture did not become ready')
            time.sleep(1)
        worker_environment = {**environment, 'NFT_MEDIA_HANDLER_IS_WORKER': 'true',
                              'NFT_MEDIA_HANDLER_NODES_MAP': json.dumps({N.PRODUCER: '/folder_1'})}
        checked = subprocess.run(command(worker, token, network, worker_environment), input=worker_script,
                                 capture_output=True, text=True, timeout=90)
        if checked.returncode or 'V10_NFT_PEERS_OK' not in checked.stdout.splitlines():
            raise RuntimeError('NFT peer fixture failed: ' + checked.stdout[-2500:] + checked.stderr[-2500:])
        print(json.dumps({'status': 'passed', 'actual_v10_producer_and_worker': True, 'automatic_peer_bootstrap': True,
                          'automatic_reconnection': True, 'queue_rpc_and_producer_folder': True,
                          'database_or_application_started': False, 'live_services_accessed': False}))
        return 0
    finally:
        F.cleanup_container(worker, token)
        F.cleanup_container(producer, token)
        if created:
            F.run(['docker', 'network', 'rm', network])


if __name__ == '__main__':
    raise SystemExit(main())
