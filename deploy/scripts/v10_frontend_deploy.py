#!/usr/bin/env python3
"""Deploy the pinned Deriw UI without restarting backend or resetting normalization."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

import candidate_build as B
import containerd_maintenance as M
import dev_config as D
import release as R
import v10_bridge_deploy as P
import v10_native_normalization as N
import v10_upgrade as U

ROOT = Path(__file__).resolve().parents[2]
BASE = Path('/data/blockscout-dev-private/frontend-releases')
FORMAT = 'deriw-frontend-release-v1'
REVISION = '27a4139d09ac57d975cc8c2bd4f1d804c8589f20'
VERSION = '2.7.2-deriw.1'
BACKEND = '875082f41d79ef72cd9dd9a4e771953a181c6cf2'
LABEL = 'org.deriw.frontend-release-state'
SIDE_LABEL = 'org.deriw.frontend-release-side'


def source_review(api):
    source, version = B.clean_source(api, ROOT)
    D.require(version == '10.2.6', 'Frontend tool requires the reviewed backend v10 source')
    paths = api.run(['git', '-C', str(ROOT), 'diff', '--name-only', BACKEND, source]).splitlines()
    D.require(all(p == 'README.md' or p == 'docker-compose/environments/README.md' or
                  p.startswith(('deploy/', 'docs/')) for p in paths), 'Backend application/configuration changed')
    return source


def effective_environment(raw, script):
    """Read the generated public script as data, without executing JavaScript."""
    lines = script.strip().splitlines()
    D.require(len(lines) >= 2 and lines[0] == 'window.__envs = {' and lines[-1] == '}',
              'Unrecognized current frontend environment script')
    result = {k: v for k, v in raw.items() if k.startswith('NEXT_PUBLIC_') or k in ('PORT', 'ENVS_PRESET')}
    for line in lines[1:-1]:
        if not line.strip():
            continue
        match = re.fullmatch(r'([A-Z][A-Z0-9_]*): "(.*)",', line)
        D.require(match is not None and match[1].startswith('NEXT_PUBLIC_'), 'Unrecognized public setting encoding')
        key, value = match.groups()
        result[key] = raw[key] if key in raw and raw[key].replace('"', "'") == value else value
    return result


def migrate_environment(environment, chain_id):
    result = dict(environment)
    D.require(result.get('NEXT_PUBLIC_NETWORK_ID') == str(chain_id), 'Frontend chain differs from dev')
    D.require(result.get('NEXT_PUBLIC_ROLLUP_TYPE') == 'arbitrum', 'Expected the Deriw Arbitrum rollup')
    parent = result.get('NEXT_PUBLIC_ROLLUP_PARENT_CHAIN', '')
    try:
        parent = json.loads(parent)
    except ValueError:
        try:
            parent = json.loads(parent.replace("'", '"'))
        except ValueError:
            raise D.ConfigError('Parent chain setting is not a JSON object; values suppressed') from None
    D.require(isinstance(parent, dict), 'Parent chain setting must be an object')
    parent['baseUrl'] = parent.get('baseUrl') or result.get('NEXT_PUBLIC_ROLLUP_L1_BASE_URL')
    D.require(isinstance(parent.get('baseUrl'), str) and parent['baseUrl'].startswith(('https://', 'http://'))
              and bool(parent.get('name')), 'Parent explorer name/baseUrl is missing')
    result['NEXT_PUBLIC_ROLLUP_PARENT_CHAIN'] = json.dumps(parent, separators=(',', ':'))
    result['NEXT_PUBLIC_ROLLUP_LAYER_NUMBER'] = '3'
    result['NEXT_PUBLIC_GIT_COMMIT_SHA'] = REVISION
    result['NEXT_PUBLIC_GIT_TAG'] = 'v' + VERSION
    result['ENVS_PRESET'] = 'none'
    result['SKIP_ENVS_VALIDATION'] = 'false'
    for name in ('NEXT_PUBLIC_ROLLUP_L1_BASE_URL', 'NEXT_PUBLIC_API_SPEC_URL'):
        result.pop(name, None)
    D.require(all(isinstance(v, str) and '\n' not in v and '\r' not in v and '\0' not in v for v in result.values()),
              'Frontend environment contains an unsupported value')
    return result


def snapshot(model, before, image, environment, path, side):
    result = deepcopy(model)
    for name, service in result['services'].items():
        R.freeze_null_environment(service, before[name])
        service.pop('build', None)
        service.update(image=before[name]['Image'], pull_policy='never')
    frontend = result['services']['frontend']
    if side == 'forward':
        frontend.update(image=image, environment=environment)
    else:
        frontend['environment'] = D.env_map(before['frontend'])
    frontend.setdefault('labels', {}).update({LABEL: str(path), SIDE_LABEL: side})
    return result


def image_info(api, reference):
    rows = json.loads(api.run(['docker', 'image', 'inspect', reference]))
    D.require(len(rows) == 1, 'Ambiguous frontend image')
    row = rows[0]
    labels = row.get('Config', {}).get('Labels') or {}
    D.require(row.get('Os') == 'linux' and row.get('Architecture') == 'amd64'
              and labels.get('org.opencontainers.image.revision') == REVISION
              and labels.get('org.opencontainers.image.version') == VERSION,
              'Candidate image platform/version/source differs from the reviewed frontend')
    return row


def load(path):
    D.require(path.is_absolute() and path.resolve() == path and path.name == 'state.json'
              and path.parent.parent == BASE and re.fullmatch(r'frontend-[0-9TZ-]+-[a-f0-9]{8}', path.parent.name),
              'Unexpected frontend release state path')
    N.J.owned_directory(path.parent)
    state = json.loads(U.private(path))
    raw = U.private(path.parent / 'inputs.json')
    inputs = json.loads(raw)
    D.require(state.get('format') == FORMAT and state.get('inputs_sha256') == R.digest(raw)
              and inputs.get('format') == FORMAT and inputs.get('frontend_revision') == REVISION,
              'Frontend release inputs changed')
    D.require(state.get('phase') in ('prepared', 'staged', 'activation_requested', 'rollback_requested', 'active', 'rolled_back'),
              'Unrecognized frontend release phase')
    for side in ('forward', 'rollback'):
        D.require(R.digest(U.private(path.parent / (side + '.compose.json'))) == inputs['snapshots'][side],
                  'Frontend Compose snapshot changed')
    return state, inputs


def save(path, state, phase):
    state.update(phase=phase, observed_at_utc=datetime.now(timezone.utc).isoformat())
    R.update_state(path, state)


def public(path, state):
    return {'status': 'frontend_' + state['phase'], 'state': str(path), 'frontend_version': VERSION,
            'frontend_revision': REVISION, 'backend_restarted': False, 'database_migrations_run': False,
            'normalization_checkpoint_preserved': True}


def private_command(command, log, phase, timeout=480):
    with os.fdopen(os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
        process = subprocess.Popen(command, stdout=stream, stderr=stream, env=D.compose_environment({}))
        started = time.monotonic()
        next_update = started + 20
        try:
            while process.poll() is None:
                D.require(time.monotonic() - started < timeout, phase + ' timed out; private log retained')
                if time.monotonic() >= next_update:
                    print(json.dumps({'phase': phase, 'elapsed_seconds': int(time.monotonic() - started)}), flush=True)
                    next_update = time.monotonic() + 20
                time.sleep(1)
            D.require(process.returncode == 0, phase + ' failed; private log retained')
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


def wait_health(api, name, seconds=300):
    deadline = time.monotonic() + seconds
    while True:
        try:
            value = api.run(['docker', 'exec', name, 'node', '-e',
                "fetch('http://127.0.0.1:3000/node-api/healthz').then(async r=>{if(r.status!==200||JSON.parse(await r.text())!=='ok')process.exit(1)}).catch(()=>process.exit(1))"], timeout=10)
            return
        except D.ConfigError:
            D.require(time.monotonic() < deadline, 'Frontend startup/validation did not become healthy; private logs retained')
            time.sleep(2)


def remove_stage(api, name, path):
    ids = api.run(['docker', 'ps', '-aq', '--filter', 'name=^/' + name + '$']).split()
    if not ids:
        return
    row = json.loads(api.run(['docker', 'inspect', name]))[0]
    D.require((row.get('Config', {}).get('Labels') or {}).get(LABEL) == str(path), 'Frontend staging container ownership changed')
    captured = subprocess.run(['docker', 'logs', name], capture_output=True)
    logs = captured.stdout + captured.stderr
    target = path.parent / ('stage-' + uuid.uuid4().hex[:8] + '.log.private')
    R.write_new(target, logs)
    api.run(['docker', 'rm', '-f', name], timeout=30)


def prepare(api, native_path, reference):
    D.require(re.fullmatch(r'quanquanah/blockscout-frontend@sha256:[a-f0-9]{64}', reference),
              'Use the immutable published frontend digest')
    source = source_review(api)
    manifest, _, _ = api.configuration(ROOT, 'dev')
    native = N.load(native_path)
    context = N.runtime_binding(native_path, native) or native['inputs']
    D.require(context.get('release_state'), 'Expected the verified bridge deployment handoff')
    before = api.inspect_project(manifest)
    api.ensure_stable(context['runtime_before'], before, 'Runtime changed before frontend preparation')
    P.Q.verify_holds(api)
    B.validate_state_directory(api, BASE, ROOT)
    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    B.validate_state_directory(api, BASE, ROOT)
    directory = BASE / ('frontend-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8])
    directory.mkdir(mode=0o700)
    path = directory / 'state.json'
    private_command(['docker', 'pull', '--platform', 'linux/amd64', reference], directory / 'pull.log.private', 'pulling_frontend')
    image = image_info(api, reference)
    script = api.run(['docker', 'exec', 'frontend', 'cat', '/app/public/assets/envs.js'])
    environment = migrate_environment(effective_environment(api.env_map(before['frontend']), script), manifest['chain_id'])
    files = api.live_files(P.V.LIVE_DIRECTORY, before['backend'], manifest)
    model = api.compose_model(P.V.LIVE_DIRECTORY, manifest, {}, files, True)
    D.require(not api.live_environment_differences(model, before), 'Current Compose environment differs from running services')
    snapshots = {}
    for side in ('forward', 'rollback'):
        result = snapshot(model, before, image['Id'], environment, path, side)
        raw = R.snapshot_bytes(result)
        R.write_new(directory / (side + '.compose.json'), raw)
        R.check_snapshot(api, directory / (side + '.compose.json'), result, manifest, P.V.LIVE_DIRECTORY)
        snapshots[side] = R.digest(raw)
    prior = {k: native[k] for k in ('runtime_binding_file', 'runtime_binding_sha256') if k in native}
    inputs = {'format': FORMAT, 'frontend_revision': REVISION, 'image_reference': reference, 'image_id': image['Id'],
              'source_root': str(ROOT), 'tool_source_commit': source, 'normalization_state': str(native_path),
              'native_inputs_sha256': native['inputs_sha256'], 'prior_binding': prior, 'before': before,
              'base_bridge_state': context['release_state'], 'snapshots': snapshots, 'environment': environment}
    R.write_new(directory / 'inputs.json', R.encoded(inputs))
    state = {'format': FORMAT, 'inputs_sha256': R.digest(R.encoded(inputs)), 'phase': 'prepared'}
    save(path, state, 'prepared')
    print(json.dumps(public(path, state)), flush=True)
    return path


def stage(api, path, state, inputs, manifest):
    name = 'deriw-frontend-check-' + path.parent.name.rsplit('-', 1)[1]
    environment = path.parent / 'candidate.env.private'
    encoded = ''.join(k + '=' + v + '\n' for k, v in sorted(inputs['environment'].items())).encode()
    if not environment.exists():
        R.write_new(environment, encoded)
    D.require(U.private(environment) == encoded, 'Frontend staging environment changed')
    networks = inputs['before']['frontend']['NetworkSettings']['Networks']
    D.require(len(networks) == 1, 'Frontend staging needs the captured single network')
    remove_stage(api, name, path)
    try:
        api.run(['docker', 'run', '-d', '--name', name, '--label', LABEL + '=' + str(path),
                 '--network', next(iter(networks)), '--env-file', str(environment), '--restart', 'no',
                 '--cpus', '1', '--memory', '1g', '--security-opt', 'no-new-privileges:true',
                 '--log-driver', 'json-file', '--log-opt', 'max-size=8m', '--log-opt', 'max-file=2', inputs['image_id']], timeout=30)
        wait_health(api, name)
        api.ensure_stable(inputs['before'], api.inspect_project(manifest), 'Runtime changed during frontend staging')
    finally:
        remove_stage(api, name, path)
    proof = R.encoded({'inputs_sha256': state['inputs_sha256'], 'image_id': inputs['image_id']})
    proof_path = path.parent / 'stage-receipt.json'
    if proof_path.exists():
        D.require(U.private(proof_path) == proof, 'Frontend staging receipt changed')
    else:
        R.write_new(proof_path, proof)
    save(path, state, 'staged')


def unchanged_others(api, before, after):
    api.ensure_stable({k: v for k, v in before.items() if k != 'frontend'},
                      {k: v for k, v in after.items() if k != 'frontend'}, 'Another service changed during frontend deployment')


def verify(api, path, side, receipt=None):
    state, inputs = load(path)
    manifest, _, _ = api.configuration(ROOT, 'dev')
    runtime = api.inspect_project(manifest)
    unchanged_others(api, inputs['before'], runtime)
    frontend = runtime['frontend']
    D.require(frontend.get('State', {}).get('Running') is True and frontend.get('RestartCount') == 0,
              'Frontend is not running without restarts')
    expected = inputs['image_id'] if side == 'forward' else inputs['before']['frontend']['Image']
    labels = frontend.get('Config', {}).get('Labels') or {}
    D.require(frontend['Image'] == expected and labels.get(LABEL) == str(path) and labels.get(SIDE_LABEL) == side,
              'Frontend is not the selected deployment side')
    old = inputs['before']['frontend']
    D.require(api.inspect_mounts(old.get('Mounts')) == api.inspect_mounts(frontend.get('Mounts'))
              and old['HostConfig'].get('PortBindings') == frontend['HostConfig'].get('PortBindings')
              and set(old['NetworkSettings']['Networks']) == set(frontend['NetworkSettings']['Networks']),
              'Frontend mounts, ports or networks changed')
    model = api.compose_model(P.V.LIVE_DIRECTORY, manifest, {}, [path.parent / (side + '.compose.json')], True)
    D.require(not api.live_environment_differences({'services': {'frontend': model['services']['frontend']}}, {'frontend': frontend}),
              'Frontend environment differs from the frozen selection')
    if receipt is not None:
        api.ensure_stable(receipt['runtime_before'], runtime, 'Runtime changed after frontend qualification')
    wait_health(api, 'frontend', seconds=0)
    bridge_path = Path(inputs['base_bridge_state'])
    bridge = R.load_state(api, bridge_path, manifest)
    # The immutable frontend receipt supplies the only changed service in the existing backend verification.
    reviewed = deepcopy(bridge)
    reviewed['before']['frontend'] = runtime['frontend']
    health = R.verify(api, B, P.C, manifest, bridge_path, reviewed, 'forward')
    D.require(health['status'] == 'verified', 'Existing backend health is not verified; native writer stays paused')
    api.ensure_stable(runtime, api.inspect_project(manifest), 'Runtime changed during frontend verification')
    return runtime


def activate(api, path, side):
    state, inputs = load(path)
    manifest, _, _ = api.configuration(ROOT, 'dev')
    source_review(api)
    D.require(B.clean_source(api, ROOT)[0] == inputs['tool_source_commit'], 'Use the frontend deployment creation commit')
    if side == 'forward':
        D.require(json.loads(U.private(path.parent / 'stage-receipt.json')) ==
                  {'inputs_sha256': state['inputs_sha256'], 'image_id': inputs['image_id']},
                  'A successful frontend staging receipt is required')
        D.require(image_info(api, inputs['image_id'])['Id'] == inputs['image_id'], 'Frontend image changed')
    receipt_path = path.parent / ('receipt-' + side + '.json')
    if receipt_path.exists():
        receipt = json.loads(U.private(receipt_path))
        verify(api, path, side, receipt)
        return receipt
    before = api.inspect_project(manifest, require_running=False, allow_missing_services=('frontend',))
    unchanged_others(api, inputs['before'], before)
    if 'frontend' in before:
        frontend = before['frontend']
        labels = frontend.get('Config', {}).get('Labels') or {}
        D.require(frontend['Id'] == inputs['before']['frontend']['Id'] or labels.get(LABEL) == str(path),
                  'Another release owns frontend')
    anchor_file = path.parent / ('database-before-' + side + '.json')
    if not anchor_file.exists():
        R.write_new(anchor_file, R.encoded(P.database_anchor(api)))
    anchor = json.loads(U.private(anchor_file))
    D.require(P.database_anchor(api) == anchor, 'Native checkpoint changed before frontend activation')
    api.run(['docker', 'image', 'tag', inputs['before']['frontend']['Image'],
             'deriw-frontend:rollback-' + path.parent.name])
    save(path, state, 'activation_requested' if side == 'forward' else 'rollback_requested')
    command = ['docker', 'compose', '-p', manifest['compose_project'], '--project-directory', str(P.V.LIVE_DIRECTORY),
               '--env-file', os.devnull, '-f', str(path.parent / (side + '.compose.json')),
               'up', '-d', '--no-deps', '--no-build', '--pull', 'never', 'frontend']
    private_command(command, path.parent / ('activate-' + uuid.uuid4().hex[:8] + '.log.private'), 'recreating_frontend')
    wait_health(api, 'frontend')
    runtime = verify(api, path, side)
    D.require(P.database_anchor(api) == anchor, 'Database migration/cursor/heap changed across frontend deployment')
    receipt = {'format': FORMAT, 'side': side, 'inputs_sha256': state['inputs_sha256'],
               'runtime_before': runtime, 'tool_source_commit': inputs['tool_source_commit'],
               'source_root': inputs['source_root'], 'normalization_state': inputs['normalization_state']}
    R.write_new(receipt_path, R.encoded(receipt))
    save(path, state, 'active' if side == 'forward' else 'rolled_back')
    return receipt


def native_context(native_path, native):
    filename = native.get('runtime_binding_file', '')
    D.require(re.fullmatch(r'frontend-runtime-binding-[a-f0-9]{8}-(?:forward|rollback)\.json', filename), 'Unexpected frontend runtime binding name')
    raw = U.private(native_path.parent / filename)
    binding = json.loads(raw)
    D.require(R.digest(raw) == native['runtime_binding_sha256'] and binding.get('format') == FORMAT
              and binding['native_inputs_sha256'] == native['inputs_sha256']
              and binding['native_worker_script_sha256'] == native['inputs']['script_sha256'], 'Frontend runtime binding changed')
    path = Path(binding['frontend_release_state'])
    _, inputs = load(path)
    receipt_raw = U.private(path.parent / ('receipt-' + binding['side'] + '.json'))
    receipt = json.loads(receipt_raw)
    D.require(R.digest(receipt_raw) == binding['receipt_sha256'] and receipt.get('format') == FORMAT
              and receipt.get('side') == binding['side'] and receipt['inputs_sha256'] == R.digest(U.private(path.parent / 'inputs.json'))
              and inputs['normalization_state'] == str(native_path) and inputs['native_inputs_sha256'] == native['inputs_sha256']
              and binding['tool_source_commit'] == inputs['tool_source_commit'] and binding['source_root'] == inputs['source_root']
              and binding['runtime_before'] == receipt['runtime_before'], 'Frontend handoff is not the verified release')
    prior = deepcopy(native)
    prior.pop('runtime_binding_file', None)
    prior.pop('runtime_binding_sha256', None)
    prior.update(inputs['prior_binding'])
    context = N.runtime_binding(native_path, prior)
    D.require(context is not None, 'Frontend handoff lost its previous deployment proof')
    unchanged_others(D, context['runtime_before'], binding['runtime_before'])
    return {**binding, 'previous_source_root': context['source_root'], 'release_state': inputs['base_bridge_state']}


def verify_native(api, native_path, native, context):
    path = Path(context['frontend_release_state'])
    receipt = json.loads(U.private(path.parent / ('receipt-' + context['side'] + '.json')))
    verify(api, path, context['side'], receipt)
    return {'status': 'verified', 'frontend_and_backend_verified': True}


def bind_native(api, path, side):
    state, inputs = load(path)
    receipt_path = path.parent / ('receipt-' + side + '.json')
    receipt = json.loads(U.private(receipt_path))
    verify(api, path, side, receipt)
    native_path = Path(inputs['normalization_state'])
    native = N.load(native_path)
    D.require(native['inputs_sha256'] == inputs['native_inputs_sha256'], 'Native frozen inputs changed')
    if native['phase'] == 'completed':
        return N.public(native_path, native)
    filename = 'frontend-runtime-binding-' + path.parent.name.rsplit('-', 1)[1] + '-' + side + '.json'
    binding = {'format': FORMAT, 'native_inputs_sha256': native['inputs_sha256'],
               'native_worker_script_sha256': native['inputs']['script_sha256'], 'frontend_release_state': str(path),
               'receipt_sha256': R.digest(U.private(receipt_path)), 'side': side,
               'tool_source_commit': inputs['tool_source_commit'], 'source_root': inputs['source_root'],
               'runtime_before': receipt['runtime_before']}
    target = native_path.parent / filename
    if target.exists():
        D.require(U.private(target) == R.encoded(binding), 'Existing frontend binding differs')
    else:
        R.write_new(target, R.encoded(binding))
    native.update(runtime_binding_file=filename, runtime_binding_sha256=R.digest(U.private(target)),
                  transient_retries=0, worker_crash_retries=0)
    N.runtime_binding(native_path, native)
    N.save(native_path, native, 'prepared')
    request = native_path.parent / 'pause.request'
    if request.exists():
        U.private(request)
        request.unlink()
    return N.public(native_path, native)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    for mode in ('run', 'resume', 'status', 'rollback'):
        modes.add_argument('--' + mode, action='store_true')
    parser.add_argument('--normalization-state', type=Path)
    parser.add_argument('--image')
    parser.add_argument('--state', type=Path)
    args = parser.parse_args()
    path = args.state
    try:
        D.require(sys.platform.startswith('linux') and os.geteuid() == 0, 'Run on the Linux dev server as root')
        if args.run:
            D.require(path is None and args.normalization_state and args.image, 'Provide native job and immutable frontend image')
            path = prepare(D, args.normalization_state, args.image)
        else:
            D.require(path is not None and not args.normalization_state and not args.image, 'Use only the saved frontend release state')
        state, inputs = load(path)
        if args.status:
            print(json.dumps(public(path, state), indent=2))
            return 0
        manifest, _, _ = D.configuration(ROOT, 'dev')
        if state['phase'] == 'prepared' and not args.rollback:
            stage(D, path, state, inputs, manifest)
        D.require(not (state['phase'] == 'rolled_back' and not args.rollback),
                  'This release was rolled back; use --rollback to reconcile that handoff')
        P.pause_native(D, Path(inputs['normalization_state']), 'frontend_deployment')
        with M.global_lock(True), B.build_lock(D, D.MANAGED_RELEASES, ROOT):
            side = 'rollback' if args.rollback else 'forward'
            activate(D, path, side)
            result = public(path, load(path)[0])
            result['normalization'] = bind_native(D, path, side)
        if result['normalization']['normalization_complete']:
            result['normalization_service'] = {'service_start_requested': False, 'normalization_complete': True}
        else:
            result['normalization_service'] = N.install_service(D, Path(inputs['normalization_state']))
        print(json.dumps(result, indent=2))
        return 0
    except Exception as error:
        print(json.dumps({'status': 'stopped', 'state': str(path) if path else None, 'raw_values_suppressed': True,
                          'reason': str(error) if isinstance(error, D.ConfigError) else 'Frontend deployment check failed; private files retained',
                          'recovery': 'Keep all private journals. Resume this frontend state, or use --rollback with the same state. Native checkpoints are retained.'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
