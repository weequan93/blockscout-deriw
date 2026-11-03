#!/usr/bin/env python3
"""Collect dev inputs, compare, build or inspect a candidate. No activation."""
import argparse
import importlib.util
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]
ENV_KEY=re.compile(r'[A-Z][A-Z0-9_]{0,159}')
IDENT=re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}')


class ConfigError(Exception):pass


class StabilityError(ConfigError):
 def __init__(self,message,changes,order_changes):
  super().__init__(message)
  self.changes=changes;self.order_changes=order_changes


def require(condition,message):
 if not condition:raise ConfigError(message)


def run(command,environment=None,timeout=30,input_text=None):
 try:result=subprocess.run(command,capture_output=True,text=True,errors='replace',env=environment,timeout=timeout,input=input_text)
 except (OSError,subprocess.SubprocessError):raise ConfigError('Docker/Compose read unavailable; raw output suppressed') from None
 require(result.returncode==0 and len(result.stdout)<16*1024*1024,'Docker/Compose read failed or exceeded limit; raw output suppressed')
 return result.stdout


def read_json(path):
 try:return json.loads(path.read_text())
 except (OSError,ValueError):raise ConfigError('Required JSON input unavailable or invalid; raw values suppressed') from None


def configuration(root,environment):
 require(environment=='dev','Only dev has a captured profile. Test/prod need their own inventories; no defaults were selected.')
 directory=root/'docker-compose/environments/dev'
 manifest=read_json(directory/'manifest.json');requirements=read_json(directory/'requirements.json')
 profile=read_json(directory/'compose.json')
 require(manifest.get('environment')=='dev' and manifest.get('chain_id')=='18417507517'
  and manifest.get('chain_type')=='arbitrum' and manifest.get('compose_project')=='docker-compose','Unexpected dev manifest identity')
 require(isinstance(requirements,dict) and len(requirements)==manifest.get('private_variable_count'),'Private input registry differs from manifest')
 for key,origin in requirements.items():
  require(bool(ENV_KEY.fullmatch(key)) and key.startswith('DEV_'),'Unexpected private variable name')
  require(isinstance(origin,dict) and set(origin)=={'service','environment_key'}
   and bool(IDENT.fullmatch(origin['service'])) and bool(ENV_KEY.fullmatch(origin['environment_key'])),'Unexpected private input origin')
 require(set(profile.get('services',{}))==set(manifest.get('baseline_image_ids',{})),'Profile service inventory differs from manifest')
 return manifest,requirements,profile


def env_map(row):
 result={}
 for item in row.get('Config',{}).get('Env',[]) or []:
  require(isinstance(item,str) and '=' in item,'Unexpected container environment format')
  key,value=item.split('=',1)
  if ENV_KEY.fullmatch(key):result[key]=value
 return result


def inspect_project(manifest):
 project=manifest['compose_project']
 ids=run(['docker','container','ls','--all','--no-trunc','--quiet','--filter','label=com.docker.compose.project='+project]).split()
 require(1<=len(ids)<=64 and len(ids)==len(set(ids)) and all(re.fullmatch(r'[a-f0-9]{64}',v) for v in ids),'Unexpected project container inventory')
 try:rows=json.loads(run(['docker','inspect',*sorted(ids)]))
 except ValueError:raise ConfigError('Invalid Docker metadata; raw output suppressed') from None
 require(isinstance(rows,list) and sorted(row.get('Id','') for row in rows)==sorted(ids),'Project changed during inspection')
 by_service={}
 for row in rows:
  labels=row.get('Config',{}).get('Labels') or {};service=labels.get('com.docker.compose.service','')
  require(labels.get('com.docker.compose.project')==project and bool(IDENT.fullmatch(service)),'Unexpected project/service identity')
  require(service not in by_service,'Multiple containers for one service require explicit review')
  by_service[service]=row
 require(set(by_service)==set(manifest['baseline_image_ids']),'Live service set differs from the captured dev inventory')
 backend=by_service['backend'];values=env_map(backend)
 require(values.get('CHAIN_ID')==manifest['chain_id'] and values.get('CHAIN_TYPE')==manifest['chain_type'],'Live backend is not the captured dev chain')
 require(backend.get('State',{}).get('Running') is True,'Dev backend must be running for these checks')
 return by_service


