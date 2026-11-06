#!/usr/bin/env python3
"""Read-only adoption workflow: synthetic private data, mocked daemon and real Compose resolution."""
import contextlib
from copy import deepcopy
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
SPEC=importlib.util.spec_from_file_location('dev_config',ROOT/'deploy/scripts/dev_config.py')
T=importlib.util.module_from_spec(SPEC);sys.modules[SPEC.name]=T;SPEC.loader.exec_module(T)
SECRET='fixture-private-do-not-export-X29'


class ConfigTests(unittest.TestCase):
 def setUp(self):
  self.manifest,self.requirements,self.profile=T.configuration(ROOT,'dev')
  self.values={key:'fixture-value-'+key for key in self.requirements}
  self.values['DEV_FRONTEND_NEXT_PUBLIC_NETWORK_ID']='18417507517'
  for key,origin in self.requirements.items():
   if origin['environment_key']=='RELEASE_COOKIE':self.values[key]=SECRET
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.parent=Path(self.tmp.name).resolve();self.live_dir=self.parent/'live';self.live_dir.mkdir()
  self.paths=[self.live_dir/name for name in self.manifest['baseline_compose_layers']]
  for path in self.paths:path.write_text('{}')
  self.rows={}
  for i,(name,service) in enumerate(self.profile['services'].items(),1):
   env={key:value for key,value in service['environment'].items() if not str(value).startswith('${DEV_')}
   for ref,origin in self.requirements.items():
    if origin['service']==name:env[origin['environment_key']]=self.values[ref]
   self.rows[name]={'Id':format(i,'064x'),'Image':self.manifest['baseline_image_ids'][name],
    'State':{'Running':True,'StartedAt':'2026-11-03T03:02:19Z'},'RestartCount':0,
    'Config':{'Env':[k+'='+v for k,v in env.items()],
     'Labels':{'com.docker.compose.project':'docker-compose','com.docker.compose.service':name,
      'com.docker.compose.project.config_files':','.join(map(str,self.paths))}}}
 def test_versioned_public_selections(self):
  env=self.profile['services']['backend']['environment']
  self.assertEqual(env['CHAIN_ID'],'18417507517');self.assertEqual(env['CACHE_TXS_COUNT_APPROXIMATE'],'true')
  self.assertEqual(env['TRACE_BLOCK_RANGES'],'176114083..latest')
  self.assertEqual(env['INDEXER_CONTRACT_CODE_RETRY_DELAY'],'60s')
  self.assertEqual(env['INDEXER_UNCATALOGED_TOKEN_TRANSFERS_ENABLED'],'false')
  self.assertEqual(len(self.requirements),133);self.assertEqual(len(self.profile['services']),14)
  for name in ('backend','nft_media_handler'):self.assertEqual(self.profile['services'][name]['build']['args']['CHAIN_TYPE'],'arbitrum')
  verifier=self.profile['services']['sc-verifier']['environment']
  self.assertIn('5de16fa32acc6b55707188922d5366915aec21af',verifier['SMART_CONTRACT_VERIFIER__VYPER__FETCHER__LIST__LIST_URL'])
 def test_confirmed_database_settings_are_versioned(self):
  db=self.profile['services']['db'];parameters=T.postgres_command_summary(db['command'],set())['parameters']
  self.assertEqual(parameters,{'max_connections':'80','shared_buffers':'4GB','effective_cache_size':'18GB',
   'work_mem':'16MB','maintenance_work_mem':'512MB','max_worker_processes':'4',
   'max_parallel_workers':'0','max_parallel_workers_per_gather':'0'})
  self.assertEqual(db['shm_size'],6442450944)
  self.assertEqual(self.manifest['expected_database']['postgres_parameters'],parameters)
 def test_test_and_prod_refuse_without_inventory(self):
  for env in ('test','prod'):
   with self.assertRaises(T.ConfigError):T.configuration(ROOT,env)
 def test_exact_private_collection_and_cookie_validation(self):
  self.assertEqual(T.collect_values(self.requirements,self.rows),self.values)
  self.rows['nft_media_handler']['Config']['Env']=[v.replace(SECRET,'fixture-other-cookie') for v in self.rows['nft_media_handler']['Config']['Env']]
  with self.assertRaises(T.ConfigError):T.collect_values(self.requirements,self.rows)
 def test_missing_private_value_refused(self):
  first=next(iter(self.requirements.values()))
  row=self.rows[first['service']];prefix=first['environment_key']+'='
  row['Config']['Env']=[v for v in row['Config']['Env'] if not v.startswith(prefix)]
  with self.assertRaises(T.ConfigError):T.collect_values(self.requirements,self.rows)
 def test_private_output_permissions_and_no_overwrite(self):
  path=self.parent/'private'/'private.json';T.save_private(path,self.values,ROOT)
  self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
  self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode),0o700)
  self.assertEqual(T.load_private(path,self.requirements),self.values)
  with self.assertRaises(T.ConfigError):T.save_private(path,self.values,ROOT)
 def test_output_inside_checkout_refused(self):
  with self.assertRaises(T.ConfigError):T.save_private(ROOT/'private-must-not-exist.json',self.values,ROOT)
  self.assertFalse((ROOT/'private-must-not-exist.json').exists())
 def test_symlink_and_loose_permissions_refused(self):
  folder=self.parent/'private';folder.mkdir(mode=0o700)
  path=folder/'link.json';path.symlink_to(folder/'missing')
  with self.assertRaises(T.ConfigError):T.save_private(path,self.values,ROOT)
  path=folder/'private.json';T.save_private(path,self.values,ROOT);path.chmod(0o644)
  with self.assertRaises(T.ConfigError):T.load_private(path,self.requirements)
 def test_private_file_shape_rejected(self):
  path=self.parent/'private'/'private.json';T.save_private(path,self.values,ROOT)
  data=json.loads(path.read_text());data['environment']='prod';path.write_text(json.dumps(data))
  with self.assertRaises(T.ConfigError):T.load_private(path,self.requirements)
 def test_ambient_profile_values_are_ignored(self):
  with patch.dict(os.environ,{'DEV_BACKEND_DATABASE_URL':'wrong','COMPOSE_FILE':'wrong'}):
   env=T.compose_environment(self.values)
  self.assertEqual(env['DEV_BACKEND_DATABASE_URL'],self.values['DEV_BACKEND_DATABASE_URL']);self.assertNotIn('COMPOSE_FILE',env)
 def test_model_diff_never_prints_values(self):
  a={'services':{'backend':{'environment':{'DATABASE_URL':SECRET},'command':['echo',SECRET],'labels':{'secret':SECRET}}}}
  b=deepcopy(a);b['services']['backend']['environment']['DATABASE_URL']='other';b['services']['backend']['command']=['other']
  b['services']['backend']['labels']={'different':'value'}
  result=T.model_differences(a,b)
  self.assertNotIn(SECRET,json.dumps(result));self.assertIn('services.backend.environment.DATABASE_URL',result)
  self.assertIn('services.backend.command',result);self.assertIn('services.backend.labels',result)
 def test_compose_file_order_and_scope(self):
  self.assertEqual(T.live_files(self.live_dir,self.rows['backend'],self.manifest),self.paths)
  self.rows['backend']['Config']['Labels']['com.docker.compose.project.config_files']=str(self.parent/'outside.yml')
  with self.assertRaises(T.ConfigError):T.live_files(self.live_dir,self.rows['backend'],self.manifest)
 def test_wrong_live_chain_refused(self):
  self.rows['backend']['Config']['Env']=[x.replace('CHAIN_ID=18417507517','CHAIN_ID=2886') for x in self.rows['backend']['Config']['Env']]
  values=list(self.rows.values());ids='\n'.join(row['Id'] for row in values)
  with patch.object(T,'run',side_effect=[ids,json.dumps(values)]):
   with self.assertRaises(T.ConfigError):T.inspect_project(self.manifest)
 def test_rpc_wrong_chain_refused(self):
  with patch.object(T,'run',return_value='{"result":"0xb46"}\nBLOCKSCOUT_RPC_META=200|0.001\nBLOCKSCOUT_RPC_EXIT=0\n'):
   with self.assertRaises(T.RpcChainError) as error:T.rpc_chain(self.manifest)
  self.assertEqual(error.exception.kind,'identity_mismatch')
  self.assertFalse(error.exception.report['chain_id_matches'])
 def test_rpc_failures_keep_safe_transport_metadata_and_hide_url_and_bodies(self):
  manifest={**self.manifest,'http_rpc':'http://fixture.invalid/'+SECRET}
  for raw,kind in [
   ('\nBLOCKSCOUT_RPC_META=503|0.001\nBLOCKSCOUT_RPC_EXIT=22\n','http_error'),
   ('\nBLOCKSCOUT_RPC_META=000|4.001\nBLOCKSCOUT_RPC_EXIT=28\n','timeout'),
   ('\nBLOCKSCOUT_RPC_META=000|0.001\nBLOCKSCOUT_RPC_EXIT=7\n','connection_error'),
   (SECRET+'\nBLOCKSCOUT_RPC_META=200|0.001\nBLOCKSCOUT_RPC_EXIT=0\n','invalid_rpc_response'),
   (SECRET,'invalid_probe')]:
   with self.subTest(kind=kind),patch.object(T,'run',return_value=raw) as run:
    with self.assertRaises(T.RpcChainError) as error:T.rpc_chain(manifest)
    self.assertEqual(error.exception.kind,kind)
    public=str(error.exception)+json.dumps(error.exception.report)
    self.assertNotIn(SECRET,public)
    self.assertNotIn(manifest['http_rpc'],' '.join(run.call_args.args[0]))
    self.assertEqual(run.call_args.kwargs['input_text'],manifest['http_rpc']+'\n')
 def test_rpc_success_uses_injected_deadline_runner_and_json_rpc_errors_are_redacted(self):
  raw=json.dumps({'result':hex(int(self.manifest['chain_id']))})+'\nBLOCKSCOUT_RPC_META=200|0.001\nBLOCKSCOUT_RPC_EXIT=0\n'
  with patch.object(T,'run',side_effect=AssertionError('default runner used')):
   self.assertEqual(T.rpc_chain(self.manifest,runner=lambda *a,**k:raw),int(self.manifest['chain_id']))
  raw=json.dumps({'error':{'code':-32603,'message':SECRET}})+'\nBLOCKSCOUT_RPC_META=200|0.001\nBLOCKSCOUT_RPC_EXIT=0\n'
  with patch.object(T,'run',return_value=raw),self.assertRaises(T.RpcChainError) as error:T.rpc_chain(self.manifest)
  self.assertEqual(error.exception.report['rpc_error_code'],-32603)
  self.assertNotIn(SECRET,str(error.exception)+json.dumps(error.exception.report))
 def test_secret_collection_stops_on_restart_before_write(self):
  after=deepcopy(self.rows);after['backend']['RestartCount']=1
  output=self.parent/'private'/'private.json';text=io.StringIO()
  with patch.object(sys,'argv',['deploy','dev','capture-private','--output',str(output)]),patch.object(T,'inspect_project',side_effect=[self.rows,after]),contextlib.redirect_stdout(text):
   self.assertEqual(T.main(),1)
  self.assertFalse(output.exists());self.assertNotIn(SECRET,text.getvalue())
  report=json.loads(text.getvalue());self.assertEqual(report['changed_containers'][0]['service'],'backend')
  self.assertEqual(report['changed_containers'][0]['changed_fields'],['restart_count'])
 def test_capture_stdout_contains_no_private_values(self):
  output=self.parent/'private'/'private.json';text=io.StringIO()
  with patch.object(sys,'argv',['deploy','dev','capture-private','--output',str(output)]),patch.object(T,'inspect_project',return_value=self.rows),contextlib.redirect_stdout(text):
   self.assertEqual(T.main(),0)
  self.assertNotIn(SECRET,text.getvalue());self.assertTrue(output.exists())
 def test_raw_subprocess_errors_suppressed(self):
  with patch.object(T.subprocess,'run',return_value=subprocess.CompletedProcess([],1,SECRET,SECRET)):
   with self.assertRaises(T.ConfigError) as error:T.run(['docker','inspect'])
  self.assertNotIn(SECRET,str(error.exception))
 def test_plan_reports_differences_and_remains_read_only(self):
  model={'services':{name:{'environment':T.env_map(row)} for name,row in self.rows.items()}}
  planned=deepcopy(model);planned['services']['backend']['environment']['INDEXER_CONTRACT_CODE_RETRY_DELAY']='different'
  with patch.object(T,'inspect_project',return_value=self.rows),patch.object(T,'live_files',return_value=self.paths),\
   patch.object(T,'compose_model',return_value=model),patch.object(T,'managed_model',return_value=planned),patch.object(T,'rpc_chain',return_value=18417507517):
   report=T.plan(ROOT,self.manifest,self.requirements,self.values,self.live_dir)
  self.assertEqual(report['status'],'differences_require_review');self.assertNotIn(SECRET,json.dumps(report))
  self.assertIn('services.backend.environment.INDEXER_CONTRACT_CODE_RETRY_DELAY',report['changed_runtime_fields'])
 def test_inspect_mount_order_is_not_a_stability_change(self):
  before=deepcopy(self.rows)
  mounts=[{'Type':'bind','Source':'/data/'+SECRET,'Destination':'/app/logs','RW':True,'Propagation':'rprivate'},
   {'Type':'bind','Source':'/data/dets','Destination':'/app/dets','RW':True,'Propagation':'rprivate'}]
  before['backend']['Mounts']=mounts
  after=deepcopy(before);after['backend']['Mounts'].reverse()
  self.assertEqual(T.stable(before),T.stable(after));self.assertEqual(T.stability_changes(before,after),[])
  note=T.inspection_order_changes(before,after)
  self.assertEqual(note,['services.backend.inspect_mounts_order']);self.assertNotIn(SECRET,json.dumps(note))
 def test_actual_inspect_mount_properties_remain_stability_changes(self):
  before=deepcopy(self.rows)
  before['backend']['Mounts']=[{'Type':'bind','Source':'/data/logs','Destination':'/app/logs','RW':True,'Propagation':'rprivate','Driver':'local'}]
  for field,value in [('Type','volume'),('Source','/private/'+SECRET),('Destination','/other'),('RW',False),('Propagation','rshared'),('Driver','different'),('OtherMetadata',SECRET)]:
   after=deepcopy(before);after['backend']['Mounts'][0][field]=value
   changes=T.stability_changes(before,after)
   self.assertEqual(changes[0]['changed_fields'],['mounts'])
   self.assertNotIn(SECRET,json.dumps(changes))
 def test_inspect_mount_multiplicity_is_not_discarded(self):
  before=deepcopy(self.rows);before['backend']['Mounts']=[{'Type':'bind','Source':'/data/logs','Destination':'/app/logs','RW':True}]
  after=deepcopy(before);after['backend']['Mounts']+=deepcopy(after['backend']['Mounts'])
  self.assertNotEqual(T.stable(before),T.stable(after))
 def test_stability_diagnostics_do_not_print_configuration_values(self):
  after=deepcopy(self.rows)
  after['backend']['Config']['Env'].append('PRIVATE='+SECRET)
  after['backend']['Config']['Cmd']=['echo',SECRET]
  after['backend']['Config']['Labels']['PRIVATE_LABEL']=SECRET
  after['backend']['HostConfig']={'Binds':['/private/'+SECRET+':/secret']}
  with self.assertRaises(T.StabilityError) as caught:T.ensure_stable(self.rows,after,'Expected test change')
  changes=caught.exception.changes
  self.assertEqual(changes[0]['changed_fields'],['container_configuration','host_configuration'])
  self.assertEqual(changes[0]['configuration_fields']['fields'],['Cmd','Env','Labels'])
  self.assertNotIn(SECRET,json.dumps(changes));self.assertNotIn('PRIVATE_LABEL',json.dumps(changes))
 def test_recreation_and_started_time_changes_are_not_ignored(self):
  for field in ('Id','StartedAt'):
   after=deepcopy(self.rows)
   if field=='Id':after['backend']['Id']='f'*64
   else:after['backend']['State']['StartedAt']='2026-11-03T04:00:00Z'
   with self.assertRaises(T.StabilityError) as caught:T.ensure_stable(self.rows,after,'Expected test change')
   self.assertEqual(caught.exception.changes[0]['changed_fields'],['container_identity'] if field=='Id' else ['started_at'])
 def test_environment_order_is_still_checked(self):
  after=deepcopy(self.rows);after['backend']['Config']['Env'].reverse()
  self.assertNotEqual(T.stable(self.rows),T.stable(after))
  self.assertEqual(T.stability_changes(self.rows,after)[0]['configuration_fields']['fields'],['Env'])
 def test_empty_network_mapping_representation_is_equivalent(self):
  a={'services':{'backend':{'networks':{'default':None}}}}
  for options in ({},{'aliases':[]}):
   b={'services':{'backend':{'networks':{'default':options}}}}
   self.assertEqual(T.model_differences(a,b),[])
   details=T.difference_details(a,b,{},self.requirements,self.values)
   self.assertEqual(details['representation_only_fields'],['services.backend.networks'])
 def test_real_network_changes_remain_visible(self):
  a={'services':{'backend':{'networks':{'default':None}}}}
  for options in ({'aliases':['different']},{'ipv4_address':'172.19.0.99'},{'priority':1}):
   b={'services':{'backend':{'networks':{'default':options}}}}
   self.assertEqual(T.model_differences(a,b),['services.backend.networks'])
 def test_default_volume_representation_is_equivalent(self):
  a={'services':{'backend':{'volumes':[{'type':'bind','source':'/data/logs','target':'/app/logs','read_only':False,'bind':{}}]}}}
  b=deepcopy(a);b['services']['backend']['volumes'][0].pop('read_only');b['services']['backend']['volumes'][0].pop('bind')
  self.assertEqual(T.model_differences(a,b),[])
  details=T.difference_details(a,b,{},self.requirements,self.values)
  self.assertEqual(details['representation_only_fields'],['services.backend.volumes'])
 def test_real_volume_policies_and_paths_remain_differences(self):
  a={'services':{'backend':{'volumes':[{'type':'bind','source':'/data/logs','target':'/app/logs'}]}}}
  for changed in ({'read_only':True},{'source':'/other/logs'},{'bind':{'create_host_path':False}},{'bind':{'propagation':'rshared'}}):
   b=deepcopy(a);b['services']['backend']['volumes'][0].update(changed)
   self.assertEqual(T.model_differences(a,b),['services.backend.volumes'])
 def test_numeric_token_rate_limit_is_not_a_secret_but_password_is(self):
  values=deepcopy(self.values)
  for ref,origin in self.requirements.items():
   if origin['environment_key']=='API_RATE_LIMIT_UI_V2_WITH_TOKEN':values[ref]='2'
  sensitive=T.sensitive_values(self.requirements,values)
  self.assertEqual(T.public_tuning_value('512MB',sensitive),'512MB')
  for ref,origin in self.requirements.items():
   if origin['service']=='db' and origin['environment_key']=='POSTGRES_PASSWORD':values[ref]='2'
  self.assertEqual(T.public_tuning_value('512MB',T.sensitive_values(self.requirements,values)),'[withheld]')
 def test_postgres_tuning_summary_withholds_unclassified_arguments(self):
  command=['postgres','-c','max_connections=800','-c','shared_buffers=16GB','-c','password='+SECRET,'--opaque='+SECRET]
  result=T.postgres_command_summary(command,{SECRET})
  self.assertEqual(result['parameters'],{'max_connections':'800','shared_buffers':'16GB'})
  self.assertEqual(result['unclassified_argument_count'],2)
  self.assertNotIn(SECRET,json.dumps(result));self.assertNotIn('password',json.dumps(result))
 def test_numeric_credentials_are_withheld_from_public_tuning(self):
  values=deepcopy(self.values)
  for ref,origin in self.requirements.items():
   if origin['environment_key']=='RELEASE_COOKIE':values[ref]='1234'
  sensitive=T.sensitive_values(self.requirements,values)
  result=T.postgres_command_summary(['postgres','-c','max_connections=1234'],sensitive)
  self.assertEqual(result['parameters']['max_connections'],'[withheld]')
  self.assertNotIn('1234',json.dumps(result))
 def test_volume_policy_details_never_export_source_paths(self):
  a={'volumes':[{'type':'bind','source':'/private/'+SECRET,'target':'/app/logs','bind':{'create_host_path':True}}]}
  b={'volumes':[{'type':'bind','source':'/different/'+SECRET,'target':'/app/logs'}]}
  result=T.volume_details(a,b,{},set())
  self.assertEqual(result['mounts'][0]['current_bind_options']['create_host_path'],True)
  self.assertFalse(result['mounts'][0]['same_source'])
  self.assertNotIn(SECRET,json.dumps(result))
 def test_database_details_include_shared_memory_and_safe_command(self):
  a={'services':{'db':{'command':['postgres','-c','max_connections=800'],'shm_size':'1073741824'}}}
  b={'services':{'db':{'command':['postgres','-c','max_connections=200'],'shm_size':'268435456'}}}
  rows={'db':{'HostConfig':{'ShmSize':1073741824}}}
  result=T.difference_details(a,b,rows,self.requirements,self.values)
  db=result['database']['db']
  self.assertEqual(db['current_shm_size'],'1073741824')
  self.assertEqual(db['live_shm_size_bytes'],'1073741824')
  self.assertEqual(db['current_command']['parameters']['max_connections'],'800')
  self.assertNotIn(SECRET,json.dumps(result))


