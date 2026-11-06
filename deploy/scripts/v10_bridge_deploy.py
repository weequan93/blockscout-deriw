#!/usr/bin/env python3
"""Build and deploy the reviewed v10 bridge fix, preserving native normalization."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

import candidate_build as B
import containerd_maintenance as M
import deploy_check as C
import dev_config as D
import release as R
import v10_candidate as V
import v10_empty_blocks_hold as E
import v10_metrics_hold as H
import v10_native_normalization as N
import v10_normalization_reuse as Q
import v10_upgrade as U

ROOT = Path(__file__).resolve().parents[2]
ORIGINAL = 'f8119a0a2f65593d184be66a57480660e3663b25'
FIX = '427661a011f635a0d6852570b09abdc910c6c727'
WORKER = 'apps/indexer/lib/indexer/fetcher/arbitrum/workers/new_messages_to_l2.ex'
INPUT_FORMAT = 'blockscout-v10-bridge-deploy-inputs-v1'


def source_review(api, root):
    source = B.clean_source(api, root)
    D.require(source[1] == '10.2.6', 'Bridge release requires v10.2.6')
    api.run(['git', '-C', str(root), 'merge-base', '--is-ancestor', FIX, source[0]])
    changes = api.run(['git', '-C', str(root), 'diff', '--name-only', ORIGINAL, source[0]]).splitlines()
    D.require(WORKER in changes and all(p == WORKER or p.startswith(
        ('deploy/', 'docs/upgrades/', 'apps/indexer/test/')) for p in changes),
        'Bridge release contains another application/configuration change')
    D.require(api.run(['git', '-C', str(root), 'show', source[0] + ':' + WORKER]) ==
              api.run(['git', '-C', str(root), 'show', FIX + ':' + WORKER]),
              'Bridge worker differs from the reviewed correction')
    return source


def pause_native(api, path):
    state = N.load(path)
    inputs = state['inputs']
    binding = N.runtime_binding(path, state)
    root = Path((binding or inputs)['source_root'])
    target = N.SYSTEMD_DIRECTORY / N.UNIT
    definitions = [N.render_service(path, root, inputs['python_executable'])]
    if binding:
        definitions.append(N.render_service(path, Path(inputs['source_root']), inputs['python_executable']))
    D.require(target.is_file() and not target.is_symlink() and target.read_text() in definitions,
              'Native service ownership changed')
    if state['phase'] != 'completed':
        request = path.parent / 'pause.request'
        if not request.exists():
            R.write_new(request, b'bridge deployment\n')
    api.run(['systemctl', 'stop', N.UNIT], timeout=65)
    status = api.run(['systemctl', 'show', N.UNIT, '--property=ActiveState', '--value'], timeout=10).strip()
    D.require(status in ('inactive', 'failed'), 'Native controller is still active; deployment not started')
    state = N.load(path)
    N.stop_client(api, path, inputs)
    D.require(N.owned(api, path, inputs) is None, 'Native writer still exists; deployment not started')
    name = inputs['client_name']
    D.require(re.fullmatch(r'blockscout-v10-native-[a-f0-9]{16}', name), 'Unexpected native database application name')
    deadline = time.monotonic() + 30
    code = '''
[[remaining]] = Explorer.Repo.query!("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND application_name=$1",
  ["''' + name + '''"], log: false, timeout: 2000).rows
IO.puts("BLOCKSCOUT_DEPLOY_CHECK=" <> Jason.encode!(%{remaining_native_connections: remaining}))
'''
    while N.rpc(api, code)['remaining_native_connections']:
        D.require(time.monotonic() < deadline, 'Native database connection cleanup is pending; deployment not started')
        time.sleep(0.25)
    if state['phase'] != 'completed':
        N.save(path, state, 'paused', 'bridge_deployment')
    return state


def database_anchor(api):
    return N.rpc(api, '''
true = Explorer.Migrator.StartupControl.disabled?()
{:ok, result} = Explorer.Repo.transaction(fn ->
  Explorer.Repo.query!("SET TRANSACTION READ ONLY", [], log: false)
  Explorer.Repo.query!("SET LOCAL statement_timeout='1500ms'", [], log: false)
  [[value]] = Explorer.Repo.query!(~S"""
  SELECT jsonb_build_object('database_oid',(SELECT oid::bigint FROM pg_database WHERE datname=current_database()),
    'heap_identity',pg_relation_filenode('public.internal_transactions'::regclass)::bigint,
    'versions',(SELECT jsonb_agg(version ORDER BY version) FROM schema_migrations),
    'native_status',(SELECT to_jsonb(s) FROM migrations_status s
      WHERE migration_name='empty_internal_transactions_data'))
  """, [], log: false, timeout: 2000).rows
  value
end, timeout: 5000)
IO.puts("BLOCKSCOUT_DEPLOY_CHECK=" <> Jason.encode!(result))
''')


def snapshot(model, before, image, source, path, side):
    result = R.snapshot(model, before, image, path, side)
    backend = result['services']['backend']
    backend['labels'].pop(U.STATE_LABEL, None)
    backend['labels'].pop(U.ROLE_LABEL, None)
    if side == 'forward':
        backend['environment'].update(RELEASE_VERSION='10.2.6', BLOCKSCOUT_VERSION=source,
                                      MIGRATION_BACKGROUND_AUTOSTART_DISABLED='true')
    return result


def load(api, path):
    D.require(path.is_absolute() and path.resolve() == path and path.name == 'state.json'
              and path.parent.parent == D.MANAGED_RELEASES
              and re.fullmatch(r'release-[0-9TZ-]+-[a-f0-9]{8}', path.parent.name),
              'Unexpected bridge release state path')
    N.J.owned_directory(path.parent)
    state = json.loads(U.private(path))
    raw = U.private(path.parent / 'inputs.json')
    inputs = json.loads(raw)
    D.require(state.get('bridge_patch') is True and inputs.get('format') == INPUT_FORMAT
              and R.digest(raw) == state['bridge_inputs_sha256']
              and state['candidate_source_commit'] == inputs['source']['source_commit']
              and state['source_root'] == inputs['source_root']
              and state['normalization_state'] == inputs['normalization_state'], 'Bridge deployment inputs changed')
    if state.get('bridge_stage') != 'building':
        manifest, _, _ = api.configuration(ROOT, 'dev')
        R.load_state(api, path, manifest)
    return state, inputs


def prepare(api, native_path):
    source_commit, _ = source_review(api, ROOT)
    native = N.load(native_path)
    D.require(native['phase'] in ('paused', 'completed', 'prepared', 'needs_attention'), 'Pause native normalization first')
    manifest, _, _ = api.configuration(ROOT, 'dev')
    before = api.inspect_project(manifest)
    api.ensure_stable(native['inputs']['runtime_before'], before, 'Runtime changed before bridge preparation')
    Q.verify_holds(api)
    upgrade_path = Path(native['inputs']['upgrade_state'])
    upgrade = U.load_state(api, upgrade_path, manifest)
    D.require(before['backend']['Image'] == upgrade['candidate_image'] == native['inputs']['candidate_image']
              and api.env_map(before['backend']).get('BLOCKSCOUT_VERSION') == ORIGINAL,
              'Bridge release must start from the reviewed deployed application')
    files = api.live_files(V.LIVE_DIRECTORY, before['backend'], manifest)
    current = api.compose_model(V.LIVE_DIRECTORY, manifest, {}, files, True)
    D.require(not api.live_environment_differences(current, before), 'Live environment differs from its release snapshot')
    # Build arguments are public; runtime secrets come only from the frozen live environment.
    public_model = api.read_json(ROOT / 'docker-compose/environments/dev/compose.json')
    public_model['services']['backend']['build']['args']['RELEASE_VERSION'] = '10.2.6'
    source = B.source_plan(api, ROOT, manifest, public_model, before)
    D.require(source['platform'] == 'linux/amd64', 'Bridge server build requires AMD64')
    source['build_arguments'][V.HOLD] = 'true'
    identity = {k: source[k] for k in ('source_commit', 'release_version', 'platform', 'build_arguments')}
    source['fingerprint'] = R.digest(R.encoded(identity))
    source['tag'] = 'deriw-blockscout:bridge-v10-' + source_commit[:12] + '-amd64'
    D.require(N.J.P.free_bytes() >= N.RESUME_FREE and os.statvfs('/').f_bavail * os.statvfs('/').f_frsize >= 10 * 1024**3,
              'Bridge build needs 220 GiB data free and 10 GiB root free')
    before_inventory = C.image_inventory(api, before['backend']['Image'])
    anchor = database_anchor(api)
    B.validate_state_directory(api, D.MANAGED_RELEASES, ROOT)
    directory = D.MANAGED_RELEASES / ('release-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8])
    directory.mkdir(mode=0o700)
    path = directory / 'state.json'
    inputs = {'format': INPUT_FORMAT, 'source': source, 'source_root': str(ROOT),
              'normalization_state': str(native_path), 'native_inputs_sha256': native['inputs_sha256'],
              'source_upgrade_state': str(upgrade_path), 'before': before, 'current_model': current,
              'backend_environment': api.env_map(before['backend']),
              'migration_inventory': before_inventory, 'database_anchor': anchor}
    R.write_new(directory / 'inputs.json', R.encoded(inputs))
    state = {'format': R.FORMAT, 'environment': 'dev', 'chain_id': manifest['chain_id'],
             'compose_project': manifest['compose_project'], 'status': 'prepared', 'bridge_stage': 'building',
             'bridge_patch': True, 'bridge_inputs_sha256': R.digest(R.encoded(inputs)),
             'startup_migrations': False, 'source_root': str(ROOT), 'candidate_source_commit': source_commit,
             'source_upgrade_state': str(upgrade_path), 'normalization_state': str(native_path),
             'before': before, 'live_directory': str(V.LIVE_DIRECTORY),
             'source_images': upgrade['source_images'],
             'image_ids': {}, 'snapshot_hashes': {}, 'deferred_migrations': sorted(R.VERSIONS)}
    R.write_new(path, R.encoded(state))
    print(json.dumps({'phase': 'bridge_deployment_saved', 'state': str(path), 'database_writes': False}), flush=True)
    return path


def build(api, path, state, inputs, manifest):
    source = inputs['source']
    image = B.image_info(api, source['tag'], missing=True)
    if image is None:
        B.compile_image(api, ROOT, source, path.parent / ('build-' + uuid.uuid4().hex[:8] + '.log.private'))
        image = B.image_info(api, source['tag'])
    V.validate_image(api, image, source)
    inventory = C.image_inventory(api, image['Id'])
    D.require(R.encoded(inventory) == R.encoded(inputs['migration_inventory']),
              'Candidate migration bytes or numeric runtime user changed')
    pending = C.pending_migrations(api, inventory)
    D.require(all(row.get('status') == 'ok' and set(row['candidate_pending_versions']) <= R.VERSIONS
                  and (not row['candidate_pending_versions'] or row['migration_directory'] == 'repo') for row in pending)
              and {row.get('migration_directory') for row in pending} == {'repo', 'account', 'arbitrum'},
              'Candidate has unreviewed pending schema migrations')
    checks = path.parent / ('validation-' + uuid.uuid4().hex[:8])
    checks.mkdir(mode=0o700)
    environment = {**inputs['backend_environment'], 'BLOCKSCOUT_VERSION': source['source_commit']}
    B.smoke_image(api, image['Id'], source, environment, checks, extra_expression=V.held_expression())
    api.ensure_stable(inputs['before'], api.inspect_project(manifest), 'Runtime changed during bridge build')
    D.require(database_anchor(api) == inputs['database_anchor'], 'Database migration/cursor/heap changed while native writer was paused')
    for side, image_id in (('forward', image['Id']), ('rollback', inputs['before']['backend']['Image'])):
        model = snapshot(inputs['current_model'], inputs['before'], image_id, source['source_commit'], path, side)
        data = R.snapshot_bytes(model)
        target = path.parent / (side + '.compose.json')
        if target.exists():
            D.require(U.private(target) == data, 'Saved bridge snapshot differs')
        else:
            R.write_new(target, data)
        R.check_snapshot(api, target, model, manifest, V.LIVE_DIRECTORY)
        state['snapshot_hashes'][side] = R.digest(data)
        state['image_ids'][side] = {n: s['image'] for n, s in model['services'].items()}
    state['bridge_stage'] = 'candidate_verified'
    R.update_state(path, state)


def restore_holds(api, root, rows):
    addresses = sorted({network[key] for network in rows['backend']['NetworkSettings']['Networks'].values()
                        for key in ('IPAddress', 'GlobalIPv6Address') if network.get(key)})
    payload = {'pid': None, 'query_start': None, 'backend_start': None}
    metrics = H.rpc(api, (root / 'deploy/scripts/v10/metrics_hold.exs').read_bytes(),
                    {**payload, 'query_md5': H.QUERY_MD5}, 'hold')
    sanitizer = E.rpc(api, (root / 'deploy/scripts/v10/empty_blocks_hold.exs').read_bytes(),
                      {**payload, 'query_md5': E.QUERY_MD5, 'backend_client_addresses': addresses}, 'hold')
    D.require(metrics['metrics_worker_held'] and sanitizer['sanitizer_worker_held'], 'Runtime holds could not be restored')
    Q.verify_holds(api)


def bridge_status(api, source):
    D.require(re.fullmatch(r'[a-f0-9]{40}', source), 'Unexpected bridge source identity')
    return N.rpc(api, 'true = Application.get_env(:block_scout_web, :version) == "' + source + '"\n' + '''
pid = Process.whereis(Indexer.Fetcher.Arbitrum.TrackingMessagesOnL1)
data = if is_pid(pid), do: Map.get(:sys.get_state(pid, 5000), :callback_module_state, %{}), else: %{}
data = data || %{}
IO.puts("BLOCKSCOUT_DEPLOY_CHECK=" <> Jason.encode!(%{
  observed_at_utc: DateTime.to_iso8601(DateTime.utc_now()), tracker_running: is_pid(pid),
  next_parent_block: get_in(data, [:task_data, :check_new, :start_block]),
  historical_boundary: get_in(data, [:task_data, :check_historical, :end_block]),
  blocks_per_request: get_in(data, [:config, :l1_rpc_block_range]),
  recheck_interval_ms: get_in(data, [:config, :recheck_interval])
}))
''')


def apply(api, path):
    state, inputs = load(api, path)
    manifest, _, _ = api.configuration(ROOT, 'dev')
    D.require(B.clean_source(api, ROOT) == (state['candidate_source_commit'], '10.2.6'), 'Use bridge deployment creation commit')
    if state['bridge_stage'] == 'building':
        build(api, path, state, inputs, manifest)
    R.load_state(api, path, manifest)
    if state['status'] != 'active':
        before = api.inspect_project(manifest, require_running=False, allow_missing_backend=True)
        R.unchanged_others(api, inputs['before'], before)
        if state['status'] == 'prepared':
            api.ensure_stable(inputs['before'], before, 'Runtime changed before bridge activation')
            D.require(database_anchor(api) == inputs['database_anchor'], 'Native checkpoint changed before deployment')
        elif 'backend' in before:
            labels = before['backend'].get('Config', {}).get('Labels') or {}
            D.require(before['backend']['Id'] == inputs['before']['backend']['Id'] or
                      labels.get(R.STATE_LABEL) == str(path), 'Another release owns backend')
        D.require(B.image_info(api, inputs['source']['tag'])['Id'] == state['image_ids']['forward']['backend'],
                  'Bridge candidate image tag changed')
        api.run(['docker', 'image', 'tag', state['image_ids']['rollback']['backend'],
                 'deriw-blockscout:rollback-' + path.parent.name])
        state['status'] = 'activation_requested'
        state['bridge_stage'] = 'activation_requested'
        R.update_state(path, state)
        R.recreate(api, path.parent / 'forward.compose.json', manifest, V.LIVE_DIRECTORY,
                   path.parent / ('activate-' + uuid.uuid4().hex[:8] + '.log.private'))
        deadline = time.monotonic() + 120
        while True:
            try:
                rows = api.inspect_project(manifest)
                restore_holds(api, ROOT, rows)
                break
            except D.ConfigError:
                D.require(time.monotonic() < deadline, 'New backend holds did not become ready; native writer remains paused')
                time.sleep(2)
        D.require(database_anchor(api) == inputs['database_anchor'], 'Database migration/cursor/heap changed across bridge deployment')
        probe = N.rpc(api, U.runtime_expression())
        D.require(probe['settings_ok'] and probe['automatic_migrators_absent'], 'New runtime settings differ')
    result = R.verify(api, B, C, manifest, path, state, 'forward', wait=True)
    D.require(result['status'] == 'verified', 'Bridge backend verification failed; native writer remains paused')
    if state['status'] != 'active':
        state['status'] = 'active'
        state['bridge_stage'] = 'active'
        R.update_state(path, state)
    result['recorded_phase'] = state['status']
    Q.verify_holds(api)
    result['bridge_tracker'] = bridge_status(api, state['candidate_source_commit'])
    return result


def bind_native(api, release_path):
    state, inputs = load(api, release_path)
    D.require(state['status'] == 'active', 'Only a verified deployment can resume native normalization')
    path = Path(state['normalization_state'])
    native = N.load(path)
    D.require(native['inputs_sha256'] == inputs['native_inputs_sha256'], 'Native frozen inputs changed')
    if native['phase'] == 'completed':
        return N.public(path, native)
    manifest, _, _ = api.configuration(ROOT, 'dev')
    Q.verify_holds(api)
    runtime = api.inspect_project(manifest)
    R.unchanged_others(api, inputs['before'], runtime)
    target = path.parent / 'bridge-runtime-binding.json'
    binding = {'format': 'blockscout-native-bridge-binding-v1',
               'native_inputs_sha256': native['inputs_sha256'],
               'native_worker_script_sha256': native['inputs']['script_sha256'],
               'release_state': str(release_path), 'release_state_sha256': R.digest(U.private(release_path)),
               'source_root': str(ROOT), 'tool_source_commit': state['candidate_source_commit'],
               'runtime_before': runtime}
    if target.exists():
        existing = json.loads(U.private(target))
        D.require({k: v for k, v in existing.items() if k != 'runtime_before'} ==
                  {k: v for k, v in binding.items() if k != 'runtime_before'},
                  'Native job already has a different bridge handoff')
        api.ensure_stable(existing['runtime_before'], runtime, 'Runtime changed after native handoff')
    else:
        R.write_new(target, R.encoded(binding))
    native['runtime_binding_file'] = target.name
    native['runtime_binding_sha256'] = R.digest(U.private(target))
    native['transient_retries'] = 0
    native['worker_crash_retries'] = 0
    N.save(path, native, 'prepared')
    N.runtime_binding(path, native)
    request = path.parent / 'pause.request'
    if request.exists():
        U.private(request)
        request.unlink()
    return N.public(path, native)


def active_model(api, model, context):
    state = context['state']
    manifest, _, _ = api.configuration(ROOT, 'dev')
    path = Path(state['source_upgrade_state'])
    upgrade = U.load_state(api, path, manifest)
    result = U.active_model(api, model, {'path': path, 'state': upgrade})
    backend = result['services']['backend']
    if model['services']['backend'].get('image') == state['source_images']['backend']:
        backend['image'] = context['image_ids']['backend']
    backend['labels'].pop(U.STATE_LABEL, None)
    backend['labels'].pop(U.ROLE_LABEL, None)
    backend['labels'].update({R.STATE_LABEL: str(context['path']), R.SIDE_LABEL: context['side']})
    if context['side'] == 'forward' and model['services']['backend'].get('environment', {}).get('BLOCKSCOUT_VERSION') == \
            upgrade['source_environment']['backend'].get('BLOCKSCOUT_VERSION'):
        backend['environment']['BLOCKSCOUT_VERSION'] = state['candidate_source_commit']
    return result


def existing_release(native_path):
    matches = []
    if D.MANAGED_RELEASES.exists():
        for directory in D.MANAGED_RELEASES.iterdir():
            path = directory / 'state.json'
            if not path.is_file():
                continue
            state = json.loads(U.private(path))
            if state.get('bridge_patch') is True and state.get('normalization_state') == str(native_path):
                matches.append(path)
    D.require(len(matches) <= 1, 'Multiple bridge deployments reference this job; retain all journals for review')
    return matches[0] if matches else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    for name in ('run', 'resume', 'status'):
        group.add_argument('--' + name, action='store_true')
    parser.add_argument('--normalization-state', type=Path)
    parser.add_argument('--state', type=Path)
    args = parser.parse_args()
    path = args.state
    paused = False
    try:
        D.require(sys.platform.startswith('linux') and os.geteuid() == 0, 'Run on the Linux dev server as root')
        manifest, _, _ = D.configuration(ROOT, 'dev')
        if args.status:
            D.require(path is not None and args.normalization_state is None, 'Use only bridge state')
            state, _ = load(D, path)
            if state['bridge_stage'] == 'building':
                result = {'status': 'bridge_build_pending', 'state': str(path)}
            else:
                result = R.status(D, B, C, manifest, path)
        else:
            source_review(D, ROOT)
            D.require((args.run and path is None and args.normalization_state is not None) or
                      (args.resume and path is not None and args.normalization_state is None),
                      'Provide native state for run, or saved bridge state for resume')
            native_path = args.normalization_state if args.run else Path(load(D, path)[0]['normalization_state'])
            pause_native(D, native_path)
            paused = True
            with M.global_lock(True), B.build_lock(D, D.MANAGED_RELEASES, ROOT):
                if args.run:
                    path = existing_release(native_path) or prepare(D, native_path)
                    print(json.dumps({'phase': 'bridge_deployment_selected', 'state': str(path)}), flush=True)
                result = apply(D, path)
                result['normalization'] = bind_native(D, path)
            # Release operation locks before systemd launches the controller.
            native = N.load(native_path)
            if native['phase'] != 'completed':
                result['normalization_service'] = N.install_service(D, native_path)
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    native = N.load(native_path)
                    if native['phase'] in ('running', 'waiting', 'completed', 'needs_attention'):
                        break
                    time.sleep(1)
                result['normalization'] = N.public(native_path, native)
        print(json.dumps(result, indent=2))
        return 0
    except Exception as error:
        print(json.dumps({'status': 'bridge_deployment_stopped', 'state': str(path) if path else None,
                          'reason': str(error) if isinstance(error, D.ConfigError) else 'Bridge check failed; private evidence retained',
                          'normalization': ('Paused with checkpoint retained. Repeat --run if no release state exists; otherwise resume that state.'
                                            if paused else 'No normalization pause was completed.'),
                          'raw_values_suppressed': True}), flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
