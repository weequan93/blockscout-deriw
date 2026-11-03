#!/usr/bin/env python3
"""Collect pre-activation evidence for the built dev candidate. Never activate."""
import hashlib
import json
from pathlib import Path
import re
import shlex
import stat
import subprocess
import uuid

MARKER='BLOCKSCOUT_DEPLOY_CHECK='
IMAGE_SCRIPT='''set -eu
printf 'IDENTITY %s %s %s\n' "$(id -u)" "$(id -g)" "$(id -G | tr ' ' ',')"
find /app/lib/explorer-*/priv -type f -path '*/migrations/*.exs' -exec sha256sum {} +
'''


def private_receipt(api,path,root):
 api.require(not path.is_symlink() and path.is_file(),'Candidate receipt must be a regular server-only file')
 api.require(stat.S_IMODE(path.stat().st_mode)&0o077==0,'Candidate receipt permissions must exclude group/other access')
 resolved=path.resolve()
 api.require(root.resolve() not in resolved.parents and not any((p/'.git').exists() for p in resolved.parents),
  'Candidate receipt must be outside Git checkouts')
 return api.read_json(path)


def candidate(api,builder,root,manifest,requirements,values,private_path,receipt_path):
 receipt=private_receipt(api,receipt_path,root)
 api.require(receipt.get('format')==builder.FORMAT and receipt.get('environment')=='dev'
  and receipt.get('activation_performed') is False,'Unrecognized candidate receipt')
 api.require(receipt.get('smoke_checks')==[{'mode':mode,'passed':True} for mode in ('unset','false','true')],
  'Candidate receipt is missing successful release checks')
 api.require(receipt.get('baseline_image_ids')==manifest['baseline_image_ids'],'Candidate baseline differs from captured dev images')
 api.require(receipt.get('private_input_file')==str(private_path.resolve()),'Use the original server-only private input file for this candidate')
 commit,version=builder.clean_source(api,root)
 original=receipt.get('source_commit','')
 api.require(bool(re.fullmatch(r'[a-f0-9]{40}',original)) and receipt.get('release_version')==version,'Candidate source/version is invalid')
 api.run(['git','-C',str(root),'merge-base','--is-ancestor',original,commit])
 changed=api.run(['git','-C',str(root),'diff','--name-only',original,commit]).splitlines()
 api.require(all(name.startswith('deploy/') or name in ('deploy.sh','docs/server-deployment-design.md',
  'docker-compose/environments/README.md') for name in changed),
  'Application, recipe or environment source changed since this candidate; build a new candidate')
 identity={key:receipt.get(key) for key in ('format','source_commit','release_version','platform','build_arguments')}
 fingerprint=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
 api.require(fingerprint==receipt.get('build_fingerprint'),'Candidate receipt build identity differs')
 image=builder.image_info(api,receipt.get('candidate_tag',''))
 labels=image.get('Config',{}).get('Labels') or {}
 api.require(image['Id']==receipt.get('image_id') and labels.get(builder.LABEL)==fingerprint
  and labels.get('org.opencontainers.image.revision')==original,'Candidate tag/image/labels changed after verification')
 api.require(image.get('Os')=='linux' and 'linux/'+image.get('Architecture','')==receipt.get('platform'),
  'Candidate platform differs from its receipt')
 return receipt,image


def parse_identity(api,line):
 match=re.fullmatch(r'IDENTITY ([0-9]+) ([0-9]+) ([0-9,]+)',line)
 api.require(match is not None,'Unexpected image user metadata')
 return (int(match[1]),int(match[2]),tuple(sorted(map(int,match[3].split(',')))))