def inspect_mounts(mounts):
 # Docker inspect describes the final mounts, not their application order. Older
 # daemons build this array by iterating a map; compare its full records as a multiset.
 require(mounts is None or isinstance(mounts,list),'Unexpected Docker mount metadata')
 require(all(isinstance(row,dict) for row in mounts or []),'Unexpected Docker mount record')
 return sorted(mounts or [],key=lambda row:json.dumps(row,sort_keys=True,separators=(',',':')))


def stable(rows):
 return {name:(row.get('Id'),row.get('Image'),row.get('Config'),row.get('HostConfig'),
  inspect_mounts(row.get('Mounts')),row.get('NetworkSettings',{}).get('Networks'),row.get('State',{}).get('StartedAt'),
  row.get('RestartCount')) for name,row in rows.items()}


def lifecycle_summary(row):
 result={};state=row.get('State') or {}
 if state.get('Status') in ('created','running','paused','restarting','removing','exited','dead'):result['status']=state['Status']
 if type(row.get('RestartCount')) is int:result['restart_count']=row['RestartCount']
 started=state.get('StartedAt','')
 if isinstance(started,str) and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z',started):result['started_utc']=started
 identity=row.get('Id','')
 if isinstance(identity,str) and re.fullmatch(r'[a-f0-9]{64}',identity):result['container_id_prefix']=identity[:12]
 return result


def inspection_order_changes(before,after):
 return ['services.'+name+'.inspect_mounts_order' for name in sorted(set(before)&set(after))
  if before[name].get('Mounts')!=after[name].get('Mounts')
  and inspect_mounts(before[name].get('Mounts'))==inspect_mounts(after[name].get('Mounts'))]


def metadata_field_changes(before,after,allowed):
 before=before or {};after=after or {}
 changed={key for key in set(before)|set(after) if before.get(key)!=after.get(key) or (key in before)!=(key in after)}
 return {'fields':sorted(changed&allowed),'other_field_count':len(changed-allowed)}


def stability_changes(before,after):
 a=stable(before);b=stable(after);result=[]
 names=('container_identity','image_identity','container_configuration','host_configuration','mounts','network_attachments','started_at','restart_count')
 for name in sorted(set(before)|set(after)):
  require(isinstance(name,str) and bool(IDENT.fullmatch(name)),'Unexpected service name during stability check')
  if name not in a or name not in b:
   result.append({'service':name,'changed_fields':['container_added_or_removed']});continue
  changed=[field for i,field in enumerate(names) if a[name][i]!=b[name][i]]
  if not changed:continue
  item={'service':name,'changed_fields':changed,'before':lifecycle_summary(before[name]),'after':lifecycle_summary(after[name])}
  if 'container_configuration' in changed:
   item['configuration_fields']=metadata_field_changes(before[name].get('Config'),after[name].get('Config'),
    {'Env','Labels','Cmd','Entrypoint','Hostname','Domainname','User','WorkingDir','Image','Healthcheck','ExposedPorts',
     'AttachStdin','AttachStdout','AttachStderr','OpenStdin','Tty','StopSignal','StopTimeout','StdinOnce','Volumes','ArgsEscaped','Shell'})
  if 'host_configuration' in changed:
   item['host_configuration_fields']=metadata_field_changes(before[name].get('HostConfig'),after[name].get('HostConfig'),
    {'Binds','Mounts','NetworkMode','PortBindings','Dns','DnsOptions','DnsSearch','ExtraHosts','Links','LogConfig','RestartPolicy',
     'Memory','MemorySwap','MemoryReservation','NanoCpus','CpuShares','CpuPeriod','CpuQuota','CpusetCpus','ShmSize',
     'IpcMode','PidMode','ReadonlyRootfs','Ulimits','OomKillDisable','AutoRemove','Init','Privileged','SecurityOpt'})
  result.append(item)
 return result


