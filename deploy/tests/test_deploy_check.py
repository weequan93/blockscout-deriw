#!/usr/bin/env python3
"""Pre-activation evidence, using synthetic private inputs and mocked Docker/Repo boundaries."""
import contextlib
from copy import deepcopy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
def load(name,path):
 spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec)
 sys.modules[name]=module;spec.loader.exec_module(module);return module
C=load('deploy_check_fixture',ROOT/'deploy/scripts/deploy_check.py')
F=load('build_fixture',ROOT/'deploy/tests/test_candidate_build.py')
B=F.B;T=F.T;IMAGE=F.IMAGE;COMMIT=F.COMMIT;SECRET=F.SECRET


class EvidenceTests(unittest.TestCase):
 def setUp(self):
  self.f=F.BuildTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
  self.manifest=self.f.f.manifest;self.rows=self.f.f.rows;self.values=self.f.f.values
  self.requirements=self.f.f.requirements
  self.model={'services':{'backend':{'environment':self.f.environment,
   'command':['sh','-c','bin/blockscout eval "Elixir.Explorer.ReleaseTasks.create_and_migrate()" && bin/blockscout start']}}}
  self.rows['backend']['Config']['Cmd']=self.model['services']['backend']['command']
  self.inventory={'identity':(10001,10001,(10001,)),'migrations':{'repo/migrations/1_fixture.exs':'a'*64}}
  self.pending=[{'repository':'Explorer.Repo','migration_directory':'repo','status':'ok','migration_files':1,'pending_count':0,
   'pending_count_basis':'live_image','candidate_migration_files':1,'candidate_pending_count':0,'candidate_pending_versions':[]}]
  self.receipt={**self.f.source,'format':B.FORMAT,'environment':'dev','candidate_tag':self.f.source['tag'],
   'activation_performed':False,'image_id':IMAGE,'baseline_image_ids':self.manifest['baseline_image_ids'],
   'private_input_file':str(self.f.private.resolve()),
   'smoke_checks':[{'mode':m,'passed':True} for m in ('unset','false','true')]}
  self.receipt['build_fingerprint']=hashlib.sha256(json.dumps({key:self.receipt[key] for key in
   ('format','source_commit','release_version','platform','build_arguments')},sort_keys=True).encode()).hexdigest()
  self.image=deepcopy(self.f.image);self.image['Config']['Labels'][B.LABEL]=self.receipt['build_fingerprint']
  self.path=self.f.f.parent/'candidate.json';self.path.write_text(json.dumps(self.receipt));self.path.chmod(0o600)
 @contextlib.contextmanager
 def mocked(self):
  with contextlib.ExitStack() as stack:
   mocks={}
   targets=[(T,'plan',{'return_value':self.f.report}),(T,'inspect_project',{'return_value':self.rows}),
    (T,'managed_model',{'return_value':self.model}),(T,'load_private',{'return_value':self.values}),
    (B,'image_info',{'return_value':self.image}),(B,'clean_source',{'return_value':('c'*40,'9.0.1')}),
    (C,'candidate',{'return_value':(self.receipt,self.image)}),
    (C,'inherited_image_changes',{'return_value':{'fields':[],'environment_keys':[B.FLAG],'unexpected_environment_keys':[]}}),
    (C,'image_inventory',{'return_value':self.inventory}),(C,'pending_migrations',{'return_value':self.pending}),
    (C,'performance_indexes',{'return_value':[]}),
    (C,'mount_access',{'return_value':{'bindings_match':True,'directory_access_checks_passed':True,'numeric_user_and_groups_match':True}}),
    (C,'live_health',{'return_value':{'backend_readiness_ok':True,'nft_connection_and_queue_ok':True}})]
   for module,name,options in targets:mocks[name]=stack.enter_context(patch.object(module,name,**options))
   yield mocks
 def check(self):
  return C.check(T,B,ROOT,self.manifest,self.requirements,self.values,self.f.private,self.path,self.f.f.live_dir)
 def test_success_collects_evidence_without_activation_or_persistent_state(self):
  with self.mocked():report=self.check()
  self.assertEqual(report['status'],'preactivation_evidence_passed');self.assertFalse(report['activation_performed'])
  self.assertFalse(report['blockers']);self.assertNotIn(SECRET,json.dumps(report));self.assertFalse(self.f.state.exists())
 def test_baseline_drift_stops_before_candidate_work(self):
  self.f.report['live_images_differing_from_capture']=['backend']
  with self.mocked() as mocks:
   with self.assertRaises(T.ConfigError):self.check()
  mocks['candidate'].assert_not_called()
 def test_migration_difference_is_a_blocker(self):
  other=deepcopy(self.inventory);other['migrations']['repo/migrations/1_fixture.exs']='b'*64
  with self.mocked() as mocks:
   mocks['image_inventory'].side_effect=[self.inventory,other];report=self.check()
  self.assertIn('migration_file_differences_require_review',report['blockers'])
  self.assertEqual(report['migration_inventory']['changed'],1)
 def test_candidate_only_pending_migrations_block_despite_zero_live_pending(self):
  with self.mocked() as mocks:
   mocks['pending_migrations'].return_value=[dict(self.pending[0],candidate_pending_count=2,candidate_pending_versions=[3,4])]
   report=self.check()
  self.assertIn('pending_or_unavailable_migration_status',report['blockers'])
 def test_migration_diff_lists_added_changed_and_removed_files(self):
  before=deepcopy(self.inventory)
  before['migrations']['repo/migrations/2_remove.exs']='b'*64
  after={'migrations':{'repo/migrations/1_fixture.exs':'c'*64,'repo/migrations/3_add.exs':'a'*64}}
  report=C.migration_diff(before,after)
  self.assertEqual(report['added_files'],['repo/migrations/3_add.exs'])
  self.assertEqual(report['removed_files'],['repo/migrations/2_remove.exs'])
  self.assertEqual(report['changed_files'],['repo/migrations/1_fixture.exs'])
  self.assertFalse(report['file_lists_truncated'])
 def test_candidate_version_inventory_validates_numeric_identity_and_duplicates(self):
  self.assertEqual(C.migration_versions(T,self.inventory),{'repo':[1]})
  for extra in ('repo/migrations/1_duplicate.exs','repo/migrations/not_numeric.exs','repo/migrations/9223372036854775808_overflow.exs'):
   changed=deepcopy(self.inventory);changed['migrations'][extra]='a'*64
   with self.assertRaises(T.ConfigError):C.migration_versions(T,changed)
 def test_pending_or_unavailable_migration_evidence_blocks(self):
  for rows in ([{'repository':'Explorer.Repo','status':'unavailable'}],
   [{'repository':'Explorer.Repo','status':'ok','migration_files':1,'pending_count':1}]):
   with self.mocked() as mocks:
    mocks['pending_migrations'].return_value=rows;report=self.check()
   self.assertIn('pending_or_unavailable_migration_status',report['blockers'])
 def test_image_default_changes_are_reported_without_values(self):
  old={'Config':{'Env':['PATH=/old','OVERRIDDEN=old'],'Entrypoint':None}}
  new={'Config':{'Env':['PATH='+SECRET,'OVERRIDDEN=new','BLOCKSCOUT_VERSION='+COMMIT],'Entrypoint':['sh']}}
  service={'environment':{'OVERRIDDEN':'live',B.FLAG:'false'},'command':['sh','-c','start']}
  live={'Config':{'Env':['PATH=/old','OVERRIDDEN=live']}}
  result=C.inherited_image_changes(T,old,new,service,live)
  self.assertEqual(result['fields'],['Entrypoint']);self.assertEqual(result['unexpected_environment_keys'],['PATH'])
  self.assertNotIn('OVERRIDDEN',result['environment_keys']);self.assertNotIn(SECRET,json.dumps(result))
 def test_default_metadata_change_and_numeric_identity_difference_block(self):
  with self.mocked() as mocks:
   mocks['inherited_image_changes'].return_value={'fields':['WorkingDir'],'environment_keys':[],'unexpected_environment_keys':[]}
   mocks['mount_access'].return_value={'numeric_user_and_groups_match':False};report=self.check()
  self.assertIn('inherited_image_defaults_require_review',report['blockers'])
  self.assertIn('candidate_numeric_user_or_groups_differ',report['blockers'])
 def test_unhealthy_live_services_block(self):
  with self.mocked() as mocks:
   mocks['live_health'].return_value={'backend_readiness_ok':False,'nft_connection_and_queue_ok':False};report=self.check()
  self.assertIn('live_readiness_or_nft_check_failed',report['blockers'])
 def test_runtime_command_drift_blocks_activation_review(self):
  self.rows['backend']['Config']['Cmd']=['sh','-c','other command']
  with self.mocked():report=self.check()
  self.assertIn('startup_command_requires_review',report['blockers']);self.assertFalse(report['startup_command_matches_live'])
 def test_live_probe_failures_return_false_without_private_output(self):
  env=T.env_map(self.rows['backend']);env['PORT']='4000'
  self.rows['backend']['Config']['Env']=[k+'='+v for k,v in env.items()]
  with patch.object(T,'run',side_effect=T.ConfigError('private '+SECRET)):
   self.assertEqual(C.live_health(T,self.rows),{'backend_readiness_ok':False,'nft_connection_and_queue_ok':False})
 def test_live_restart_during_evidence_collection_is_refused(self):
  after=deepcopy(self.rows);after['backend']['RestartCount']=1
  with self.mocked() as mocks:
   mocks['inspect_project'].side_effect=[self.rows,after]
   with self.assertRaises(T.StabilityError):self.check()
 def test_profile_input_or_candidate_race_is_refused(self):
  with self.mocked() as mocks:
   mocks['load_private'].return_value={'changed':'private'}
   with self.assertRaises(T.ConfigError):self.check()
  with self.mocked() as mocks:
   mocks['image_info'].side_effect=[self.image,dict(self.image,Id='sha256:'+'f'*64)]
   with self.assertRaises(T.ConfigError):self.check()
  with self.mocked() as mocks:
   mocks['clean_source'].side_effect=[('c'*40,'9.0.1'),('d'*40,'9.0.1')]
   with self.assertRaises(T.ConfigError):self.check()
 def test_inventory_parsing_and_invalid_or_duplicate_paths(self):
  text='IDENTITY 10001 10001 10001\n'+'a'*64+'  /app/lib/explorer-9.0.1/priv/repo/migrations/1_fixture.exs\n'
  self.assertEqual(C.parse_inventory(T,text),self.inventory)
  for data in (text+text.splitlines()[1]+'\n',text.replace('repo/migrations','../repo/migrations'),'raw '+SECRET):
   with self.assertRaises(T.ConfigError) as caught:C.parse_inventory(T,data)
   self.assertNotIn(SECRET,str(caught.exception))
 def test_isolated_inventory_container_cleanup_on_failure(self):
  with patch.object(T,'run',side_effect=T.ConfigError('fixture')) as run,patch.object(C.subprocess,'run') as cleanup:
   with self.assertRaises(T.ConfigError):C.image_inventory(T,IMAGE)
  command=run.call_args.args[0]
  self.assertIn('--read-only',command);self.assertEqual(command[command.index('--network')+1],'none')
  self.assertEqual(command[command.index('--pull')+1],'never');self.assertNotIn('--mount',command)
  self.assertEqual(cleanup.call_args.args[0][:3],['docker','rm','-f'])
 def test_pending_parser_only_exports_allowlisted_fields(self):
  data=[dict(self.pending[0],private=SECRET)]
  with patch.object(T,'run',return_value=C.MARKER+json.dumps(data)):
   self.assertEqual(C.pending_migrations(T,self.inventory),self.pending)
  with patch.object(T,'run',return_value=SECRET):
   with self.assertRaises(T.ConfigError) as caught:C.pending_migrations(T,self.inventory)
  self.assertNotIn(SECRET,str(caught.exception))
 def test_pending_parser_rejects_wrong_candidate_version_or_count(self):
  for changes in ({'candidate_pending_versions':[2],'candidate_pending_count':1},
   {'candidate_migration_files':2},{'candidate_pending_count':1},{'pending_count_basis':'unknown'}):
   data=[dict(self.pending[0],**changes)]
   with patch.object(T,'run',return_value=C.MARKER+json.dumps(data)):
    with self.assertRaises(T.ConfigError):C.pending_migrations(T,self.inventory)
 def test_performance_catalog_is_bounded_and_outputs_no_raw_definitions(self):
  first=C.PERFORMANCE_INDEXES[0]
  with patch.object(T,'run',return_value=C.MARKER+json.dumps([[first,True,True,2048]])) as run:
   result=C.performance_indexes(T)
  self.assertEqual(result[0],{'name':first,'present':True,'valid':True,'ready':True,'bytes':2048})
  self.assertTrue(all(not row['present'] for row in result[1:]))
  expression=run.call_args.args[0][-1];self.assertIn('SET TRANSACTION READ ONLY',expression)
  self.assertNotIn('pg_get_indexdef',expression);self.assertNotIn('CREATE INDEX',expression)
  with patch.object(T,'run',return_value=C.MARKER+json.dumps([[SECRET,True,True,2048]])):
   with self.assertRaises(T.ConfigError) as caught:C.performance_indexes(T)
  self.assertNotIn(SECRET,str(caught.exception))
 def test_mount_checks_read_access_without_writing_files(self):
  service={'volumes':[{'type':'bind','source':str(self.f.f.parent),'target':'/app/logs'}]}
  row={'Mounts':[{'Type':'bind','Source':str(self.f.f.parent),'Destination':'/app/logs','RW':True}]}
  with patch.object(T,'run',return_value='IDENTITY 10001 10001 10001\n') as run:
   result=C.mount_access(T,service,row,self.inventory['identity'])
  self.assertTrue(result['numeric_user_and_groups_match']);script=run.call_args.args[0][-1]
  self.assertIn('test -w /app/logs',script);self.assertNotIn('touch',script);self.assertNotIn('>',script)
  row['Mounts'][0]['RW']=False
  with self.assertRaises(T.ConfigError):C.mount_access(T,service,row,self.inventory['identity'])
 def test_candidate_accepts_tool_only_commit_without_rebuild(self):
  with patch.object(B,'clean_source',return_value=('c'*40,'9.0.1')),patch.object(T,'run',side_effect=['','deploy/scripts/deploy_check.py\n']),\
   patch.object(B,'image_info',return_value=self.image):
   receipt,image=C.candidate(T,B,ROOT,self.manifest,self.requirements,self.values,self.f.private,self.path)
  self.assertEqual(receipt['source_commit'],COMMIT);self.assertEqual(image['Id'],IMAGE)
 def test_candidate_rejects_app_recipe_or_profile_changes(self):
  for path in ('apps/explorer/lib/explorer/chain.ex','docker/Dockerfile','docker-compose/environments/dev/compose.json'):
   with patch.object(B,'clean_source',return_value=('c'*40,'9.0.1')),patch.object(T,'run',side_effect=['',path+'\n']):
    with self.assertRaises(T.ConfigError):C.candidate(T,B,ROOT,self.manifest,self.requirements,self.values,self.f.private,self.path)
 def test_candidate_rejects_rebound_tag_or_incomplete_smoke(self):
  with patch.object(B,'clean_source',return_value=(COMMIT,'9.0.1')),patch.object(T,'run',side_effect=['','']),\
   patch.object(B,'image_info',return_value=dict(self.image,Id='sha256:'+'d'*64)):
   with self.assertRaises(T.ConfigError):C.candidate(T,B,ROOT,self.manifest,self.requirements,self.values,self.f.private,self.path)
  self.receipt['smoke_checks']=[];self.path.write_text(json.dumps(self.receipt))
  with self.assertRaises(T.ConfigError):C.candidate(T,B,ROOT,self.manifest,self.requirements,self.values,self.f.private,self.path)