def parse_inventory(api,text):
 files={};identity=None
 for line in text.splitlines():
  if line.startswith('IDENTITY '):
   api.require(identity is None,'Duplicate image user metadata')
   identity=parse_identity(api,line)
  else:
   match=re.fullmatch(r'([a-f0-9]{64})  /app/lib/explorer-[^/]+/priv/([A-Za-z0-9_/.-]+/migrations/[A-Za-z0-9_.-]+\.exs)',line)
   api.require(match is not None,'Unexpected image migration inventory; raw output suppressed')
   name=match[2];api.require(name not in files and '..' not in Path(name).parts,'Duplicate/invalid migration path')
   files[name]=match[1]
 api.require(identity is not None and len(files)>0,'Image migration inventory is empty')
 return {'identity':identity,'migrations':files}


def image_inventory(api,image):
 name='blockscout-dev-inspect-'+uuid.uuid4().hex[:12]
 try:
  output=api.run(['docker','run','--rm','--pull','never','--name',name,'--network','none','--read-only',
   '--cap-drop','ALL','--security-opt','no-new-privileges','--cpus','1','--memory','128m',
   '--entrypoint','sh',image,'-c',IMAGE_SCRIPT],timeout=60)
  return parse_inventory(api,output)
 finally:
  subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=20)


def migration_diff(before,after):
 a=before['migrations'];b=after['migrations']
 return {'baseline_files':len(a),'candidate_files':len(b),'added':len(b.keys()-a.keys()),
  'removed':len(a.keys()-b.keys()),'changed':sum(a[key]!=b[key] for key in a.keys()&b.keys()),
  'identical':a==b}


def inherited_image_changes(api,baseline,image,service,live):
 a=baseline.get('Config') or {};b=image.get('Config') or {}
 # Explicit service fields override corresponding image defaults.
 overrides={'Cmd':'command','Entrypoint':'entrypoint','WorkingDir':'working_dir',
  'User':'user','Healthcheck':'healthcheck','StopSignal':'stop_signal'}
 fields=[key for key in ('Cmd','Entrypoint','WorkingDir','User','Healthcheck','StopSignal','Volumes','ExposedPorts')
  if a.get(key)!=b.get(key) and (key not in overrides or service.get(overrides[key]) is None)]
 effective=api.env_map(image);effective.update({k:v for k,v in service.get('environment',{}).items() if v is not None})
 old=api.env_map(live)
 changed=sorted(k for k in old.keys()|effective.keys() if old.get(k)!=effective.get(k))
 expected={'BLOCKSCOUT_VERSION','INDEXER_UNCATALOGED_TOKEN_TRANSFERS_ENABLED'}
 return {'fields':fields,'environment_keys':changed,'unexpected_environment_keys':sorted(set(changed)-expected)}


def marked_json(api,text):
 lines=[line[len(MARKER):] for line in text.splitlines() if line.startswith(MARKER)]
 api.require(len(lines)==1,'Runtime evidence unavailable; raw values suppressed')
 try:return json.loads(lines[0])
 except ValueError:raise api.ConfigError('Runtime evidence was invalid; raw values suppressed') from None


def migration_expression():
 # Ecto.Migrator.migrations/2 can create schema_migrations. Read it directly in
 # existing Repo connections with READ ONLY instead; never start a Repo here.
 return '''
main = Explorer.Repo;
route_keys = [:url, :hostname, :port, :database, :username, :password, :ssl, :socket_dir, :parameters];
main_route = Map.take(Map.new(main.config()), route_keys);
rows = Enum.map(Explorer.ReleaseTasks.repos(), fn repo ->
  try do
    config = repo.config();
    true = Keyword.get(config, :migration_source, "schema_migrations") == "schema_migrations";
    true = Keyword.get(config, :migration_default_prefix, "public") in [nil, "public"];
    path = Path.join([to_string(:code.priv_dir(Keyword.fetch!(config, :otp_app))),
      repo |> Module.split() |> List.last() |> Macro.underscore(), "migrations"]);
    files = Path.wildcard(Path.join(path, "*.exs"));
    true = files != [];
    versions = Enum.map(files, fn file -> file |> Path.basename() |> String.split("_", parts: 2) |> hd() |> String.to_integer() end);
    actual_route = Map.take(Map.new(config), route_keys);
    reader = if actual_route == main_route, do: main, else: repo;
    true = is_pid(Process.whereis(reader));
    {:ok, applied} = reader.transaction(fn ->
      Ecto.Adapters.SQL.query!(reader, "SET TRANSACTION READ ONLY", [], timeout: 2000, log: false);
      Ecto.Adapters.SQL.query!(reader, "SET LOCAL statement_timeout = '1500ms'", [], timeout: 2000, log: false);
      Ecto.Adapters.SQL.query!(reader, "SET LOCAL lock_timeout = '500ms'", [], timeout: 2000, log: false);
      Ecto.Adapters.SQL.query!(reader, "SELECT version FROM public.schema_migrations", [], timeout: 2000, log: false).rows |> List.flatten()
    end, timeout: 5000);
    %{repository: inspect(repo), status: "ok", migration_files: length(versions),
      pending_count: length(versions -- applied)}
  rescue
    _ -> %{repository: inspect(repo), status: "unavailable"}
  catch
    _, _ -> %{repository: inspect(repo), status: "unavailable"}
  end
end);
IO.puts("BLOCKSCOUT_DEPLOY_CHECK=" <> Jason.encode!(rows))
'''