def ensure_stable(before,after,message):
 changes=stability_changes(before,after)
 if changes:raise StabilityError(message,changes,inspection_order_changes(before,after))


def collect_values(requirements,rows):
 values={}
 for ref,origin in requirements.items():
  env=env_map(rows[origin['service']]);value=env.get(origin['environment_key'])
  require(isinstance(value,str) and value!='','Required live value missing: '+ref)
  values[ref]=value
 require(env_map(rows['backend']).get('RELEASE_COOKIE')==env_map(rows['nft_media_handler']).get('RELEASE_COOKIE')
  and bool(env_map(rows['backend']).get('RELEASE_COOKIE')),'Backend and NFT worker release cookies must match')
 return values


def save_private(path,values,root):
 require(not path.is_symlink(),'Private output cannot be a symlink')
 resolved=path.resolve()
 require(resolved!=root.resolve() and root.resolve() not in resolved.parents,'Private output must be outside the Git checkout')
 require(not path.exists(),'Private output already exists; choose a new file')
 path.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
 require(stat.S_IMODE(path.parent.stat().st_mode)&0o077==0,'Private output parent must have permissions 0700')
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 try:
  with os.fdopen(fd,'w') as stream:
   json.dump({'format':'blockscout-private-v1','environment':'dev','values':values},stream,indent=2,sort_keys=True)
   stream.write('\n');stream.flush();os.fsync(stream.fileno())
 except BaseException:
  path.unlink();raise


def load_private(path,requirements):
 require(not path.is_symlink() and path.is_file(),'Expected a regular private input file')
 require(stat.S_IMODE(path.stat().st_mode)&0o077==0,'Private input permissions must exclude group/other access')
 data=read_json(path)
 require(isinstance(data,dict) and data.get('format')=='blockscout-private-v1' and data.get('environment')=='dev','Private input format/environment mismatch')
 values=data.get('values')
 require(isinstance(values,dict) and set(values)==set(requirements),'Private input names differ from the versioned registry')
 require(all(isinstance(v,str) and v!='' and '\x00' not in v for v in values.values()),'Private inputs must be nonempty strings without NUL characters')
 return values


def compose_environment(values):
 # Preserve Docker connection/auth settings, but ignore ambient profile values and implicit Compose selection.
 environment={k:v for k,v in os.environ.items() if not k.startswith(('DEV_','COMPOSE_'))}
 environment.update(values)
 return environment


@lru_cache(maxsize=1)
def compose_json_escapes_dollars():
 # Compose renders a replayable document and some versions double every dollar.
 # Calibrate with public fixture data instead of guessing from the CLI version.
 value='blockscout$literal$$pair'
 fixture={'services':{'fixture':{'image':'busybox:config-only','environment':{'VALUE':'${BLOCKSCOUT_RENDER_FIXTURE:?required}'}}}}
 environment=compose_environment({});environment['BLOCKSCOUT_RENDER_FIXTURE']=value
 command=['docker','compose','-p','blockscout-config-format','--env-file',os.devnull,'-f','-','config','--format','json']
 try:actual=json.loads(run(command,environment=environment,input_text=json.dumps(fixture)))['services']['fixture']['environment']['VALUE']
 except (ValueError,KeyError,TypeError):raise ConfigError('Cannot establish Compose JSON escaping behavior; raw output suppressed') from None
 if actual==value.replace('$','$$'):return True
 require(actual==value,'Unexpected Compose JSON escaping behavior; no private model comparison attempted')
 return False


