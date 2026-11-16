#!/usr/bin/env python3
"""Rehearse the directory move and branch switch with disposable bind-mounted data.

All host writes are confined to this run's private temporary fixture directory.
No live service is accessed, no host port is published, and Docker cleanup is
restricted by ownership labels. PostgreSQL and Redis data survive a real rename
and container recreation. The parent RPC is synthetic.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import testnet_v10_layout as L
import deploy_check as C
import check_upgrade_database as F
T = L.T
IMAGE = 'deriw-blockscout:test-v10-fixture-' + T.SOURCE[:12]


def main():
    os.umask(0o077)
    token = uuid.uuid4().hex[:16]
    # This workspace is shared with Docker Desktop; the macOS temporary
    # filesystem need not be. Keep the private fixture outside the Git checkout.
    base = Path(tempfile.mkdtemp(prefix='.test-v10-layout-', dir=str(ROOT.parent))).resolve()
    repo, journal = base / 'repo', base / 'journal'
    journal.mkdir()
    path = journal / 'state.json'
    project = 'test-layout-' + token
    network = project + '_default'
    password = uuid.uuid4().hex
    images = {name: json.loads(F.run(['docker', 'image', 'inspect', reference]))[0]['Id'] for name, reference in {
        'db': 'postgres:15-alpine', 'stats-db': 'postgres:15-alpine', 'backend': 'node:22-alpine',
        'redis-db': 'redis:7-alpine', 'proxy': 'nginx:latest', 'new-backend': IMAGE}.items()}
    (base/'shared-sentinel').write_text(token)
    assert F.run(['docker','run','--rm','--pull','never','--network','none','--label',F.LABEL+'='+token,
        '--mount','type=bind,src='+str(base)+',dst=/fixture,readonly','--entrypoint','cat',images['db'],
        '/fixture/shared-sentinel']).strip() == token, 'Docker cannot see this private host fixture directory'
    old_image, _ = F.image_inventory(T.D, C, 'deriw-blockscout:v9-upgrade-baseline')
    _, inventory = F.image_inventory(T.D, C, images['new-backend'])
    versions = C.migration_versions(T.D, inventory)
    versions = {k: versions[k] for k in ('repo','account','arbitrum')}
    F.run(['git', 'clone', '--quiet', '--shared', '--single-branch', '--branch', 'deriw-dev', str(ROOT), str(repo)])
    F.run(['git', '-C', str(repo), 'switch', '-c', 'deriw-pre'])
    runtime = repo / 'docker-compose'
    source = runtime / 'docker-compose.yml'
    for name in ('blockscout-db-data', 'stats-db-data', 'redis-data', 'logs', 'dets'):
        folder = runtime / 'services' / name
        folder.mkdir(exist_ok=True)
        folder.chmod(0o777)
    proxy_file = runtime / 'proxy' / 'nginx.conf'
    proxy_file.write_text('events {}\nhttp { server { listen 8080; location / { proxy_pass http://backend:4000; } } }\n')
    proxy_file.chmod(0o644)
    (runtime / 'envs' / 'fixture.private.env').write_text('PRIVATE=literal$with$$pair\n')
    environment = {'DATABASE_URL': 'postgresql://fixture:' + password + '@db:5432/fixture',
        'ECTO_USE_SSL': 'false', 'POOL_SIZE': '2', 'ACCOUNT_POOL_SIZE': '2', 'POOL_SIZE_API': '2',
        'CHAIN_ID': '2885', 'CHAIN_TYPE': 'arbitrum', 'SECRET_KEY_BASE': 'a' * 64,
        'DISABLE_MARKET': 'true', 'DISABLE_INDEXER': 'false', 'DISABLE_CATCHUP_INDEXER': 'true',
        'DISABLE_REALTIME_INDEXER': 'true', 'INDEXER_DISABLE_INTERNAL_TRANSACTIONS_FETCHER': 'true',
        'INDEXER_ARBITRUM_BRIDGE_MESSAGES_TRACKING_ENABLED': 'false', 'INDEXER_ARBITRUM_BATCHES_TRACKING_ENABLED': 'false',
        'NFT_MEDIA_HANDLER_ENABLED': 'false', 'ACCOUNT_REDIS_URL': 'redis://redis-db:6379',
        'ETHEREUM_JSONRPC_HTTP_URL': 'http://127.0.0.1:1', 'ETHEREUM_JSONRPC_TRACE_URL': 'http://127.0.0.1:1',
        'RELEASE_DISTRIBUTION': 'name', 'RELEASE_NODE': 'blockscout@127.0.0.1',
        'RELEASE_COOKIE': 'synthetic_layout_cookie', 'RELEASE_TMP': '/tmp', 'ERL_FLAGS': '+S 2:2',
        'DISABLE_FILE_LOGGING': 'true', 'CACHE_TXS_COUNT_APPROXIMATE': 'true',
        'ETHEREUM_JSONRPC_RECEIPTS_BY_BLOCK': 'false', 'PRIVATE': 'literal$with$$pair\nand newline', **T.HOLDS}
    initial = {'name': project, 'services': {}, 'networks': {'default': {'name': network, 'internal': True}}}
    for name in images.keys() - {'new-backend'}:
        initial['services'][name] = {'image': images[name], 'pull_policy': 'never', 'restart': 'always',
            'labels': {F.LABEL: token}, 'networks': {'default': {'aliases': ['pg'] if name == 'db' else []}}}
    for name, folder in (('db','blockscout-db-data'), ('stats-db','stats-db-data')):
        initial['services'][name].update(environment={'POSTGRES_USER':'fixture','POSTGRES_PASSWORD':password,'POSTGRES_DB':'fixture'},
            volumes=[{'type':'bind','source':str(runtime / 'services' / folder),'target':'/var/lib/postgresql/data'}],
            healthcheck={'test':['CMD-SHELL','pg_isready -q -U fixture -d fixture'], 'interval':'1s','timeout':'2s','retries':30})
    initial['services']['redis-db'].update(command=['redis-server','--appendonly','yes'],
        volumes=[{'type':'bind','source':str(runtime / 'services/redis-data'),'target':'/data'}])
    initial['services']['backend'].update(environment=environment, entrypoint=[], working_dir='/app',
        command=['node','-e',"const s=require('http').createServer((q,r)=>r.end('OK')).listen(4000,'0.0.0.0');process.on('SIGTERM',()=>s.close(()=>process.exit(0)))"],
        volumes=[{'type':'bind','source':str(runtime / 'services' / name),'target':'/app/'+name} for name in ('logs','dets')])
    initial['services']['proxy']['volumes'] = [{'type':'bind','source':str(proxy_file),'target':'/etc/nginx/nginx.conf','read_only':True}]
    for name, database in (('db-init','db'),('stats-db-init','stats-db')):
        # Exercise the completed-service dependency without a second Docker
        # Desktop mount racing PostgreSQL's first-time ownership setup.
        initial['services'][name] = {'image':images['backend'],'pull_policy':'never','restart':'no',
            'labels':{F.LABEL:token},'networks':{'default':None},'entrypoint':['sh','-ec'],'command':['true']}
        initial['services'][database]['depends_on'] = {name:{'condition':'service_completed_successfully'}}
    T.R.write_new(journal / 'initial.private.json', T.R.snapshot_bytes(initial))
    for service in initial['services'].values():
        for mount in service.get('volumes',[]):
            mount['bind'] = {'create_host_path':False}
    source.write_bytes(T.R.snapshot_bytes(initial))
    created = False
    state = None
    try:
        print(json.dumps({'phase':'starting_private_layout_fixture','private_logs':str(base)}),flush=True)
        created = True
        F.run(['docker','compose','-p',project,'--project-directory',str(runtime),'--env-file',os.devnull,
               '-f',str(source),'up','-d','--no-build','--pull','never'])
        placeholder = {'project':project}
        L.wait_databases(placeholder)
        rows = T.current(project)
        db = rows['db']['Id']
        F.evaluate(old_image['Id'],'baseline',token,network,password,versions,journal,db,defer_indexes=True)
        F.run(['docker','exec',rows['redis-db']['Id'],'redis-cli','SET','layout_sentinel',token])
        model = T.D.compose_model(runtime, {'compose_project':project},{},[source],True)
        layout = L.plan(repo,model,rows,project)
        state = {'format':L.FORMAT,'repo':str(repo),'project':project,'before':deepcopy(rows),'layout':layout,
            'source':T.SOURCE,'environment':{**environment,'BLOCKSCOUT_VERSION':T.SOURCE},
            'network':network,'data_host_path':str(runtime / 'services/blockscout-db-data'),
            'image_id':images['new-backend'],'image_tag':IMAGE,'preparation_complete':True,
            'handoff':{'cursors':[]}, 'wide_reads_verified':False}
        state['schema_before'] = T.client(state,path,'inspect')
        migrated = T.client(state,path,'migrate',state['schema_before'])
        state['schema_complete'] = True
        selected = T.candidate_model(model,rows,IMAGE,path,False)
        relocated = L.relocated_model(selected,rows,path,layout)
        selected_file = journal / 'relocated.compose.json'
        T.R.write_new(selected_file,T.R.snapshot_bytes(relocated))
        resolved = T.D.compose_model(Path(layout['new']),{'compose_project':project},{},[selected_file],True)
        assert not T.D.model_differences(relocated,resolved)
        state['relocated_sha256'] = T.R.digest(selected_file.read_bytes())
        layout['preparation_complete'] = True
        T.save(path,state,'prepared')
        def verify_fixture(path, state, stability):
            live = T.current(project)
            stability(state,live)
            expression = '''
"2885" = Application.get_env(:block_scout_web,:chain_id)
"10.2.6" = to_string(Application.spec(:explorer,:vsn))
true = Explorer.Migrator.StartupControl.disabled?()
IO.puts("TEST_V10_CHECK={\\"ready\\":true}")
'''
            assert T.rpc(live['backend'],expression,journal/'runtime-fixture.private.log')['ready']
            metrics = F.run(['docker','exec',live['backend']['Id'],'curl','-fsS','--max-time','10','http://127.0.0.1:4000/metrics'])
            assert all('deriw_bridge_query_success{section="'+section+'"} 1' in metrics
                       for section in ('deposits','withdrawals','rollup','published','confirmed'))
            return {'version':'10.2.6','chain_id':'2885','fixture_parent_rpc_verification':False}
        print(json.dumps({'phase':'moving_runtime_and_recreating_containers'}),flush=True)
        with patch.object(T,'verify_backend',side_effect=verify_fixture): L.apply(path,state)
        live = T.current(project)
        assert L.git(repo,'branch','--show-current') == 'deriw-dev'
        assert L.git(repo,'status','--porcelain') == ''
        assert (Path(layout['new'])/'envs/fixture.private.env').read_text() == 'PRIVATE=literal$with$$pair\n'
        assert not (runtime/'services/blockscout-db-data').exists()
        assert F.run(['docker','exec',live['redis-db']['Id'],'redis-cli','GET','layout_sentinel']).strip() == token
        assert F.run(['docker','exec',live['proxy']['Id'],'curl','-fsS','--max-time','10','http://127.0.0.1:8080/api/health/readiness']).strip() == 'OK'
        final = T.client(state,path,'inspect',state['schema_before'])
        assert final['heap_identity'] == migrated['heap_identity']
        assert final['pending'] == migrated['pending']
        assert not (L.INIT & set(live))
        for name in set(rows) - L.INIT:
            assert live[name]['Id'] != rows[name]['Id']
            if name != 'backend': assert live[name]['Image'] == rows[name]['Image']
        print(json.dumps({'status':'passed','source_branch':'deriw-dev','runtime_directory_renamed':True,
            'data_copy_required':False,'all_bind_inodes_preserved':True,'postgres_database_and_heap_preserved':True,
            'redis_data_preserved':True,'private_configuration_preserved':True,'public_compose_defaults_restored':True,
            'backend_native_metrics_and_proxy_readiness':True,'other_image_ids_preserved':True,
            'stopped_initializers_removed_without_rerunning':True,
            'live_services_accessed':False,'full_database_rehearsal':False,'private_logs':str(base)}))
    finally:
        if state: T.remove_clients(path)
        ids = F.run(['docker','ps','-aq','--filter','label='+F.LABEL+'='+token]).split()
        for ident in ids:
            row = json.loads(F.run(['docker','inspect',ident]))[0]
            assert row['Config']['Labels'].get(F.LABEL) == token
            raw = subprocess.run(['docker','logs',ident],capture_output=True)
            T.P.private_write(journal/(ident[:12]+'.private.log'),raw.stdout+raw.stderr)
            F.run(['docker','rm','-f',ident])
        if created:
            metadata = json.loads(F.run(['docker','network','inspect',network]))[0]
            assert not metadata.get('Containers') and metadata['Labels']['com.docker.compose.project'] == project
            F.run(['docker','network','rm',network])
        # PG/Redis may own these disposable files with Linux UIDs. Remove only
        # this test's labelled temporary fixture data through a root helper.
        for candidate in (repo/'docker-compose/services',repo/'docker-compose.test/services'):
            for folder in ('blockscout-db-data','stats-db-data','redis-data'):
                target = candidate/folder
                if target.exists():
                    assert target.is_relative_to(base) and not target.is_symlink()
                    F.run(['docker','run','--rm','--pull','never','--network','none','--user','0',
                        '--label',F.LABEL+'='+token,'--mount','type=bind,src='+str(target)+',dst=/fixture',
                        '--entrypoint','sh',images['db'],'-c','find /fixture -mindepth 1 -delete'])


if __name__ == '__main__':
    main()
