#!/usr/bin/env python3
"""Repair the dev stats HTTP route, preserving existing proxy and TLS settings."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import ssl
import stat
import time
import uuid

FORMAT='blockscout-dev-proxy-stats-v2'
LEGACY_FORMAT='blockscout-dev-proxy-stats-v1'
TEMPLATE='/etc/nginx/templates/default.conf.template'
RUNTIME='/etc/nginx/conf.d/default.conf'
RUNTIMES={name:'/etc/nginx/conf.d/'+name for name in ('default.conf','explorer.conf')}
HOST='explorer.dev.deriw.com'
BLOCK='''
    # CDN origin requests can arrive over HTTP.
    location /statsapi/ {
        proxy_pass            http://stats:8050/;
        proxy_http_version    1.1;
        proxy_set_header      Host "$host";
        proxy_set_header      X-Real-IP "$remote_addr";
        proxy_set_header      X-Forwarded-For "$proxy_add_x_forwarded_for";
        proxy_set_header      X-Forwarded-Proto "$scheme";
    }
'''


def tokens(api,text):
 result=[];i=0
 while i<len(text):
  if text[i].isspace():i+=1;continue
  if text[i]=='#':
   stop=text.find('\n',i);i=len(text) if stop<0 else stop+1;continue
  start=i
  if text[i] in '{};':i+=1;result.append((text[start:i],start,i));continue
  if text[i] in "\"'":
   quote=text[i];i+=1
   while i<len(text) and text[i]!=quote:
    i+=2 if text[i]=='\\' else 1
   api.require(i<len(text),'Unterminated quoted Nginx directive')
   i+=1;result.append((text[start+1:i-1],start,i));continue
  while i<len(text) and not text[i].isspace() and text[i] not in '{};':
   if text[i:i+2]=='${':
    stop=text.find('}',i+2);api.require(stop>=0,'Unterminated template variable');i=stop+1
   else:i+=1
  result.append((text[start:i],start,i))
 return result


def parse(api,text):
 stream=tokens(api,text);position=0
 def nodes(nested=False):
  nonlocal position
  result=[];args=[];start=None
  while position<len(stream):
   value,a,b=stream[position];position+=1
   if value=='}':
    api.require(nested and not args,'Unexpected Nginx closing brace');return result,b
   if value in (';','{'):
    api.require(bool(args),'Missing Nginx directive')
    if value=='{':children,end=nodes(True)
    else:children=None;end=b
    result.append({'args':args,'children':children,'start':start,'end':end});args=[];start=None
   else:
    if start is None:start=a
    args.append((value,a,b))
  api.require(not nested and not args,'Incomplete Nginx configuration');return result,len(text)
 return nodes()[0]


def values(node):return [part[0] for part in node['args']]
def directives(node,name):return [child for child in node['children'] or [] if values(child)[0]==name]


def servers(nodes):
 result=[]
 for node in nodes:
  if values(node)==['server']:result.append(node)
  elif node['children']:result.extend(servers(node['children']))
 return result


def listens(node,port):
 for item in directives(node,'listen'):
  address=values(item)[1]
  if address==str(port) or address.endswith(':'+str(port)):return True
 return False


def frontend_servers(api,text,frontend):
 eligible=[]
 for server in servers(parse(api,text)):
  if not listens(server,80):continue
  roots=[node for node in directives(server,'location') if values(node)==['location','/']]
  if len(roots)!=1:continue
  target=directives(roots[0],'proxy_pass')
  if len(target)==1 and values(target[0])[1] in (frontend,'${FRONT_PROXY_PASS}','$FRONT_PROXY_PASS'):
   eligible.append(server)
 return eligible


def patch(api,text,frontend):
 eligible=frontend_servers(api,text,frontend)
 api.require(bool(eligible),'No matching HTTP frontend server in this configuration; live routing requires review')
 edits=[];actions=[]
 for server in eligible:
  routes=[node for node in directives(server,'location') if values(node)[-1]=='/statsapi/']
  api.require(len(routes)<=1,'Multiple HTTP stats locations in one server require review')
  if not routes:
   offset=server['end']-1;edits.append((offset,offset,BLOCK));actions.append('add_http_stats_location');continue
  api.require(values(routes[0]) in (['location','/statsapi/'],['location','^~','/statsapi/']),
   'Unexpected stats location policy')
  targets=directives(routes[0],'proxy_pass')
  api.require(len(targets)==1,'Stats proxy_pass is ambiguous')
  target=targets[0];args=values(target)
  api.require(len(args)==2 and args[1] in ('http://stats:8050','http://stats:8050/'),
   'Unexpected HTTP stats upstream; no existing policy was overwritten')
  if args[1].endswith('/'):actions.append('refresh_upstream_resolution');continue
  _,start,end=target['args'][1];edits.append((start,end,'http://stats:8050/'));actions.append('strip_stats_prefix')
 for start,end,value in sorted(edits,reverse=True):text=text[:start]+value+text[end:]
 return text,{'action':actions[0] if len(set(actions))==1 else 'update_http_stats_locations','changed':bool(edits),
  'http_frontend_servers':len(eligible),'locations_added':actions.count('add_http_stats_location'),
  'uri_slashes_added':actions.count('strip_stats_prefix')}


def route_facts(api,text):
 facts=[]
 for server in servers(parse(api,text)):
  ports=[port for port in (80,443,8080) if listens(server,port)]
  if not ports:continue
  locations=[]
  for location in directives(server,'location'):
   args=values(location)
   if args[-1] not in ('/','/statsapi/'):continue
   targets=directives(location,'proxy_pass')
   target=values(targets[0])[1] if len(targets)==1 else ''
   locations.append({'location':args[-1],'upstream_is_stats':target in ('http://stats:8050','http://stats:8050/'),
    'upstream_is_stats_with_uri_slash':target=='http://stats:8050/'})
  facts.append({'ports':ports,'locations':locations})
 return facts


def read_runtime(api,path=RUNTIME):
 api.require(path in RUNTIMES.values(),'Unexpected runtime proxy configuration path')
 return api.run(['docker','exec','proxy','sh','-c','test ! -L '+path+' && cat '+path])


def write_runtime(api,text,path=RUNTIME):
 api.require(path in RUNTIMES.values(),'Unexpected runtime proxy configuration path')
 api.run(['docker','exec','-i','proxy','sh','-c','test ! -L '+path+' && cat > '+path],input_text=text)


def write_template(path,text,metadata):
 temporary=path.parent/('.stats-route-'+uuid.uuid4().hex+'.tmp')
 try:
  descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,metadata['mode'])
  with os.fdopen(descriptor,'w') as stream:
   stream.write(text);stream.flush();os.fsync(stream.fileno())
  os.chown(temporary,metadata['uid'],metadata['gid']);os.chmod(temporary,metadata['mode']);os.replace(temporary,path)
 finally:
  if temporary.exists():temporary.unlink()


def probe(api,label,url,extra=()):
 try:
  text=api.run(['docker','exec','backend','curl','-q','--noproxy','*','-sS','--connect-timeout','2','--max-time','6',
   '--max-filesize','1048576','--write-out','\n%{http_code}',*extra,url],timeout=10)
  body,separator,status=text.rpartition('\n')
  api.require(bool(separator) and bool(re.fullmatch(r'[0-9]{3}',status)),'Invalid HTTP probe status')
  try:
   obj=json.loads(body);json_object=isinstance(obj,dict);keys=set(obj) if json_object else set()
  except ValueError:json_object=False;keys=set()
  return {'name':label,'status':int(status),'json_object':json_object,'_keys':keys}
 except api.ConfigError:return {'name':label,'status':None,'json_object':False,'_keys':set()}


def probes(api):
 return [probe(api,'stats_direct','http://stats:8050/api/v1/pages/main'),
  probe(api,'proxy_http','http://proxy:80/statsapi/api/v1/pages/main',('-H','Host: '+HOST)),
  probe(api,'proxy_stats_port','http://proxy:8080/api/v1/pages/main'),
  probe(api,'proxy_https_verified','https://'+HOST+'/statsapi/api/v1/pages/main',('--connect-to',HOST+':443:proxy:443')),
  probe(api,'stats_direct_public_host','http://stats:8050/api/v1/pages/main',('-H','Host: '+HOST)),
  probe(api,'proxy_backend_api','http://proxy:80/api/v2/stats',('-H','Host: '+HOST))]


def public_probes(rows):return [{key:value for key,value in row.items() if not key.startswith('_')} for row in rows]


def certificate(api,text,mount):
 try:
  entries=[item for node in servers(parse(api,text)) if listens(node,443) for item in directives(node,'ssl_certificate')]
  names=sorted({values(entry)[1] for entry in entries});results=[]
  for name in names:
   relative=Path(name).relative_to('/etc/nginx/templates')
   if '..' in relative.parts:return {'status':'unavailable'}
   path=Path(mount['Source'])/relative
   decoded=ssl._ssl._test_decode_cert(str(path));end=ssl.cert_time_to_seconds(decoded['notAfter'])
   results.append({'not_after_utc':datetime.fromtimestamp(end,timezone.utc).isoformat(),'expired':end<time.time()})
  if not results:return {'status':'unavailable'}
  if len(results)==1:return {'status':'read',**results[0]}
  return {'status':'read','certificate_count':len(results),'expired_count':sum(item['expired'] for item in results),'certificates':results}
 except (OSError,ValueError,KeyError,ssl.SSLError):return {'status':'unavailable'}


def loaded_sections(api,dump):
 headers=list(re.finditer(r'(?m)^# configuration file (/.+):[ \t]*$',dump))
 api.require(bool(headers),'Loaded Nginx file inventory is unavailable')
 result={}
 for index,header in enumerate(headers):
  path=header[1];api.require(path not in result,'Repeated loaded configuration file record')
  end=headers[index+1].start() if index+1<len(headers) else len(dump)
  result[path]=dump[header.end():end]
 return result


def file_entries(state):
 if 'files' in state:return state['files']
 return [{'name':'default.conf','runtime_path':RUNTIME,**{key:state[key] for key in (
  'template_path','template_before','runtime_before','template_after','runtime_after','template_metadata')}}]


def files_match(api,files,side):
 return all(not Path(item['template_path']).is_symlink()
  and Path(item['template_path']).read_text()==item['template_'+side]
  and read_runtime(api,item['runtime_path'])==item['runtime_'+side] for item in files)


def prepare(api,root,manifest):
 before=api.inspect_project(manifest);proxy=before['proxy']
 mount=[row for row in proxy.get('Mounts') or [] if row.get('Destination')=='/etc/nginx/templates']
 api.require(len(mount)==1 and mount[0].get('Type')=='bind' and mount[0].get('RW') is True,
  'Expected the writable proxy template directory bind mount')
 mount=mount[0]
 profile=api.read_json(root/'docker-compose/environments/dev/compose.json')
 expected=[row['source'] for row in profile['services']['proxy']['volumes'] if row['target']=='/etc/nginx/templates']
 api.require(expected==[str(Path(mount['Source']))],
  'Proxy template path differs from the dev profile')
 api.require(proxy.get('Config',{}).get('Cmd')==['nginx','-g','daemon off;'],'Custom Nginx master arguments require review')
 for row in proxy.get('Mounts') or []:
  destination=Path(row['Destination'])
  api.require(all(destination!=Path(path) and destination not in Path(path).parents for path in RUNTIMES.values()),
   'Rendered config has an unexpected host mount')
 dumped=api.run(['docker','exec','proxy','nginx','-T'])
 sections=loaded_sections(api,dumped)
 api.require(RUNTIME in sections,'default.conf is not in the loaded Nginx configuration')
 frontend=api.env_map(proxy).get('FRONT_PROXY_PASS')
 api.require(isinstance(frontend,str) and frontend!='','Proxy frontend destination is unavailable')
 for name,text in sections.items():
  if name not in RUNTIMES.values():
   api.require(not frontend_servers(api,text,frontend),'HTTP frontend exists in an unsupported loaded file; no partial repair was applied')
 files=[];public_files=[]
 for name,runtime_path in RUNTIMES.items():
  if runtime_path not in sections:continue
  runtime=read_runtime(api,runtime_path);path=Path(mount['Source'])/(name+'.template')
  api.require(not path.is_symlink() and path.is_file(),'Loaded frontend configuration requires its corresponding mounted template')
  template=path.read_text()
  template_count=len(frontend_servers(api,template,frontend));runtime_count=len(frontend_servers(api,runtime,frontend))
  api.require(template_count==runtime_count,'Template and rendered HTTP frontend server counts differ; no partial repair was applied')
  if not runtime_count:continue
  after_template,template_action=patch(api,template,frontend);after_runtime,runtime_action=patch(api,runtime,frontend)
  info=path.stat()
  files.append({'name':name,'runtime_path':runtime_path,'template_path':str(path),
   'template_before':template,'runtime_before':runtime,'template_after':after_template,'runtime_after':after_runtime,
   'template_metadata':{'mode':stat.S_IMODE(info.st_mode),'uid':info.st_uid,'gid':info.st_gid}})
  public_files.append({'configuration':name,'http_frontend_servers':runtime_count,'template_change':template_action,
   'runtime_change':runtime_action,'live_routes':route_facts(api,runtime)})
 api.require(bool(files),'No matching HTTP frontend servers in supported loaded configurations')
 results=probes(api);api.require(results[0]['status']==200 and results[0]['json_object'],'Direct stats main endpoint must return JSON HTTP 200')
 api.require(results[-2]['status']==200 and results[-2]['json_object'],'Direct stats endpoint rejects the forwarded public Host; upstream header review is required')
 api.require(results[-1]['status']==200 and results[-1]['json_object'],'Existing HTTP backend API route must work before proxy repair')
 api.ensure_stable(before,api.inspect_project(manifest),'Container/configuration changed during proxy preflight')
 api.require(files_match(api,files,'before'),'Proxy files changed during preflight')
 api.require(api.run(['docker','exec','proxy','nginx','-T'])==dumped,'Loaded Nginx configuration changed during preflight')
 return {'before':before,'files':files,'loaded_config_before':dumped,
  'public':{'status':'proxy_stats_plan_ready','environment':'dev','configurations':public_files,
   'http_frontend_servers':sum(item['http_frontend_servers'] for item in public_files),
   'probes':public_probes(results),'origin_certificate':certificate(api,read_runtime(api),mount),
   'effect':'Apply backs up and updates matching HTTP stats routes in the loaded default/explorer templates and rendered files, tests Nginx and gracefully reloads it. '
    'No container recreation, backend/DB change or TLS certificate replacement.'}}


def restore_files(api,state):
 for item in file_entries(state):
  write_template(Path(item['template_path']),item['template_before'],item['template_metadata'])
  write_runtime(api,item['runtime_before'],item['runtime_path'])
 api.run(['docker','exec','proxy','nginx','-t'])


def apply(api,builder,releases,root,manifest,do_apply=False):
 if do_apply:
  with builder.build_lock(api,api.MANAGED_RELEASES,root):return _apply(api,releases,root,manifest,True)
 return _apply(api,releases,root,manifest,False)


def _apply(api,releases,root,manifest,do_apply):
 prepared=prepare(api,root,manifest)
 if not do_apply:return prepared['public']
 directory=api.MANAGED_RELEASES/('proxy-stats-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')+uuid.uuid4().hex[:8])
 directory.mkdir(mode=0o700);path=directory/'state.json'
 state={key:value for key,value in prepared.items() if key!='public'}
 state.update(format=FORMAT,status='prepared',proxy_id=prepared['before']['proxy']['Id'])
 releases.write_new(path,releases.encoded(state))
 print(json.dumps({'phase':'proxy_stats_backup_saved','state':str(path),
  'restore_command':'./deploy.sh dev proxy-stats --restore '+str(path),
  'note':'Keep this private backup on the server; it may contain private configuration.'}),flush=True)
 api.ensure_stable(prepared['before'],api.inspect_project(manifest),'Deployment changed before proxy apply')
 api.require(files_match(api,state['files'],'before'),'Proxy files changed before apply')
 api.require(api.run(['docker','exec','proxy','nginx','-T'])==state['loaded_config_before'],'Loaded Nginx configuration changed before apply')
 reloaded=False
 try:
  for item in state['files']:
   write_template(Path(item['template_path']),item['template_after'],item['template_metadata'])
   write_runtime(api,item['runtime_after'],item['runtime_path'])
  api.run(['docker','exec','proxy','nginx','-t'])
  state['status']='reload_requested';releases.update_state(path,state)
  reloaded=True;api.run(['docker','exec','proxy','nginx','-s','reload'])
  for _ in range(8):
   direct=probe(api,'stats_direct','http://stats:8050/api/v1/pages/main')
   routed=probe(api,'proxy_http','http://proxy:80/statsapi/api/v1/pages/main',('-H','Host: '+HOST))
   if direct['status']==routed['status']==200 and direct['json_object'] and routed['json_object'] and direct['_keys']==routed['_keys']:break
   if _%2==0:print(json.dumps({'phase':'waiting_for_http_stats_route','attempt':_+1}),flush=True)
   time.sleep(.5)
  else:raise api.ConfigError('HTTP stats route did not return the direct-service JSON schema after reload')
  main=probe(api,'proxy_backend_api','http://proxy:80/api/v2/stats',('-H','Host: '+HOST))
  api.require(main['status']==200 and main['json_object'],'Main backend API route failed after reload')
 except BaseException:
  try:
   restore_files(api,state)
   if reloaded:api.run(['docker','exec','proxy','nginx','-s','reload'])
   state['status']='restored_after_failed_apply';releases.update_state(path,state)
  except BaseException:
   state['status']='needs_attention';releases.update_state(path,state)
  raise
 results=probes(api)
 api.ensure_stable(prepared['before'],api.inspect_project(manifest),'Container changed during proxy apply; inspect status before further action')
 required={'stats_direct','stats_direct_public_host','proxy_http','proxy_backend_api'}
 ok=all(row['status']==200 and row['json_object'] for row in results if row['name'] in required)
 state['status']='applied' if ok else 'needs_attention';releases.update_state(path,state)
 return {'status':'proxy_stats_verified' if ok else 'proxy_stats_needs_attention','state':str(path),'probes':public_probes(results),
  'containers_unchanged':True,'nginx_gracefully_reloaded':True,'origin_certificate':prepared['public']['origin_certificate'],
  'note':'HTTP stats route verified against direct-service JSON shape. Origin certificate was not renewed; public CDN URL still needs a check.'}


def restore(api,builder,releases,root,manifest,path):
 with builder.build_lock(api,api.MANAGED_RELEASES,root):
  api.require(path.is_absolute() and path.name=='state.json' and path.parent.parent.resolve()==api.MANAGED_RELEASES.resolve()
   and bool(re.fullmatch(r'proxy-stats-[0-9TZ-]+-[a-f0-9]{8}',path.parent.name))
   and not path.is_symlink() and not path.parent.is_symlink() and path.is_file(), 'Unexpected proxy backup path')
  api.require(stat.S_IMODE(path.stat().st_mode)==0o600 and stat.S_IMODE(path.parent.stat().st_mode)==0o700,'Proxy backup permissions differ')
  state=api.read_json(path);api.require(state.get('format') in (FORMAT,LEGACY_FORMAT),'Unrecognized proxy backup')
  before=api.inspect_project(manifest);api.require(before['proxy']['Id']==state.get('proxy_id'),'Proxy container changed; do not restore an older configuration automatically')
  mounts=[row for row in before['proxy'].get('Mounts') or [] if row.get('Destination')=='/etc/nginx/templates' and row.get('Type')=='bind']
  api.require(len(mounts)==1,'Proxy template mount changed')
  files=file_entries(state)
  api.require(bool(files) and len(files)<=len(RUNTIMES) and len({item['name'] for item in files})==len(files),'Unexpected proxy backup file inventory')
  for item in files:
   api.require(item['name'] in RUNTIMES and item['runtime_path']==RUNTIMES[item['name']]
    and Path(item['template_path'])==Path(mounts[0]['Source'])/(item['name']+'.template'),'Unexpected proxy backup target')
   template=Path(item['template_path'])
   api.require(not template.is_symlink() and template.read_text() in (item['template_after'],item['template_before'])
    and read_runtime(api,item['runtime_path']) in (item['runtime_after'],item['runtime_before']),
    'Proxy files changed after repair; restore will not overwrite later edits')
  restore_files(api,state);api.run(['docker','exec','proxy','nginx','-s','reload'])
  api.ensure_stable(before,api.inspect_project(manifest),'Container changed during proxy restore')
  state['status']='restored';releases.update_state(path,state)
  return {'status':'proxy_stats_restored','state':str(path),'containers_unchanged':True,'nginx_gracefully_reloaded':True}