def compose_model(root,manifest,values,files,managed):
 command=['docker','compose','-p',manifest['compose_project'],'--project-directory',str(root)]
 if managed:command+=['--env-file',os.devnull]
 for path in files:command+=['-f',str(path)]
 escaped=compose_json_escapes_dollars()
 output=run(command+['config','--format','json'],environment=compose_environment(values))
 if escaped:output=output.replace('$$','$')
 try:model=json.loads(output)
 except ValueError:raise ConfigError('Compose returned invalid JSON; raw values suppressed') from None
 require(isinstance(model,dict) and isinstance(model.get('services'),dict),'Unexpected resolved Compose model')
 return model


def managed_model(root,manifest,values):
 directory=root/'docker-compose'
 model=compose_model(directory,manifest,values,[directory/'docker-compose.yml',directory/'environments/dev/compose.json'],True)
 backend=model['services'].get('backend',{}).get('environment',{})
 require(backend.get('CHAIN_ID')==manifest['chain_id'] and backend.get('CHAIN_TYPE')==manifest['chain_type'],'Resolved profile selected the wrong chain')
 require(backend.get('ETHEREUM_JSONRPC_HTTP_URL')==manifest['http_rpc'] and backend.get('ETHEREUM_JSONRPC_TRACE_URL')==manifest['http_rpc'],'Resolved profile selected unexpected L2 endpoints')
 frontend=model['services'].get('frontend',{}).get('environment',{})
 require(frontend.get('NEXT_PUBLIC_NETWORK_ID')==manifest['chain_id'],'Frontend network ID differs from the dev chain')
 require(frontend.get('NEXT_PUBLIC_NETWORK_RPC_URL')=='https://rpc.dev.deriw.com','Frontend RPC differs from the captured dev endpoint')
 user_ops=model['services'].get('user-ops-indexer',{}).get('environment',{})
 require(user_ops.get('USER_OPS_INDEXER__INDEXER__RPC_URL')==manifest['http_rpc'],'User-ops indexer RPC differs from dev backend RPC')
 def ports(rows):
  return sorted((str(row.get('target','')),str(row.get('published','')),row.get('host_ip') or '',
   row.get('protocol','tcp'),row.get('mode','ingress')) for row in rows)
 require(set(manifest.get('expected_ports',{}))==set(model['services']),'Captured port contract is incomplete')
 for name,service in model['services'].items():
  require(ports(service.get('ports') or [])==ports(manifest['expected_ports'][name]),'Inherited ports differ from captured dev settings: '+name)
 database=manifest.get('expected_database') or {}
 require(database.get('service')=='db' and isinstance(database.get('postgres_parameters'),dict),'Captured database contract is incomplete')
 db=model['services'].get('db') or {};tuning=postgres_command_summary(db.get('command'),set())
 require(tuning.get('recognized') and tuning.get('unclassified_argument_count')==0
  and tuning.get('parameters')==database['postgres_parameters']
  and len(tuning.get('ordered_parameters',[]))==len(database['postgres_parameters']),'Resolved PostgreSQL tuning differs from confirmed dev settings')
 require(str(db.get('shm_size'))==str(database.get('shm_size_bytes')),'Resolved PostgreSQL shared-memory limit differs from confirmed dev settings')
 for key in ('CHAIN_ID','ETHEREUM_JSONRPC_HTTP_URL','ETHEREUM_JSONRPC_TRACE_URL'):
  require('rpc.prod.deriw.com' not in str(backend.get(key,'')),'Production RPC leaked into dev backend selection')
 return model


def live_files(directory,backend,manifest):
 labels=backend.get('Config',{}).get('Labels') or {}
 raw=labels.get('com.docker.compose.project.config_files','').split(',')
 require(1<=len(raw)<=16 and all(raw),'Backend Compose file labels are unavailable')
 paths=[]
 for name in raw:
  path=Path(name)
  require(path.is_absolute() and not path.is_symlink() and path.is_file(),'Expected regular recorded live Compose files')
  try:path.resolve().relative_to(directory.resolve())
  except ValueError:raise ConfigError('Live Compose files are outside the selected live directory') from None
  paths.append(path)
 require([str(p.resolve().relative_to(directory.resolve())) for p in paths]==manifest['baseline_compose_layers'],'Live file order differs from the captured eight layers')
 return paths


