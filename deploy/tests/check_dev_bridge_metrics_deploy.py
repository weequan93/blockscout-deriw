#!/usr/bin/env python3
"""Exercise the dev metrics rollout with real Git/Compose and simulated containers.

The fixture checks private value round-tripping, committed-only build input,
backend-only replacement, image identity and preservation of database sentinels.
No Docker application containers are started and no remote repository is used.
"""


def main(catchup=False, fail_handoff=False):
    from copy import deepcopy
    import contextlib,importlib.util,io,json,os,subprocess,sys,tarfile,tempfile
    from pathlib import Path
    root=Path(__file__).resolve().parents[2]
    package=root/'deploy/scripts'
    spec=importlib.util.spec_from_file_location('native_deploy_flow_fixture',package/'deploy_dev_bridge_metrics.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    expected_revision=subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True).strip()
    sys.path.insert(0,str(root/'deploy/scripts'))
    import dev_config as D
    import release as R
    import deploy_dev_bridge_catchup as C
    C.M=m
    real_run=subprocess.run;real_popen=subprocess.Popen;real_euid=os.geteuid
    commands=[];built_names=[];deployed=False;seeded=False
    with tempfile.TemporaryDirectory(prefix='native-deploy-flow-') as temporary:
     base=Path(temporary);checkout=base/'blockscout'
     def git(*args):return real_run(['git','-C',str(checkout),*args],capture_output=True,text=True,check=True).stdout.strip()
     real_run(['git','clone','--quiet','--shared','--single-branch','--branch','deriw-dev',str(root),str(checkout)],check=True)
     m.ROOT=checkout;m.COMPOSE=checkout/'docker-compose.dev';m.COMPOSE.mkdir()
     m.PRIVATE=base/'private'/'deployments';m.NATIVE=base/'native-state.json'
     native_bytes='{"phase":"paused","fixture":"checkpoint"}';m.NATIVE.write_text(native_bytes)
     archive_dir=checkout/'old-repair-archive';archive_dir.mkdir();(archive_dir/'do-not-delete').write_text('archive')
     ignore=checkout/'.dockerignore';ignore.write_text(ignore.read_text()+'\n# fixture server-private excludes\ndocker-compose.dev\n')
     before=json.loads((root/'docker-compose/environments/dev/compose.json').read_text().replace('/data/blockscout',str(checkout)))
     before['name']='docker-compose'
     for init in ['db-init','stats-db-init']:before['services'].pop(init)
     for service in before['services'].values():
      for init in ['db-init','stats-db-init']:service.get('depends_on',{}).pop(init,None)
      service.pop('build',None)
      for mount in service.get('volumes',[]):
       if mount.get('type')=='bind':
        mount['source']=mount['source'].replace(str(checkout/'docker-compose'),str(m.COMPOSE),1)
        Path(mount['source']).mkdir(parents=True,exist_ok=True)
     b=before['services']['backend'];b['command']=['bin/blockscout','start'];b['platform']='linux/amd64'
     b['environment'].update(MIGRATION_BACKGROUND_AUTOSTART_DISABLED='true',INDEXER_METRICS_ENABLED='false',INDEXER_DISABLE_EMPTY_BLOCKS_SANITIZER='true')
     b['environment']['BLOCKSCOUT_VERSION']='1'*40
     secret='example$literal'+'$'+'{do_not_expand}'+'$$pair\nsecond-line';b['environment']['FIXTURE_SECRET']=secret
     b['build']={'context':str(checkout),'dockerfile':'docker/Dockerfile','args':{'CHAIN_TYPE':'arbitrum','RELEASE_VERSION':'10.2.6','MIGRATION_BACKGROUND_AUTOSTART_DISABLED':'true'}}
     config=m.COMPOSE/'docker-compose.yml';config.write_bytes(R.snapshot_bytes(before))
     (m.COMPOSE/'.env').write_text('COMPOSE_PROJECT_NAME=docker-compose\n')
     data=m.COMPOSE/'services/blockscout-db-data'/'do-not-change';data.write_text('database sentinel')
     ids={name:format(i+1,'064x') for i,name in enumerate(before['services'])}
     def metadata():
      current=D.compose_model(m.COMPOSE,{'compose_project':'docker-compose'},{},[config],True) if deployed else before
      return [{'Id':('f'*64 if deployed and name=='backend' else ident),'Image':'sha256:'+'a'*64,'Config':{'Labels':{'com.docker.compose.service':name},'Env':[k+'='+str(v) for k,v in current['services'][name].get('environment',{}).items()]},'State':{'Running':True}} for name,ident in ids.items()]
     def fake_run(command,*args,**kwargs):
      nonlocal deployed,seeded
      commands.append(command)
      if command[:3]==['docker','ps','-aq']:return subprocess.CompletedProcess(command,0,'\n'.join(ids.values()),'')
      if command[:2]==['docker','inspect']:return subprocess.CompletedProcess(command,0,json.dumps(metadata()),'')
      if command[:2]==['docker','run']:
       assert '--network' in command and 'none' in command
       return subprocess.CompletedProcess(command,0,'NATIVE_METRICS_IMAGE_OK\n','')
      if command[:2]==['docker','compose'] and 'up' in command:
       assert command[-1]=='backend' and '--no-deps' in command and '--no-build' in command
       assert kwargs['timeout']==360
       if catchup:assert seeded
       deployed=True;return subprocess.CompletedProcess(command,0,'','')
      if catchup and command[:5]==['docker','exec','backend','bin/blockscout','rpc']:
       if 'INSERT INTO' in command[-1]:
        assert not deployed
        if fail_handoff:return subprocess.CompletedProcess(command,1,'','synthetic handoff failure')
        seeded=True
        handoff={'common_range':50,'cursors':[
          {'tracker':'deposits','counter_key':'fixture-deposits','live_next_parent_block':280421247,'saved_next_parent_block':280421247,'interval_ms':10000},
          {'tracker':'withdrawals','counter_key':'fixture-withdrawals','live_next_parent_block':324655961,'saved_next_parent_block':324655961,'interval_ms':3000}]}
        return subprocess.CompletedProcess(command,0,C.MARKER+json.dumps(handoff)+'\n','')
       assert seeded and deployed
       assert 'state.config[key_field]' in command[-1]
       return subprocess.CompletedProcess(command,0,C.MARKER+json.dumps({'cursors':[],'resumed_from_handoff':True})+'\n','')
      if command[:3]==['docker','exec','backend']:
       body='deriw_bridge_exporter_up 1\n'+''.join('deriw_bridge_query_success{section="'+s+'"} 1\n' for s in m.SECTIONS)+''.join('deriw_bridge_verification_success{section="'+s+'"} 1\n' for s in m.VERIFICATION)
       return subprocess.CompletedProcess(command,0,body,'')
      assert command[0] in ['git','docker']
      if command[0]=='docker':assert command[1]=='compose' and 'config' in command
      return real_run(command,*args,**kwargs)
     class FakeBuild:
      def __init__(self,stdin):self.stream=os.fdopen(os.dup(stdin.fileno()),'rb')
      def wait(self):
       with self.stream as stream:
        with tarfile.open(fileobj=stream,mode='r|') as archive:
         for member in archive:built_names.append(member.name)
       return 0
     def fake_popen(command,*args,**kwargs):
      if command[:2]==['docker','build']:
       commands.append(command);return FakeBuild(kwargs['stdin'])
      return real_popen(command,*args,**kwargs)
     subprocess.run=fake_run;subprocess.Popen=fake_popen;os.geteuid=lambda:0
     try:
      options=dict(prepare=C.prepare_model,before_replace=C.before_replace,after_ready=C.after_ready,
        success_phase='bridge_catchup_deployed') if catchup else {}
      with contextlib.redirect_stdout(io.StringIO()) as output,contextlib.redirect_stderr(io.StringIO()) as errors:result=m.main(**options)
      if fail_handoff:assert result==1
      elif result:
       logs=list(m.PRIVATE.glob('*/error.private.log'))
       print(logs[-1].read_text() if logs else errors.getvalue());raise SystemExit(1)
     finally:subprocess.run=real_run;subprocess.Popen=real_popen;os.geteuid=real_euid
     if fail_handoff:
      assert not seeded and not deployed
      assert config.read_bytes()==R.snapshot_bytes(before)
      assert not (m.COMPOSE/'.native-metrics-prepared.yml').exists()
      assert data.read_text()=='database sentinel' and m.NATIVE.read_text()==native_bytes
      print(json.dumps({'failed_handoff_prevents_recreation':True,'original_compose_preserved':True,'staged_file_removed':True}))
      return
     after=D.compose_model(m.COMPOSE,{'compose_project':'docker-compose'},{},[config],True)
     normalized_before=D.compose_model(m.COMPOSE,{'compose_project':'docker-compose'},{},[next(m.PRIVATE.glob('*/before-docker-compose.yml'))],True)
     assert all(s==normalized_before['services'][n] for n,s in after['services'].items() if n not in ('backend','nft_media_handler'))
     assert after['services']['backend']['environment']['FIXTURE_SECRET']==secret
     assert after['services']['nft_media_handler']['image']=='sha256:'+'a'*64
     assert 'build' not in after['services']['nft_media_handler']
     assert after['services']['nft_media_handler']['environment']==normalized_before['services']['nft_media_handler']['environment']
     assert git('branch','--show-current')=='deriw-dev' and git('rev-parse','HEAD')==expected_revision
     assert data.read_text()=='database sentinel' and (archive_dir/'do-not-delete').exists()
     assert m.NATIVE.read_text()==native_bytes and '# fixture server-private excludes' in ignore.read_text()
     assert all(not n.startswith(('docker-compose.dev/','old-repair-archive/')) for n in built_names)
     assert config.stat().st_mode & 0o777==0o600
     assert all(c[0]!='systemctl' for c in commands)
     if catchup:
      assert seeded
      assert all(after['services']['backend']['environment'][key]=='10000' for key in (C.DEPOSITS,C.WITHDRAWALS))
      assert (next(m.PRIVATE.glob('*/bridge-cursor-handoff.json'))).is_file()
      assert (next(m.PRIVATE.glob('*/bridge-cursor-resumed.json'))).is_file()
      assert 'bridge_catchup_deployed' in output.getvalue()
     print(json.dumps({'git_source_deployment_simulation':'passed','committed_source_identity':'passed','only_backend_recreated':True,'other_services_and_private_values':'preserved','private_data_excluded_from_build_archive':True,'normalization_checkpoint':'unchanged','real_containers_started':False,'bridge_cursor_handoff_before_recreation':catchup}))


if __name__ == '__main__':
    main()
