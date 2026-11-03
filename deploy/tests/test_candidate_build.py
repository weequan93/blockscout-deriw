#!/usr/bin/env python3
"""Candidate build orchestration, private logging and optional archive/image integration."""
import contextlib
from copy import deepcopy
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
import uuid
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
def load(name,path):
 spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec)
 sys.modules[name]=module;spec.loader.exec_module(module);return module
B=load('candidate_build_fixture',ROOT/'deploy/scripts/candidate_build.py')
F=load('dev_fixture',ROOT/'deploy/tests/test_dev_config.py');T=F.T
sys.modules[T.__name__]=T
SECRET='fixture-private-candidate-Z91'
COMMIT='a'*40
IMAGE='sha256:'+'b'*64


class BuildTests(unittest.TestCase):
 def setUp(self):
  self.f=F.ConfigTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
  self.private=self.f.parent/'private-input.json'
  self.private.write_text('{}');self.private.chmod(0o600)
  self.state=self.f.parent/'state'
  self.report={'changed_runtime_fields':['services.backend.environment.'+B.FLAG],
   'live_declared_environment_drift':[],'live_images_differing_from_capture':[]}
  self.source={'source_commit':COMMIT,'release_version':'9.0.1','platform':'linux/amd64',
   'tag':'deriw-blockscout:dev-'+COMMIT[:12]+'-arbitrum-amd64','fingerprint':'c'*64,
   'build_arguments':{'CHAIN_TYPE':'arbitrum','RELEASE_VERSION':'9.0.1'},'baseline_runtime_user':'10001:10001'}
  self.image={'Id':IMAGE,'Os':'linux','Architecture':'amd64',
   'Config':{'User':'10001:10001','Labels':{B.LABEL:self.source['fingerprint'],'org.opencontainers.image.revision':COMMIT}}}
  self.environment={'CHAIN_ID':'18417507517','RELEASE_COOKIE':SECRET,'MULTILINE':'fixture\nline',B.FLAG:'false'}
 @contextlib.contextmanager
 def mocked(self,existing=None):
  with contextlib.ExitStack() as stack:
   mocks={}
   targets=[(T,'plan',{'return_value':self.report}),(T,'inspect_project',{'return_value':self.f.rows}),
    (T,'managed_model',{'return_value':{'services':{'backend':{'environment':self.environment}}}}),
    (T,'load_private',{'return_value':self.f.values}),(B,'source_plan',{'return_value':self.source}),
    (B,'clean_source',{'return_value':(COMMIT,'9.0.1')}),
    (B,'image_info',{'side_effect':[existing,self.image,self.image]}),
    (B,'compile_image',{}),(B,'smoke_image',{'return_value':[{'mode':v,'passed':True} for v in ('unset','false','true')]})]
   for module,name,options in targets:mocks[name]=stack.enter_context(patch.object(module,name,**options))
   yield mocks
 def build(self,**kwargs):
  return B.build_candidate(T,ROOT,self.f.manifest,self.f.requirements,self.f.values,self.private,self.f.live_dir,self.state,**kwargs)
 def test_check_only_has_no_build_smoke_or_persistent_state(self):
  with self.mocked() as mocks:report=self.build(check_only=True)
  self.assertEqual(report['status'],'candidate_build_plan_ready');self.assertFalse(self.state.exists())
  mocks['compile_image'].assert_not_called();mocks['smoke_image'].assert_not_called()
 def test_runtime_differences_block_before_build(self):
  self.report['changed_runtime_fields'].append('services.db.command')
  with self.mocked() as mocks:
   with self.assertRaises(T.ConfigError):self.build()
  mocks['source_plan'].assert_not_called();mocks['compile_image'].assert_not_called()
 def test_build_records_public_receipt_with_private_permissions(self):
  output=io.StringIO()
  with self.mocked() as mocks,contextlib.redirect_stdout(output):report=self.build()
  mocks['compile_image'].assert_called_once();mocks['smoke_image'].assert_called_once()
  self.assertEqual(report['image_id'],IMAGE);self.assertFalse(report['activation_performed'])
  path=Path(report['receipt']);self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
  self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode),0o700)
  self.assertNotIn(SECRET,path.read_text());self.assertNotIn(SECRET,output.getvalue())
 def test_build_failure_has_no_success_receipt(self):
  with self.mocked() as mocks,contextlib.redirect_stdout(io.StringIO()):
   mocks['compile_image'].side_effect=T.ConfigError('Fixture compile failure')
   with self.assertRaises(T.ConfigError):self.build()
  self.assertFalse(list(self.state.glob('*/candidate.json')))
 def test_smoke_failure_has_no_success_receipt(self):
  with self.mocked() as mocks,contextlib.redirect_stdout(io.StringIO()):
   mocks['smoke_image'].side_effect=T.ConfigError('Fixture smoke failure')
   with self.assertRaises(T.ConfigError):self.build()
  self.assertFalse(list(self.state.glob('*/candidate.json')))
 def test_existing_candidate_requires_explicit_reuse(self):
  with self.mocked(existing=self.image) as mocks:
   with self.assertRaises(T.ConfigError):self.build()
  mocks['compile_image'].assert_not_called();self.assertFalse(list(self.state.glob('candidate-*')))
 def test_concurrent_build_is_refused_and_lock_is_reusable(self):
  with B.build_lock(T,self.state,ROOT):
   with self.mocked() as mocks:
    with self.assertRaisesRegex(T.ConfigError,'Another dev candidate build'):self.build()
   mocks['plan'].assert_not_called();mocks['compile_image'].assert_not_called()
  with B.build_lock(T,self.state,ROOT):pass
  self.assertEqual(stat.S_IMODE((self.state/'build.lock').stat().st_mode),0o600)
 def test_symlinked_lock_is_refused(self):
  self.state.mkdir(mode=0o700)
  target=self.f.parent/'untouched';target.write_text('fixture')
  (self.state/'build.lock').symlink_to(target)
  with self.mocked() as mocks:
   with self.assertRaises(OSError):self.build()
  mocks['plan'].assert_not_called();self.assertEqual(target.read_text(),'fixture')
 def test_reuse_requires_matching_source_labels(self):
  image=deepcopy(self.image);image['Config']['Labels'][B.LABEL]='different'
  with self.mocked(existing=image) as mocks:
   with self.assertRaises(T.ConfigError):self.build(reuse=True)
  mocks['compile_image'].assert_not_called()
 def test_reuse_smokes_without_rebuilding(self):
  with self.mocked(existing=self.image) as mocks,contextlib.redirect_stdout(io.StringIO()):report=self.build(reuse=True)
  mocks['compile_image'].assert_not_called();mocks['smoke_image'].assert_called_once()
  self.assertEqual(report['status'],'candidate_verified')
 def test_source_change_prevents_acceptance(self):
  with self.mocked() as mocks,contextlib.redirect_stdout(io.StringIO()):
   mocks['clean_source'].return_value=('d'*40,'9.0.1')
   with self.assertRaises(T.ConfigError):self.build()
  self.assertFalse(list(self.state.glob('*/candidate.json')))
 def test_private_input_change_prevents_acceptance(self):
  with self.mocked() as mocks,contextlib.redirect_stdout(io.StringIO()):
   mocks['load_private'].return_value={'different':'private'}
   with self.assertRaises(T.ConfigError):self.build()
  self.assertFalse(list(self.state.glob('*/candidate.json')))
 def test_live_restart_prevents_acceptance(self):
  changed=deepcopy(self.f.rows);changed['backend']['RestartCount']=1
  with self.mocked() as mocks,contextlib.redirect_stdout(io.StringIO()):
   mocks['inspect_project'].side_effect=[self.f.rows,changed]
   with self.assertRaises(T.StabilityError):self.build()
  self.assertFalse(list(self.state.glob('*/candidate.json')))
 def test_tag_change_prevents_acceptance(self):
  changed=deepcopy(self.image);changed['Id']='sha256:'+'e'*64
  with self.mocked() as mocks,contextlib.redirect_stdout(io.StringIO()):
   mocks['image_info'].side_effect=[None,self.image,changed]
   with self.assertRaises(T.ConfigError):self.build()
  self.assertFalse(list(self.state.glob('*/candidate.json')))
 def test_state_inside_checkout_is_refused(self):
  self.state=ROOT/'must-not-create-build-state'
  with self.mocked() as mocks:
   with self.assertRaises(T.ConfigError):self.build(check_only=True)
  mocks['plan'].assert_not_called();self.assertFalse(self.state.exists())
 def test_clean_source_rejects_dirty_checkout(self):
  with patch.object(T,'run',side_effect=[str(ROOT)+'\n',' M config/runtime.exs\n']):
   with self.assertRaises(T.ConfigError):B.clean_source(T,ROOT)
 def test_source_plan_records_exact_commit_version_args_and_platform(self):
  baseline={'Id':IMAGE,'Os':'linux','Architecture':'amd64','Config':{'User':'10001:10001'}}
  model={'services':{'backend':{'build':{'args':{'CHAIN_TYPE':'arbitrum','RELEASE_VERSION':'9.0.1','API_V1_READ_METHODS_DISABLED':'false'}}}}}
  with patch.object(B,'clean_source',return_value=(COMMIT,'9.0.1')),patch.object(B,'image_info',return_value=baseline):
   plan=B.source_plan(T,ROOT,self.f.manifest,model,self.f.rows)
  self.assertEqual(plan['source_commit'],COMMIT);self.assertEqual(plan['platform'],'linux/amd64')
  self.assertEqual(plan['build_arguments']['RELEASE_VERSION'],'9.0.1');self.assertEqual(plan['build_arguments']['CHAIN_TYPE'],'arbitrum')
  self.assertNotIn(SECRET,json.dumps(plan))
 def test_smoke_environment_is_stdin_only_and_checks_three_modes(self):
  directory=self.f.parent/'smoke';directory.mkdir(mode=0o700)
  success=subprocess.CompletedProcess([],0,B.MARKER+'\n',SECRET)
  output=io.StringIO()
  with patch.object(B.subprocess,'run',return_value=success) as run,contextlib.redirect_stdout(output):
   checks=B.smoke_image(T,IMAGE,self.source,self.environment,directory)
  calls=run.call_args_list[::2];self.assertEqual(len(calls),3)
  for call,mode in zip(calls,('unset','false','true')):
   command=call.args[0];environment=json.loads(call.kwargs['input'])
   self.assertNotIn(SECRET,str(command));self.assertIn('--read-only',command)
   self.assertIn('--network',command);self.assertEqual(command[command.index('--network')+1],'none')
   self.assertEqual(environment['MULTILINE'],'fixture\nline');self.assertEqual(environment['RELEASE_DISTRIBUTION'],'none')
   self.assertEqual(environment.get(B.FLAG),None if mode=='unset' else mode)
  self.assertTrue(all(row['passed'] for row in checks));self.assertNotIn(SECRET,output.getvalue())
  for path in directory.iterdir():self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
 def test_smoke_failure_does_not_print_private_errors(self):
  directory=self.f.parent/'smoke';directory.mkdir(mode=0o700)
  failed=subprocess.CompletedProcess([],1,SECRET,SECRET)
  output=io.StringIO()
  with patch.object(B.subprocess,'run',return_value=failed) as run,contextlib.redirect_stdout(output):
   with self.assertRaises(T.ConfigError):B.smoke_image(T,IMAGE,self.source,self.environment,directory)
  self.assertNotIn(SECRET,output.getvalue());self.assertEqual(run.call_args_list[-1].args[0][:3],['docker','rm','-f'])
  self.assertIn(SECRET,(directory/'smoke-unset.log.private').read_text())
 def test_shell_private_values_round_trip_without_execution(self):
  self.assertIsNotNone(shutil.which('jq'))
  path=self.f.parent/'env.json';marker=self.f.parent/'must-not-exist'
  value='fixture $$dollar ${variable} "quotes"\nline; $(touch '+str(marker)+')'
  for mode in ('unset','false','true'):
   script=B.smoke_script(mode)
   syntax=subprocess.run(['sh','-n'],input=script,text=True,capture_output=True,timeout=5)
   self.assertEqual(syntax.returncode,0)
   prefix=script.split('rm /tmp/candidate-env.json\n',1)[0].replace('/tmp/candidate-env.json',shlex.quote(str(path)))
   result=subprocess.run(['sh','-c',prefix+'\nprintf \'%s\' "$FIXTURE_VALUE"\n'],input=json.dumps({'FIXTURE_VALUE':value}),text=True,capture_output=True,timeout=10)
   self.assertEqual(result.returncode,0);self.assertEqual(result.stdout,value);self.assertFalse(marker.exists())


