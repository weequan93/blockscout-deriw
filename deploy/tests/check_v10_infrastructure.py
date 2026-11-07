#!/usr/bin/env python3
"""Real patch-image fixtures. Only labeled local containers/volumes and synthetic data."""
import argparse
from copy import deepcopy
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import types
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import dev_config as D
import deploy_check as C
import v10_upgrade as U
import v10_infrastructure_checks as K
import v10_infrastructure_deploy as I
import v10_infrastructure_stage as S
import v10_frontend_deploy as F
import check_upgrade_database as G

OLD_PG = 'postgres@sha256:2c64f6d1be50ea37f722d27b936abc6557e5fb95c995514bd9c43490ef8160ef'
OLD_REDIS = 'redis@sha256:cd5f3ac681c77791c6a8eaa62de876ad2be043ee5a428afb7c0095aa08246277'
OLD_NGINX = 'nginx@sha256:4a2d27f57e72adbc1e1cfed8db6cbdef22c080e058565f92647f7aad258292f2'


class Fixture:
    def __init__(self):
        os.umask(0o077)
        self.token = uuid.uuid4().hex[:16]
        self.directory = Path(tempfile.mkdtemp(prefix='v10-infrastructure-', dir='/private/tmp')).resolve()
        self.network = 'infra-fixture-' + self.token
        self.created_network = False
        self.volumes = []
        self.processes = []
        self.password = uuid.uuid4().hex
        self.pg = 'infra-pg-' + self.token
        self.project = 'infra-' + self.token
        self.native_names = []

    def run(self, command, input=None, timeout=180):
        result = subprocess.run(command, input=input, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            path = self.directory / ('error-' + uuid.uuid4().hex[:8] + '.log.private')
            path.write_text(result.stdout + result.stderr)
            raise RuntimeError('Disposable fixture command failed; private log: ' + str(path))
        return result.stdout

    def setup(self):
        endpoint = self.run(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}']).strip()
        assert endpoint.startswith('unix://'), 'This fixture requires local Docker'
        networks = self.run(['docker', 'network', 'ls', '-q']).split()
        metadata = json.loads(self.run(['docker', 'network', 'inspect', *networks]))
        allocated = [ipaddress.ip_network(row['Subnet']) for n in metadata for row in n.get('IPAM', {}).get('Config') or [] if row.get('Subnet')]
        for index in range(1, 254):
            subnet = ipaddress.ip_network('10.253.' + str(index) + '.0/24')
            if not any(n.version == subnet.version and n.overlaps(subnet) for n in allocated):
                self.subnet = subnet
                self.pg_ip = str(subnet.network_address + 10)
                break
        self.run(['docker', 'network', 'create', '--internal', '--subnet', str(self.subnet),
                  '--label', G.LABEL + '=' + self.token, self.network])
        self.created_network = True

    def volume(self, purpose):
        name = 'infra-' + purpose + '-' + self.token
        self.run(['docker', 'volume', 'create', '--label', G.LABEL + '=' + self.token, name])
        self.volumes.append(name)
        return name

    def seed_files(self, volume, files):
        # Docker Desktop need not share the agent's temporary filesystem. Stream
        # synthetic config files into an owned volume and bind only that volume's
        # daemon-side paths. No user files or unrelated daemon paths are mounted.
        stream=io.BytesIO()
        with tarfile.open(fileobj=stream,mode='w') as archive:
            for file in files:
                archive.add(file,arcname=str(file.relative_to(self.directory)),recursive=False)
        command=['docker','run','--rm','--pull','never','-i','--label',G.LABEL+'='+self.token,
            '--network','none','--mount','type=volume,source='+volume+',target=/seed','--entrypoint','sh',OLD_NGINX,
            '-ec','tar -xf - -C /seed']
        result=subprocess.run(command,input=stream.getvalue(),capture_output=True,timeout=30)
        if result.returncode:
            log=self.directory/'seed-files.log.private'
            log.write_bytes(result.stdout+result.stderr)
            raise RuntimeError('Synthetic configuration seed failed; private log: '+str(log))
        metadata=json.loads(self.run(['docker','volume','inspect',volume]))[0]
        assert metadata.get('Labels',{}).get(G.LABEL)==self.token
        return Path(metadata['Mountpoint'])

    def sql(self, text):
        return self.run(['docker', 'exec', '-i', self.pg, 'psql', '-X', '-U', 'fixture', '-d', 'fixture',
                         '-v', 'ON_ERROR_STOP=1', '-At'], input=text)

    def pg_start(self, reference, volume, guard=False):
        image = json.loads(self.run(['docker', 'image', 'inspect', reference]))[0]['Id']
        service = {'image': image, 'container_name': self.pg, 'user': '2000:2000',
            'labels': {G.LABEL: self.token}, 'read_only': True, 'mem_limit': '1g', 'cpus': 2,
            'environment': {'POSTGRES_USER': 'fixture', 'POSTGRES_PASSWORD': self.password, 'POSTGRES_DB': 'fixture'},
            'volumes': [{'type':'volume','source':'data','target':'/var/lib/postgresql/data','volume':{'nocopy':True}}],
            'tmpfs': ['/var/run/postgresql:rw,mode=1777,size=16m', '/tmp:rw,mode=1777,size=16m'],
            'networks': {'default': {'ipv4_address': self.pg_ip, 'aliases': ['pg']}},
            'command': ['postgres','-c','shared_buffers=32MB','-c','max_connections=30']}
        if guard:
            service['entrypoint'] = K.PG_GUARD
        model = {'services': {'pg': service}, 'networks': {'default': {'external':True,'name':self.network}},
                 'volumes': {'data': {'external':True,'name':volume}}}
        filename = self.directory / ('pg-' + uuid.uuid4().hex[:8] + '.compose.json')
        filename.write_bytes(U.R.snapshot_bytes(model))
        self.run(['docker','compose','-p',self.project,'--project-directory',str(self.directory),
                  '--env-file',os.devnull,'-f',str(filename),'up','-d','--no-deps','--no-build','--pull','never','pg'])
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            check = subprocess.run(['docker','exec',self.pg,'pg_isready','-q','-h','127.0.0.1','-U','fixture','-d','fixture'],capture_output=True)
            if check.returncode == 0:
                break
            metadata = json.loads(self.run(['docker', 'inspect', self.pg]))[0]
            if not metadata['State']['Running']:
                log = self.private_logs(self.pg)
                raise RuntimeError('Synthetic PostgreSQL exited; private log: ' + str(log))
            time.sleep(0.2)
        else:
            log = self.private_logs(self.pg)
            raise RuntimeError('Synthetic PostgreSQL did not become ready; private log: ' + str(log))
        assert json.loads(self.run(['docker','inspect',self.pg]))[0]['NetworkSettings']['Networks'][self.network]['IPAddress'] == self.pg_ip

    def private_logs(self, name):
        result = subprocess.run(['docker', 'logs', '--tail', '100', name], capture_output=True, text=True, timeout=10)
        path = self.directory / ('container-' + uuid.uuid4().hex[:8] + '.log.private')
        path.write_text(result.stdout + result.stderr)
        return path

    def pg_stop(self):
        self.run(['docker','stop','--time','60',self.pg])
        state = json.loads(self.run(['docker','inspect',self.pg]))[0]['State']
        assert state['Running'] is False and state['ExitCode'] == 0

    def native_start(self, image, environment, payload):
        name = 'infra-native-' + uuid.uuid4().hex[:16]
        self.native_names.append(name)
        command = U.client_command(image, self.network, name, self.token)
        command[2:2] = ['--label',G.LABEL+'='+self.token]
        log = (self.directory / (name + '.log.private')).open('wb')
        process = subprocess.Popen(command,stdin=subprocess.PIPE,stdout=log,stderr=log)
        self.processes.append((process,log))
        original = (ROOT/'deploy/scripts/v10/native_normalization.exs').read_bytes()
        process.stdin.write(U.archive(environment,payload,original))
        process.stdin.close()
        deadline = time.monotonic()+30
        while time.monotonic()<deadline:
            if process.poll() is not None:
                raise RuntimeError('Native fixture exited; private log: '+str(log.name))
            status = subprocess.run(['docker','exec',name,'cat','/tmp/native-status.json'],capture_output=True,text=True)
            if status.returncode==0:
                result=json.loads(status.stdout)
                if result['phase']=='needs_attention':
                    raise RuntimeError('Native fixture stopped: '+str(result.get('reason'))+'; '+str(log.name))
                return name
            time.sleep(0.2)
        raise RuntimeError('Native fixture initialization timed out')

    def native_stop(self,name):
        G.cleanup_container(name,self.token)

    def grant(self,name):
        data={'token':self.token,'mode':'run','valid_until_unix_ms':int(time.time()*1000)+30000,'batch_blocks':1}
        self.run(['docker','exec','-i',name,'sh','-c','umask 077; cat > /tmp/native-lease.json'],input=json.dumps(data))

    def client_start(self, kind, image, environment):
        name='infra-client-'+uuid.uuid4().hex[:16]
        common='for app <- [:logger,:crypto,:ssl,:postgrex,:ecto_sql,:redix], do: ({:ok,_}=Application.ensure_all_started(app))\n'
        if kind=='postgresql':
            common+='{:ok,_}=Explorer.Repo.start_link(pool_size: 2,log: false)\n'
            observation='''
try do
  [[version]]=Explorer.Repo.query!("SELECT current_setting('server_version_num')::integer",[],log: false,timeout: 1000).rows
  %{ready: true, version: version}
rescue
  _ -> %{ready: false}
catch
  _,_ -> %{ready: false}
end
'''
        else:
            common+='{:ok,redis}=Redix.start_link(host: "redis",port: 6379,backoff_initial: 100,backoff_max: 1000)\n'
            observation='''
case Redix.command(redis,["PING"],timeout: 1000) do
  {:ok,"PONG"} -> %{ready: true}
  _ -> %{ready: false}
end
'''
        source=common+'''
loop=fn again ->
  state = '''+observation+'''
  File.write!("/tmp/infra-client-status.json.tmp",Jason.encode!(Map.put(state,:pid,inspect(self()))))
  File.rename!("/tmp/infra-client-status.json.tmp","/tmp/infra-client-status.json")
  Process.sleep(200)
  again.(again)
end
loop.(loop)
'''
        command=U.client_command(image,self.network,name,self.token)
        command[2:2]=['--label',G.LABEL+'='+self.token]
        log=(self.directory/(name+'.log.private')).open('wb')
        process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=log,stderr=log)
        self.processes.append((process,log))
        process.stdin.write(U.archive(environment,{},source.encode()))
        process.stdin.close()
        self.wait(lambda:self.client_status(name).get('ready') is True,'Long-lived client did not connect')
        return name

    def client_status(self,name):
        result=subprocess.run(['docker','exec',name,'cat','/tmp/infra-client-status.json'],capture_output=True,text=True)
        return json.loads(result.stdout) if result.returncode==0 else {}

    def wait(self, condition, message, seconds=30):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            if condition(): return
            time.sleep(0.2)
        raise RuntimeError(message)

    def postgres(self):
        empty=self.volume('pg-empty')
        candidate=K.catalog()['postgresql']['reference']
        guarded=subprocess.run(['docker','run','--rm','--pull','never','--network','none',
            '--label',G.LABEL+'='+self.token,'--mount','type=volume,source='+empty+',target=/var/lib/postgresql/data',
            '--entrypoint','sh',candidate,*K.PG_GUARD[1:],'postgres'],capture_output=True,timeout=30)
        assert guarded.returncode==73, 'Missing PG15 data must never initialize a new cluster'
        self.run(['docker','run','--rm','--pull','never','--network','none',
            '--label',G.LABEL+'='+self.token,'--mount','type=volume,source='+empty+',target=/check,readonly',
            '--entrypoint','sh',candidate,'-ec','test ! -e /check/PG_VERSION; test ! -e /check/global/pg_control'])
        data=self.volume('pg-data')
        self.run(['docker','run','--rm','--pull','never','--network','none','--user','0:0',
            '--mount','type=volume,source='+data+',target=/var/lib/postgresql/data','--entrypoint','sh',OLD_PG,
            '-ec','chown 2000:2000 /var/lib/postgresql/data; chmod 700 /var/lib/postgresql/data'])
        self.pg_start(OLD_PG,data)
        old, _ = G.image_inventory(D,C,'deriw-blockscout:v9-upgrade-baseline')
        app, inventory = G.image_inventory(D,C,'deriw-blockscout:v10-nft-local-validation')
        versions=C.migration_versions(D,inventory)
        versions={k:versions[k] for k in ('repo','account','arbitrum')}
        for phase,image in [('baseline',old['Id']),('upgrade',app['Id'])]:
            G.evaluate(image,phase,self.token,self.network,self.password,versions,self.directory,self.pg,defer_indexes=True)
        self.sql('CREATE INDEX "internal_transactions_block_number_DESC_transaction_index_DESC_" ON internal_transactions(block_number DESC,transaction_index DESC,"index" DESC);')
        self.sql("INSERT INTO migrations_status(migration_name,status,inserted_at,updated_at) VALUES('reindex_duplicated_internal_transactions','completed',now(),now());")
        self.sql("INSERT INTO internal_transactions SELECT (jsonb_populate_record(NULL::internal_transactions,to_jsonb(t)||jsonb_build_object('block_index',100000+g,'index',100000+g,'block_number',19))).* FROM (SELECT * FROM internal_transactions WHERE block_number=20 LIMIT 1) t CROSS JOIN generate_series(0,99) g;")
        self.sql("CREATE TABLE patch_fixture_updates(block_hash bytea, block_index integer, block_number integer); CREATE FUNCTION patch_fixture_updated() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN INSERT INTO patch_fixture_updates VALUES(NEW.block_hash,NEW.block_index,NEW.block_number); RETURN NEW; END $$; CREATE TRIGGER patch_fixture_updated AFTER UPDATE ON internal_transactions FOR EACH ROW EXECUTE FUNCTION patch_fixture_updated();")
        self.sql("CREATE FUNCTION patch_fixture_checkpoint() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF (NEW.meta->>'max_block_number')::bigint < (OLD.meta->>'max_block_number')::bigint THEN PERFORM pg_sleep(20); END IF; RETURN NEW; END $$; CREATE TRIGGER patch_fixture_checkpoint BEFORE UPDATE OF meta ON migrations_status FOR EACH ROW EXECUTE FUNCTION patch_fixture_checkpoint();")
        sql=self.sql
        def identity():
            return json.loads(sql("SELECT jsonb_build_object('system_identifier',(SELECT system_identifier::text FROM pg_control_system()),'oid',(SELECT oid::bigint FROM pg_database WHERE datname='fixture'),'heap',pg_relation_filenode('internal_transactions'::regclass)::bigint,'versions',(SELECT jsonb_agg(version ORDER BY version) FROM schema_migrations));"))
        initial=identity()
        other=json.loads(sql("SELECT jsonb_agg(to_jsonb(s) ORDER BY migration_name) FROM migrations_status s WHERE migration_name<>'empty_internal_transactions_data';"))
        expected=int(sql("SELECT count(*) FROM internal_transactions WHERE trace_address IS NOT NULL OR value=0 OR (call_type_enum IS NULL AND call_type IS NOT NULL) OR error IS NOT NULL OR (created_contract_address_hash IS NOT NULL AND to_address_hash IS NULL);").strip())
        payload={'token':self.token,'batch_blocks':1,'start_before_block':21,'application_name':'blockscout-v10-native-'+self.token,
            'database_identity':{'database':'fixture','oid':initial['oid'],'address':self.pg_ip,'port':5432,'postgres_major':15},
            'heap_identity':initial['heap'],'applied_versions':initial['versions'],'native_status_before':None,
            'table_options_before':json.loads(sql("SELECT coalesce(to_jsonb(reloptions),'[]'::jsonb) FROM pg_class WHERE oid='internal_transactions'::regclass;"))}
        frozen=U.R.encoded(payload)
        environment={'DATABASE_URL':'postgresql://fixture:'+self.password+'@pg:5432/fixture','ECTO_USE_SSL':'false',
            'CHAIN_TYPE':'arbitrum','CHAIN_ID':'18417507517','SECRET_KEY_BASE':'a'*64,'ETHEREUM_JSONRPC_HTTP_URL':'http://127.0.0.1:1','ERL_FLAGS':'+S 2:2'}
        client=self.client_start('postgresql',app['Id'],environment)
        client_before=json.loads(self.run(['docker','inspect',client]))[0]
        first=self.native_start(app['Id'],environment,payload)
        self.grant(first)
        self.wait(lambda:int(sql('SELECT count(*) FROM patch_fixture_updates WHERE block_number=20;').strip())>0,'First native batch did not commit')
        self.native_stop(first)
        assert sql("SELECT meta->>'max_block_number' FROM migrations_status WHERE migration_name='empty_internal_transactions_data';").strip()=='20'
        self.sql("CREATE OR REPLACE FUNCTION patch_fixture_checkpoint() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF (NEW.meta->>'max_block_number')::bigint < 19 THEN PERFORM pg_sleep(20); END IF; RETURN NEW; END $$;")
        self.pg_stop()
        self.pg_start(K.catalog()['postgresql']['reference'],data,guard=True)
        self.wait(lambda:self.client_status(client).get('version')==150019,'Existing Ecto client did not reconnect to patched PostgreSQL')
        assert identity()==initial
        assert sql("SELECT current_setting('server_version_num');").strip()=='150019'
        current=json.loads(sql(K.PG_QUERY+';'))
        K.validate_postgres(current)
        # The release note condition is detected from catalog metadata, without
        # scanning values or automatically reindexing any production relation.
        sql("CREATE TABLE patch_fixture_float(value float8); CREATE INDEX patch_fixture_float_gist ON patch_fixture_float USING gist(value);")
        with_float=json.loads(sql(K.PG_QUERY+';'))
        assert with_float['float_gist_indexes']>=1
        try:K.validate_postgres(with_float)
        except D.ConfigError:pass
        else:raise AssertionError('Float GiST review was bypassed')
        sql('DROP TABLE patch_fixture_float;')
        second=self.native_start(app['Id'],environment,payload)
        self.grant(second)
        self.wait(lambda:int(sql('SELECT count(*) FROM patch_fixture_updates WHERE block_number=19;').strip())==100,'Native migration did not continue on PG15.19')
        self.native_stop(second)
        assert sql("SELECT meta->>'max_block_number' FROM migrations_status WHERE migration_name='empty_internal_transactions_data';").strip()=='19'
        sql('DROP TRIGGER patch_fixture_checkpoint ON migrations_status;')
        self.pg_stop()
        self.pg_start(OLD_PG,data,guard=True)
        self.wait(lambda:self.client_status(client).get('version')==150018,'Existing Ecto client did not reconnect after rollback')
        client_after=json.loads(self.run(['docker','inspect',client]))[0]
        assert client_after['Id']==client_before['Id'] and client_after['State']['StartedAt']==client_before['State']['StartedAt'] and client_after['RestartCount']==0
        assert identity()==initial
        assert sql("SELECT current_setting('server_version_num');").strip()=='150018'
        assert sql("SELECT meta->>'max_block_number' FROM migrations_status WHERE migration_name='empty_internal_transactions_data';").strip()=='19'
        third=self.native_start(app['Id'],environment,payload)
        self.grant(third)
        self.wait(lambda:sql("SELECT status FROM migrations_status WHERE migration_name='empty_internal_transactions_data';").strip()=='completed','Native migration failed to finish after binary rollback',seconds=45)
        assert sql("SELECT meta->>'v10_native_job' FROM migrations_status WHERE migration_name='empty_internal_transactions_data';").strip()==self.token
        assert sql("SELECT meta->>'max_block_number' FROM migrations_status WHERE migration_name='empty_internal_transactions_data';").strip()=='-1'
        assert int(sql('SELECT count(*) FROM patch_fixture_updates;').strip())==expected
        assert sql('SELECT count(*) FROM (SELECT block_hash,block_index FROM patch_fixture_updates GROUP BY 1,2 HAVING count(*)>1) t;').strip()=='0'
        assert json.loads(sql("SELECT jsonb_agg(to_jsonb(s) ORDER BY migration_name) FROM migrations_status s WHERE migration_name<>'empty_internal_transactions_data';"))==other
        assert U.R.encoded(payload)==frozen
        self.native_stop(third)
        return {'postgresql':{'status':'passed','old_version':'15.18','patch_version':'15.19','uid_gid':'2000:2000',
            'same_named_data_volume_and_static_ip':True,'cluster_oid_heap_and_migration_versions_preserved':True,
            'original_native_worker_and_payload_used':True,'committed_batch_with_lost_cursor_replayed_idempotently':True,
            'native_cursor_advanced_on_patch':True,'native_job_resumed_after_binary_rollback':True,
            'same_job_completed_without_duplicate_updates':True,'btree_gist_float_review_guard_verified':True,
            'other_migration_completion_records_preserved':True,'existing_ecto_client_reconnected_without_restart':True,
            'empty_data_directory_refused_without_initialization':True}}

    def redis(self):
        data=self.volume('redis-data')
        name='infra-redis-'+self.token
        address=str(self.subnet.network_address+11)
        def start(reference):
            image=json.loads(self.run(['docker','image','inspect',reference]))[0]['Id']
            model={'services':{'redis':{'image':image,'container_name':name,'labels':{G.LABEL:self.token},
                'read_only':True,'mem_limit':'128m','cpus':1,'tmpfs':['/tmp:rw,mode=1777'],
                'volumes':[{'type':'volume','source':'data','target':'/data'}],
                'networks':{'default':{'ipv4_address':address,'aliases':['redis']}},
                'command':['redis-server','--appendonly','no','--save','3600 1']}},
                'networks':{'default':{'external':True,'name':self.network}},'volumes':{'data':{'external':True,'name':data}}}
            file=self.directory/('redis-'+uuid.uuid4().hex[:8]+'.compose.json')
            file.write_bytes(U.R.snapshot_bytes(model))
            self.run(['docker','compose','-p',self.project,'--project-directory',str(self.directory),
                '--env-file',os.devnull,'-f',str(file),'up','-d','--no-deps','--no-build','--pull','never','redis'])
            self.wait(lambda:subprocess.run(['docker','exec',name,'redis-cli','PING'],capture_output=True,text=True).stdout.strip()=='PONG','Redis fixture did not start')
        def cli(*command,db=0):
            return self.run(['docker','exec',name,'redis-cli','-n',str(db),*command]).strip()
        def stop():
            self.run(['docker','stop','--time','60',name])
            assert json.loads(self.run(['docker','inspect',name]))[0]['State']['ExitCode']==0
        start(OLD_REDIS)
        cli('SET','fixture:string','original')
        cli('HSET','fixture:hash','field','value')
        cli('RPUSH','fixture:list','one','two')
        cli('SADD','fixture:set','alpha','beta')
        cli('ZADD','fixture:zset','1','one','2','two')
        cli('XADD','fixture:stream','*','field','value')
        cli('SET','fixture:ttl','expires','EX','3600')
        cli('SET','fixture:db1','separate-db',db=1)
        app=json.loads(self.run(['docker','image','inspect','deriw-blockscout:v10-nft-local-validation']))[0]['Id']
        environment={'DATABASE_URL':'postgresql://fixture:synthetic@127.0.0.1:1/fixture','ECTO_USE_SSL':'false',
            'CHAIN_TYPE':'arbitrum','CHAIN_ID':'18417507517','SECRET_KEY_BASE':'a'*64,
            'ETHEREUM_JSONRPC_HTTP_URL':'http://127.0.0.1:1','ERL_FLAGS':'+S 2:2'}
        client=self.client_start('redis',app,environment)
        before=json.loads(self.run(['docker','inspect',client]))[0]
        cli('SAVE')
        stop()
        folder=self.directory/'redis-final-data'
        folder.mkdir(mode=0o700)
        self.run(['docker','cp',name+':/data/dump.rdb',str(folder/'dump.rdb')])
        inputs={'component':'redis','before':{'redis-db':{'Mounts':[{'Type':'bind','Source':str(folder),'Destination':'/data','RW':True}]}}}
        I.private_backup(D,self.directory/'state.json',inputs,'forward')
        I.private_backup(D,self.directory/'state.json',inputs,'forward')
        backup=json.loads((self.directory/'redis-backup-forward.json').read_text())
        assert backup['sha256']==I.file_digest(self.directory/backup['file']) and backup['automatic_restore'] is False
        self.run(['docker','run','--rm','--pull','never','--network','none','--read-only','--mount',
            'type=volume,source='+data+',target=/data,readonly','--entrypoint','redis-check-rdb',
            K.catalog()['redis']['reference'],'/data/dump.rdb'])
        start(K.catalog()['redis']['reference'])
        assert K.live_fingerprint(types.SimpleNamespace(run=self.run),name,'redis')['version']=='8.8.4'
        self.wait(lambda:self.client_status(client).get('ready') is True,'Existing Redis client did not reconnect')
        def check():
            assert cli('GET','fixture:string')=='original'
            assert cli('HGET','fixture:hash','field')=='value'
            assert cli('LRANGE','fixture:list','0','-1').splitlines()==['one','two']
            assert set(cli('SMEMBERS','fixture:set').splitlines())=={'alpha','beta'}
            assert cli('ZRANGE','fixture:zset','0','-1').splitlines()==['one','two']
            assert cli('XLEN','fixture:stream')=='1'
            assert 0<int(cli('TTL','fixture:ttl'))<=3600
            assert cli('GET','fixture:db1',db=1)=='separate-db'
        check()
        cli('SET','fixture:after-patch','retain-new-writes')
        cli('SAVE')
        stop()
        self.run(['docker','run','--rm','--pull','never','--network','none','--read-only','--mount',
            'type=volume,source='+data+',target=/data,readonly','--entrypoint','redis-check-rdb',OLD_REDIS,'/data/dump.rdb'])
        start(OLD_REDIS)
        check()
        assert cli('GET','fixture:after-patch')=='retain-new-writes'
        self.wait(lambda:self.client_status(client).get('ready') is True,'Redis client did not reconnect after rollback')
        after=json.loads(self.run(['docker','inspect',client]))[0]
        assert before['Id']==after['Id'] and before['State']['StartedAt']==after['State']['StartedAt'] and after['RestartCount']==0
        assert json.loads(self.run(['docker','inspect',name]))[0]['NetworkSettings']['Networks'][self.network]['IPAddress']==address
        return {'redis':{'status':'passed','old_version':'8.8.0','patch_version':'8.8.4','rdb_multitype_and_multidatabase_restored':True,
            'ttl_preserved':True,'final_rdb_backup_checksum_verified':True,'old_binary_reads_new_rdb':True,
            'image_rollback_preserves_post_patch_writes':True,'static_ip_preserved':True,'existing_redix_client_reconnected_without_restart':True}}

    def nginx(self):
        hostname='explorer.fixture.invalid'
        folder=self.directory/'nginx'
        folder.mkdir(mode=0o700)
        (folder/'certificate.cnf').write_text('[req]\ndistinguished_name=dn\nx509_extensions=v3\nprompt=no\n[dn]\nCN=Infrastructure Fixture CA\n[v3]\nsubjectAltName=DNS:'+hostname+'\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,digitalSignature,keyEncipherment,keyCertSign\n')
        self.run(['openssl','req','-new','-x509','-newkey','rsa:2048','-nodes','-days','1',
            '-config',str(folder/'certificate.cnf'),'-keyout',str(folder/'fixture.key'),'-out',str(folder/'fixture.crt')])
        (folder/'trust.crt').write_bytes((folder/'fixture.crt').read_bytes())
        mock='infra-mock-'+self.token
        proxy='infra-proxy-'+self.token
        client='infra-http-'+self.token
        mock_config='''events {}\nhttp { server { listen 8081;
location /socket/v2/websocket {
 if ($http_upgrade != "websocket") { return 400; }
 add_header Upgrade websocket always;
 add_header Connection upgrade always;
 add_header Sec-WebSocket-Accept "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=" always;
 return 101;
}
location / { default_type application/json; return 200 '{"fixture":true}'; }
} }\n'''
        (folder/'mock.conf').write_text(mock_config)
        app=json.loads(self.run(['docker','image','inspect',
            'quanquanah/blockscout-frontend@sha256:44bd15d39c839baf1e32e28ce99c63c9eee99db8e08043cc0df8ceb1d84faf28']))[0]['Id']
        self.run(['docker','run','-d','--pull','never','--name',client,'--label',G.LABEL+'='+self.token,
            '--network',self.network,'--read-only','--tmpfs','/tmp:rw,mode=1777','--memory','128m',
            '--entrypoint','sleep',app,'infinity'])
        locations='''location ~ \\.map$ { return 404; }
location /socket/v2/websocket {
 proxy_pass http://mock:8081;
 proxy_http_version 1.1;
 proxy_set_header Upgrade $http_upgrade;
 proxy_set_header Connection "upgrade";
}
location / { proxy_pass http://mock:8081; }
'''
        nginx='events {}\nhttp {\nserver { listen 80; server_name '+hostname+';\n'+locations+'}\nserver { listen 443 ssl; server_name '+hostname+';\nssl_certificate /etc/nginx/templates/fixture.crt;\nssl_certificate_key /etc/nginx/templates/fixture.key;\n'+locations+'}\n}\n'
        (folder/'nginx.conf').write_text(nginx)
        configuration=self.volume('nginx-config')
        daemon_root=self.seed_files(configuration,[folder/f for f in ('mock.conf','nginx.conf','fixture.crt','fixture.key')])
        def daemon_path(file):return str(daemon_root/file.relative_to(self.directory))
        self.run(['docker','run','-d','--pull','never','--name',mock,'--label',G.LABEL+'='+self.token,
            '--network',self.network,'--network-alias','mock','--mount','type=bind,source='+daemon_path(folder/'mock.conf')+',target=/etc/nginx/nginx.conf,readonly',
            '--entrypoint','nginx',OLD_NGINX,'-g','daemon off;'])
        original=json.loads(self.run(['docker','image','inspect',OLD_NGINX]))[0]['Id']
        candidate=json.loads(self.run(['docker','image','inspect',K.catalog()['nginx']['reference']]))[0]['Id']
        model={'services':{'proxy':{'image':original,'container_name':proxy,'labels':{G.LABEL:self.token},
            'entrypoint':['nginx'],'command':['-g','daemon off;'],'mem_limit':'128m',
            'volumes':[{'type':'bind','source':daemon_path(folder/'nginx.conf'),'target':'/etc/nginx/nginx.conf','read_only':True},
                       {'type':'bind','source':daemon_path(folder),'target':'/etc/nginx/templates'}],
            'networks':{'default':{'aliases':['proxy']}}},'backend':{'image':app},'frontend':{'image':original}},
            'networks':{'default':{'external':True,'name':self.network}}}
        initial=self.directory/'nginx-initial.compose.json'
        initial.write_bytes(U.R.snapshot_bytes(model))
        compose=['docker','compose','-p',self.project,'--project-directory',str(self.directory),'--env-file',os.devnull]
        self.run(compose+['-f',str(initial),'up','-d','--no-deps','--no-build','--pull','never','proxy'])
        def inspect():
            return {role:json.loads(self.run(['docker','inspect',name]))[0] for role,name in [('proxy',proxy),('backend',client),('frontend',mock)]}
        before=inspect()
        def run(command,**kwargs):
            command=list(command)
            if command[:2]==['docker','exec']:
                at=3 if command[2]=='-i' else 2
                if command[at]=='backend':command[at]=client
                elif command[at]=='proxy':command[at]=proxy
            elif command[:2]==['docker','cp']:
                at=3 if command[2]=='-L' else 2
                if command[at].startswith('proxy:'):
                    command[at]=proxy+command[at][len('proxy'):]
            kwargs.pop('environment',None)
            return self.run(command,input=kwargs.pop('input_text',None),**kwargs)
        manifest={'compose_project':self.project}
        api=types.SimpleNamespace(run=run,require=D.require,ConfigError=D.ConfigError,ensure_stable=D.ensure_stable,
            inspect_project=lambda *_a,**_k:inspect(),configuration=lambda *_a:(manifest,{},{}),
            inspect_mounts=D.inspect_mounts,live_environment_differences=D.live_environment_differences,env_map=D.env_map,
            compose_model=D.compose_model,model_differences=D.model_differences)
        model=api.compose_model(self.directory,manifest,{},[initial],True)
        path=self.directory/'state.json'
        saved=S.capture_nginx(api,self.directory,before['proxy']['Mounts'])
        self.seed_files(configuration,[Path(file['source']) for file in saved['files']])
        for artifact in saved['files']:
            artifact['source']=daemon_path(Path(artifact['source']))
        inputs={'component':'nginx','before':before,'nginx':saved,'public_host':hostname,
                'image_id':candidate,'tls_ca_file':str(folder/'trust.crt')}
        for side in ('forward','rollback'):
            selected=S.nginx_snapshot(K.snapshot(model,before,'nginx',candidate,path,side),saved['files'])
            snapshot=self.directory/(side+'.compose.json')
            snapshot.write_bytes(U.R.snapshot_bytes(selected))
            U.R.check_snapshot(api,snapshot,selected,manifest,self.directory)
        original_validation=S.validate_artifacts
        def validate_mirrored(values):
            local=deepcopy(values)
            for artifact in local['nginx']['files']:
                artifact['source']=str(self.directory/Path(artifact['source']).relative_to(daemon_root))
            original_validation(local)
        with patch.object(F.P.V,'LIVE_DIRECTORY',self.directory), patch.object(S,'validate_artifacts',side_effect=validate_mirrored):
            staged=S.stage(api,path,inputs,manifest)
            assert staged['configuration_frozen']
            self.run(compose+['-f',str(self.directory/'forward.compose.json'),'up','-d','--no-deps','--no-build','--pull','never','proxy'])
            api.run(['docker','exec','proxy','nginx','-t'])
            S.ready_routes(api,inputs,proxy)
            after=inspect()
            K.unchanged_others(api,before,after,'nginx')
            I.component_contract(api,path,inputs,'forward',after)
            assert K.live_fingerprint(api,'proxy','nginx')['version']=='1.31.8'
            assert all(m['Destination'] not in ('/etc/nginx/templates/fixture.crt','/etc/nginx/templates/fixture.key')
                       for m in after['proxy']['Mounts'])
            def peer_fingerprint():
                code='const fs=require("fs"),tls=require("tls");const s=tls.connect({host:process.argv[1],port:443,servername:process.argv[2],ca:fs.readFileSync(0,"utf8")},()=>{process.stdout.write(s.getPeerCertificate().fingerprint256.replaceAll(":","").toLowerCase());s.end()});s.on("error",()=>process.exit(2));'
                return self.run(['docker','exec','-i',client,'node','-e',code,proxy,hostname],
                                input=(folder/'trust.crt').read_text(),timeout=15).strip()
            first_fingerprint=peer_fingerprint()
            self.run(['openssl','req','-new','-newkey','rsa:2048','-nodes','-subj','/CN='+hostname,
                '-keyout',str(folder/'next.key'),'-out',str(folder/'next.csr')])
            (folder/'next.cnf').write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:'+hostname+'\n')
            self.run(['openssl','x509','-req','-in',str(folder/'next.csr'),'-CA',str(folder/'fixture.crt'),
                '-CAkey',str(folder/'fixture.key'),'-set_serial','2','-days','1','-extfile',str(folder/'next.cnf'),
                '-out',str(folder/'next.crt')])
            renewed_fingerprint=hashlib.sha256(ssl.PEM_cert_to_DER_cert((folder/'next.crt').read_text())).hexdigest()
            assert first_fingerprint != renewed_fingerprint
            (folder/'fixture.crt').write_bytes((folder/'next.crt').read_bytes())
            (folder/'fixture.key').write_bytes((folder/'next.key').read_bytes())
            self.seed_files(configuration,[folder/'fixture.crt',folder/'fixture.key'])
            api.run(['docker','exec','proxy','nginx','-t'])
            api.run(['docker','exec','proxy','nginx','-s','reload'])
            self.wait(lambda:peer_fingerprint()==renewed_fingerprint,'Renewed certificate was not served after reload')
            S.validate_artifacts(inputs)
            S.ready_routes(api,inputs,proxy)
            D.ensure_stable(after,inspect(),'Certificate reload recreated a container')
            self.run(compose+['-f',str(self.directory/'rollback.compose.json'),'up','-d','--no-deps','--no-build','--pull','never','proxy'])
            S.ready_routes(api,inputs,proxy)
            assert peer_fingerprint()==renewed_fingerprint
            rollback=inspect()
            K.unchanged_others(api,before,rollback,'nginx')
            I.component_contract(api,path,inputs,'rollback',rollback)
            assert K.live_fingerprint(api,'proxy','nginx')['version']=='1.31.1'
        return {'nginx':{'status':'passed','old_version':'1.31.1','patch_version':'1.31.8',
            'compose_snapshot_round_trip_verified':True,
            'actual_staging_and_component_contract_used':True,'frozen_configuration_and_private_certificate_evidence':True,
            'directory_mounted_tls_assets_remain_renewable':True,
            'http_and_verified_https_routes':True,'deposits_backend_and_stats_routes':True,
            'websocket_101_upgrade':True,'source_maps_restricted':True,'static_ip_and_unrelated_containers_preserved':True,
            'certificate_rotation_verified_without_container_recreation':True,
            'historical_certificate_evidence_unchanged':True,'image_rollback_keeps_the_renewed_certificate':True,
            'previous_image_rollback_verified':True}}

    def cleanup(self):
        ids=self.run(['docker','ps','-aq','--filter','label='+G.LABEL+'='+self.token]).split()
        for identity in ids:
            G.cleanup_container(identity,self.token)
        for process,log in self.processes:
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
            log.close()
        for volume in self.volumes:
            meta=json.loads(self.run(['docker','volume','inspect',volume]))[0]
            assert meta.get('Labels',{}).get(G.LABEL)==self.token
            self.run(['docker','volume','rm',volume])
        if self.created_network:
            meta=json.loads(self.run(['docker','network','inspect',self.network]))[0]
            assert meta.get('Labels',{}).get(G.LABEL)==self.token
            self.run(['docker','network','rm',self.network])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--component',choices=('postgresql','redis','nginx'),default='postgresql')
    args=parser.parse_args()
    fixture=Fixture()
    result={}
    try:
        fixture.setup()
        result=getattr(fixture,{'postgresql':'postgres','redis':'redis','nginx':'nginx'}[args.component])()
    finally:
        fixture.cleanup()
    result={'status':'passed','live_services_accessed':False,'fixture_resources_removed':True,
            'private_logs':str(fixture.directory),**result}
    (fixture.directory/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    main()
