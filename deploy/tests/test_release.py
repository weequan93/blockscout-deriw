#!/usr/bin/env python3
"""Targeted release/rollback orchestration with private synthetic deployment state."""
import contextlib
from copy import deepcopy
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import unittest
import uuid
from unittest.mock import patch

from test_deploy_check import load, ROOT, C, B, T, EvidenceTests, IMAGE, SECRET
R=load('release_fixture',ROOT/'deploy/scripts/release.py')


class ReleaseTests(unittest.TestCase):
 def setUp(self):
  self.e=EvidenceTests();self.e.setUp();self.addCleanup(self.e.doCleanups)
  self.parent=self.e.f.f.parent;self.root=self.parent/'releases'
  self.addCleanup(patch.stopall);patch.object(T,'MANAGED_RELEASES',self.root).start()
  self.rows=deepcopy(self.e.rows);self.manifest=self.e.manifest
  self.current={'name':'docker-compose','services':{name:{'image':row['Image'],'environment':T.env_map(row),
   'command':R.LEGACY if name=='backend' else ['fixture'], 'networks':{'default':None}}
   for name,row in self.rows.items()},'networks':{'default':{'name':'docker-compose_default'}}}
  self.current['services']['backend']['environment'].pop(B.FLAG,None)
  self.rows['backend']['Config']['Env']=[k+'='+v for k,v in self.current['services']['backend']['environment'].items()]
  self.managed=deepcopy(self.current);self.managed['services']['backend']['environment'][B.FLAG]='false'
  self.old={'identity':(10001,10001,(10001,)),'migrations':{'repo/migrations/1_fixture.exs':'a'*64}}
  self.new=deepcopy(self.old);self.new['migrations'].update(R.DEFERRED)
  self.evidence={'blockers':['migration_file_differences_require_review','pending_or_unavailable_migration_status'],
   'current_database_migrations':[{'status':'ok','migration_directory':'repo','pending_count':0,
     'candidate_pending_versions':sorted(R.VERSIONS)}]}
  self.calls=[]
 def compose(self,directory,manifest,values,files,managed):
  if len(files)==1 and files[0].name.endswith('.compose.json'):
   return json.loads(files[0].read_bytes().replace(b'$$',b'$'))
  return deepcopy(self.current)
 def mutation(self,api,path,manifest,directory,log):
  self.calls.append(path.name)
  model=self.compose(directory,manifest,{},[path],True);service=model['services']['backend']
  row=self.rows['backend'];row['Id']=('f' if path.name.startswith('forward') else 'e')*64
  row['Image']=service['image'];row['Config']['Cmd']=service['command']
  row['Config']['Labels'].update(service['labels'])
  row['Config']['Labels']['com.docker.compose.project.config_files']=str(path)
  row['Config']['Env']=[k+'='+v for k,v in service['environment'].items()]
  row['State'].update(Running=True,StartedAt='2026-11-03T05:00:00Z');row['RestartCount']=0
 def runner(self,command,**kwargs):
  if command[:3]==['docker','image','tag']:return ''
  if command[:3]==['docker','exec','backend']:
   forward=self.rows['backend']['Image']==IMAGE
   return C.MARKER+json.dumps({'settings_ok':True,'repair_disabled':forward,'historical_scanner_running':not forward,
    'market_price_ready':True if T.env_map(self.rows['backend']).get('MARKET_NATIVE_COIN_PRICE_ONLY')=='true' else None})
  raise AssertionError('Unexpected command: '+str(command[:3]))
 @contextlib.contextmanager
 def mocked(self):
  with contextlib.ExitStack() as stack:
   mocks={}
   def image(api,reference,*args,**kwargs):return dict(self.e.image,Id=reference if reference.startswith('sha256:') else IMAGE)
   targets=[(T,'inspect_project',{'side_effect':lambda *a,**k:deepcopy(self.rows)}),
    (T,'managed_model',{'side_effect':lambda *a:deepcopy(self.managed)}),
    (T,'compose_model',{'side_effect':self.compose}),(T,'load_private',{'return_value':self.e.values}),
    (T,'rpc_chain',{'return_value':18417507517}),(T,'run',{'side_effect':self.runner}),
    (B,'clean_source',{'return_value':('c'*40,'9.0.1')}),(B,'image_info',{'side_effect':image}),
    (C,'candidate',{'return_value':(self.e.receipt,self.e.image)}),(C,'check',{'return_value':self.evidence}),
    (C,'image_inventory',{'side_effect':lambda api,image:self.new if image==IMAGE else self.old}),
    (C,'live_health',{'return_value':{'backend_readiness_ok':True,'nft_connection_and_queue_ok':True}}),
    (R,'recreate',{'side_effect':self.mutation})]
   for module,name,options in targets:mocks[name]=stack.enter_context(patch.object(module,name,**options))
   yield mocks
 def deploy(self,apply=False):
  return R.deploy(T,B,C,ROOT,self.manifest,self.e.requirements,self.e.values,self.e.f.private,self.e.path,
   self.e.f.f.live_dir,apply=apply)
 def test_check_has_no_persistent_or_service_changes(self):
  with self.mocked() as mocks:result=self.deploy()
  self.assertEqual(result['status'],'start_only_deployment_plan_ready');self.assertFalse(self.root.exists())
  mocks['recreate'].assert_not_called();self.assertFalse(result['startup_migrations'])
 def test_apply_records_private_snapshots_and_recreates_only_backend(self):
  output=io.StringIO()
  with self.mocked(),contextlib.redirect_stdout(output):result=self.deploy(True)
  self.assertEqual(result['status'],'verified');self.assertEqual(self.calls,['forward.compose.json'])
  self.assertNotIn(SECRET,output.getvalue());self.assertNotIn(SECRET,json.dumps(result))
  path=Path(result['state']);state=T.read_json(path)
  self.assertEqual(state['status'],'active');self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
  self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode),0o700)
  for side in ('forward','rollback'):
   snapshot=path.parent/(side+'.compose.json');self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode),0o600)
   model=self.compose(None,None,None,[snapshot],None)
   self.assertEqual(model['services']['backend']['command'],R.START)
   self.assertTrue(all('build' not in service and service['pull_policy']=='never' for service in model['services'].values()))
   self.assertEqual(set(model['services']),set(self.manifest['baseline_image_ids']))
 def test_rollback_restores_previous_image_and_env_without_migrations(self):
  with self.mocked(),contextlib.redirect_stdout(io.StringIO()):
   result=self.deploy(True);path=Path(result['state'])
   plan=R.rollback(T,B,C,ROOT,self.manifest,path)
   self.assertEqual(plan['status'],'rollback_plan_ready');self.assertEqual(len(self.calls),1)
   undone=R.rollback(T,B,C,ROOT,self.manifest,path,apply=True)
  self.assertEqual(undone['status'],'verified');self.assertEqual(self.calls,['forward.compose.json','rollback.compose.json'])
  self.assertEqual(self.rows['backend']['Image'],self.manifest['baseline_image_ids']['backend'])
  self.assertNotIn(B.FLAG,T.env_map(self.rows['backend']))
  self.assertEqual(self.rows['backend']['Config']['Cmd'],R.START)
  self.assertEqual(T.read_json(path)['status'],'rolled_back');self.assertTrue(undone['historical_scanner_running'])
 def test_recreation_failure_keeps_recoverable_state_and_rollback(self):
  with self.mocked() as mocks,contextlib.redirect_stdout(io.StringIO()):
   mocks['recreate'].side_effect=T.ConfigError('Fixture interruption')
   with self.assertRaises(T.ConfigError):self.deploy(True)
   path=next(self.root.glob('release-*/state.json'))
   self.assertEqual(T.read_json(path)['status'],'activation_requested')
   self.assertEqual(R.status(T,B,C,self.manifest,path)['status'],'not_activated')
   mocks['recreate'].side_effect=self.mutation
   result=R.rollback(T,B,C,ROOT,self.manifest,path,apply=True)
  self.assertEqual(result['status'],'verified')
 def test_interruption_after_backend_removal_can_be_recovered(self):
  with self.mocked() as mocks,contextlib.redirect_stdout(io.StringIO()):
   def fail(*args):self.rows.pop('backend');raise T.ConfigError('Fixture interruption')
   mocks['recreate'].side_effect=fail
   with self.assertRaises(T.ConfigError):self.deploy(True)
   path=next(self.root.glob('release-*/state.json'))
   self.assertEqual(R.status(T,B,C,self.manifest,path)['status'],'backend_missing_needs_attention')
   self.rows['backend']=deepcopy(T.read_json(path)['before']['backend'])
   saved=self.rows.pop('backend')
   def recover(*args):self.rows['backend']=deepcopy(saved);self.mutation(*args)
   mocks['recreate'].side_effect=recover
   result=R.rollback(T,B,C,ROOT,self.manifest,path,apply=True)
  self.assertEqual(result['status'],'verified')
 def test_does_not_overwrite_later_release_or_unrelated_restart(self):
  with self.mocked(),contextlib.redirect_stdout(io.StringIO()):
   result=self.deploy(True);path=Path(result['state'])
   self.rows['backend']['Config']['Labels'][R.STATE_LABEL]='/other/release/state.json'
   with self.assertRaises(T.ConfigError):R.rollback(T,B,C,ROOT,self.manifest,path,apply=True)
   self.rows['backend']['Config']['Labels'][R.STATE_LABEL]=str(path)
   self.rows['nft_media_handler']['RestartCount']=1
   with self.assertRaises(T.StabilityError):R.rollback(T,B,C,ROOT,self.manifest,path,apply=True)
  self.assertEqual(len(self.calls),1)
 def test_tampered_snapshot_is_not_replayed(self):
  with self.mocked(),contextlib.redirect_stdout(io.StringIO()):
   result=self.deploy(True);path=Path(result['state'])
   (path.parent/'rollback.compose.json').write_text('{}')
   with self.assertRaises(T.ConfigError):R.rollback(T,B,C,ROOT,self.manifest,path,apply=True)
  self.assertEqual(len(self.calls),1)
 def test_unreviewed_migration_and_nonmigration_blockers_refused(self):
  for inventory in (dict(self.new,migrations={**self.new['migrations'],'repo/migrations/999_structural.exs':'a'*64}),
   dict(self.new,migrations={**self.new['migrations'],next(iter(R.DEFERRED)):'b'*64})):
   with self.assertRaises(T.ConfigError):R.defer_policy(T,self.old,inventory,self.evidence)
  with self.assertRaises(T.ConfigError):R.defer_policy(T,self.old,self.new,dict(self.evidence,blockers=['other']))
  bad=deepcopy(self.evidence);bad['current_database_migrations'][0]['candidate_pending_versions']=[999]
  with self.assertRaises(T.ConfigError):R.defer_policy(T,self.old,self.new,bad)
 def test_existing_reviewed_index_migrations_can_remain_deferred(self):
  evidence=deepcopy(self.evidence);evidence['current_database_migrations'][0]['pending_count']=2
  self.assertEqual(R.defer_policy(T,self.new,self.new,evidence),sorted(R.VERSIONS))
 def test_active_context_and_next_plan_use_selected_image(self):
  with self.mocked(),contextlib.redirect_stdout(io.StringIO()):
   result=self.deploy(True);context=R.active_context(T,self.rows['backend'],self.manifest)
   self.assertEqual(context['image_ids']['backend'],IMAGE)
   report=T.plan(ROOT,self.manifest,self.e.requirements,self.e.values,self.e.f.f.live_dir)
  self.assertEqual(report['changed_runtime_fields'],[]);self.assertEqual(report['live_images_differing_from_capture'],[])
  self.assertEqual(report['status'],'runtime_model_comparison_passed')
 def test_declared_runtime_drift_blocks_rollback(self):
  with self.mocked(),contextlib.redirect_stdout(io.StringIO()):
   result=self.deploy(True);self.rows['backend']['Config']['Env'].append('CHAIN_ID=2886')
   with self.assertRaises(T.ConfigError):R.rollback(T,B,C,ROOT,self.manifest,Path(result['state']),apply=True)
  self.assertEqual(len(self.calls),1)
 def test_new_component_image_selection_is_not_hidden_by_active_state(self):
  with self.mocked(),contextlib.redirect_stdout(io.StringIO()):
   result=self.deploy(True);self.managed['services']['frontend']['image']='frontend:another-version'
   report=T.plan(ROOT,self.manifest,self.e.requirements,self.e.values,self.e.f.f.live_dir)
  self.assertIn('services.frontend.image',report['changed_runtime_fields'])
 def test_status_does_not_modify_or_automatically_rollback_unready_backend(self):
  with self.mocked() as mocks,contextlib.redirect_stdout(io.StringIO()):
   result=self.deploy(True);path=Path(result['state']);saved=path.read_bytes()
   mocks['live_health'].return_value={'backend_readiness_ok':False,'nft_connection_and_queue_ok':True}
   status=R.status(T,B,C,self.manifest,path)
  self.assertEqual(status['status'],'needs_attention');self.assertEqual(path.read_bytes(),saved)
  self.assertEqual(self.calls,['forward.compose.json'])
 def test_enabled_price_feed_requires_an_actual_rate_before_success(self):
  self.managed['services']['backend']['environment']['MARKET_NATIVE_COIN_PRICE_ONLY']='true'
  with self.mocked(),contextlib.redirect_stdout(io.StringIO()):
   result=self.deploy(True);path=Path(result['state']);saved=path.read_bytes()
   original=self.runner
   def missing_rate(command,**kwargs):
    response=original(command,**kwargs)
    if command[:3]==['docker','exec','backend']:
     data=json.loads(response[len(C.MARKER):]);data['market_price_ready']=False
     return C.MARKER+json.dumps(data)
    return response
   with patch.object(T,'run',side_effect=missing_rate):
    status=R.status(T,B,C,self.manifest,path)
  self.assertEqual(status['status'],'needs_attention');self.assertFalse(status['market_price_ready'])
  self.assertEqual(path.read_bytes(),saved);self.assertEqual(self.calls,['forward.compose.json'])
 def test_cli_requires_explicit_check_or_apply(self):
  arguments=['deploy','dev','deploy','--private-file',str(self.e.f.private),'--receipt',str(self.e.path)]
  with patch.object(sys,'argv',arguments),contextlib.redirect_stderr(io.StringIO()):
   with self.assertRaises(SystemExit) as caught:T.main()
  self.assertEqual(caught.exception.code,2);self.assertFalse(self.root.exists())
 def test_snapshot_literal_dollars_survive_replay(self):
  model=deepcopy(self.current);value='fixture $ONE $$TWO ${THREE} "quotes"\nnewlines'
  model['services']['backend']['environment']['SPECIAL']=value
  data=R.snapshot_bytes(model)
  self.assertEqual(json.loads(data.replace(b'$$',b'$'))['services']['backend']['environment']['SPECIAL'],value)
 def test_unassigned_snapshot_env_cannot_inherit_operator_ambient_values(self):
  model=deepcopy(self.current);env=model['services']['backend']['environment']
  env.update(UNASSIGNED=None,MISSING=None)
  self.rows['backend']['Config']['Env'].append('UNASSIGNED='+SECRET)
  frozen=R.snapshot(model,self.rows,IMAGE,self.root/'state.json','forward')
  self.assertEqual(frozen['services']['backend']['environment']['UNASSIGNED'],SECRET)
  self.assertNotIn('MISSING',frozen['services']['backend']['environment'])