class DockerArchiveTest(BuildTests):
 def test_real_git_archive_build_excludes_ignored_private_files(self):
  source_root=self.f.parent/'source';source_root.mkdir()
  (source_root/'docker').mkdir();(source_root/'docker/Dockerfile').write_text('FROM scratch\nCOPY . /fixture\n')
  (source_root/'proof.txt').write_text('public archive fixture\n')
  (source_root/'.gitignore').write_text('*.private.json\n')
  def git(*args):return subprocess.run(['git','-C',str(source_root),*args],capture_output=True,text=True,check=True,timeout=10).stdout
  git('init','--quiet');git('add','.gitignore','docker/Dockerfile','proof.txt')
  git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','-c','core.hooksPath=/dev/null','-c','commit.gpgsign=false','commit','--quiet','-m','fixture')
  (source_root/'host.private.json').write_text(SECRET)
  commit=git('rev-parse','HEAD').strip()
  source=dict(self.source,source_commit=commit,tag='deriw-blockscout:archive-fixture-'+uuid.uuid4().hex[:12])
  container=None
  try:
   B.compile_image(T,source_root,source,self.f.parent/'archive-build.log.private')
   info=B.image_info(T,source['tag']);self.assertEqual(info['Config']['Labels']['org.opencontainers.image.revision'],commit)
   container=subprocess.run(['docker','create','--network','none','--read-only',source['tag'],'/not-executed'],capture_output=True,text=True,check=True,timeout=20).stdout.strip()
   data=subprocess.run(['docker','export',container],capture_output=True,check=True,timeout=20).stdout
   with tarfile.open(fileobj=io.BytesIO(data)) as archive:names=archive.getnames()
   self.assertIn('fixture/proof.txt',names);self.assertNotIn('fixture/host.private.json',names)
   self.assertFalse(any(name.startswith('fixture/.git/') for name in names))
  finally:
   if container:subprocess.run(['docker','rm','-f',container],capture_output=True,timeout=20)
   subprocess.run(['docker','image','rm',source['tag']],capture_output=True,timeout=20)


if __name__=='__main__':
 suite=unittest.defaultTestLoader.loadTestsFromTestCase(BuildTests)
 if '--docker' in sys.argv:suite.addTest(DockerArchiveTest('test_real_git_archive_build_excludes_ignored_private_files'))
 result=unittest.TextTestRunner(verbosity=2).run(suite)
 raise SystemExit(not result.wasSuccessful())
