#!/usr/bin/env python3
"""Stats proxy repair, secret-safe output and optional actual Nginx routing fixture."""
import contextlib
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from test_deploy_check import load,ROOT,B,T,SECRET,EvidenceTests
R=load('proxy_release_fixture',ROOT/'deploy/scripts/release.py')
P=load('proxy_stats_fixture',ROOT/'deploy/scripts/proxy_stats.py')
ORIGINAL=(ROOT/'docker-compose/proxy/default.conf.template').read_text().replace(P.BLOCK,'',1)


class ProxyTests(unittest.TestCase):
 def setUp(self):
  self.e=EvidenceTests();self.e.setUp();self.addCleanup(self.e.doCleanups)
  self.parent=self.e.f.f.parent;self.root=self.parent/'private'
  self.template=self.parent/'templates/default.conf.template';self.template.parent.mkdir()
  self.original=ORIGINAL+'\n# private '+SECRET+'\n';self.template.write_text(self.original)
  self.runtime=self.original.replace('${FRONT_PROXY_PASS}','http://frontend:3000').replace('${BACK_PROXY_PASS}','http://backend:4000')
  self.original_runtime=self.runtime;self.active=self.runtime;self.rows=deepcopy(self.e.rows)
  row=self.rows['proxy'];row['Config']['Cmd']=['nginx','-g','daemon off;']
  row['Config']['Env']=['FRONT_PROXY_PASS=http://frontend:3000','BACK_PROXY_PASS=http://backend:4000','PRIVATE='+SECRET]
  row['Mounts']=[{'Destination':'/etc/nginx/templates','Source':str(self.template.parent),'RW':True,'Type':'bind'}]
  self.reloads=0;self.fail_test=False;self.bad_route=False
 def runner(self,command,**kwargs):
  if command[:4]==['docker','exec','proxy','nginx']:
   if command[-1]=='-T':return '# configuration file '+P.RUNTIME+':\n'+self.runtime
   if command[-1]=='-t':
    if self.fail_test:self.fail_test=False;raise T.ConfigError('fixture syntax error')
    return ''
   if command[-1]=='reload':self.reloads+=1;self.active=self.runtime;return ''
  if command[:4]==['docker','exec','proxy','sh']:return self.runtime
  if command[:4]==['docker','exec','-i','proxy']:self.runtime=kwargs['input_text'];return ''
  if command[:4]==['docker','exec','backend','curl']:
   url=command[-1]
   if url.startswith('https:'):raise T.ConfigError('fixture expired cert')
   ready=url.startswith('http://stats:') or ':8080/' in url or '/api/v2/stats' in url or (not P.patch(T,self.active,'http://frontend:3000')[1]['changed'] and not self.bad_route)
   return json.dumps({'counter':SECRET})+'\n200' if ready else '\n404'
  raise AssertionError('Unexpected command')
 @contextlib.contextmanager
 def mocked(self):
  actual_read=T.read_json
  profile={'services':{'proxy':{'volumes':[{'source':str(self.template.parent),'target':'/etc/nginx/templates'}]}}}
  with patch.object(T,'MANAGED_RELEASES',self.root),patch.object(T,'inspect_project',side_effect=lambda *a,**k:deepcopy(self.rows)),\
   patch.object(T,'read_json',side_effect=lambda path:profile if path.name=='compose.json' else actual_read(path)),\
   patch.object(T,'run',side_effect=self.runner),patch.object(P.time,'sleep'),\
   patch.object(P,'certificate',return_value={'status':'read','expired':True}):yield
 def test_adds_only_http_route_and_is_idempotent(self):
  updated,action=P.patch(T,self.original,'http://frontend:3000')
  self.assertEqual(action['action'],'add_http_stats_location');self.assertEqual(updated.replace(P.BLOCK,'',1),self.original)
  self.assertEqual(P.patch(T,updated,'http://frontend:3000')[0],updated)
  self.assertEqual(P.patch(T,(ROOT/'docker-compose/proxy/default.conf.template').read_text(),'http://frontend:3000')[1]['changed'],False)
 def test_preserves_custom_headers_and_corrects_missing_slash(self):
  text=self.original.replace('    proxy_http_version 1.1;','    location /statsapi/ { proxy_pass http://stats:8050; proxy_set_header X-Custom "'+SECRET+'"; }\n    proxy_http_version 1.1;',1)
  updated,action=P.patch(T,text,'http://frontend:3000')
  self.assertEqual(action['action'],'strip_stats_prefix');self.assertIn('X-Custom "'+SECRET+'"',updated)
 def test_does_not_overwrite_unknown_upstream_or_ambiguous_server(self):
  for text in (self.original+'\nserver { listen 80; location / { proxy_pass http://frontend:3000; } }',
   self.original.replace('    proxy_http_version 1.1;','    location /statsapi/ { proxy_pass http://other-service:8050/; }\n    proxy_http_version 1.1;',1)):
   with self.assertRaises(T.ConfigError):P.patch(T,text,'http://frontend:3000')
 def test_read_only_plan_and_output_never_export_private_config(self):
  with self.mocked():result=P.apply(T,B,R,ROOT,self.e.manifest)
  self.assertEqual(result['status'],'proxy_stats_plan_ready');self.assertFalse(self.root.exists());self.assertEqual(self.reloads,0)
  self.assertNotIn(SECRET,json.dumps(result));self.assertEqual(self.template.read_text(),self.original)
  self.assertEqual(self.runtime,self.original_runtime)
 def test_apply_and_restore_only_proxy_files_without_container_recreation(self):
  output=io.StringIO()
  with self.mocked(),contextlib.redirect_stdout(output):
   result=P.apply(T,B,R,ROOT,self.e.manifest,True);path=Path(result['state'])
   self.assertEqual(result['status'],'proxy_stats_verified');self.assertEqual(self.reloads,1)
   self.assertEqual(os.stat(path).st_mode&0o777,0o600)
   restored=P.restore(T,B,R,ROOT,self.e.manifest,path)
  self.assertEqual(restored['status'],'proxy_stats_restored');self.assertEqual(self.reloads,2)
  self.assertEqual(self.template.read_text(),self.original);self.assertEqual(self.runtime,self.original_runtime)
  self.assertNotIn(SECRET,output.getvalue());self.assertNotIn(SECRET,json.dumps(result))
 def test_failed_config_test_restores_before_reload(self):
  with self.mocked(),contextlib.redirect_stdout(io.StringIO()):
   self.fail_test=True
   with self.assertRaises(T.ConfigError):P.apply(T,B,R,ROOT,self.e.manifest,True)
  self.assertEqual(self.reloads,0);self.assertEqual(self.template.read_text(),self.original)
  self.assertEqual(self.runtime,self.original_runtime)
  state=T.read_json(next(self.root.glob('proxy-stats-*/state.json')));self.assertEqual(state['status'],'restored_after_failed_apply')
 def test_failed_post_reload_route_is_reverted(self):
  with self.mocked(),contextlib.redirect_stdout(io.StringIO()):
   self.bad_route=True
   with self.assertRaises(T.ConfigError):P.apply(T,B,R,ROOT,self.e.manifest,True)
  self.assertEqual(self.reloads,2);self.assertEqual(self.runtime,self.original_runtime);self.assertEqual(self.template.read_text(),self.original)
 def test_partial_known_state_can_be_restored_after_interruption(self):
  with self.mocked(),contextlib.redirect_stdout(io.StringIO()):
   result=P.apply(T,B,R,ROOT,self.e.manifest,True)
   self.template.write_text(self.original)
   restored=P.restore(T,B,R,ROOT,self.e.manifest,Path(result['state']))
  self.assertEqual(restored['status'],'proxy_stats_restored');self.assertEqual(self.runtime,self.original_runtime)
 def test_later_edits_are_not_overwritten_by_restore(self):
  with self.mocked(),contextlib.redirect_stdout(io.StringIO()):
   result=P.apply(T,B,R,ROOT,self.e.manifest,True);self.template.write_text(self.template.read_text()+'# later edit\n')
   with self.assertRaises(T.ConfigError):P.restore(T,B,R,ROOT,self.e.manifest,Path(result['state']))
  self.assertEqual(self.reloads,1)
 def test_facts_do_not_export_other_upstreams_or_headers(self):
  text=self.runtime.replace('http://frontend:3000','http://user:'+SECRET+'@private.example/').replace('http://backend:4000','http://'+SECRET+'.internal/')
  self.assertNotIn(SECRET,json.dumps(P.route_facts(T,text)))


