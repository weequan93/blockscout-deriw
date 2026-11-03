#!/usr/bin/env python3
"""Build the committed dev candidate and check release config without activation."""
from contextlib import contextmanager
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

FORMAT='blockscout-dev-candidate-v1'
LABEL='org.deriw.build-fingerprint'
FLAG='INDEXER_UNCATALOGED_TOKEN_TRANSFERS_ENABLED'
MARKER='BLOCKSCOUT_DEV_CANDIDATE_CONFIG_OK'


def market_policy():
 spec=importlib.util.spec_from_file_location('dev_market_policy',Path(__file__).with_name('market_policy.py'))
 module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def clean_source(api,root):
 actual=Path(api.run(['git','-C',str(root),'rev-parse','--show-toplevel']).strip()).resolve()
 api.require(actual==root.resolve(),'Build must run from the repository root selected by deploy.sh')
 api.require(not api.run(['git','-C',str(root),'status','--porcelain','--untracked-files=all']).strip(),'Source checkout has local changes; no build started')
 commit=api.run(['git','-C',str(root),'rev-parse','HEAD']).strip()
 api.require(bool(re.fullmatch(r'[a-f0-9]{40}',commit)),'Unexpected source commit identity')
 mix=api.run(['git','-C',str(root),'show',commit+':mix.exs'])
 version=re.search(r'\bversion:\s*"([0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?)"',mix)
 api.require(version is not None,'Cannot establish the committed release version')
 return commit,version[1]


def image_info(api,reference,missing=False):
 if missing:
  ids=api.run(['docker','image','ls','--no-trunc','--quiet','--filter','reference='+reference]).split()
  api.require(len(ids)<=1,'Candidate image reference is ambiguous')
  if not ids:return None
 try:rows=json.loads(api.run(['docker','image','inspect',reference]))
 except ValueError:raise api.ConfigError('Invalid image metadata; raw output suppressed') from None
 api.require(isinstance(rows,list) and len(rows)==1 and bool(re.fullmatch(r'sha256:[a-f0-9]{64}',rows[0].get('Id',''))),'Unexpected image identity')
 return rows[0]


def source_plan(api,root,manifest,model,baseline):
 commit,version=clean_source(api,root)
 public=model['services']['backend'].get('build',{}).get('args') or {}
 api.require(public.get('CHAIN_TYPE')=='arbitrum' and public.get('RELEASE_VERSION')==version,'Build chain type or release version differs from source/profile')
 info=image_info(api,baseline['backend']['Image'])
 arch=info.get('Architecture')
 api.require(info.get('Os')=='linux' and arch in ('amd64','arm64'),'Existing application platform requires explicit build review')
 arguments={'CHAIN_TYPE':'arbitrum','RELEASE_VERSION':version,'BLOCKSCOUT_VERSION':commit}
 for key in ('DISABLE_API','API_V1_READ_METHODS_DISABLED','API_V1_WRITE_METHODS_DISABLED','BRIDGED_TOKENS_ENABLED'):
  if key in public:
   api.require(public[key] in ('','true','false'),'Unexpected public boolean build option')
   arguments[key]=public[key]
 if 'API_GRAPHQL_MAX_COMPLEXITY' in public:
  value=public['API_GRAPHQL_MAX_COMPLEXITY']
  api.require(isinstance(value,str) and bool(re.fullmatch(r'[0-9]+',value)),'Unexpected public numeric build option')
  arguments['API_GRAPHQL_MAX_COMPLEXITY']=value
 identity={'format':FORMAT,'source_commit':commit,'release_version':version,'platform':'linux/'+arch,'build_arguments':arguments}
 fingerprint=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
 return {**identity,'fingerprint':fingerprint,'tag':'deriw-blockscout:dev-'+commit[:12]+'-arbitrum-'+arch,
  'baseline_runtime_user':info.get('Config',{}).get('User','')}