def pending_migrations(api):
 rows=marked_json(api,api.run(['docker','exec','backend','/app/bin/blockscout','rpc',migration_expression()],timeout=40))
 api.require(isinstance(rows,list) and 1<=len(rows)<=10,'Unexpected migration repository inventory')
 allowed=[]
 for row in rows:
  api.require(isinstance(row,dict) and bool(re.fullmatch(r'Explorer\.Repo(?:\.[A-Za-z]+)?',str(row.get('repository','')))),
   'Unexpected migration repository identity')
  api.require(row.get('status') in ('ok','unavailable'),'Unexpected migration evidence status')
  item={key:row[key] for key in ('repository','status')}
  if row['status']=='ok':
   api.require(all(type(row.get(key)) is int and row[key]>=0 for key in ('migration_files','pending_count')),'Unexpected migration count')
   item.update({key:row[key] for key in ('migration_files','pending_count')})
  allowed.append(item)
 api.require(len({row['repository'] for row in allowed})==len(allowed),'Duplicate migration repository evidence')
 return allowed


def mount_access(api,service,row,identity):
 mounts=service.get('volumes') or []
 api.require(len(mounts)>0 and all(m.get('type')=='bind' and m.get('target') in ('/app/logs','/app/dets') for m in mounts),
  'Backend mount policy requires separate review')
 expected={m['target']:(str(Path(m['source']).resolve()),not m.get('read_only',False)) for m in mounts}
 actual={m.get('Destination'):(m.get('Source'),m.get('RW')) for m in row.get('Mounts') or [] if m.get('Type')=='bind'}
 api.require(len(expected)==len(mounts) and len(actual)==len(row.get('Mounts') or []) and actual==expected,
  'Live backend mounts differ from the proposed profile')
 tests=[]
 for target,(_,writable) in expected.items():
  tests+=['test -d '+shlex.quote(target),'test -x '+shlex.quote(target),'test -r '+shlex.quote(target)]
  if writable:tests+=['test -w '+shlex.quote(target)]
 script=IMAGE_SCRIPT.split('find ',1)[0]+' && '.join(tests)+'\n'
 output=api.run(['docker','exec','backend','sh','-c',script],timeout=10)
 numeric=parse_identity(api,output.strip())
 return {'bindings_match':True,'directory_access_checks_passed':True,'numeric_user_and_groups_match':numeric==identity,
  'note':'Access tests only; no file was created. This does not prove application startup or ongoing write success.'}