class ElixirExpressionTests(unittest.TestCase):
 def test_real_elixir_expression_with_read_only_repo_fixture(self):
  script='''
defmodule Explorer.Repo do
 def config, do: [otp_app: :explorer, url: "fixture", pool_size: 4]
 def transaction(fun, _opts), do: {:ok, fun.()}
end
defmodule Explorer.Repo.Arbitrum do
 def config, do: [otp_app: :explorer, url: "fixture", pool_size: 1]
end
defmodule Explorer.ReleaseTasks do
 def repos, do: [Explorer.Repo, Explorer.Repo.Arbitrum]
end
defmodule Ecto.Adapters.SQL do
 def query!(_repo, sql, params, opts) do
  false = Keyword.fetch!(opts, :log)
  Process.put(:queries, Process.get(:queries, []) ++ [sql])
  cond do
   sql == "SELECT version FROM public.schema_migrations" ->
    [] = params
    %{rows: if(Process.get(:missing), do: [], else: [[1], [2]])}
   String.contains?(sql, "FROM pg_catalog.pg_index") ->
    [names] = params
    6 = length(names)
    true = Enum.all?(names, &is_binary/1)
    %{rows: [["blocks_consensus_number_timestamp_hash_idx", true, true, 2048]]}
   true ->
    [] = params
    %{rows: []}
  end
 end
end
defmodule Jason do
 def encode!([["blocks_consensus_number_timestamp_hash_idx", true, true, 2048]]) do
  ["SET TRANSACTION READ ONLY", "SET LOCAL statement_timeout = '1500ms'",
    "SET LOCAL lock_timeout = '500ms'", query] = Process.get(:queries)
  true = String.starts_with?(query, "SELECT ")
  "FIXTURE_OK"
 end
 def encode!(rows) do
  2 = length(rows)
  true = Enum.all?(rows, &(&1.status == "ok" and &1.migration_files == 1))
  true = Enum.all?(rows, &(&1.pending_count == if(Process.get(:missing), do: 1, else: 0)))
  true = Enum.all?(rows, &(&1.pending_count_basis == "live_image"))
  repo = Enum.find(rows, &(&1.repository == "Explorer.Repo"))
  2 = repo.candidate_migration_files
  true = repo.candidate_pending_versions == if(Process.get(:missing), do: [1, 3], else: [3])
  true = repo.candidate_pending_count == length(repo.candidate_pending_versions)
  queries = Process.get(:queries)
  8 = length(queries)
  true = Enum.all?(Enum.chunk_every(queries, 4), fn chunk ->
   ["SET TRANSACTION READ ONLY", "SET LOCAL statement_timeout = '1500ms'",
    "SET LOCAL lock_timeout = '500ms'", "SELECT version FROM public.schema_migrations"] == chunk
  end)
  "FIXTURE_OK"
 end
end
true = :code.add_patha(~c"/work/explorer-1.0/ebin")
Process.register(self(), Explorer.Repo)
'''
  candidate_versions={'repo':[1,3],'arbitrum':[2]}
  script+='Process.put(:queries, [])\nProcess.put(:missing, false)\n'+C.migration_expression(candidate_versions)
  script+='\nProcess.put(:queries, [])\nProcess.put(:missing, true)\n'+C.migration_expression(candidate_versions)
  script+='\nProcess.put(:queries, [])\n'+C.performance_expression()
  data=io.BytesIO()
  files={'fixture.exs':script,'explorer-1.0/ebin/explorer.app':'{application,explorer,[{vsn,"1.0"},{modules,[]}]}.',
   'explorer-1.0/priv/repo/migrations/1_fixture.exs':'# fixture',
   'explorer-1.0/priv/arbitrum/migrations/2_fixture.exs':'# fixture'}
  with tarfile.open(fileobj=data,mode='w') as archive:
   for path,value in files.items():
    raw=value.encode();entry=tarfile.TarInfo(path);entry.size=len(raw);archive.addfile(entry,io.BytesIO(raw))
  result=subprocess.run(['docker','run','--rm','-i','--network','none','--read-only','--tmpfs','/work:rw,exec,size=64m',
   '--workdir','/work','--cpus','2','--memory','256m','-e','ERL_FLAGS=+S 2:2',
   'elixir@sha256:0ff6cbfc498e86d21d1f8939672f32acaffe9e5be8924da7942ce69fd34e2c68',
   'sh','-c','tar -xf - && exec elixir fixture.exs'],input=data.getvalue(),capture_output=True,timeout=60)
  self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
  self.assertEqual(result.stdout.decode().count(C.MARKER+'FIXTURE_OK'),3)


if __name__=='__main__':
 suite=unittest.defaultTestLoader.loadTestsFromTestCase(EvidenceTests)
 if '--docker' in sys.argv:suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(ElixirExpressionTests))
 result=unittest.TextTestRunner(verbosity=2).run(suite)
 raise SystemExit(not result.wasSuccessful())
