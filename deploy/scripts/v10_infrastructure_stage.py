#!/usr/bin/env python3
"""Stage patch images with synthetic storage or frozen Nginx configuration."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

import dev_config as D
import release as R
import proxy_stats as W
import v10_frontend_deploy as F
import v10_infrastructure_checks as K


def capture_nginx(api, directory, mounts=()):
    raw = api.run(['docker', 'exec', 'proxy', 'nginx', '-T'])
    sections = W.loaded_sections(api, raw)
    D.require('/etc/nginx/nginx.conf' in sections, 'Loaded Nginx master configuration is missing')
    artifacts = []
    paths = set(sections)
    for text in sections.values():
        for match in re.finditer(r'^\s*(?:ssl_certificate|ssl_certificate_key|ssl_trusted_certificate|ssl_session_ticket_key|auth_basic_user_file)\s+([^;]+);', text, re.M):
            paths.add(match[1].strip().strip('"\''))
    for number, filename in enumerate(sorted(paths)):
        D.require(filename.startswith('/') and not re.search(r'[\s$\0\r\n]', filename)
                  and '..' not in Path(filename).parts, 'Dynamic Nginx include/credential paths require review')
        destination = directory / ('nginx-file-' + str(number) + '.private')
        api.run(['docker', 'cp', '-L', 'proxy:' + filename, str(destination)], timeout=30)
        D.require(destination.is_file() and not destination.is_symlink(), 'Nginx configuration artifact is not a regular file')
        os.chmod(destination, 0o600)
        live_asset = filename not in sections and any(
            m.get('Type') == 'bind' and filename.startswith(m['Destination'].rstrip('/') + '/')
            for m in mounts)
        artifacts.append({'source': str(destination), 'target': filename,
                          'sha256': R.digest(destination.read_bytes()), 'type': 'bind', 'read_only': True,
                          'runtime_overlay': not live_asset})
    return {'loaded': raw, 'files': artifacts}


def deployment_files(artifacts):
    # Older journals mounted every saved artifact. Preserve that contract.
    for item in artifacts:
        D.require(type(item.get('runtime_overlay', True)) is bool, 'Invalid Nginx artifact role')
    return [item for item in artifacts if item.get('runtime_overlay', True)]


def nginx_snapshot(model, artifacts):
    model = deepcopy(model)
    service = model['services']['proxy']
    volumes = service.setdefault('volumes', [])
    for artifact in deployment_files(artifacts):
        # Freeze the configuration actually loaded by the current process. No
        # entrypoint template rewrite or copying of old dynamic-module binaries.
        volumes[:] = [v for v in volumes if v.get('target') != artifact['target']]
        volumes.append({k: artifact[k] for k in ('source', 'target', 'type', 'read_only')})
    service['entrypoint'] = ['nginx']
    service['command'] = ['-g', 'daemon off;']
    return model


def validate_artifacts(inputs):
    artifacts = (inputs.get('nginx') or {}).get('files', [])
    deployment_files(artifacts)
    for artifact in artifacts:
        path = Path(artifact['source'])
        D.require(path.is_file() and not path.is_symlink() and R.digest(path.read_bytes()) == artifact['sha256'],
                  'Frozen Nginx configuration/certificate changed')


def current_nginx_matches(api, inputs, name='proxy'):
    if api.run(['docker', 'exec', name, 'nginx', '-T']) != inputs['nginx']['loaded']:
        return False
    for item in inputs['nginx']['files']:
        digest = api.run(['docker', 'exec', name, 'sha256sum', item['target']]).split()[0]
        if digest != item['sha256']:
            return False
    return True


def http_probe(api, hostname, target, route, tls=False, websocket=False, ca=None):
    args = ['curl', '-q', '--noproxy', '*', '--silent', '--show-error', '--output', '/dev/null',
            '--connect-timeout', '2', '--max-time', '5', '--write-out', '%{http_code}', '-H', 'Host: ' + hostname]
    if websocket:
        args += ['--http1.1', '-H', 'Connection: Upgrade', '-H', 'Upgrade: websocket',
                 '-H', 'Sec-WebSocket-Version: 13', '-H', 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==']
    if tls:
        args += ['--connect-to', hostname + ':443:' + target + ':443']
        url = 'https://' + hostname + route
    else:
        url = 'http://' + target + ':80' + route
    # A websocket 101 followed by curl's deliberate timeout remains an observed
    # successful handshake. Raw response bodies/headers are never returned.
    if ca:
        script = 'set -eu; probe_ca=$(mktemp /tmp/infra-ca.XXXXXXXX); trap \'rm -f "$probe_ca"\' EXIT HUP INT TERM; cat > "$probe_ca"; result=0; value=$("$@" --cacert "$probe_ca") || result=$?; printf "%s %s\\n" "$value" "$result"'
        command = ['docker', 'exec', '-i', 'backend', 'sh', '-c', script, '--', *args, url]
    else:
        script = 'result=0; value=$("$@") || result=$?; printf "%s %s\\n" "$value" "$result"'
        command = ['docker', 'exec', 'backend', 'sh', '-c', script, '--', *args, url]
    output = api.run(command, input_text=ca if ca else None, timeout=10).strip().split()
    D.require(len(output) == 2 and all(v.isdigit() for v in output), 'Invalid private HTTP status observation')
    return {'http_status': int(output[0]), 'transport_exit': int(output[1])}


def nginx_routes(api, inputs, target):
    hostname = inputs['public_host']
    D.require(re.fullmatch(r'[A-Za-z0-9.-]+', hostname), 'Invalid captured frontend hostname')
    ca = Path(inputs['tls_ca_file']).read_text() if inputs.get('tls_ca_file') else None
    rows = {}
    for key, path in {'deposits': '/deposits', 'backend_api': '/api/v2/stats',
                      'deposit_api': '/api/v2/arbitrum/messages/to-rollup',
                      'stats_api': '/statsapi/api/v1/pages/main',
                      'source_map': '/_next/static/chunks/infra-probe.js.map'}.items():
        rows[key] = http_probe(api, hostname, target, path, ca=ca)
    rows['https'] = http_probe(api, hostname, target, '/deposits', tls=True, ca=ca)
    rows['websocket'] = http_probe(api, hostname, target, '/socket/v2/websocket?vsn=2.0.0', websocket=True)
    return rows


def validate_routes(rows):
    for key in ('deposits', 'backend_api', 'deposit_api', 'stats_api', 'https'):
        D.require(rows[key] == {'http_status': 200, 'transport_exit': 0},
                  'Nginx ' + key + ' route failed; keep the current service until the staged route is verified')
    D.require(rows['source_map']['http_status'] in (403, 404) and rows['source_map']['transport_exit'] == 0,
              'Nginx source-map restriction failed')
    D.require(rows['websocket']['http_status'] == 101, 'Nginx websocket upgrade was not verified')


def ready_routes(api, inputs, target):
    deadline = time.monotonic() + 30
    while True:
        try:
            rows = nginx_routes(api, inputs, target)
            validate_routes(rows)
            return rows
        except D.ConfigError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)


def remove_owned(api, name, state_path):
    ids = api.run(['docker', 'ps', '-aq', '--filter', 'name=^/' + name + '$']).split()
    if not ids:
        return
    D.require(len(ids) == 1, 'Ambiguous infrastructure stage container')
    row = json.loads(api.run(['docker', 'inspect', ids[0]]))[0]
    D.require((row.get('Config', {}).get('Labels') or {}).get(K.LABEL) == str(state_path),
              'Staging container is owned by another operation')
    api.run(['docker', 'rm', '-f', '--volumes', ids[0]], timeout=30)


def stage(api, path, inputs, manifest):
    component = inputs['component']
    service, kind, _ = K.COMPONENTS[component]
    name = 'infra-stage-' + path.parent.name.rsplit('-', 1)[1]
    remove_owned(api, name, path)
    try:
        if kind == 'nginx':
            K.unchanged_others(api, inputs['before'], api.inspect_project(manifest), component)
            D.require(current_nginx_matches(api, inputs),
                      'Loaded Nginx configuration changed before staging')
            validate_artifacts(inputs)
            baseline = nginx_routes(api, inputs, 'proxy')
            validate_routes(baseline)
            forward = api.compose_model(F.P.V.LIVE_DIRECTORY, manifest, {}, [path.parent / 'forward.compose.json'], True)
            selected = deepcopy(forward['services'][service])
            for key in ('ports', 'depends_on', 'build', 'profiles', 'hostname'):
                selected.pop(key, None)
            selected.update(container_name=name, restart='no')
            selected['networks'] = {key: {'aliases': [name]} for key in selected['networks']}
            for volume in selected.get('volumes', []):
                volume['read_only'] = True
            staged = {'services': {'stage': selected}, 'networks': forward['networks']}
            stage_file = path.parent / 'stage.compose.json'
            if stage_file.exists():
                D.require(stage_file.read_bytes() == R.snapshot_bytes(staged), 'Saved stage Compose model changed')
            else:
                R.write_new(stage_file, R.snapshot_bytes(staged))
            F.private_command(['docker', 'compose', '-p', name, '--project-directory', str(F.P.V.LIVE_DIRECTORY),
                '--env-file', os.devnull, '-f', str(stage_file), 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', 'stage'],
                path.parent / ('stage-' + uuid.uuid4().hex[:8] + '.log.private'), 'staging_nginx')
            api.run(['docker', 'exec', name, 'nginx', '-t'])
            routes = ready_routes(api, inputs, name)
            return {'routes': routes, 'configuration_frozen': True}
        environment = {'POSTGRES_USER': 'patch_fixture', 'POSTGRES_PASSWORD': uuid.uuid4().hex,
                       'POSTGRES_DB': 'patch_fixture'} if kind == 'postgresql' else {}
        command = ['docker', 'run', '-d', '--pull', 'never', '--name', name, '--label', K.LABEL + '=' + str(path),
                   '--network', 'none', '--memory', '512m', '--cpus', '1', '--read-only',
                   '--tmpfs', '/tmp:rw,mode=1777']
        if kind == 'postgresql':
            command += ['--user', '2000:2000', '--tmpfs', '/var/lib/postgresql/data:rw,uid=2000,gid=2000,mode=0700,size=256m',
                        '--tmpfs', '/var/run/postgresql:rw,mode=1777,size=16m']
        else:
            command += ['--tmpfs', '/data:rw,mode=1777,size=64m']
        for key, value in environment.items():
            command += ['--env', key + '=' + value]
        command.append(inputs['image_id'])
        if kind == 'postgresql':
            command += ['postgres', '-c', 'shared_buffers=32MB', '-c', 'max_connections=10']
        else:
            command += ['redis-server', '--appendonly', 'no', '--save', '']
        api.run(command, timeout=30)
        deadline = time.monotonic() + 60
        while True:
            try:
                if kind == 'postgresql':
                    api.run(['docker', 'exec', name, 'pg_isready', '-q', '-h', '127.0.0.1', '-U', 'patch_fixture', '-d', 'patch_fixture'])
                    break
                if api.run(['docker', 'exec', name, 'redis-cli', 'PING']).strip() == 'PONG':
                    break
            except D.ConfigError:
                pass
            D.require(time.monotonic() < deadline, 'Synthetic component staging timed out')
            time.sleep(0.5)
        if kind == 'postgresql':
            for extension, version in inputs['component_before']['extensions'].items():
                D.require(re.fullmatch(r'[a-z_][a-z0-9_]*', extension) and re.fullmatch(r'[0-9.]+', version),
                          'Unexpected PostgreSQL extension identity')
                sql = 'CREATE EXTENSION IF NOT EXISTS "' + extension + '" VERSION \'' + version + "'"
                api.run(['docker', 'exec', name, *K.PSQL, sql])
            current = K.postgres(api, name)
            D.require(current['database_encoding'] == inputs['component_before']['database_encoding']
                      and current['library_versions'] == inputs['component_before']['library_versions'],
                      'Candidate collation-library versions or encoding differ from the existing database')
            return {'fresh_cluster_started_with_uid_2000': True, 'installed_extension_versions_available': True,
                    'collation_library_versions_match': True}
        api.run(['docker', 'exec', name, 'redis-cli', 'SET', 'deriw:patch:fixture', 'synthetic'])
        api.run(['docker', 'exec', name, 'redis-cli', 'SAVE'])
        D.require(api.run(['docker', 'exec', name, 'redis-cli', 'GET', 'deriw:patch:fixture']).strip() == 'synthetic',
                  'Synthetic Redis read/write failed')
        api.run(['docker', 'exec', name, 'redis-check-rdb', '/data/dump.rdb'])
        return {'fresh_redis_started': True, 'synthetic_rdb_save_and_check': True}
    finally:
        remove_owned(api, name, path)