class ComposeTests(ConfigTests):
 def test_real_profile_and_literal_private_value_round_trip(self):
  special='fixture $$literal $NOT_EXPANDED ${ALSO_NOT_EXPANDED} "quotes" and\nnewline'
  self.values['DEV_BACKEND_SECRET_KEY_BASE']=special
  with patch.dict(os.environ,{'DEV_BACKEND_SECRET_KEY_BASE':'ambient-wrong','NOT_EXPANDED':'wrong'}):
   model=T.managed_model(ROOT,self.manifest,self.values)
  self.assertEqual(len(model['services']),14)
  self.assertEqual(model['services']['backend']['environment']['SECRET_KEY_BASE'],special)
  for ref,origin in self.requirements.items():
   self.assertEqual(model['services'][origin['service']]['environment'][origin['environment_key']],self.values[ref])
  self.assertEqual(model['services']['backend']['environment']['CHAIN_ID'],'18417507517')
  self.assertEqual(model['services']['nft_media_handler']['networks']['default']['aliases'],['nft-media.blockscout.internal'])
  for name in ('backend','nft_media_handler'):
   self.assertEqual(model['services'][name]['build']['args']['CHAIN_TYPE'],'arbitrum')
  self.assertEqual(model['services']['backend']['volumes'][0]['source'],'/data/blockscout/docker-compose/services/logs')
  self.assertEqual(model['services']['db']['command'],self.profile['services']['db']['command'])
  self.assertEqual(str(model['services']['db']['shm_size']),'6442450944')
  self.assertNotIn('client_connection_check_interval',T.postgres_command_summary(model['services']['db']['command'],set())['parameters'])
 def test_database_contract_rejects_legacy_defaults(self):
  model=T.managed_model(ROOT,self.manifest,self.values)
  for field,value in (('command',['postgres','-c','max_connections=200']),('shm_size','268435456')):
   invalid=deepcopy(model);invalid['services']['db'][field]=value
   with patch.object(T,'compose_model',return_value=invalid):
    with self.assertRaises(T.ConfigError):T.managed_model(ROOT,self.manifest,self.values)
 def test_real_eight_to_two_layer_comparison(self):
  model=T.managed_model(ROOT,self.manifest,self.values)
  current=deepcopy(model)
  current['services']['backend']['environment'].pop('INDEXER_UNCATALOGED_TOKEN_TRANSFERS_ENABLED')
  # Escape a resolved model for a second Compose parse, just as Compose's config export does.
  self.paths[0].write_text(json.dumps(current).replace('$','$$'))
  for path in self.paths[1:]:path.write_text('{"services":{}}')
  rows=deepcopy(self.rows)
  rows['backend']['Config']['Env']=[v for v in rows['backend']['Config']['Env'] if not v.startswith('INDEXER_UNCATALOGED_TOKEN_TRANSFERS_ENABLED=')]
  rows['backend']['Mounts']=[{'Type':'bind','Source':'/data/logs','Destination':'/app/logs','RW':True},
   {'Type':'bind','Source':'/data/dets','Destination':'/app/dets','RW':True}]
  after=deepcopy(rows);after['backend']['Mounts'].reverse()
  with patch.object(T,'inspect_project',side_effect=[rows,after]),patch.object(T,'rpc_chain',return_value=18417507517):
   report=T.plan(ROOT,self.manifest,self.requirements,self.values,self.live_dir)
  self.assertEqual(report['current_compose_layers'],8);self.assertEqual(report['proposed_compose_layers'],2)
  self.assertEqual(report['changed_runtime_fields'],['services.backend.environment.INDEXER_UNCATALOGED_TOKEN_TRANSFERS_ENABLED'])
  self.assertEqual(report['live_declared_environment_drift'],[])
  self.assertTrue(report['startup_policy_change_expected']);self.assertNotIn(SECRET,json.dumps(report))
  self.assertEqual(report['inspection_order_only_changes'],['services.backend.inspect_mounts_order'])


if __name__=='__main__':
 suite=unittest.defaultTestLoader.loadTestsFromTestCase(ConfigTests)
 if '--compose' in sys.argv:
  suite.addTest(ComposeTests('test_real_profile_and_literal_private_value_round_trip'))
  suite.addTest(ComposeTests('test_real_eight_to_two_layer_comparison'))
  suite.addTest(ComposeTests('test_database_contract_rejects_legacy_defaults'))
 result=unittest.TextTestRunner(verbosity=2).run(suite)
 raise SystemExit(not result.wasSuccessful())