def model_differences(before,after):
 # Values are never included. Limit path names to trusted Compose fields and validated env keys.
 result=[]
 for name in sorted(set(before.get('services',{}))|set(after.get('services',{}))):
  require(bool(IDENT.fullmatch(name)),'Unexpected service name in model')
  a=before.get('services',{}).get(name);b=after.get('services',{}).get(name)
  if a is None or b is None:result.append('services.'+name);continue
  for key in sorted(set(a)|set(b)):
   if key=='build':continue
   if a.get(key)==b.get(key):continue
   if key=='networks' and canonical_networks(a.get(key))==canonical_networks(b.get(key)):continue
   if key=='volumes' and canonical_volumes(a.get(key))==canonical_volumes(b.get(key)):continue
   if key=='environment':
    av=a.get(key) or {};bv=b.get(key) or {}
    for env in sorted(set(av)|set(bv)):
     if av.get(env)!=bv.get(env):
      require(bool(ENV_KEY.fullmatch(env)),'Unexpected environment key in model')
      result.append('services.'+name+'.environment.'+env)
   else:
    result.append('services.'+name+'.'+key if re.fullmatch(r'[a-z_]+',key) else 'services.'+name+'.other_field')
 for key in ('networks','volumes','secrets','configs'):
  if (before.get(key) or {})!=(after.get(key) or {}):result.append(key)
 return result


def canonical_networks(networks):
 result={}
 for name,options in (networks or {}).items():
  options=dict(options or {})
  if options.get('aliases')==[]:options.pop('aliases')
  result[name]=options
 return result


def canonical_volumes(volumes):
 result=[]
 for row in volumes or []:
  item=dict(row)
  # Read/write is the default. An empty bind-options object supplies no override.
  # Preserve every explicit bind policy, nondefault value and mount ordering.
  if item.get('read_only') is False:item.pop('read_only')
  if item.get('type')=='bind' and item.get('bind') in (None,{}):item.pop('bind',None)
  result.append(item)
 return result


PUBLIC_PG_PARAMETERS=frozenset(('max_connections shared_buffers effective_cache_size maintenance_work_mem work_mem '
 'wal_buffers min_wal_size max_wal_size checkpoint_timeout checkpoint_completion_target effective_io_concurrency '
 'max_worker_processes max_parallel_workers max_parallel_workers_per_gather max_parallel_maintenance_workers '
 'random_page_cost seq_page_cost default_statistics_target client_connection_check_interval autovacuum_max_workers '
 'autovacuum_vacuum_cost_limit autovacuum_vacuum_scale_factor autovacuum_analyze_scale_factor autovacuum_work_mem '
 'statement_timeout idle_in_transaction_session_timeout max_locks_per_transaction jit synchronous_commit').split())


def sensitive_values(requirements,values):
 result=set()
 for ref,origin in requirements.items():
  key=origin['environment_key'];value=values[ref]
  # This runtime setting is an integer rate limit, despite its TOKEN suffix.
  if key=='API_RATE_LIMIT_UI_V2_WITH_TOKEN' and re.fullmatch(r'[0-9]+',value):continue
  if re.search(r'PASSWORD|PASSWD|SECRET|COOKIE|CREDENTIAL|PRIVATE.?KEY|API.?KEY|BEARER|(?:^|_)TOKEN$',key,re.I) and value:
   result.add(value)
  # URLs can carry credentials even when the environment key looks public.
  if '://' in value:
   from urllib.parse import unquote,urlsplit
   try:password=urlsplit(value).password
   except ValueError:password=None
   if password:
    result.update(v for v in (password,unquote(password)) if v)
 return result


