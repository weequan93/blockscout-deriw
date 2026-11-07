#!/usr/bin/env python3
"""Targeted dev Nginx/Redis/PG15 patches with immutable migration handoffs."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

import candidate_build as B
import containerd_maintenance as M
import dev_config as D
import release as R
import v10_bridge_deploy as P
import v10_frontend_deploy as F
import v10_infrastructure_checks as K
import v10_infrastructure_stage as S
import v10_native_normalization as N
import v10_upgrade as U

ROOT = Path(__file__).resolve().parents[2]
BASE = Path('/data/blockscout-dev-private/infrastructure-releases')
FORMAT = 'deriw-v10-infrastructure-release-v1'
PHASES = ('prepared', 'staged', 'activation_requested', 'active', 'finished',
          'rollback_requested', 'rolled_back', 'rollback_finished')


def save(path, state, phase):
    state.update(phase=phase, observed_at_utc=datetime.now(timezone.utc).isoformat())
    R.update_state(path, state)


def load(path):
    D.require(path.is_absolute() and path.resolve() == path and path.name == 'state.json'
              and path.parent.parent == BASE and re.fullmatch(r'infra-[0-9TZ-]+-[a-f0-9]{8}', path.parent.name),
              'Unexpected infrastructure journal path')
    N.J.owned_directory(path.parent)
    state = json.loads(U.private(path))
    raw = U.private(path.parent / 'inputs.json')
    inputs = json.loads(raw)
    D.require(state.get('format') == FORMAT and inputs.get('format') == FORMAT
              and state.get('inputs_sha256') == R.digest(raw) and state.get('phase') in PHASES
              and inputs.get('component') in K.COMPONENTS, 'Infrastructure inputs or phase changed')
    catalog_raw = U.private(path.parent / 'targets.json')
    D.require(R.digest(catalog_raw) == inputs['catalog_sha256']
              and json.loads(catalog_raw)[K.COMPONENTS[inputs['component']][1]] == inputs['target'],
              'Frozen patch image catalog changed')
    for side in ('forward', 'rollback'):
        D.require(R.digest(U.private(path.parent / (side + '.compose.json'))) == inputs['snapshots'][side],
                  'Infrastructure Compose snapshot changed')
        anchor = path.parent / ('database-before-' + side + '.json')
        if anchor.exists():
            known = state.get('anchor_sha256', {}).get(side)
            if known is not None:
                D.require(R.digest(U.private(anchor)) == known, 'Paused DB anchor changed')
            else:
                D.require(state['phase'] != ('activation_requested' if side == 'forward' else 'rollback_requested')
                          and not (path.parent / ('receipt-' + side + '.json')).exists(),
                          'Requested activation has an incomplete DB anchor')
    D.require(R.digest(U.private(path.parent / 'native-service-before.private')) == inputs['native_service_sha256'],
              'Original migration unit evidence changed')
    S.validate_artifacts(inputs)
    if inputs.get('tls_ca_file'):
        D.require(R.digest(U.private(Path(inputs['tls_ca_file']))) == inputs['tls_ca_sha256'], 'TLS trust bundle changed')
    return state, inputs


def public(path, state=None):
    state, inputs = load(path) if state is None else (state, load(path)[1])
    return {'status': 'infrastructure_' + state['phase'], 'state': str(path), 'component': inputs['component'],
            'target_version': inputs['target']['version'], 'image_reference': inputs['target']['reference'],
            'only_recreated_service': K.COMPONENTS[inputs['component']][0],
            'backend_image_replaced': False, 'schema_migrations_run': False,
            'normalization_state': inputs['normalization_state'], 'frozen_normalization_inputs_preserved': True,
            'automatic_data_restore': False, 'recovery_policy': inputs['recovery_policy'],
            'observed_at_utc': state['observed_at_utc']}


def current_files(context):
    path = Path(context['infrastructure_release_state'])
    load(path)
    return [path.parent / (context['side'] + '.compose.json')]


def prepare(api, native_path, component, tls_ca=None):
    D.require(component in K.COMPONENTS, 'Select one reviewed infrastructure component')
    source = F.source_review(api)
    native = N.load(native_path)
    context = N.runtime_binding(native_path, native) or native['inputs']
    D.require(context.get('release_state'), 'Expected the verified deployment handoff')
    manifest, _, _ = api.configuration(ROOT, 'dev')
    before = api.inspect_project(manifest)
    api.ensure_stable(context['runtime_before'], before, 'Runtime changed before patch preparation')
    P.Q.verify_holds(api)
    observed = K.stable_database_anchor(api)
    payload = native['inputs']['payload']
    meta = (observed.get('native_status') or {}).get('meta') or {}
    D.require(observed['database_identity'] == payload['database_identity']
              and observed['heap_identity'] == payload['heap_identity']
              and observed['versions'] == payload['applied_versions']
              and meta.get('v10_native_job') == payload['token']
              and type(meta.get('max_block_number')) is int
              and -1 <= meta['max_block_number'] < payload['start_before_block'],
              'An initialized owned native checkpoint and its original database are required')
    D.require(observed['native_status']['status'] in ('started', 'completed')
              and (native['phase'] != 'completed' or observed['native_status']['status'] == 'completed'),
              'The native database completion record changed')
    service, kind, _ = K.COMPONENTS[component]
    target = K.catalog()[kind]
    original = K.live_fingerprint(api, service, kind)
    component_before = K.postgres(api, service) if kind == 'postgresql' else K.redis(api, service) if kind == 'redis' else {}
    if kind == 'postgresql':
        K.validate_postgres(component_before)
        data = K.data_mount(before[service], '/var/lib/postgresql/data')
        D.require((data / 'PG_VERSION').read_text().strip() == '15' and (data / 'global/pg_control').is_file(),
                  'The existing PostgreSQL data directory is required; initialization is forbidden')
    if kind == 'redis':
        K.data_mount(before[service], '/data')
    B.validate_state_directory(api, BASE, ROOT)
    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    B.validate_state_directory(api, BASE, ROOT)
    directory = BASE / ('infra-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8])
    directory.mkdir(mode=0o700)
    path = directory / 'state.json'
    F.private_command(['docker', 'pull', '--platform', 'linux/amd64', target['reference']],
                      directory / 'pull.log.private', 'pulling_' + component, timeout=300)
    candidate = K.image_fingerprint(api, target['reference'], kind)
    K.validate_target(kind, original, candidate, target)
    D.require(before[service]['Image'] != candidate['image_id'], 'Component already uses the target image; inspect the saved release')
    files = F.current_compose_files(api, before, manifest, context)
    model = api.compose_model(P.V.LIVE_DIRECTORY, manifest, {}, files, True)
    D.require(not api.live_environment_differences(model, before), 'Current Compose environment differs from running services')
    nginx = S.capture_nginx(api, directory) if kind == 'nginx' else None
    tls_ca_file = None
    if tls_ca:
        raw = tls_ca.read_bytes()
        D.require(b'BEGIN CERTIFICATE' in raw and b'PRIVATE KEY' not in raw and len(raw) <= 1024 * 1024,
                  'Provide only a PEM CA certificate bundle')
        tls_ca_file = directory / 'tls-ca.pem.private'
        R.write_new(tls_ca_file, raw)
    snapshots = {}
    for side in ('forward', 'rollback'):
        selected = K.snapshot(model, before, component, candidate['image_id'], path, side)
        if nginx:
            selected = S.nginx_snapshot(selected, nginx['files'])
        raw = R.snapshot_bytes(selected)
        R.write_new(directory / (side + '.compose.json'), raw)
        R.check_snapshot(api, directory / (side + '.compose.json'), selected, manifest, P.V.LIVE_DIRECTORY)
        snapshots[side] = R.digest(raw)
    prior = {k: native[k] for k in ('runtime_binding_file', 'runtime_binding_sha256') if k in native}
    unit = N.SYSTEMD_DIRECTORY / N.UNIT
    D.require(unit.is_file() and not unit.is_symlink(), 'Existing migration service unit is required')
    service_raw = unit.read_bytes()
    D.require(service_raw == N.render_service(native_path, Path(context['source_root']), native['inputs']['python_executable']).encode(),
              'Native service ownership changed')
    R.write_new(directory / 'native-service-before.private', service_raw)
    catalog_raw = (ROOT / 'deploy/infra/targets.json').read_bytes()
    R.write_new(directory / 'targets.json', catalog_raw)
    inputs = {'format': FORMAT, 'component': component, 'target': target, 'image_id': candidate['image_id'],
              'fingerprint_before': original, 'component_before': component_before, 'before': before,
              'source_root': str(ROOT), 'tool_source_commit': source, 'normalization_state': str(native_path),
              'native_inputs_sha256': native['inputs_sha256'], 'prior_binding': prior,
              'native_service_sha256': R.digest(service_raw), 'base_bridge_state': context['release_state'],
              'snapshots': snapshots, 'catalog_sha256': R.digest(catalog_raw), 'nginx': nginx,
              'public_host': api.env_map(before['frontend']).get('NEXT_PUBLIC_APP_HOST'),
              'recovery_policy': U.RECOVERY, 'tls_ca_file': str(tls_ca_file) if tls_ca_file else None,
              'tls_ca_sha256': None}
    if tls_ca_file:
        inputs['tls_ca_sha256'] = R.digest(U.private(tls_ca_file))
    R.write_new(directory / 'inputs.json', R.encoded(inputs))
    state = {'format': FORMAT, 'inputs_sha256': R.digest(R.encoded(inputs)), 'phase': 'prepared', 'anchor_sha256': {}}
    save(path, state, 'prepared')
    api.ensure_stable(before, api.inspect_project(manifest), 'Runtime changed during patch preparation')
    print(json.dumps(public(path, state)), flush=True)
    return path


def stage(api, path):
    state, inputs = load(path)
    manifest, _, _ = api.configuration(ROOT, 'dev')
    api.ensure_stable(inputs['before'], api.inspect_project(manifest), 'Runtime changed before image staging')
    file = path.parent / 'stage-receipt.json'
    if file.exists():
        receipt = json.loads(U.private(file))
        D.require(receipt.get('format') == FORMAT and receipt['inputs_sha256'] == state['inputs_sha256']
                  and receipt['image_id'] == inputs['image_id'], 'Existing stage receipt changed')
        save(path, state, 'staged')
        return
    result = S.stage(api, path, inputs, manifest)
    api.ensure_stable(inputs['before'], api.inspect_project(manifest), 'Runtime changed during image staging')
    receipt = {'format': FORMAT, 'inputs_sha256': state['inputs_sha256'], 'image_id': inputs['image_id'], 'checks': result}
    if file.exists():
        D.require(U.private(file) == R.encoded(receipt), 'Existing stage receipt changed')
    else:
        R.write_new(file, R.encoded(receipt))
    save(path, state, 'staged')


def own_native_binding(native, inputs, path):
    current = {k: native[k] for k in ('runtime_binding_file', 'runtime_binding_sha256') if k in native}
    own_prefix = 'infrastructure-runtime-binding-' + path.parent.name.rsplit('-', 1)[1] + '-'
    D.require(current == inputs['prior_binding'] or current.get('runtime_binding_file', '').startswith(own_prefix),
              'A newer deployment owns the migration; this release cannot overwrite it')


def pause(api, path, side):
    state, inputs = load(path)
    native_path = Path(inputs['normalization_state'])
    native = N.load(native_path)
    own_native_binding(native, inputs, path)
    if not any((path.parent / ('database-before-' + candidate + '.json')).exists() for candidate in ('forward', side)):
        return P.pause_native(api, native_path, 'infrastructure_' + inputs['component'])
    # Recovery may begin with PostgreSQL stopped. The saved paused anchor and
    # the owned stopped controller/container are sufficient to retry recreation.
    unit = N.SYSTEMD_DIRECTORY / N.UNIT
    allowed = [U.private(path.parent / 'native-service-before.private'),
               N.render_service(native_path, ROOT, native['inputs']['python_executable']).encode()]
    D.require(unit.is_file() and not unit.is_symlink() and unit.read_bytes() in allowed, 'Native service ownership changed')
    if native['phase'] != 'completed' and not (native_path.parent / 'pause.request').exists():
        R.write_new(native_path.parent / 'pause.request', b'infrastructure recovery\n')
    api.run(['systemctl', 'stop', N.UNIT], timeout=65)
    D.require(api.run(['systemctl', 'show', N.UNIT, '--property=ActiveState', '--value']).strip() in ('inactive', 'failed'),
              'Native controller did not stop')
    N.stop_client(api, native_path, native['inputs'])
    D.require(N.owned(api, native_path, native['inputs']) is None, 'Native writer still exists')
    if native['phase'] != 'completed':
        N.save(native_path, native, 'paused', 'infrastructure_recovery')
    return native


def expected_mounts(inputs, selected):
    name = K.COMPONENTS[inputs['component']][0]
    mounts = {v['Destination']: {key: v[key] for key in ('Type', 'Source', 'Destination', 'RW')}
              for v in inputs['before'][name].get('Mounts', [])}
    for item in (inputs.get('nginx') or {}).get('files', []):
        mounts[item['target']] = {'Type': 'bind', 'Source': item['source'], 'Destination': item['target'], 'RW': False}
    actual = {v['Destination']: {key: v[key] for key in ('Type', 'Source', 'Destination', 'RW')} for v in selected.get('Mounts', [])}
    D.require(actual == mounts, 'Service data/configuration mounts changed outside the frozen patch selection')


def component_contract(api, path, inputs, side, runtime, healthy=True):
    name, kind, _ = K.COMPONENTS[inputs['component']]
    selected, old = runtime[name], inputs['before'][name]
    labels = selected.get('Config', {}).get('Labels') or {}
    expected_image = inputs['image_id'] if side == 'forward' else old['Image']
    D.require(selected['Image'] == expected_image and labels.get(K.LABEL) == str(path)
              and labels.get(K.SIDE_LABEL) == side
              and (not healthy or (selected['State']['Running'] and selected.get('RestartCount') == 0)),
              'Component is not the healthy owned selection')
    expected_mounts(inputs, selected)
    D.require(selected['HostConfig'].get('PortBindings') == old['HostConfig'].get('PortBindings')
              and selected['HostConfig'].get('ShmSize') == old['HostConfig'].get('ShmSize')
              and selected['Config'].get('User') == old['Config'].get('User'), 'Service ports, shared memory or user changed')
    networks = selected['NetworkSettings']['Networks']
    D.require(set(networks) == set(old['NetworkSettings']['Networks']), 'Component network set changed')
    for key, row in old['NetworkSettings']['Networks'].items():
        ipv4 = networks[key]['IPAddress']
        ipv6 = networks[key].get('GlobalIPv6Address')
        D.require(ipv4 in (row['IPAddress'], *([''] if not selected['State']['Running'] else []))
                  and ipv6 in (row.get('GlobalIPv6Address'), *([''] if not selected['State']['Running'] else [])),
                  'Frozen component network endpoint changed')
    manifest, _, _ = api.configuration(ROOT, 'dev')
    model = api.compose_model(P.V.LIVE_DIRECTORY, manifest, {}, [path.parent / (side + '.compose.json')], True)
    D.require(not api.live_environment_differences({'services': {name: model['services'][name]}}, {name: selected}),
              'Service environment changed outside the frozen selection')
    command = K.PG_GUARD if kind == 'postgresql' else ['nginx'] if kind == 'nginx' else old['Config'].get('Entrypoint')
    D.require(selected['Config'].get('Entrypoint') == command, 'Service entrypoint differs from the selected patch')
    expected_command = ['-g', 'daemon off;'] if kind == 'nginx' else old['Config'].get('Cmd')
    D.require(selected['Config'].get('Cmd') == expected_command, 'Service command/tuning changed')


def verify(api, path, side, receipt=None, wait=False, progress=False, periodic=False):
    state, inputs = load(path)
    manifest, _, _ = api.configuration(ROOT, 'dev')
    runtime = api.inspect_project(manifest)
    K.unchanged_others(api, inputs['before'], runtime, inputs['component'])
    component_contract(api, path, inputs, side, runtime)
    if receipt:
        api.ensure_stable(receipt['runtime_before'], runtime, 'Runtime changed after infrastructure qualification')
    bridge_path = Path(inputs['base_bridge_state'])
    bridge = R.load_state(api, bridge_path, manifest)
    reviewed = deepcopy(bridge)
    # Every replacement in this baseline is authenticated by the immutable
    # recursive frontend/bridge/infrastructure handoff chain.
    reviewed['before'] = deepcopy(runtime)
    health = R.verify(api, B, P.C, manifest, bridge_path, reviewed, 'forward', wait=wait)
    D.require(health['status'] == 'verified', 'Existing application health is not verified; migration stays paused')
    P.Q.verify_holds(api)
    anchor = (receipt or {}).get('database_after') or json.loads(U.private(path.parent / ('database-before-' + side + '.json')))
    K.check_anchor(anchor, K.stable_database_anchor(api), progress=progress or anchor.get('continuity_mode') == 'bounded_progress')
    if not periodic:
        name, kind, _ = K.COMPONENTS[inputs['component']]
        actual = K.live_fingerprint(api, name, kind)
        version = inputs['target']['version'] if side == 'forward' else inputs['fingerprint_before']['version']
        D.require(actual['version'] == version, 'Live component binary version is unexpected')
        if kind == 'postgresql':
            K.validate_postgres(K.postgres(api, name), inputs['component_before'], allow_minor=True)
        elif kind == 'redis':
            D.require(K.redis(api, name) == inputs['component_before'], 'Redis persistence policy changed')
            D.require(api.run(['docker', 'exec', name, 'redis-cli', 'PING']).strip() == 'PONG', 'Redis is not ready')
        else:
            api.run(['docker', 'exec', name, 'nginx', '-t'])
            S.validate_routes(S.nginx_routes(api, inputs, name))
    api.ensure_stable(runtime, api.inspect_project(manifest), 'Runtime changed during patch verification')
    return runtime


def private_backup(api, path, inputs, side):
    name, kind, _ = K.COMPONENTS[inputs['component']]
    if kind != 'redis':
        return
    source = K.data_mount(inputs['before'][name], '/data')
    receipt = path.parent / ('redis-backup-' + side + '.json')
    if receipt.exists():
        metadata = json.loads(U.private(receipt))
        D.require(re.fullmatch(r'redis-before-(?:forward|rollback)-[a-f0-9]{8}\.tar\.private', metadata['file']),
                  'Unexpected Redis backup filename')
        backup = path.parent / metadata['file']
        D.require(backup.is_file() and not backup.is_symlink(), 'Final Redis backup is unavailable')
        D.require(file_digest(backup) == metadata['sha256'], 'Final Redis backup checksum changed')
        return
    backup = path.parent / ('redis-before-' + side + '-' + uuid.uuid4().hex[:8] + '.tar.private')
    F.private_command(['tar', '--numeric-owner', '-cpf', str(backup), '-C', str(source), '.'],
                      path.parent / ('redis-backup-' + uuid.uuid4().hex[:8] + '.log.private'), 'saving_final_redis_rdb', timeout=600)
    os.chmod(backup, 0o600)
    R.write_new(receipt, R.encoded({'file': backup.name, 'sha256': file_digest(backup), 'automatic_restore': False}))


def file_digest(path):
    digest = hashlib.sha256()
    next_update = time.monotonic() + 20
    with path.open('rb') as file:
        for block in iter(lambda: file.read(1024 * 1024), b''):
            digest.update(block)
            if time.monotonic() >= next_update:
                print(json.dumps({'phase': 'checking_redis_backup'}), flush=True)
                next_update = time.monotonic() + 20
    return digest.hexdigest()


def stopped_redis_check(api, path, inputs, side):
    name = K.COMPONENTS[inputs['component']][0]
    source = K.data_mount(inputs['before'][name], '/data')
    image = inputs['image_id'] if side == 'forward' else inputs['before'][name]['Image']
    filename = inputs['component_before']['dbfilename']
    D.require((source / filename).is_file(), 'The final persisted Redis RDB is missing; keep the original data')
    api.run(['docker', 'run', '--rm', '--pull', 'never', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
             '--mount', 'type=bind,source=' + str(source) + ',target=/data,readonly', '--entrypoint', 'redis-check-rdb',
             image, '/data/' + filename], timeout=120)


def activate(api, path, side):
    state, inputs = load(path)
    D.require(F.source_review(api) == inputs['tool_source_commit'], 'Use this infrastructure release creation commit')
    manifest, _, _ = api.configuration(ROOT, 'dev')
    name, kind, _ = K.COMPONENTS[inputs['component']]
    receipt_path = path.parent / ('receipt-' + side + '.json')
    if receipt_path.exists():
        receipt = json.loads(U.private(receipt_path))
        verify(api, path, side, receipt, wait=True, progress=True)
        return receipt
    if side == 'forward':
        stage_receipt = json.loads(U.private(path.parent / 'stage-receipt.json'))
        D.require(stage_receipt['inputs_sha256'] == state['inputs_sha256'] and stage_receipt['image_id'] == inputs['image_id'],
                  'A successful stage receipt is required')
    runtime = api.inspect_project(manifest, require_running=False, allow_missing_services=(name,))
    K.unchanged_others(api, inputs['before'], runtime, inputs['component'])
    if name in runtime:
        labels = runtime[name].get('Config', {}).get('Labels') or {}
        D.require(runtime[name]['Id'] == inputs['before'][name]['Id'] or labels.get(K.LABEL) == str(path),
                  'Another release owns the selected service')
        if runtime[name]['Id'] == inputs['before'][name]['Id']:
            D.require(runtime[name]['Config'] == inputs['before'][name]['Config']
                      and runtime[name]['HostConfig'] == inputs['before'][name]['HostConfig'],
                      'Original component configuration changed after preparation')
        else:
            selected_side = labels.get(K.SIDE_LABEL)
            D.require(selected_side in ('forward', 'rollback'), 'Owned component has an unknown release side')
            component_contract(api, path, inputs, selected_side, runtime, healthy=False)
        if kind == 'nginx' and runtime[name]['Id'] == inputs['before'][name]['Id']:
            D.require(S.current_nginx_matches(api, inputs, name),
                      'Loaded Nginx configuration changed after staging')
        if kind == 'redis' and runtime[name]['State']['Running']:
            D.require(K.redis(api, name) == inputs['component_before'], 'Redis persistence policy changed after preparation')
    anchor_path = path.parent / ('database-before-' + side + '.json')
    if not anchor_path.exists():
        native = N.load(Path(inputs['normalization_state']))
        try:
            anchor = K.stable_database_anchor(api)
        except D.ConfigError:
            D.require(side == 'rollback' and name == 'db' and (path.parent / 'database-before-forward.json').exists(),
                      'A new DB anchor is unavailable outside the recorded main-database recovery')
            anchor = json.loads(U.private(path.parent / 'database-before-forward.json'))
            meta = (native.get('database') or {}).get('native_meta') or {}
            previous = anchor['native_status']['meta']
            D.require(meta.get('v10_native_job') == previous['v10_native_job']
                      and meta.get('max_block_number', 1) <= previous['max_block_number'],
                      'Saved native metadata does not establish rollback continuity')
            anchor['native_status']['meta'] = meta
            anchor['continuity_mode'] = 'bounded_progress'
        D.require(anchor['database_identity'] == native['inputs']['payload']['database_identity'],
                  'The frozen native database endpoint changed before patching')
        D.require(anchor['native_status']['meta']['v10_native_job'] == native['inputs']['payload']['token'],
                  'The database checkpoint belongs to another normalization job')
        R.write_new(anchor_path, R.encoded(anchor))
    if side not in state['anchor_sha256']:
        # Reconcile a crash between writing the immutable anchor and recording
        # its hash, while the original DB is still available before first stop.
        saved = json.loads(U.private(anchor_path))
        if saved.get('continuity_mode') != 'bounded_progress':
            K.check_anchor(saved, K.stable_database_anchor(api))
        state['anchor_sha256'][side] = R.digest(U.private(anchor_path))
        save(path, state, state['phase'])
    if kind == 'postgresql' and name in runtime and runtime[name]['State']['Running']:
        K.validate_postgres(K.postgres(api, name), inputs['component_before'], allow_minor=True)
    if kind != 'postgresql' or (name in runtime and runtime[name]['State']['Running']):
        expected = json.loads(U.private(anchor_path))
        K.check_anchor(expected, K.stable_database_anchor(api), progress=expected.get('continuity_mode') == 'bounded_progress')
    D.require(N.J.P.free_bytes() >= N.RESUME_FREE, 'Keep the existing 220 GiB deployment/resume reserve')
    api.run(['docker', 'image', 'tag', inputs['before'][name]['Image'], 'deriw-infra:rollback-' + path.parent.name])
    save(path, state, 'activation_requested' if side == 'forward' else 'rollback_requested')
    if kind in ('redis', 'postgresql') and name in runtime and runtime[name]['State']['Running']:
        if kind == 'redis':
            F.private_command(['docker', 'exec', name, 'redis-cli', 'SAVE'],
                              path.parent / ('redis-save-' + uuid.uuid4().hex[:8] + '.log.private'), 'saving_redis', timeout=120)
        api.run(['docker', 'stop', '--time', '120', name], timeout=130)
        stopped = json.loads(api.run(['docker', 'inspect', name]))[0]
        D.require(stopped['State']['Running'] is False and stopped['State']['ExitCode'] == 0,
                  'Component did not shut down cleanly; preserve the data and use the saved recovery journal')
    if kind == 'redis':
        private_backup(api, path, inputs, side)
        stopped_redis_check(api, path, inputs, side)
    elif kind == 'postgresql':
        source = K.data_mount(inputs['before'][name], '/var/lib/postgresql/data')
        D.require((source / 'PG_VERSION').read_text().strip() == '15' and (source / 'global/pg_control').is_file(),
                  'The original PostgreSQL data is unavailable; never initialize or substitute a directory')
    command = ['docker', 'compose', '-p', manifest['compose_project'], '--project-directory', str(P.V.LIVE_DIRECTORY),
               '--env-file', os.devnull, '-f', str(path.parent / (side + '.compose.json')),
               'up', '-d', '--no-deps', '--no-build', '--pull', 'never', name]
    F.private_command(command, path.parent / ('activate-' + uuid.uuid4().hex[:8] + '.log.private'), 'patching_' + inputs['component'], timeout=300)
    deadline = time.monotonic() + 180
    while True:
        try:
            runtime = verify(api, path, side, wait=True)
            break
        except D.ConfigError:
            D.require(time.monotonic() < deadline, 'Patch health did not recover; migration remains paused. Resume or roll back this journal')
            time.sleep(2)
    receipt = {'format': FORMAT, 'side': side, 'inputs_sha256': state['inputs_sha256'],
               'runtime_before': runtime, 'source_root': inputs['source_root'], 'tool_source_commit': inputs['tool_source_commit'],
               'normalization_state': inputs['normalization_state'], 'anchor_sha256': state['anchor_sha256'][side],
               'database_after': K.stable_database_anchor(api)}
    R.write_new(receipt_path, R.encoded(receipt))
    save(path, state, 'active' if side == 'forward' else 'rolled_back')
    return receipt


def native_context(native_path, native):
    filename = native.get('runtime_binding_file', '')
    D.require(re.fullmatch(r'infrastructure-runtime-binding-[a-f0-9]{8}-(?:forward|rollback)\.json', filename),
              'Unexpected infrastructure binding name')
    raw = U.private(native_path.parent / filename)
    binding = json.loads(raw)
    D.require(R.digest(raw) == native['runtime_binding_sha256'] and binding['format'] == FORMAT
              and binding['native_inputs_sha256'] == native['inputs_sha256']
              and binding['native_worker_script_sha256'] == native['inputs']['script_sha256'], 'Infrastructure runtime binding changed')
    path = Path(binding['infrastructure_release_state'])
    state, inputs = load(path)
    receipt_raw = U.private(path.parent / ('receipt-' + binding['side'] + '.json'))
    receipt = json.loads(receipt_raw)
    D.require(R.digest(receipt_raw) == binding['receipt_sha256'] and receipt['format'] == FORMAT
              and receipt['side'] == binding['side'] and receipt['inputs_sha256'] == state['inputs_sha256']
              and receipt['runtime_before'] == binding['runtime_before']
              and inputs['normalization_state'] == str(native_path) and inputs['native_inputs_sha256'] == native['inputs_sha256']
              and binding['source_root'] == inputs['source_root'] and binding['tool_source_commit'] == inputs['tool_source_commit']
              and receipt['anchor_sha256'] == state['anchor_sha256'][binding['side']], 'Infrastructure handoff is not the qualified release')
    prior = deepcopy(native)
    prior.pop('runtime_binding_file', None)
    prior.pop('runtime_binding_sha256', None)
    prior.update(inputs['prior_binding'])
    previous = N.runtime_binding(native_path, prior)
    D.require(previous is not None, 'Infrastructure handoff lost its previous deployment proof')
    K.unchanged_others(D, previous['runtime_before'], binding['runtime_before'], inputs['component'])
    return {**binding, 'previous_source_root': previous['source_root'], 'release_state': inputs['base_bridge_state']}


def verify_native(api, native_path, native, context):
    path = Path(context['infrastructure_release_state'])
    receipt = json.loads(U.private(path.parent / ('receipt-' + context['side'] + '.json')))
    verify(api, path, context['side'], receipt, progress=True, periodic=True)
    return {'status': 'verified', 'infrastructure_and_existing_application_verified': True}


def bind_native(api, path, side):
    state, inputs = load(path)
    receipt_path = path.parent / ('receipt-' + side + '.json')
    receipt = json.loads(U.private(receipt_path))
    verify(api, path, side, receipt, progress=True)
    native_path = Path(inputs['normalization_state'])
    native = N.load(native_path)
    own_native_binding(native, inputs, path)
    current = K.stable_database_anchor(api)
    K.check_anchor(receipt['database_after'], current, progress=True)
    D.require(native['phase'] != 'completed' or current['native_status']['status'] == 'completed',
              'A completed native database record cannot be reopened')
    completed = current['native_status']['status'] == 'completed'
    D.require(native['inputs_sha256'] == inputs['native_inputs_sha256'], 'Frozen native inputs changed')
    filename = 'infrastructure-runtime-binding-' + path.parent.name.rsplit('-', 1)[1] + '-' + side + '.json'
    binding = {'format': FORMAT, 'infrastructure_release_state': str(path), 'side': side,
               'native_inputs_sha256': native['inputs_sha256'], 'native_worker_script_sha256': native['inputs']['script_sha256'],
               'receipt_sha256': R.digest(U.private(receipt_path)), 'source_root': inputs['source_root'],
               'tool_source_commit': inputs['tool_source_commit'], 'runtime_before': receipt['runtime_before']}
    target = native_path.parent / filename
    if target.exists():
        D.require(U.private(target) == R.encoded(binding), 'Existing infrastructure binding differs')
    else:
        R.write_new(target, R.encoded(binding))
    native.update(runtime_binding_file=filename, runtime_binding_sha256=R.digest(U.private(target)),
                  transient_retries=0, worker_crash_retries=0)
    native.setdefault('database', {}).update(native_status=current['native_status']['status'],
                                           native_meta=current['native_status']['meta'])
    N.runtime_binding(native_path, native)
    N.save(native_path, native, 'completed' if completed else 'prepared')
    request = native_path.parent / 'pause.request'
    if request.exists():
        U.private(request)
        request.unlink()
    return N.public(native_path, native)


def resume_native(api, path, side):
    _, inputs = load(path)
    native_path = Path(inputs['normalization_state'])
    native = N.load(native_path)
    result = N.install_service(api, native_path, start=native['phase'] != 'completed')
    if native['phase'] == 'completed':
        return result
    deadline = time.monotonic() + 90
    next_update = time.monotonic() + 20
    while time.monotonic() < deadline:
        native = N.load(native_path)
        if native['phase'] in ('running', 'waiting', 'completed'):
            context = N.runtime_binding(native_path, native)
            D.require(context['infrastructure_release_state'] == str(path) and context['side'] == side,
                      'Another runtime binding replaced the resumed migration')
            return N.public(native_path, native)
        D.require(native['phase'] != 'needs_attention', 'Native resume needs attention; preserve the same checkpoint')
        if time.monotonic() >= next_update:
            print(json.dumps({'phase': 'waiting_for_native_checkpoint', 'state': str(native_path)}), flush=True)
            next_update = time.monotonic() + 20
        time.sleep(1)
    raise D.ConfigError('Native service start was requested but no current running/waiting checkpoint was observed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    for name in ('prepare', 'run', 'stage', 'resume', 'status', 'rollback'):
        modes.add_argument('--' + name, action='store_true')
    parser.add_argument('--component', choices=tuple(K.COMPONENTS))
    parser.add_argument('--normalization-state', type=Path)
    parser.add_argument('--state', type=Path)
    parser.add_argument('--tls-ca', type=Path)
    args = parser.parse_args()
    path = args.state
    try:
        D.require(sys.platform.startswith('linux') and os.geteuid() == 0, 'Run on the dev Linux server as root')
        if args.prepare or args.run:
            D.require(path is None and args.component and args.normalization_state, 'Provide the component and existing native job state')
            path = prepare(D, args.normalization_state, args.component, args.tls_ca)
        else:
            D.require(path is not None and not args.component and not args.normalization_state and not args.tls_ca,
                      'Resume/status/rollback uses only its saved infrastructure state')
        state, inputs = load(path)
        if args.status or args.prepare:
            print(json.dumps(public(path, state), indent=2))
            return 0
        if state['phase'] in ('finished', 'rollback_finished') and not args.rollback:
            side = 'rollback' if state['phase'] == 'rollback_finished' else 'forward'
            verify(D, path, side, json.loads(U.private(path.parent / ('receipt-' + side + '.json'))), progress=True)
            print(json.dumps(public(path, state), indent=2))
            return 0
        if state['phase'] == 'prepared' and not args.rollback:
            stage(D, path)
        if args.stage:
            print(json.dumps(public(path), indent=2))
            return 0
        side = 'rollback' if args.rollback else 'forward'
        if args.rollback:
            D.require((path.parent / 'database-before-forward.json').exists(),
                      'No activation was requested; the original service is unchanged and needs no rollback')
        D.require(not (state['phase'] in ('rolled_back', 'rollback_finished') and side == 'forward'),
                  'This release was rolled back; do not reactivate it with resume')
        pause(D, path, side)
        with M.global_lock(True), B.build_lock(D, D.MANAGED_RELEASES, ROOT):
            activate(D, path, side)
            normalization = bind_native(D, path, side)
        normalization = resume_native(D, path, side)
        state, _ = load(path)
        save(path, state, 'finished' if side == 'forward' else 'rollback_finished')
        print(json.dumps({**public(path, state), 'normalization': normalization}, indent=2))
        return 0
    except Exception as error:
        print(json.dumps({'status': 'stopped', 'state': str(path) if path else None, 'raw_values_suppressed': True,
                          'reason': str(error) if isinstance(error, D.ConfigError) else 'Infrastructure operation failed; private evidence retained',
                          'recovery': 'Keep this journal and all native inputs. Resume or roll back the same state. Unknown runtime changes leave normalization paused; no data restore or cursor reset is automatic.'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