class ComposeReleaseTests(ReleaseTests):
 def test_real_compose_private_snapshot_round_trip(self):
  model=T.managed_model(ROOT,self.manifest,self.e.values)
  special='fixture $NOT_EXPANDED $$PAIR ${NOT_EXPANDED} "quotes"\nnewlines'
  model['services']['backend']['environment']['SECRET_KEY_BASE']=special
  self.root.mkdir(mode=0o700)
  path=self.root/'fixture.compose.json'
  frozen=R.snapshot(model,self.rows,IMAGE,self.root/'state.json','forward');path.write_bytes(R.snapshot_bytes(frozen));path.chmod(0o600)
  actual=R.check_snapshot(T,path,frozen,self.manifest,self.e.f.f.live_dir)
  self.assertEqual(actual['services']['backend']['environment']['SECRET_KEY_BASE'],special)
  self.assertEqual(actual['services']['backend']['command'],R.START)


class DockerTransportTests(ReleaseTests):
 def test_targeted_compose_recreation_preserves_peer_and_literal_environment(self):
  project='blockscout-release-fixture-'+uuid.uuid4().hex[:10]
  image='elixir@sha256:0ff6cbfc498e86d21d1f8939672f32acaffe9e5be8924da7942ce69fd34e2c68'
  value='fixture $LITERAL $$PAIR ${NOT_EXPANDED} "quotes"\nnewlines'
  command=['sh','-c','while :; do sleep 10; done']
  model={'name':project,'services':{name:{'image':image,'pull_policy':'never','command':command,'environment':{'FIXTURE_VALUE':value}}
   for name in ('backend','peer')}}
  model['services']['backend'].update(depends_on={'peer':{'condition':'service_started','required':True}},links=['peer:database'])
  directory=self.parent/'transport';directory.mkdir(mode=0o700)
  initial=directory/'initial.json';initial.write_bytes(R.snapshot_bytes(model))
  selected=deepcopy(model);selected['services']['backend']['environment']['REVISION']='candidate'
  forward=directory/'forward.json';forward.write_bytes(R.snapshot_bytes(selected))
  prefix=['docker','compose','-p',project,'--env-file','/dev/null']
  def run(args):return subprocess.run(args,capture_output=True,text=True,check=True,timeout=45).stdout
  def inspect():
   ids=run(prefix+['-f',str(initial),'ps','-aq']).split()
   rows=json.loads(run(['docker','inspect',*ids]))
   return {row['Config']['Labels']['com.docker.compose.service']:row for row in rows}
  try:
   run(prefix+['-f',str(initial),'up','-d','--no-build','--pull','never'])
   before=inspect()
   R.recreate(T,forward,{'compose_project':project},directory,directory/'forward.log.private')
   after=inspect()
   self.assertEqual(after['peer']['Id'],before['peer']['Id']);self.assertNotEqual(after['backend']['Id'],before['backend']['Id'])
   self.assertEqual(T.env_map(after['backend'])['FIXTURE_VALUE'],value)
   self.assertEqual(T.env_map(after['backend'])['REVISION'],'candidate')
   R.recreate(T,initial,{'compose_project':project},directory,directory/'rollback.log.private')
   reverted=inspect();self.assertEqual(reverted['peer']['Id'],before['peer']['Id'])
   self.assertNotIn('REVISION',T.env_map(reverted['backend']));self.assertEqual(T.env_map(reverted['backend'])['FIXTURE_VALUE'],value)
  finally:
   subprocess.run(prefix+['-f',str(initial),'down','--volumes','--remove-orphans','--timeout','2'],capture_output=True,timeout=45)


if __name__=='__main__':
 suite=unittest.defaultTestLoader.loadTestsFromTestCase(ReleaseTests)
 if '--compose' in sys.argv:suite.addTest(ComposeReleaseTests('test_real_compose_private_snapshot_round_trip'))
 if '--docker' in sys.argv:suite.addTest(DockerTransportTests('test_targeted_compose_recreation_preserves_peer_and_literal_environment'))
 result=unittest.TextTestRunner(verbosity=2).run(suite)
 raise SystemExit(not result.wasSuccessful())