def public_tuning_value(value,sensitive):
 if isinstance(value,(str,int)) and not isinstance(value,bool):
  text=str(value)
  if (re.fullmatch(r'[0-9]+(?:\.[0-9]+)?(?:[kKmMgGtT]?[bB]|[kKmMgGtT]|us|ms|s|min|h|d)?',text)
    or text in ('on','off','local','remote_write','remote_apply')) and not any(v in text for v in sensitive):return text
 return '[withheld]'


def postgres_command_summary(command,sensitive):
 if isinstance(command,str):
  try:command=shlex.split(command)
  except ValueError:return {'recognized':False,'raw_command_withheld':True}
 if not isinstance(command,list) or not command or command[0]!='postgres':
  return {'recognized':False,'raw_command_withheld':True}
 result={'recognized':True,'parameters':{},'unclassified_argument_count':0,'raw_command_withheld':True}
 i=1
 while i<len(command):
  if command[i]=='-c' and i+1<len(command) and isinstance(command[i+1],str):
   item=command[i+1];i+=2
  elif isinstance(command[i],str) and command[i].startswith('-c') and len(command[i])>2:
   item=command[i][2:];i+=1
  else:result['unclassified_argument_count']+=1;i+=1;continue
  key,separator,value=item.partition('=')
  if separator and key in PUBLIC_PG_PARAMETERS:
   # Retain duplicates and ordering, since the last assignment wins.
   result.setdefault('ordered_parameters',[]).append({'name':key,'value':public_tuning_value(value,sensitive)})
   result['parameters'][key]=public_tuning_value(value,sensitive)
  else:result['unclassified_argument_count']+=1
 return result


def bind_options(row):
 result={};options=row.get('bind') or {}
 for key in ('create_host_path','non_recursive','recursive_read_only'):
  if type(options.get(key)) is bool:result[key]=options[key]
 if options.get('propagation') in ('private','rprivate','shared','rshared','slave','rslave'):
  result['propagation']=options['propagation']
 if options.get('selinux') in ('z','Z'):result['selinux']=options['selinux']
 result['unclassified_option_count']=len(set(options)-set(result))
 return result


def volume_details(current,managed,live,sensitive):
 before=current.get('volumes') or [];after=managed.get('volumes') or []
 a={row.get('target'):row for row in before};b={row.get('target'):row for row in after}
 observed={row.get('Destination'):row for row in live.get('Mounts',[]) or []}
 details={'target_sets_equal':set(a)==set(b),'mount_order_equal':[v.get('target') for v in before]==[v.get('target') for v in after],
  'current_count':len(before),'proposed_count':len(after),'mounts':[]}
 for target,row in b.items():
  old=a.get(target)
  if not isinstance(target,str) or not re.fullmatch(r'/[A-Za-z0-9_./-]{0,511}',target) or any(v in target for v in sensitive):
   details['mounts'].append({'target_withheld':True});continue
  item={'target':target,'present_in_current':old is not None}
  if old is not None:
   item.update({'same_source':old.get('source')==row.get('source'),'same_type':old.get('type')==row.get('type'),
    'current_read_only':bool(old.get('read_only',False)),'proposed_read_only':bool(row.get('read_only',False)),
    'current_bind_options':bind_options(old),'proposed_bind_options':bind_options(row),
    'other_fields_equal':{k:v for k,v in old.items() if k not in ('type','source','target','read_only','bind')}
     =={k:v for k,v in row.items() if k not in ('type','source','target','read_only','bind')}})
   if target in observed:
    item['live_source_matches_current']=observed[target].get('Source')==old.get('source')
    item['live_read_only_matches_current']=(not bool(observed[target].get('RW',True)))==bool(old.get('read_only',False))
  details['mounts'].append(item)
 return details