class NginxTests(unittest.TestCase):
 def test_real_http_route_uri_query_and_other_routes_survive_reload(self):
  image='nginx@sha256:c35662d99b40137247cf7913871e97348a0524753a6925e09f7a5320b0b2f78b'
  def docker(*args,data=None):return subprocess.run(['docker',*args],input=data,capture_output=True,check=True,timeout=30).stdout
  with tempfile.TemporaryDirectory() as folder:
   folder=Path(folder)
   subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(folder/'key.pem'),
    '-out',str(folder/'cert.pem'),'-subj','/CN=explorer.dev.deriw.com','-days','1'],capture_output=True,check=True,timeout=15)
   original=ORIGINAL.replace('${BACK_PROXY_PASS}','http://127.0.0.1:4000').replace('${FRONT_PROXY_PASS}','http://127.0.0.1:3000')
   for name,target in (('fullchain.pem','cert.pem'),('chain.pem','cert.pem'),('privkey.pem','key.pem')):
    original=original.replace('/etc/nginx/templates/ssl/dev.deriw.com/'+name,'/work/'+target)
   updated,_=P.patch(T,original,'http://127.0.0.1:3000')
   prefix='pid /work/nginx.pid; error_log /work/error.log notice; events {} http { access_log off; client_body_temp_path /work/body; proxy_temp_path /work/proxy;\n'
   suffix='''
server { listen 127.0.0.1:8050; location = /api/v1/pages/main { default_type application/json; return 200 '{"uri":"$request_uri","host":"$http_host"}'; } location / { return 404; } }
server { listen 127.0.0.1:3000; location / { return 200 'frontend'; } }
server { listen 127.0.0.1:4000; location / { default_type application/json; return 200 '{"backend":true}'; } }
}
'''
   files={'nginx.conf':(prefix+original+suffix).encode(),'new.conf':(prefix+updated+suffix).encode(),
    'cert.pem':(folder/'cert.pem').read_bytes(),'key.pem':(folder/'key.pem').read_bytes()}
   archive=io.BytesIO()
   with tarfile.open(fileobj=archive,mode='w') as tar:
    for name,raw in files.items():
     entry=tarfile.TarInfo(name);entry.mode=0o600;entry.size=len(raw);tar.addfile(entry,io.BytesIO(raw))
   container=None
   try:
    container=docker('run','--rm','-d','--network','none','--read-only','--tmpfs','/work:rw,exec,size=16m',
     '--tmpfs','/var/cache/nginx:rw,size=8m','--add-host','stats:127.0.0.1','--add-host','visualizer:127.0.0.1',
     '--entrypoint','sh',image,'-c','while :; do sleep 1; done').decode().strip()
    docker('exec','-i',container,'tar','xf','-','-C','/work',data=archive.getvalue())
    docker('exec',container,'nginx','-t','-p','/work/','-c','/work/nginx.conf')
    docker('exec',container,'nginx','-p','/work/','-c','/work/nginx.conf')
    def request(path,port=80):
     return docker('exec',container,'curl','-q','--noproxy','*','-sS','--max-time','3','-H','Host: explorer.dev.deriw.com',
      '-w','\n%{http_code}','http://127.0.0.1:'+str(port)+path).decode()
    # Before the fix, the HTTP origin sends stats requests to the frontend.
    self.assertEqual(request('/statsapi/api/v1/pages/main'),'frontend\n200')
    docker('exec',container,'cp','/work/new.conf','/work/nginx.conf')
    docker('exec',container,'nginx','-t','-p','/work/','-c','/work/nginx.conf')
    docker('exec',container,'nginx','-s','reload','-p','/work/','-c','/work/nginx.conf')
    import time
    for _ in range(20):
     response=request('/statsapi/api/v1/pages/main?view=compact')
     if response.startswith('{'):break
     time.sleep(.1)
    body,status=response.rsplit('\n',1);self.assertEqual(status,'200')
    self.assertEqual(json.loads(body),{'uri':'/api/v1/pages/main?view=compact','host':'explorer.dev.deriw.com'})
    self.assertEqual(request('/'),'frontend\n200');self.assertEqual(request('/api/v2/stats'),'{"backend":true}\n200')
    self.assertEqual(json.loads(request('/api/v1/pages/main',8080).rsplit('\n',1)[0])['uri'],'/api/v1/pages/main')
   finally:
    if container:docker('rm','-f',container)


if __name__=='__main__':
 suite=unittest.defaultTestLoader.loadTestsFromTestCase(ProxyTests)
 if '--docker' in sys.argv:suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(NginxTests))
 result=unittest.TextTestRunner(verbosity=2).run(suite)
 raise SystemExit(not result.wasSuccessful())