def validate_state_directory(api,directory,root):
 api.require(not directory.is_symlink(),'Build state directory cannot be a symlink')
 resolved=directory.resolve()
 api.require(resolved!=root.resolve() and root.resolve() not in resolved.parents,'Build state must be outside the checkout')
 api.require(not any((p/'.git').exists() for p in [resolved,*resolved.parents]),'Build state must be outside Git checkouts')
 if directory.exists():api.require(directory.is_dir() and directory.stat().st_mode&0o077==0,'Build state directory must have permissions 0700')


def private_directory(api,directory,root):
 validate_state_directory(api,directory,root)
 directory.mkdir(parents=True,mode=0o700,exist_ok=True)
 api.require(directory.is_dir() and directory.stat().st_mode&0o077==0,'Build state directory must have permissions 0700')
 return Path(tempfile.mkdtemp(prefix='candidate-',dir=directory))


@contextmanager
def build_lock(api,directory,root):
 validate_state_directory(api,directory,root)
 directory.mkdir(parents=True,mode=0o700,exist_ok=True)
 validate_state_directory(api,directory,root)
 descriptor=os.open(directory/'build.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
 with os.fdopen(descriptor,'rb') as lock:
  api.require(os.fstat(lock.fileno()).st_mode&0o077==0,'Build lock must have permissions 0600')
  try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:raise api.ConfigError('Another dev candidate build holds this state directory lock') from None
  try:yield
  finally:fcntl.flock(lock,fcntl.LOCK_UN)


def private_log(path):
 return os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb')


def terminate(process):
 if process is not None and process.poll() is None:
  process.terminate()
  try:process.wait(timeout=10)
  except subprocess.TimeoutExpired:
   process.kill();process.wait(timeout=10)


def compile_image(api,root,source,log_path):
 archive=None;build=None
 command=['docker','build','--platform',source['platform'],'-f','docker/Dockerfile','-t',source['tag'],
  '--label','org.opencontainers.image.revision='+source['source_commit'],'--label',LABEL+'='+source['fingerprint']]
 for key,value in sorted(source['build_arguments'].items()):command+=['--build-arg',key+'='+value]
 command+=['-']
 with private_log(log_path) as log:
  try:
   archive=subprocess.Popen(['git','-C',str(root),'archive','--format=tar',source['source_commit']],stdout=subprocess.PIPE,stderr=log)
   build=subprocess.Popen(command,stdin=archive.stdout,stdout=log,stderr=log,env=api.compose_environment({}))
   archive.stdout.close()
   started=time.monotonic();next_update=started+20
   while build.poll() is None:
    elapsed=time.monotonic()-started
    api.require(elapsed<3600,'Candidate build exceeded its one-hour limit; inspect the private build log')
    if time.monotonic()>=next_update:
     print(json.dumps({'phase':'building_candidate','elapsed_seconds':int(elapsed)}),flush=True);next_update=time.monotonic()+20
    time.sleep(1)
   api.require(archive.wait(timeout=10)==0 and build.returncode==0,'Candidate build failed; inspect the server-only build log')
  finally:
   terminate(build);terminate(archive)


def smoke_expression(mode):
 expected='false' if mode=='true' else 'true'
 return ('true = Application.get_env(:explorer, Explorer.Chain.Cache.Counters.TransactionsCount)[:approximate?]; '
  '"18417507517" = Application.get_env(:block_scout_web, :chain_id); '
  '60000 = Application.get_env(:indexer, :contract_code_retry_delay); '
  '1 = Application.get_env(:indexer, :contract_code_concurrency); '
  '"176114083..latest" = Application.get_env(:indexer, :trace_block_ranges); '
  '{:module, _} = Code.ensure_loaded(Indexer.Block.Fetcher.ReceiptFallback); '
  'sup = Indexer.Temporary.UncatalogedTokenTransfers.Supervisor; '
  '{:module, _} = Code.ensure_loaded(sup); '+expected+' = sup.disabled?(); '
  +('' if mode=='true' else ':ignore = sup.start_link([]); ')
  +'nil = Explorer.Chain.fetch_transaction_revert_reason(%{block_number: 1335744}); '
  +market_policy().smoke_expression()+'IO.puts("'+MARKER+'")')


def smoke_script(mode):
 # jq @sh quotes values rather than executing their contents. Runtime config must
 # see this environment before release eval loads Config.Provider.
 return ("set -eu\numask 077\ncat > /tmp/candidate-env.json\n"
  "jq -e 'type == \"object\" and all(keys[]; test(\"^[A-Z][A-Z0-9_]*$\")) and all(.[]; type == \"string\" and (contains(\"\\u0000\") | not))' /tmp/candidate-env.json > /dev/null\n"
  "eval \"$(jq -r 'to_entries[] | \"export \\(.key)=\\(.value|@sh)\"' /tmp/candidate-env.json)\"\n"
  'rm /tmp/candidate-env.json\n'
  'test -s "/app/releases/$RELEASE_VERSION/config_helper.exs"\n'
  'test -s "/app/releases/$RELEASE_VERSION/runtime.exs"\n'
  "exec /app/bin/blockscout eval '"+smoke_expression(mode)+"'\n")


def smoke_image(api,image,source,backend_environment,directory):
 results=[]
 for mode in ('unset','false','true'):
  environment={k:v for k,v in backend_environment.items() if v is not None}
  api.require(all(api.ENV_KEY.fullmatch(k) and isinstance(v,str) and '\x00' not in v for k,v in environment.items()),'Unsupported runtime environment format')
  environment.update(RELEASE_VERSION=source['release_version'],RELEASE_DISTRIBUTION='none',RELEASE_TMP='/tmp')
  environment.pop(FLAG,None)
  if mode!='unset':environment[FLAG]=mode
  name='blockscout-dev-smoke-'+uuid.uuid4().hex[:12]
  command=['docker','run','--rm','--name',name,'--network','none','--read-only',
   '--tmpfs','/tmp:rw,mode=1777','--cpus','2','--memory','512m','--entrypoint','sh','-i',image,'-c',smoke_script(mode)]
  try:
   result=subprocess.run(command,input=json.dumps(environment),capture_output=True,text=True,errors='replace',timeout=120)
   with private_log(directory/('smoke-'+mode+'.log.private')) as log:
    log.write(result.stdout.encode());log.write(result.stderr.encode())
   api.require(result.returncode==0 and MARKER in result.stdout.splitlines(),'Candidate release configuration check failed for '+mode+'; inspect its server-only log')
  except subprocess.TimeoutExpired:
   raise api.ConfigError('Candidate release configuration check timed out; temporary container cleanup requested') from None
  finally:
   subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=20)
  results.append({'mode':mode,'passed':True})
  print(json.dumps({'phase':'candidate_config_checked','mode':mode}),flush=True)
 return results