def difference_details(current,managed,rows,requirements,values):
 sensitive=sensitive_values(requirements,values)
 output={'representation_only_fields':[],'volumes':{},'database':{}}
 for name,after in managed['services'].items():
  before=current['services'].get(name,{})
  if before.get('networks')!=after.get('networks') and canonical_networks(before.get('networks'))==canonical_networks(after.get('networks')):
   output['representation_only_fields'].append('services.'+name+'.networks')
  if before.get('volumes')!=after.get('volumes'):
   if canonical_volumes(before.get('volumes'))==canonical_volumes(after.get('volumes')):
    output['representation_only_fields'].append('services.'+name+'.volumes')
   else:output['volumes'][name]=volume_details(before,after,rows.get(name,{}),sensitive)
  if name in ('db','stats-db'):
   item={}
   if before.get('command')!=after.get('command'):
    item['current_command']=postgres_command_summary(before.get('command'),sensitive)
    item['proposed_command']=postgres_command_summary(after.get('command'),sensitive)
   if before.get('shm_size')!=after.get('shm_size'):
    item['current_shm_size_present']='shm_size' in before
    item['proposed_shm_size_present']='shm_size' in after
    item['current_shm_size']=public_tuning_value(before.get('shm_size'),sensitive)
    item['proposed_shm_size']=public_tuning_value(after.get('shm_size'),sensitive)
    item['live_shm_size_bytes']=public_tuning_value(rows.get(name,{}).get('HostConfig',{}).get('ShmSize'),sensitive)
   if item:output['database'][name]=item
 return output


def live_environment_differences(model,rows):
 result=[]
 for service,row in rows.items():
  actual=env_map(row)
  for key,value in (model['services'].get(service,{}).get('environment') or {}).items():
   if value is not None and str(value)!=actual.get(key):
    require(bool(ENV_KEY.fullmatch(key)),'Unexpected environment key')
    result.append('services.'+service+'.environment.'+key)
 return sorted(result)


def rpc_chain(manifest):
 request=json.dumps({'jsonrpc':'2.0','id':1,'method':'eth_chainId','params':[]})
 result=run(['docker','exec','backend','curl','-q','--noproxy','*','-fsS','--connect-timeout','2','--max-time','4',
  '--max-filesize','4096','-H','Content-Type: application/json','--data',request,manifest['http_rpc']],timeout=10)
 try:
  data=json.loads(result);number=int(data['result'],16)
 except (ValueError,KeyError,TypeError):raise ConfigError('L2 RPC chain identity check failed; raw response suppressed') from None
 require(str(number)==manifest['chain_id'],'L2 RPC chain does not match dev manifest')
 return number


def plan(root,manifest,requirements,values,live_directory):
 before=inspect_project(manifest);collect_values(requirements,before)
 current_files=live_files(live_directory,before['backend'],manifest)
 current=compose_model(live_directory,manifest,values,current_files,False)
 managed=managed_model(root,manifest,values)
 require(set(managed['services'])==set(manifest['baseline_image_ids']),'Resolved managed service set changed')
 diff=model_differences(current,managed);drift=live_environment_differences(current,before)
 image_drift=sorted(name for name,row in before.items() if row.get('Image')!=manifest['baseline_image_ids'][name])
 private_drift=[ref for ref,origin in requirements.items() if env_map(before[origin['service']]).get(origin['environment_key'])!=values[ref]]
 require(not private_drift,'Private inputs differ from live values; capture to a new private file and review before adoption')
 chain=rpc_chain(manifest)
 after=inspect_project(manifest)
 ensure_stable(before,after,'Container/configuration/topology changed during comparison; inspect changed_containers')
 require(compose_model(live_directory,manifest,values,current_files,False)==current,'Live resolved model changed during comparison')
 require(managed_model(root,manifest,values)==managed,'Managed resolved model changed during comparison')
 intended='services.backend.environment.INDEXER_UNCATALOGED_TOKEN_TRANSFERS_ENABLED'
 return {'status':'differences_require_review' if diff or drift or image_drift else 'runtime_model_comparison_passed',
  'environment':'dev','rpc_chain_id':chain,'services':len(managed['services']),
  'private_values_exported':False,'changed_runtime_fields':diff,'live_declared_environment_drift':drift,
  'live_images_differing_from_capture':image_drift,
  'inspection_order_only_changes':inspection_order_changes(before,after),
  'runtime_difference_details':difference_details(current,managed,before,requirements,values),
  'startup_policy_change_expected':intended in diff,'current_compose_layers':len(current_files),
  'proposed_compose_layers':2,'build_fields_compared':False,
  'note':'Comparison only. Build recipes, image/tag binding and mounted file contents require separate review before adoption. No build, recreation, migration or deployment was performed.'}