def live_health(api,rows):
 port=api.env_map(rows['backend']).get('PORT','4000')
 api.require(bool(re.fullmatch(r'[0-9]{1,5}',port)) and 0<int(port)<65536,'Unexpected backend readiness port')
 try:
  ready=api.run(['docker','exec','backend','curl','-q','--noproxy','*','-fsS','--max-time','5','--max-filesize','4096',
   'http://127.0.0.1:'+port+'/api/health/readiness'],timeout=10).strip()=='OK'
 except api.ConfigError:ready=False
 expression='''
ok = node() == :"worker@nft-media.blockscout.internal" and
  Node.ping(:"producer@backend.blockscout.internal") == :pong and
  is_pid(:rpc.call(:"producer@backend.blockscout.internal", Process, :whereis, [Indexer.NFTMediaHandler.Queue], 5000));
IO.puts("BLOCKSCOUT_DEPLOY_CHECK=" <> Jason.encode!(%{connected: ok}))
'''
 try:
  nft=marked_json(api,api.run(['docker','exec','nft_media_handler','/app/bin/blockscout','rpc',expression],timeout=20))
  nft_ok=isinstance(nft,dict) and nft.get('connected') is True
 except api.ConfigError:nft_ok=False
 return {'backend_readiness_ok':ready,'nft_connection_and_queue_ok':nft_ok}


def check(api,builder,root,manifest,requirements,values,private_path,receipt_path,live_directory):
 source_state=builder.clean_source(api,root)
 builder.approved_preflight(api,api.plan(root,manifest,requirements,values,live_directory))
 before=api.inspect_project(manifest);model=api.managed_model(root,manifest,values)
 receipt,image=candidate(api,builder,root,manifest,requirements,values,private_path,receipt_path)
 baseline=builder.image_info(api,before['backend']['Image'])
 defaults=inherited_image_changes(api,baseline,image,model['services']['backend'],before['backend'])
 old=image_inventory(api,baseline['Id']);new=image_inventory(api,image['Id'])
 migrations=migration_diff(old,new);pending=pending_migrations(api)
 mounts=mount_access(api,model['services']['backend'],before['backend'],new['identity'])
 health=live_health(api,before)
 api.ensure_stable(before,api.inspect_project(manifest),'Live deployment changed during candidate pre-activation checks')
 api.require(api.managed_model(root,manifest,values)==model and api.load_private(private_path,requirements)==values,
  'Managed profile or private inputs changed during pre-activation checks')
 api.require(builder.image_info(api,receipt['candidate_tag'])['Id']==image['Id'],'Candidate tag changed during pre-activation checks')
 api.require(builder.clean_source(api,root)==source_state and private_receipt(api,receipt_path,root)==receipt,
  'Source or candidate receipt changed during pre-activation checks')
 blockers=[]
 if defaults['fields'] or defaults['unexpected_environment_keys']:blockers.append('inherited_image_defaults_require_review')
 if not migrations['identical']:blockers.append('migration_file_differences_require_review')
 if any(row['status']!='ok' or row['pending_count'] for row in pending):blockers.append('pending_or_unavailable_migration_status')
 if not mounts['numeric_user_and_groups_match']:blockers.append('candidate_numeric_user_or_groups_differ')
 if not all(health.values()):blockers.append('live_readiness_or_nft_check_failed')
 command=model['services']['backend'].get('command')
 migration_startup=isinstance(command,(list,str)) and 'Explorer.ReleaseTasks.create_and_migrate()' in str(command)
 command_matches=command==before['backend'].get('Config',{}).get('Cmd')
 if not migration_startup or not command_matches:blockers.append('startup_command_requires_review')
 return {'status':'preactivation_evidence_passed' if not blockers else 'preactivation_review_required',
  'environment':'dev','candidate_image':image['Id'],'candidate_source_commit':receipt['source_commit'],
  'activation_performed':False,'blockers':blockers,'inherited_image_changes':defaults,
  'migration_inventory':migrations,'current_database_migrations':pending,'mount_access':mounts,**health,
  'startup_includes_create_and_migrate':migration_startup,
  'startup_command_matches_live':command_matches,
  'note':'Evidence collection only. No deployment, database creation/migration, mount writes or service restart. '
    'Schema versions do not prove database backup/recovery readiness; activation/rollback remain separate.'}