def approved_preflight(api,report):
 intended='services.backend.environment.'+FLAG
 api.require(set(report['changed_runtime_fields'])<=({intended}|market_policy().FIELDS) and not report['live_declared_environment_drift']
  and not report['live_images_differing_from_capture'],'Dev profile has unreviewed runtime/image differences; build preflight stopped')


def build_candidate(api,root,manifest,requirements,values,private_path,live_directory,state_directory,check_only=False,reuse=False):
 arguments=(api,root,manifest,requirements,values,private_path,live_directory,state_directory)
 if check_only:return _build_candidate(*arguments,check_only=True,reuse=reuse)
 with build_lock(api,state_directory,root):return _build_candidate(*arguments,reuse=reuse)


def _build_candidate(api,root,manifest,requirements,values,private_path,live_directory,state_directory,check_only=False,reuse=False):
 api.require(root.resolve() not in private_path.resolve().parents,'Private input must be outside the build checkout')
 api.require(not any((p/'.git').exists() for p in private_path.resolve().parents),'Private input must be outside Git checkouts')
 validate_state_directory(api,state_directory,root)
 report=api.plan(root,manifest,requirements,values,live_directory);approved_preflight(api,report)
 before=api.inspect_project(manifest);model=api.managed_model(root,manifest,values)
 market=market_policy().provider_check(api,model['services']['backend'].get('environment') or {})
 api.require(market['status']!='unavailable','ETH price provider is unavailable from backend; no build or restart requested')
 source=source_plan(api,root,manifest,model,before)
 existing=image_info(api,source['tag'],missing=True)
 summary={'environment':'dev','source_commit':source['source_commit'],'release_version':source['release_version'],
  'platform':source['platform'],'candidate_tag':source['tag'],'source_context':'git archive of the committed revision',
  'activation_performed':False,'native_coin_market':market}
 if check_only:return {'status':'candidate_build_plan_ready',**summary,'candidate_already_exists':existing is not None,
  'effect':'Build mode creates a candidate image and isolated release-config checks. No live service recreation, application startup or migrations.'}
 api.require((existing is not None)==reuse,'Candidate already exists; use --reuse, or omit --reuse when no candidate exists')
 if existing:
  labels=existing.get('Config',{}).get('Labels') or {}
  api.require(labels.get(LABEL)==source['fingerprint'] and labels.get('org.opencontainers.image.revision')==source['source_commit'],'Existing candidate does not match the committed build identity')
 directory=private_directory(api,state_directory,root)
 print(json.dumps({'phase':'candidate_prepared',**summary,'private_run_directory':str(directory),
  'note':'Build and smoke logs are server-only and may contain private values. The build consumes host resources.'}),flush=True)
 if not reuse:compile_image(api,root,source,directory/'build.log.private')
 candidate=image_info(api,source['tag'])
 labels=candidate.get('Config',{}).get('Labels') or {}
 api.require(labels.get(LABEL)==source['fingerprint'] and labels.get('org.opencontainers.image.revision')==source['source_commit'],'Built candidate image identity/labels differ')
 api.require(candidate.get('Os')=='linux' and 'linux/'+candidate.get('Architecture','')==source['platform'],'Built candidate platform differs')
 checks=smoke_image(api,candidate['Id'],source,model['services']['backend']['environment'],directory)
 api.require(clean_source(api,root)==(source['source_commit'],source['release_version']),'Source changed during the build; no success receipt accepted')
 api.require(api.load_private(private_path,requirements)==values,'Private input changed during the build; no success receipt accepted')
 api.ensure_stable(before,api.inspect_project(manifest),'Live deployment changed during build; inspect changed_containers before accepting candidate')
 after_report=api.plan(root,manifest,requirements,values,live_directory);approved_preflight(api,after_report)
 api.require(image_info(api,source['tag'])['Id']==candidate['Id'],'Candidate tag changed during validation')
 receipt={'format':FORMAT,**summary,'image_id':candidate['Id'],'build_fingerprint':source['fingerprint'],
  'build_arguments':source['build_arguments'],'smoke_checks':checks,'private_input_file':str(private_path.resolve()),
  'baseline_image_ids':{name:row['Image'] for name,row in before.items()},
  'runtime_user_matches_baseline':candidate.get('Config',{}).get('User','')==source['baseline_runtime_user'],
  'scope':'Release configuration load and selected repair behavior only; no full application startup, DB migration, host mount permission or NFT connectivity check.'}
 path=directory/'candidate.json'
 with private_log(path) as stream:stream.write((json.dumps(receipt,indent=2,sort_keys=True)+'\n').encode())
 return {'status':'candidate_verified',**summary,'image_id':candidate['Id'],'receipt':str(path),
  'runtime_user_matches_baseline':receipt['runtime_user_matches_baseline'],'scope':receipt['scope']}