def main():
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('environment',choices=['dev','test','prod'])
 sub=parser.add_subparsers(dest='operation',required=True)
 capture=sub.add_parser('capture-private',help='Write existing live values to a new private server-only JSON file')
 capture.add_argument('--output',type=Path,required=True)
 compare=sub.add_parser('plan',help='Compare the checked-in dev profile against the running eight-layer deployment')
 compare.add_argument('--private-file',type=Path,required=True)
 compare.add_argument('--live-compose-dir',type=Path,default=Path('/data/blockscout/docker-compose'))
 build=sub.add_parser('build',help='Build/check a clean committed candidate without activation')
 build.add_argument('--private-file',type=Path,required=True)
 build.add_argument('--live-compose-dir',type=Path,default=Path('/data/blockscout/docker-compose'))
 build.add_argument('--state-dir',type=Path,default=Path('/data/blockscout-dev-private/releases'))
 mode=build.add_mutually_exclusive_group();mode.add_argument('--check',action='store_true');mode.add_argument('--reuse',action='store_true')
 evidence=sub.add_parser('deploy-check',help='Check candidate migrations, image defaults, mount access and live readiness without activation')
 evidence.add_argument('--private-file',type=Path,required=True)
 evidence.add_argument('--receipt',type=Path,required=True)
 evidence.add_argument('--live-compose-dir',type=Path,default=Path('/data/blockscout/docker-compose'))
 args=parser.parse_args()
 try:
  manifest,requirements,_=configuration(ROOT,args.environment)
  if args.operation=='capture-private':
   before=inspect_project(manifest);values=collect_values(requirements,before)
   ensure_stable(before,inspect_project(manifest),'Deployment changed while collecting private values; no output written')
   save_private(args.output,values,ROOT)
   report={'status':'private_inputs_saved','environment':'dev','value_count':len(values),'output':str(args.output),
    'note':'Keep this file on the server. Do not upload or commit it. No services changed.'}
  elif args.operation=='plan':
   values=load_private(args.private_file,requirements)
   report=plan(ROOT,manifest,requirements,values,args.live_compose_dir)
  else:
   values=load_private(args.private_file,requirements)
   spec=importlib.util.spec_from_file_location('candidate_build',ROOT/'deploy/scripts/candidate_build.py')
   builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)
   if args.operation=='build':
    report=builder.build_candidate(sys.modules[__name__],ROOT,manifest,requirements,values,args.private_file,
     args.live_compose_dir,args.state_dir,check_only=args.check,reuse=args.reuse)
   else:
    spec=importlib.util.spec_from_file_location('deploy_check',ROOT/'deploy/scripts/deploy_check.py')
    checker=importlib.util.module_from_spec(spec);spec.loader.exec_module(checker)
    report=checker.check(sys.modules[__name__],builder,ROOT,manifest,requirements,values,args.private_file,
     args.receipt,args.live_compose_dir)
  print(json.dumps(report,indent=2));return 0
 except (ConfigError,OSError,ValueError,KeyError,TypeError,IndexError,AttributeError,subprocess.SubprocessError) as error:
  report={'status':'stopped','reason':str(error) if isinstance(error,ConfigError) else 'Configuration check failed; raw values suppressed'}
  if isinstance(error,StabilityError):
   report['changed_containers']=error.changes
   report['inspection_order_only_changes']=error.order_changes
  print(json.dumps(report,indent=2));return 1


if __name__=='__main__':raise SystemExit(main())
