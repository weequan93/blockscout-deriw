"""Keep the dev NFT producer and worker on the v10 queue protocol."""
import json
import subprocess
import uuid

import candidate_build as B

PRODUCER = 'producer@backend.blockscout.internal'
WORKER = 'worker@nft-media.blockscout.internal'
MARKER = 'BLOCKSCOUT_V10_NFT_WORKER_CONFIG_OK'


def plan(api, model):
    backend = model['services']['backend']['environment']
    worker = model['services']['nft_media_handler']['environment']
    api.require(backend.get('RELEASE_NODE') == PRODUCER and worker.get('RELEASE_NODE') == WORKER,
                'NFT peer identities differ from the captured dev topology')
    for environment in (backend, worker):
        api.require(environment.get('NFT_MEDIA_HANDLER_ENABLED') == 'true'
                    and environment.get('NFT_MEDIA_HANDLER_REMOTE_DISPATCHER_NODE_MODE_ENABLED') == 'true',
                    'Expected remote NFT media mode')
    api.require(worker.get('NFT_MEDIA_HANDLER_IS_WORKER') == 'true', 'Expected standalone NFT worker')
    peers = json.loads(worker.get('NFT_MEDIA_HANDLER_NODES_MAP', '{}'))
    api.require(isinstance(peers, dict) and set(peers) == {PRODUCER}, 'Review the NFT producer map before upgrading')
    folder = peers[PRODUCER]
    api.require(isinstance(folder, str) and folder.startswith('/') and not folder.startswith('//')
                and '\0' not in folder and '..' not in folder.split('/'), 'Unsupported existing NFT bucket folder')
    current = backend.get('NFT_MEDIA_HANDLER_BUCKET_FOLDER')
    api.require(current in (None, '', folder), 'Backend and worker NFT bucket folders differ')
    return {'replace_services': ['backend', 'nft_media_handler'], 'producer_node': PRODUCER, 'worker_node': WORKER,
            'backend_environment': {'NFT_MEDIA_HANDLER_BUCKET_FOLDER': folder},
            'queue_protocol': 'v10_urls_and_producer_folder', 'folder_preserved_from_legacy_map': True}


def worker_expression():
    return ('true = Application.get_env(:nft_media_handler, :standalone_media_worker?); '
            'config = Application.get_env(:libcluster, :topologies) |> Keyword.fetch!(:nft_media_handler); '
            'Cluster.Strategy.Epmd = Keyword.fetch!(config, :strategy); '
            '5000 = Keyword.fetch!(Keyword.fetch!(config, :config), :timeout); '
            '[ :"producer@backend.blockscout.internal" ] = Keyword.fetch!(Keyword.fetch!(config, :config), :hosts); '
            'IO.puts("' + MARKER + '")')


def smoke_worker(api, image, version, environment, directory):
    values = {key: value for key, value in environment.items() if value is not None}
    api.require(all(api.ENV_KEY.fullmatch(key) and isinstance(value, str) and '\0' not in value
                    for key, value in values.items()), 'Invalid NFT worker environment')
    values.update(RELEASE_VERSION=version, RELEASE_DISTRIBUTION='none', RELEASE_TMP='/tmp')
    name = 'blockscout-v10-nft-smoke-' + uuid.uuid4().hex[:12]
    command = ['docker', 'run', '--rm', '--pull', 'never', '--name', name, '--network', 'none', '--read-only',
               '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--tmpfs', '/tmp:rw,mode=1777',
               '--cpus', '2', '--memory', '512m', '--entrypoint', 'sh', '-i', image, '-c', B.eval_script(worker_expression())]
    try:
        result = subprocess.run(command, input=json.dumps(values), capture_output=True, text=True, timeout=120)
        with B.private_log(directory / 'nft-worker-smoke.log.private') as log:
            log.write(result.stdout.encode())
            log.write(result.stderr.encode())
        api.require(result.returncode == 0 and MARKER in result.stdout.splitlines(),
                    'NFT worker release config failed; inspect its private log')
    finally:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=20)
    print(json.dumps({'phase': 'nft_worker_release_config_checked', 'passed': True}), flush=True)
    return {'passed': True, 'static_peer_discovery': True, 'producer_node': PRODUCER}
