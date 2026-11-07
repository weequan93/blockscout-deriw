#!/usr/bin/env python3
"""Targeted dev backend releases. Startup migrations are deliberately separate."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import uuid

FORMAT='blockscout-dev-release-v1'
STATE_LABEL='org.deriw.release-state'
SIDE_LABEL='org.deriw.release-side'
START=['bin/blockscout','start']
LEGACY=['sh','-c','bin/blockscout eval "Elixir.Explorer.ReleaseTasks.create_and_migrate()" && bin/blockscout start']
DEFERRED={
 'repo/migrations/20260721000000_add_transactions_status_created_contract_code_indexed_at_index.exs':
  'e57c6049e31adca49d21bf78c7355e3b6e32ff31d3e213f91c848157bbf77c21',
 'repo/migrations/20260812000000_add_logs_token_transfer_topics_block_number_cover_index.exs':
  '9d30ead1a840cb28900b42e779de794047e81d19c41b53a6701e1e095ff9c220',
}
VERSIONS={20260721000000,20260812000000}


def encoded(data):return (json.dumps(data,indent=2,sort_keys=True)+'\n').encode()
def digest(data):return hashlib.sha256(data).hexdigest()


def write_new(path,data):
 descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
 with os.fdopen(descriptor,'wb') as stream:
  stream.write(data);stream.flush();os.fsync(stream.fileno())


def update_state(path,state):
 temporary=path.parent/('state-'+uuid.uuid4().hex+'.tmp')
 try:
  write_new(temporary,encoded(state));os.replace(temporary,path)
  descriptor=os.open(path.parent,os.O_RDONLY)
  try:os.fsync(descriptor)
  finally:os.close(descriptor)
 finally:
  if temporary.exists():temporary.unlink()


def load_state(api,path,manifest):
 api.require(path.is_absolute() and path.name=='state.json' and not path.is_symlink() and path.is_file(),
  'Expected an existing private release state.json')
 api.require(path.parent.parent.resolve()==api.MANAGED_RELEASES.resolve()
  and bool(re.fullmatch(r'release-[0-9TZ-]+-[a-f0-9]{8}',path.parent.name)),'Release state is outside the managed dev release directory')
 api.require(not path.parent.is_symlink() and stat.S_IMODE(path.parent.stat().st_mode)==0o700
  and stat.S_IMODE(path.stat().st_mode)==0o600,'Release state directory/file permissions must be 0700/0600')
 state=api.read_json(path)
 api.require(state.get('format')==FORMAT and state.get('environment')=='dev'
  and state.get('chain_id')==manifest['chain_id'] and state.get('compose_project')==manifest['compose_project']
  and state.get('startup_migrations') is False,'Unrecognized dev release state')
 api.require(state.get('status') in ('prepared','activation_requested','active','needs_attention',
  'rollback_requested','rolled_back','rollback_needs_attention'),'Unrecognized release phase')
 api.require(set(state.get('before',{}))==set(manifest['baseline_image_ids']),'Release service inventory differs')
 api.require(set(state.get('source_images',{}))==set(manifest['baseline_image_ids']),'Release source image inventory differs')
 for side in ('forward','rollback'):
  model_path=path.parent/(side+'.compose.json')
  api.require(not model_path.is_symlink() and model_path.is_file() and stat.S_IMODE(model_path.stat().st_mode)==0o600,
   'Private release Compose snapshot is unavailable')
  api.require(digest(model_path.read_bytes())==state.get('snapshot_hashes',{}).get(side),'Release Compose snapshot changed; later edits will not be overwritten')
  ids=state.get('image_ids',{}).get(side,{})
  api.require(set(ids)==set(manifest['baseline_image_ids']) and all(re.fullmatch(r'sha256:[a-f0-9]{64}',v) for v in ids.values()),
   'Release image inventory is invalid')
 return state


def active_context(api,backend,manifest):
 labels=backend.get('Config',{}).get('Labels') or {}
 if 'org.deriw.v10-upgrade-state' in labels:
  api.require(STATE_LABEL not in labels and SIDE_LABEL not in labels,'Ambiguous managed release labels')
  return api.load_tool('v10_upgrade').active_context(api,backend,manifest)
 if STATE_LABEL not in labels:
  api.require(SIDE_LABEL not in labels,'Incomplete managed release labels');return None
 path=Path(labels[STATE_LABEL]);state=load_state(api,path,manifest);side=labels.get(SIDE_LABEL)
 api.require(side in ('forward','rollback'),'Unexpected release side')
 snapshot=path.parent/(side+'.compose.json')
 api.require(labels.get('com.docker.compose.project.config_files')==str(snapshot),'Backend Compose file labels differ from its release snapshot')
 api.require(backend.get('Image')==state['image_ids'][side]['backend'],'Backend image differs from its managed release state')
 return {'path':path,'state':state,'side':side,'file':snapshot,'image_ids':state['image_ids'][side]}


def active_model(api,model,context):
 if context is None:return model
 if context.get('upgrade'):return api.load_tool('v10_upgrade').active_model(api,model,context)
 if context['state'].get('bridge_patch'):return api.load_tool('v10_bridge_deploy').active_model(api,model,context)
 model=deepcopy(model)
 for name,service in model['services'].items():
  freeze_null_environment(service,context['state']['before'][name])
  if service.get('image')==context['state']['source_images'][name]:
   service['image']=context['image_ids'][name];service['pull_policy']='never'
 backend=model['services']['backend']
 command=backend.get('command')
 api.require(command in (START,LEGACY),
  'Source backend startup command changed; explicit release review is required')
 backend['command']=START
 backend.setdefault('labels',{}).update({STATE_LABEL:str(context['path']),SIDE_LABEL:context['side']})
 return model


def defer_policy(api,old,new,evidence):
 a=old['migrations'];b=new['migrations']
 api.require(not a.keys()-b.keys() and all(a[key]==b[key] for key in a.keys()&b.keys()),
  'Removed/modified migration files require a separate schema compatibility review')
 added=b.keys()-a.keys()
 api.require(added<=DEFERRED.keys() and all(b[key]==DEFERRED[key] for key in added),
  'Candidate contains unreviewed migrations; start-only deployment is not approved for this candidate')
 # Even when already present in a previous release, these exact index-only
 # migrations may remain pending. No other unapplied migration is permitted.
 for key in b.keys()&DEFERRED.keys():
  api.require(b[key]==DEFERRED[key],'Reviewed deferred migration contents changed')
 rows=evidence['current_database_migrations']
 api.require(bool(rows) and all(row.get('status')=='ok' for row in rows),'Migration status is unavailable')
 for row in rows:
  pending=set(row['candidate_pending_versions'])
  api.require(pending<=VERSIONS and (not pending or row.get('migration_directory')=='repo'),
   'Non-index migrations are pending; deployment requires a separate migration/recovery plan')
  # Live-image pending migrations are acceptable only after a previous managed
  # start-only release, and only when they are the reviewed index migrations.
  api.require(row['pending_count']<=len(pending),'Unexpected live-image pending migration count')
 permitted={'migration_file_differences_require_review','pending_or_unavailable_migration_status'}
 api.require(set(evidence['blockers'])<=permitted,'Candidate has unrelated pre-activation blockers')
 return sorted(set().union(*(set(row['candidate_pending_versions']) for row in rows)))


def preflight(api,builder,checker,root,manifest,requirements,values,private_path,receipt_path,live_directory):
 before=api.inspect_project(manifest)
 evidence=checker.check(api,builder,root,manifest,requirements,values,private_path,receipt_path,live_directory)
 receipt,image=checker.candidate(api,builder,root,manifest,requirements,values,private_path,receipt_path)
 api.require(receipt['baseline_image_ids']=={name:row['Image'] for name,row in before.items()},'Live images changed since this candidate was built')
 old=checker.image_inventory(api,before['backend']['Image']);new=checker.image_inventory(api,image['Id'])
 pending=defer_policy(api,old,new,evidence)
 context=active_context(api,before['backend'],manifest)
 files=api.live_files(live_directory,before['backend'],manifest)
 current=api.compose_model(live_directory,manifest,values,files,context is not None)
 managed=api.managed_model(root,manifest,values)
 api.require(current['services']['backend'].get('command') in (START,LEGACY)
  and managed['services']['backend'].get('command') in (START,LEGACY),'Unreviewed backend startup command')
 api.require(not api.live_environment_differences(current,before),'Live declared environment differs from the saved Compose model')
 api.ensure_stable(before,api.inspect_project(manifest),'Live deployment changed during release preflight')
 return {'before':before,'current':current,'managed':managed,'receipt':receipt,'image':image,'files':files,'managed_live':context is not None,
  'pending':pending,'source_state':builder.clean_source(api,root)}


def snapshot(model,rows,image,state_path,side):
 result=deepcopy(model)
 for name,service in result['services'].items():
  freeze_null_environment(service,rows[name])
  service.pop('build',None);service['image']=rows[name]['Image'];service['pull_policy']='never'
 backend=result['services']['backend'];backend['image']=image;backend['command']=START
 backend.setdefault('labels',{}).update({STATE_LABEL:str(state_path),SIDE_LABEL:side})
 return result


def freeze_null_environment(service,row):
 observed={value.split('=',1)[0]:value.split('=',1)[1] for value in row.get('Config',{}).get('Env') or [] if '=' in value}
 env=service.get('environment') or {}
 for key in list(env):
  if env[key] is None:
   if key in observed:env[key]=observed[key]
   else:env.pop(key)


def snapshot_bytes(model):
 # Resolved values must be quoted for a second Compose interpolation pass.
 return encoded(model).replace(b'$',b'$$')


def check_snapshot(api,path,expected,manifest,live_directory):
 actual=api.compose_model(live_directory,manifest,{},[path],True)
 differences=api.model_differences(expected,actual)
 api.require(not differences,'Stored Compose snapshot differs from the selected release'+
  (': '+', '.join(differences[:12]) if differences else ''))
 # model_differences checks network definitions and normalizes empty IPAM only.
 for key in (set(expected)|set(actual))-{'services','version','networks'}:
  api.require(expected.get(key)==actual.get(key),'Stored Compose topology differs from selected release')
 return actual


def recreate(api,path,manifest,live_directory,log):
 command=['docker','compose','-p',manifest['compose_project'],'--project-directory',str(live_directory),
  '--env-file',os.devnull,'-f',str(path),'up','-d','--no-deps','--no-build','--pull','never','backend']
 with os.fdopen(os.open(log,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as stream:
  process=subprocess.Popen(command,stdout=stream,stderr=stream,env=api.compose_environment({}))
  started=time.monotonic();next_update=started+20
  try:
   while process.poll() is None:
    elapsed=time.monotonic()-started
    api.require(elapsed<480,'Targeted backend recreation timed out; inspect server-only log and run status/rollback')
    if time.monotonic()>=next_update:
     print(json.dumps({'phase':'recreating_backend','elapsed_seconds':int(elapsed)}),flush=True);next_update=time.monotonic()+20
    time.sleep(1)
   api.require(process.returncode==0,'Targeted backend recreation failed; inspect server-only log and run status/rollback')
  finally:
   if process.poll() is None:
    process.terminate()
    try:process.wait(timeout=10)
    except subprocess.TimeoutExpired:process.kill();process.wait(timeout=10)


def runtime_probe(market_expression=''):
 expression='''
true = Application.get_env(:explorer, Explorer.Chain.Cache.Counters.TransactionsCount)[:approximate?];
"18417507517" = Application.get_env(:block_scout_web, :chain_id);
60000 = Application.get_env(:indexer, :contract_code_retry_delay);
1 = Application.get_env(:indexer, :contract_code_concurrency);
"176114083..latest" = Application.get_env(:indexer, :trace_block_ranges);
market_price_ready = if System.get_env("MARKET_NATIVE_COIN_PRICE_ONLY") == "true" do
  case Explorer.Market.Fetcher.Coin.get_coin_exchange_rate() do
    %{fiat_value: %Decimal{} = price} = coin ->
      rate = Explorer.Market.apply_native_coin_policy(coin);
      Decimal.compare(price, Decimal.new(0)) == :gt and
        is_nil(rate.total_supply) and is_nil(rate.available_supply) and is_nil(rate.market_cap)
    _ -> false
  end
else
  nil
end;
IO.puts("BLOCKSCOUT_DEPLOY_CHECK=" <> Jason.encode!(%{
  settings_ok: true,
  market_price_ready: market_price_ready,
  historical_scanner_running: is_pid(Process.whereis(Indexer.Temporary.UncatalogedTokenTransfers)),
  repair_disabled: Indexer.Temporary.UncatalogedTokenTransfers.Supervisor.disabled?()
}))
'''
 return market_expression+expression


def unchanged_others(api,before,after):
 api.ensure_stable({k:v for k,v in before.items() if k!='backend'},
  {k:v for k,v in after.items() if k!='backend'},'Unrelated service changed during release; no automatic overwrite or rollback')
 api.require(all(before[k].get('State',{}).get('Running')==after[k].get('State',{}).get('Running')
  for k in before if k!='backend'),'Unrelated service running state changed')


def verify(api,builder,checker,manifest,state_path,state,side,wait=False):
 model=api.compose_model(Path(state['live_directory']),manifest,{},[state_path.parent/(side+'.compose.json')],True)
 ready=False;runtime=False;scanner=None;price_ready=None;deadline=time.monotonic()+120
 for attempt in range(30 if wait else 1):
  rows=api.inspect_project(manifest,require_running=False)
  unchanged_others(api,state['before'],rows)
  backend=rows['backend'];service=model['services']['backend']
  api.require(backend['Image']==state['image_ids'][side]['backend'],'Backend image is not the selected release')
  expected_labels={STATE_LABEL:str(state_path),SIDE_LABEL:side}
  api.require(all((backend.get('Config',{}).get('Labels') or {}).get(k)==v for k,v in expected_labels.items()),
   'Backend is not owned by the selected release side')
  api.require(backend.get('Config',{}).get('Cmd')==START,'Backend command differs; startup migrations must remain disabled')
  api.require(not api.live_environment_differences({'services':{'backend':service}},{'backend':backend}),
   'Selected backend environment differs from its private release snapshot')
  old=state['before']['backend']
  api.require(api.inspect_mounts(old.get('Mounts'))==api.inspect_mounts(backend.get('Mounts'))
   and (old.get('HostConfig') or {}).get('PortBindings')==(backend.get('HostConfig') or {}).get('PortBindings'),
   'Backend mounts or published ports changed')
  api.require(set(old.get('NetworkSettings',{}).get('Networks') or {})==set(backend.get('NetworkSettings',{}).get('Networks') or {}),
   'Backend network set changed')
  try:
   health=checker.live_health(api,rows)
   probe=checker.marked_json(api,api.run(['docker','exec','backend','/app/bin/blockscout','rpc',
    runtime_probe(builder.market_policy().smoke_expression())],timeout=20))
   scanner=probe.get('historical_scanner_running')
   price_ready=probe.get('market_price_ready')
   runtime=probe.get('settings_ok') is True
   if side=='forward':runtime=runtime and probe.get('repair_disabled') is True and scanner is False
   price_ok=service.get('environment',{}).get('MARKET_NATIVE_COIN_PRICE_ONLY')!='true' or price_ready is True
   ready=all(health.values()) and runtime and price_ok and backend.get('State',{}).get('Running') is True and backend.get('RestartCount')==0
   if ready:api.rpc_chain(manifest)
  except api.ConfigError:
   health={'backend_readiness_ok':False,'nft_connection_and_queue_ok':False};ready=False;runtime=False;price_ready=None
  fresh=api.inspect_project(manifest,require_running=False)
  api.ensure_stable(rows,fresh,'Deployment changed during release verification')
  if ready or not wait or time.monotonic()>=deadline:break
  if attempt%5==0:print(json.dumps({'phase':'waiting_for_backend_readiness','attempt':attempt+1}),flush=True)
  time.sleep(2)
 return {'status':'verified' if ready else 'needs_attention','environment':'dev','side':side,'state':str(state_path),'recorded_phase':state['status'],
  'backend_image':backend['Image'],'backend':api.lifecycle_summary(backend),**health,'runtime_settings_ok':runtime,
  'market_price_ready':price_ready,
  'historical_scanner_running':scanner,'startup_migrations_executed':False,'unrelated_containers_unchanged':True,
  'deferred_index_migrations':state['deferred_migrations'],
  'scope':'Readiness, selected runtime settings, scanner policy, NFT connectivity, container/mount preservation and enabled native-price cache. '
   'No DB restore or migration. Chain/indexing progress and representative API behavior need subsequent observation.'}


def deploy(api,builder,checker,root,manifest,requirements,values,private_path,receipt_path,live_directory,apply=False):
 if apply:
  with builder.build_lock(api,api.MANAGED_RELEASES,root):
   return _deploy(api,builder,checker,root,manifest,requirements,values,private_path,receipt_path,live_directory,True)
 return _deploy(api,builder,checker,root,manifest,requirements,values,private_path,receipt_path,live_directory,False)


def _deploy(api,builder,checker,root,manifest,requirements,values,private_path,receipt_path,live_directory,apply):
 prepared=preflight(api,builder,checker,root,manifest,requirements,values,private_path,receipt_path,live_directory)
 public={'status':'start_only_deployment_plan_ready','environment':'dev','candidate_image':prepared['image']['Id'],
  'only_recreated_service':'backend','startup_command':START,'startup_migrations':False,
  'deferred_index_migrations':prepared['pending'],'historical_token_repair_enabled':False,
  'effect':'Apply recreates only backend, briefly pausing API/indexing. Existing data/project/network and other services are preserved. '
   'No database migration/index build is requested. Rollback also starts the previous image without migrations.'}
 if not apply:return public
 directory=api.MANAGED_RELEASES/('release-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')+uuid.uuid4().hex[:8])
 directory.mkdir(mode=0o700);state_path=directory/'state.json'
 forward=snapshot(prepared['managed'],prepared['before'],prepared['image']['Id'],state_path,'forward')
 rollback=snapshot(prepared['current'],prepared['before'],prepared['before']['backend']['Image'],state_path,'rollback')
 hashes={}
 for side,model in (('forward',forward),('rollback',rollback)):
  data=snapshot_bytes(model);write_new(directory/(side+'.compose.json'),data);hashes[side]=digest(data)
  check_snapshot(api,directory/(side+'.compose.json'),model,manifest,live_directory)
 state={'format':FORMAT,'environment':'dev','chain_id':manifest['chain_id'],'compose_project':manifest['compose_project'],
  'status':'prepared','startup_migrations':False,'before':prepared['before'],'snapshot_hashes':hashes,
  'source_images':{name:service.get('image') for name,service in prepared['managed']['services'].items()},
  'live_directory':str(live_directory.resolve()),'candidate_source_commit':prepared['receipt']['source_commit'],
  'image_ids':{side:{name:service['image'] for name,service in model['services'].items()}
   for side,model in (('forward',forward),('rollback',rollback))},'deferred_migrations':prepared['pending']}
 write_new(state_path,encoded(state))
 print(json.dumps({'phase':'release_prepared','state':str(state_path),
  'rollback_command':'./deploy.sh dev rollback --state '+str(state_path)+' --apply',
  'note':'Keep this entire private release directory on the server; it contains runtime secrets.'}),flush=True)
 api.require(builder.clean_source(api,root)==prepared['source_state'] and api.load_private(private_path,requirements)==values,
  'Source or private inputs changed before activation')
 api.require(api.compose_model(live_directory,manifest,values,prepared['files'],prepared['managed_live'])==prepared['current']
  and api.managed_model(root,manifest,values)==prepared['managed'],'Source/live deployment model changed before activation')
 api.ensure_stable(prepared['before'],api.inspect_project(manifest),'Live deployment changed before activation')
 api.require(builder.image_info(api,prepared['receipt']['candidate_tag'])['Id']==prepared['image']['Id'],'Candidate tag changed before activation')
 builder.image_info(api,state['image_ids']['rollback']['backend'])
 api.run(['docker','image','tag',state['image_ids']['rollback']['backend'],'deriw-blockscout:rollback-'+directory.name])
 state['status']='activation_requested';update_state(state_path,state)
 load_state(api,state_path,manifest)
 recreate(api,directory/'forward.compose.json',manifest,live_directory,directory/'activate.log.private')
 result=verify(api,builder,checker,manifest,state_path,state,'forward',wait=True)
 state['status']='active' if result['status']=='verified' else 'needs_attention';update_state(state_path,state)
 result['recorded_phase']=state['status']
 return result


def status(api,builder,checker,manifest,state_path):
 state=load_state(api,state_path,manifest)
 rows=api.inspect_project(manifest,require_running=False,allow_missing_backend=True)
 if 'backend' not in rows:
  unchanged_others(api,state['before'],rows)
  return {'status':'backend_missing_needs_attention','state':str(state_path),'recorded_phase':state['status'],
   'rollback_command':'./deploy.sh dev rollback --state '+str(state_path)+' --apply'}
 labels=rows['backend'].get('Config',{}).get('Labels') or {}
 if labels.get(STATE_LABEL)!=str(state_path):
  unchanged_others(api,state['before'],rows)
  api.require(rows['backend']['Id']==state['before']['backend']['Id'],'Another release now owns backend; this state cannot manage it')
  return {'status':'not_activated','state':str(state_path),'recorded_phase':state['status'],'activation_performed':False}
 side=labels.get(SIDE_LABEL);api.require(side in ('forward','rollback'),'Unexpected live release side')
 return verify(api,builder,checker,manifest,state_path,state,side)


def rollback(api,builder,checker,root,manifest,state_path,apply=False):
 if apply:
  with builder.build_lock(api,api.MANAGED_RELEASES,root):return _rollback(api,builder,checker,manifest,state_path,True)
 return _rollback(api,builder,checker,manifest,state_path,False)


def _rollback(api,builder,checker,manifest,state_path,apply):
 state=load_state(api,state_path,manifest);before=api.inspect_project(manifest,require_running=False,allow_missing_backend=True)
 unchanged_others(api,state['before'],before)
 if 'backend' in before:
  labels=before['backend'].get('Config',{}).get('Labels') or {}
  owned=labels.get(STATE_LABEL)==str(state_path) and labels.get(SIDE_LABEL) in ('forward','rollback')
  api.require(owned or before['backend']['Id']==state['before']['backend']['Id'],
   'Another release owns backend; rollback will not overwrite it')
  if owned:
   side=labels[SIDE_LABEL]
   expected=api.compose_model(Path(state['live_directory']),manifest,{},[state_path.parent/(side+'.compose.json')],True)
   api.require(before['backend']['Image']==state['image_ids'][side]['backend']
    and before['backend'].get('Config',{}).get('Cmd')==START
    and not api.live_environment_differences({'services':{'backend':expected['services']['backend']}},{'backend':before['backend']}),
    'Owned backend settings changed after this release; review before rollback')
 else:
  api.require(state['status'] in ('activation_requested','rollback_requested','needs_attention','rollback_needs_attention'),
   'Backend is missing without a recorded recreation attempt; inspect manually before recovery')
 builder.image_info(api,state['image_ids']['rollback']['backend'])
 if not apply:return {'status':'rollback_plan_ready','state':str(state_path),'image':state['image_ids']['rollback']['backend'],
  'only_recreated_service':'backend','startup_migrations':False,
  'note':'Restores previous image and environment using start-only startup. The previous historical scanner policy can resume. No schema downgrade or DB restore.'}
 state['status']='rollback_requested';update_state(state_path,state)
 load_state(api,state_path,manifest)
 recreate(api,state_path.parent/'rollback.compose.json',manifest,Path(state['live_directory']),
  state_path.parent/('rollback-'+uuid.uuid4().hex[:8]+'.log.private'))
 result=verify(api,builder,checker,manifest,state_path,state,'rollback',wait=True)
 state['status']='rolled_back' if result['status']=='verified' else 'rollback_needs_attention';update_state(state_path,state)
 result['recorded_phase']=state['status']
 return result
