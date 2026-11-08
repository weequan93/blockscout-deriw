#!/usr/bin/env python3
"""Deploy the durable deposit cursor over the latest qualified dev stack."""
import argparse
from copy import deepcopy
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
import dev_config as D
import release as R
import v10_bridge_deploy as P
import v10_frontend_deploy as F
import v10_infrastructure_checks as K
import v10_native_normalization as N
import v10_upgrade as U

ROOT = Path(__file__).resolve().parents[2]
BASE = '875082f41d79ef72cd9dd9a4e771953a181c6cf2'
FORMAT = 'deriw-bridge-checkpoint-deployment-v1'
APP_FILES = {
    'apps/indexer/lib/indexer/fetcher/arbitrum/utils/db/message_scan_cursor.ex',
    'apps/indexer/lib/indexer/fetcher/arbitrum/tracking_messages_on_l1.ex',
    P.WORKER,
}


def review_application(api, root, commit):
    manifest = json.loads((root / 'deploy/bridge-checkpoint-source.json').read_text())
    D.require(manifest['format'] == FORMAT and manifest['base'] == BASE
              and set(manifest['application_sha256']) == APP_FILES, 'Unknown bridge checkpoint source profile')
    # The maintained tools branch and the deployed bridge release can have
    # identical application trees without a direct ancestry relationship.
    # Review the complete tree diff and exact tested bytes below.
    api.run(['git', '-C', str(root), 'cat-file', '-e', BASE + '^{commit}'])
    paths = api.run(['git', '-C', str(root), 'diff', '--name-only', BASE, commit]).splitlines()
    D.require(APP_FILES <= set(paths) and all(name in APP_FILES or name in ('README.md', 'docker-compose/environments/README.md')
              or name.startswith(('deploy/', 'docs/', 'apps/indexer/test/')) for name in paths),
              'Bridge checkpoint release contains another application/configuration change')
    for name, digest in manifest['application_sha256'].items():
        D.require(R.digest(api.run(['git', '-C', str(root), 'show', commit + ':' + name]).encode()) == digest,
                  'Bridge checkpoint application bytes differ from the tested source profile')


def source_review(api):
    commit, version = B.clean_source(api, ROOT)
    D.require(version == '10.2.6', 'Use the reviewed v10 backend')
    review_application(api, ROOT, commit)
    return commit


def snapshot(model, before, image, source, path, side):
    selected = P.snapshot(model, before, image, source, path, side)
    # Init containers remain stopped at their historical image. Keep the newer
    # qualified declarations rather than reverting them to those old containers.
    for name in ('db-init', 'stats-db-init'):
        if name in model['services']:
            selected['services'][name]['image'] = model['services'][name]['image']
    return selected


def load(api, path):
    D.require(path.is_absolute() and path.resolve() == path and path.name == 'state.json'
              and path.parent.parent == D.MANAGED_RELEASES
              and re.fullmatch(r'release-[0-9TZ-]+-[a-f0-9]{8}', path.parent.name), 'Unexpected checkpoint release path')
    N.J.owned_directory(path.parent)
    state = json.loads(U.private(path))
    raw = U.private(path.parent / 'inputs.json')
    inputs = json.loads(raw)
    D.require(state.get('bridge_checkpoint_patch') is True and inputs.get('format') == FORMAT
              and state['bridge_inputs_sha256'] == R.digest(raw)
              and state['candidate_source_commit'] == inputs['source']['source_commit']
              and state['source_root'] == inputs['source_root']
              and state['normalization_state'] == inputs['normalization_state'], 'Checkpoint release inputs changed')
    if state['bridge_stage'] != 'building':
        manifest, _, _ = api.configuration(ROOT, 'dev')
        R.load_state(api, path, manifest)
    for side, digest in state.get('anchor_sha256', {}).items():
        D.require(side in ('forward', 'rollback') and R.digest(U.private(path.parent / ('database-before-' + side + '.json'))) == digest,
                  'Checkpoint deployment database anchor changed')
    if state['status'] != 'prepared':
        D.require('forward' in state.get('anchor_sha256', {}), 'Requested activation has no authenticated database anchor')
        if state['status'] in ('rollback_requested', 'rolled_back', 'rollback_needs_attention'):
            D.require('rollback' in state['anchor_sha256'], 'Requested rollback has no authenticated database anchor')
    seed = path.parent / 'bridge-seed.json'
    if state.get('seed_sha256'):
        D.require(R.digest(U.private(seed)) == state['seed_sha256'], 'Live bridge handoff changed')
    else:
        D.require(state['status'] == 'prepared', 'Requested backend activation has no authenticated bridge handoff')
    return state, inputs


def public(path, state, inputs):
    return {'status': 'bridge_checkpoint_' + state['bridge_stage'], 'state': str(path),
            'observed_at_utc': state.get('observed_at_utc'), 'backend_version': '10.2.6',
            'only_recreated_service': 'backend', 'schema_migrations_run': False,
            'normalization_state': inputs['normalization_state'], 'normalization_checkpoint_preserved': True,
            'infrastructure_and_frontend_images_preserved': True,
            'durable_cursor_storage': 'last_fetched_counters', 'cumulative_growth_cap_bytes': None}


def prepare(api, native_path):
    commit = source_review(api)
    native = N.load(native_path)
    D.require(native['phase'] in ('paused', 'completed'), 'Pause the same native writer before backend preparation')
    context = N.runtime_binding(native_path, native)
    D.require(context and context.get('release_state'), 'Expected the latest qualified deployment binding')
    manifest, _, _ = api.configuration(ROOT, 'dev')
    before = api.inspect_project(manifest)
    api.ensure_stable(context['runtime_before'], before, 'Runtime changed before checkpoint deployment')
    P.Q.verify_holds(api)
    anchor = K.stable_database_anchor(api)
    payload = native['inputs']['payload']
    D.require(anchor['database_identity'] == payload['database_identity'] and anchor['heap_identity'] == payload['heap_identity']
              and anchor['versions'] == payload['applied_versions']
              and anchor['native_status']['meta']['v10_native_job'] == payload['token'],
              'The original normalization database and checkpoint are required')
    D.require(api.env_map(before['backend']).get('BLOCKSCOUT_VERSION') == BASE,
              'This checkpoint release starts from the reviewed bridge backend')
    files = F.current_compose_files(api, before, manifest, context)
    model = api.compose_model(P.V.LIVE_DIRECTORY, manifest, {}, files, True)
    D.require(not api.live_environment_differences(model, before), 'Live settings differ from the qualified snapshot')
    public_model = api.read_json(ROOT / 'docker-compose/environments/dev/compose.json')
    public_model['services']['backend']['build']['args']['RELEASE_VERSION'] = '10.2.6'
    source = B.source_plan(api, ROOT, manifest, public_model, before)
    source['build_arguments'][P.V.HOLD] = 'true'
    D.require(source['platform'] == 'linux/amd64', 'Use the reviewed AMD64 backend build')
    identity = {key: source[key] for key in ('source_commit', 'release_version', 'platform', 'build_arguments')}
    source.update(fingerprint=R.digest(R.encoded(identity)), tag='deriw-blockscout:bridge-checkpoint-' + commit[:12] + '-amd64')
    D.require(N.J.P.free_bytes() >= N.RESUME_FREE and os.statvfs('/').f_bavail * os.statvfs('/').f_frsize >= 10 * 1024**3,
              'Keep the 220 GiB data reserve and 10 GiB build reserve')
    previous = R.load_state(api, Path(context['release_state']), manifest)
    B.validate_state_directory(api, D.MANAGED_RELEASES, ROOT)
    directory = D.MANAGED_RELEASES / ('release-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8])
    directory.mkdir(mode=0o700)
    path = directory / 'state.json'
    inputs = {'format': FORMAT, 'source': source, 'source_root': str(ROOT), 'before': before,
              'normalization_state': str(native_path), 'native_inputs_sha256': native['inputs_sha256'],
              'prior_binding': {key: native[key] for key in ('runtime_binding_file', 'runtime_binding_sha256')},
              'base_release_state': context['release_state'], 'current_model': model,
              'backend_environment': api.env_map(before['backend']),
              'migration_inventory': P.C.image_inventory(api, before['backend']['Image']),
              'database_anchor': P.database_anchor(api)}
    R.write_new(directory / 'inputs.json', R.encoded(inputs))
    state = {'format': R.FORMAT, 'environment': 'dev', 'chain_id': manifest['chain_id'],
             'compose_project': manifest['compose_project'], 'status': 'prepared', 'bridge_stage': 'building',
             'bridge_patch': True, 'bridge_checkpoint_patch': True, 'bridge_inputs_sha256': R.digest(R.encoded(inputs)),
             'startup_migrations': False, 'source_root': str(ROOT), 'candidate_source_commit': commit,
             'source_upgrade_state': native['inputs']['upgrade_state'], 'normalization_state': str(native_path),
             'before': before, 'live_directory': str(P.V.LIVE_DIRECTORY), 'source_images': previous['source_images'],
             'image_ids': {}, 'snapshot_hashes': {}, 'deferred_migrations': sorted(R.VERSIONS), 'anchor_sha256': {}}
    R.write_new(path, R.encoded(state))
    print(json.dumps({'phase': 'bridge_checkpoint_prepared', 'state': str(path), 'database_writes': False}), flush=True)
    return path


def seed_expression():
    # This runs only on the authenticated original backend immediately before
    # replacement. :sys.get_state observes a completed callback, so this cursor
    # covers successful RPC/import work, including empty ranges.
    return '''
true = Explorer.Migrator.StartupControl.disabled?()
"''' + BASE + '''" = Application.get_env(:block_scout_web, :version)
pid = Process.whereis(Indexer.Fetcher.Arbitrum.TrackingMessagesOnL1)
true = is_pid(pid)
data = :sys.get_state(pid, 5000) |> Map.fetch!(:callback_module_state)
next = get_in(data, [:task_data, :check_new, :start_block])
true = is_integer(next) and next >= 0
config = data.config
{:ok, parent} = EthereumJSONRPC.fetch_chain_id(config.json_l1_rpc_named_arguments)
addresses = [config.l1_rollup_address, config.l1_bridge_address]
true = Enum.all?(addresses, &Regex.match?(~r/^0x[0-9a-fA-F]{40}$/, &1))
identity = Enum.join([parent | Enum.map(addresses, &String.downcase/1)], ":")
key = "arbitrum_to_l2_scan_v1:" <> Base.encode16(:crypto.hash(:sha256, identity), case: :lower)
[[value]] = Explorer.Repo.query!(~S"""
INSERT INTO last_fetched_counters(counter_type,value,inserted_at,updated_at)
VALUES($1,$2,timezone('UTC',now()),timezone('UTC',now()))
ON CONFLICT(counter_type) DO UPDATE SET value=GREATEST(last_fetched_counters.value,EXCLUDED.value),updated_at=EXCLUDED.updated_at
RETURNING value
""", [key, Decimal.new(next)], log: false, timeout: 5000).rows
saved = Decimal.to_integer(value)
true = saved >= next and Decimal.equal?(value,Decimal.new(saved))
IO.puts("BLOCKSCOUT_DEPLOY_CHECK=" <> Jason.encode!(%{counter_key: key,
  live_next_parent_block: next, saved_next_parent_block: saved, parent_chain_id: parent}))
'''


def observe(api, path, inputs, side):
    seed = json.loads(U.private(path.parent / 'bridge-seed.json'))
    version = inputs['source']['source_commit'] if side == 'forward' else inputs['backend_environment']['BLOCKSCOUT_VERSION']
    key = json.dumps(seed['counter_key'])
    return N.rpc(api, '"' + version + '" = Application.get_env(:block_scout_web, :version)\n' + '''
pid = Process.whereis(Indexer.Fetcher.Arbitrum.TrackingMessagesOnL1)
true = is_pid(pid)
data = :sys.get_state(pid,5000) |> Map.fetch!(:callback_module_state)
key = ''' + key + '''
value = Explorer.Chain.Cache.Counters.LastFetchedCounter.get(key,nullable: true)
saved = if value, do: Decimal.to_integer(value), else: nil
IO.puts("BLOCKSCOUT_DEPLOY_CHECK=" <> Jason.encode!(%{observed_at_utc: DateTime.to_iso8601(DateTime.utc_now()),
  tracker_running: true, next_parent_block: get_in(data,[:task_data,:check_new,:start_block]),
  durable_next_parent_block: saved, checkpoint_key_matches: get_in(data,[:config,:l1_messages_scan_cursor_key]) == key}))
''')


def verify(api, path, state, inputs, side, progress=False):
    manifest, _, _ = api.configuration(ROOT, 'dev')
    health = R.verify(api, B, P.C, manifest, path, state, side, wait=True)
    D.require(health['status'] == 'verified', 'Backend health is not qualified; keep the native job paused')
    P.Q.verify_holds(api)
    expected = json.loads(U.private(path.parent / ('database-before-' + side + '.json')))
    K.check_anchor(expected, K.stable_database_anchor(api), progress=progress)
    observed = observe(api, path, inputs, side)
    seed = json.loads(U.private(path.parent / 'bridge-seed.json'))
    D.require(type(observed['durable_next_parent_block']) is int
              and observed['durable_next_parent_block'] >= seed['saved_next_parent_block'], 'Durable deposit checkpoint regressed')
    if side == 'forward':
        D.require(observed['checkpoint_key_matches'] and type(observed['next_parent_block']) is int
                  and observed['next_parent_block'] >= seed['saved_next_parent_block'],
                  'The fixed tracker did not resume its saved deposit scan')
    return health, observed


def own_binding(native, inputs, path):
    current = {key: native[key] for key in ('runtime_binding_file', 'runtime_binding_sha256')}
    prefix = 'bridge-checkpoint-runtime-binding-' + path.parent.name.rsplit('-', 1)[1] + '-'
    D.require(current == inputs['prior_binding'] or current['runtime_binding_file'].startswith(prefix),
              'A newer deployment owns the native job')


def activate(api, path, side):
    state, inputs = load(api, path)
    D.require(source_review(api) == state['candidate_source_commit'], 'Use this checkpoint deployment creation checkout')
    manifest, _, _ = api.configuration(ROOT, 'dev')
    if state['bridge_stage'] == 'building':
        P.build(api, path, state, inputs, manifest, snapshotter=snapshot)
        state, inputs = load(api, path)
    receipt_path = path.parent / ('receipt-' + side + '.json')
    if receipt_path.exists():
        return verify(api, path, state, inputs, side, progress=True)[1]
    runtime = api.inspect_project(manifest, require_running=False)
    R.unchanged_others(api, inputs['before'], runtime)
    backend = runtime['backend']
    labels = backend.get('Config', {}).get('Labels') or {}
    original = backend['Id'] == inputs['before']['backend']['Id']
    owned = labels.get(R.STATE_LABEL) == str(path)
    D.require(original or owned, 'Another release owns the backend')
    if original:
        api.ensure_stable(inputs['before'], runtime, 'Original runtime changed after checkpoint preparation')
    else:
        previous_side = labels.get(R.SIDE_LABEL)
        D.require(previous_side in ('forward', 'rollback') and backend['Image'] == state['image_ids'][previous_side]['backend']
                  and backend.get('Config', {}).get('Cmd') == R.START,
                  'Owned backend image or startup command changed')
        selected = api.compose_model(P.V.LIVE_DIRECTORY, manifest, {}, [path.parent / (previous_side + '.compose.json')], True)
        D.require(not api.live_environment_differences({'services': {'backend': selected['services']['backend']}}, {'backend': backend}),
                  'Owned backend environment changed outside the checkpoint release')
    seed_path = path.parent / 'bridge-seed.json'
    if not seed_path.exists():
        D.require(side == 'forward' and original, 'A saved live-scan handoff is required before backend replacement')
        R.write_new(seed_path, R.encoded(N.rpc(api, seed_expression())))
    if not state.get('seed_sha256'):
        D.require(original and state['status'] == 'prepared', 'Unrequested handoff recovery requires the original backend')
        state['seed_sha256'] = R.digest(U.private(seed_path))
        R.update_state(path, state)
    anchor_path = path.parent / ('database-before-' + side + '.json')
    if not anchor_path.exists():
        R.write_new(anchor_path, R.encoded(K.stable_database_anchor(api)))
    if side not in state['anchor_sha256']:
        state['anchor_sha256'][side] = R.digest(U.private(anchor_path))
        R.update_state(path, state)
    K.check_anchor(json.loads(U.private(anchor_path)), K.stable_database_anchor(api))
    expected = state['image_ids'][side]['backend']
    requested = 'activation_requested' if side == 'forward' else 'rollback_requested'
    matching_running = owned and labels.get(R.SIDE_LABEL) == side and backend['Image'] == expected and backend['State']['Running']
    D.require(not matching_running or state['status'] == requested, 'Running target has no matching activation request')
    if not matching_running:
        D.require(N.J.P.free_bytes() >= N.RESUME_FREE, 'Keep the 220 GiB deployment/resume reserve')
        api.run(['docker', 'image', 'tag', inputs['before']['backend']['Image'], 'deriw-blockscout:rollback-checkpoint-' + path.parent.name])
        state.update(status=requested, bridge_stage=requested)
        R.update_state(path, state)
        R.recreate(api, path.parent / (side + '.compose.json'), manifest, P.V.LIVE_DIRECTORY,
                   path.parent / ('activate-' + uuid.uuid4().hex[:8] + '.log.private'))
    deadline = time.monotonic() + 180
    while True:
        try:
            P.restore_holds(api, ROOT, api.inspect_project(manifest))
            break
        except D.ConfigError:
            D.require(time.monotonic() < deadline, 'Backend worker holds are not ready; preserve this journal')
            time.sleep(2)
    _, observed = verify(api, path, state, inputs, side)
    runtime = api.inspect_project(manifest)
    receipt = {'format': FORMAT, 'side': side, 'inputs_sha256': state['bridge_inputs_sha256'],
               'runtime_before': runtime, 'database_after': K.stable_database_anchor(api),
               'seed_sha256': R.digest(U.private(seed_path)), 'bridge': observed}
    R.write_new(receipt_path, R.encoded(receipt))
    state.update(status='active' if side == 'forward' else 'rolled_back',
                 bridge_stage='active' if side == 'forward' else 'rolled_back',
                 observed_at_utc=datetime.now(timezone.utc).isoformat())
    R.update_state(path, state)
    return observed


def bind_native(api, path, side):
    state, inputs = load(api, path)
    receipt_path = path.parent / ('receipt-' + side + '.json')
    receipt = json.loads(U.private(receipt_path))
    verify(api, path, state, inputs, side, progress=True)
    native_path = Path(inputs['normalization_state'])
    native = N.load(native_path)
    own_binding(native, inputs, path)
    D.require(native['inputs_sha256'] == inputs['native_inputs_sha256'], 'Frozen normalization inputs changed')
    D.require(native['phase'] in ('paused', 'completed') and N.owned(api, native_path, native['inputs']) is None,
              'The same native writer must be stopped before its backend handoff')
    current = K.stable_database_anchor(api)
    K.check_anchor(receipt['database_after'], current, progress=True)
    filename = 'bridge-checkpoint-runtime-binding-' + path.parent.name.rsplit('-', 1)[1] + '-' + side + '.json'
    binding = {'format': FORMAT, 'backend_checkpoint_release_state': str(path), 'side': side,
               'native_inputs_sha256': native['inputs_sha256'], 'native_worker_script_sha256': native['inputs']['script_sha256'],
               'receipt_sha256': R.digest(U.private(receipt_path)), 'source_root': str(ROOT),
               'tool_source_commit': state['candidate_source_commit'], 'runtime_before': receipt['runtime_before']}
    target = native_path.parent / filename
    if target.exists():
        D.require(U.private(target) == R.encoded(binding), 'Existing backend checkpoint handoff changed')
    else:
        R.write_new(target, R.encoded(binding))
    native.update(runtime_binding_file=filename, runtime_binding_sha256=R.digest(U.private(target)), transient_retries=0, worker_crash_retries=0)
    completed = current['native_status']['status'] == 'completed'
    D.require(native['phase'] != 'completed' or completed, 'A completed normalization cannot be reopened')
    native.setdefault('database', {}).update(native_status=current['native_status']['status'], native_meta=current['native_status']['meta'])
    N.runtime_binding(native_path, native)
    N.save(native_path, native, 'completed' if completed else 'prepared')
    request = native_path.parent / 'pause.request'
    if request.exists():
        U.private(request)
        request.unlink()


def native_context(native_path, native):
    raw = U.private(native_path.parent / native['runtime_binding_file'])
    binding = json.loads(raw)
    D.require(R.digest(raw) == native['runtime_binding_sha256'] and binding['format'] == FORMAT
              and binding['native_inputs_sha256'] == native['inputs_sha256']
              and binding['native_worker_script_sha256'] == native['inputs']['script_sha256'], 'Backend checkpoint binding changed')
    path = Path(binding['backend_checkpoint_release_state'])
    state, inputs = load(D, path)
    receipt_raw = U.private(path.parent / ('receipt-' + binding['side'] + '.json'))
    receipt = json.loads(receipt_raw)
    D.require(R.digest(receipt_raw) == binding['receipt_sha256'] and receipt['format'] == FORMAT
              and receipt['side'] == binding['side'] and receipt['inputs_sha256'] == state['bridge_inputs_sha256']
              and receipt['runtime_before'] == binding['runtime_before']
              and receipt['seed_sha256'] == R.digest(U.private(path.parent / 'bridge-seed.json'))
              and inputs['normalization_state'] == str(native_path) and inputs['native_inputs_sha256'] == native['inputs_sha256']
              and binding['source_root'] == inputs['source_root'] and binding['tool_source_commit'] == state['candidate_source_commit'],
              'Backend checkpoint handoff is not the qualified release')
    prior = deepcopy(native)
    prior.update(inputs['prior_binding'])
    previous = N.runtime_binding(native_path, prior)
    D.require(previous is not None, 'The previous deployment proof is missing')
    R.unchanged_others(D, previous['runtime_before'], binding['runtime_before'])
    D.require(binding['runtime_before']['backend']['Image'] == state['image_ids'][binding['side']]['backend'],
              'Backend checkpoint image differs from its release')
    return {**binding, 'release_state': str(path), 'release': state, 'previous_source_root': previous['source_root']}


def current_files(context):
    path = Path(context['backend_checkpoint_release_state'])
    load(D, path)
    return [path.parent / (context['side'] + '.compose.json')]


def active_model(api, model, context):
    D.require(set(model['services']) == set(context['state']['image_ids'][context['side']]), 'Backend release service inventory changed')
    manifest, _, _ = api.configuration(ROOT, 'dev')
    return api.compose_model(P.V.LIVE_DIRECTORY, manifest, {}, [context['file']], True)


def resume_native(api, path, side):
    _, inputs = load(api, path)
    native_path = Path(inputs['normalization_state'])
    native = N.load(native_path)
    N.install_service(api, native_path, start=native['phase'] != 'completed')
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        native = N.load(native_path)
        if native['phase'] in ('running', 'waiting', 'completed'):
            context = N.runtime_binding(native_path, native)
            D.require(context['backend_checkpoint_release_state'] == str(path) and context['side'] == side,
                      'Another deployment replaced the resumed normalization')
            return N.public(native_path, native)
        D.require(native['phase'] != 'needs_attention', 'Normalization needs review; preserve its checkpoint')
        time.sleep(1)
    raise D.ConfigError('No current normalization running/waiting checkpoint was observed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    for name in ('run', 'resume', 'status', 'rollback'):
        modes.add_argument('--' + name, action='store_true')
    parser.add_argument('--normalization-state', type=Path)
    parser.add_argument('--state', type=Path)
    args = parser.parse_args()
    path = args.state
    try:
        D.require(sys.platform.startswith('linux') and os.geteuid() == 0, 'Run on the dev Linux server as root')
        if args.status:
            D.require(path and not args.normalization_state, 'Use the saved checkpoint deployment state')
            state, inputs = load(D, path)
            print(json.dumps(public(path, state, inputs), indent=2))
            return 0
        commit = source_review(D)
        D.require((args.run and not path and args.normalization_state) or
                  (not args.run and path and not args.normalization_state), 'Use native state for run or deployment state for resume/rollback')
        if args.run:
            matches = [candidate for candidate in D.MANAGED_RELEASES.glob('release-*/state.json')
                       if (data := json.loads(U.private(candidate))).get('bridge_checkpoint_patch') is True
                       and data.get('normalization_state') == str(args.normalization_state)]
            D.require(len(matches) <= 1, 'Multiple checkpoint releases reference this native job')
            path = matches[0] if matches else None
        native_path = args.normalization_state if args.run else Path(load(D, path)[1]['normalization_state'])
        if path:
            state, inputs = load(D, path)
            D.require(commit == state['candidate_source_commit'] and ROOT == Path(inputs['source_root']),
                      'Resume through the pinned checkpoint deployment creation checkout')
            own_binding(N.load(native_path), inputs, path)
            if args.rollback:
                D.require(state['status'] != 'prepared' and (path.parent / 'database-before-forward.json').exists(),
                          'No backend activation was requested; no image rollback is needed')
        P.pause_native(D, native_path, 'bridge_checkpoint_deployment')
        side = 'rollback' if args.rollback else 'forward'
        with M.global_lock(True), B.build_lock(D, D.MANAGED_RELEASES, ROOT):
            path = path or prepare(D, native_path)
            state, inputs = load(D, path)
            D.require(not (side == 'forward' and state['status'] in ('rolled_back', 'rollback_requested')),
                      'A rolled-back checkpoint release cannot be reactivated')
            bridge = activate(D, path, side)
            bind_native(D, path, side)
        normalization = resume_native(D, path, side)
        state, inputs = load(D, path)
        state.update(bridge_stage='finished' if side == 'forward' else 'rollback_finished',
                     observed_at_utc=datetime.now(timezone.utc).isoformat())
        R.update_state(path, state)
        print(json.dumps({**public(path, state, inputs), 'bridge': bridge, 'normalization': normalization}, indent=2))
        return 0
    except Exception as error:
        print(json.dumps({'status': 'bridge_checkpoint_stopped', 'state': str(path) if path else None,
            'raw_values_suppressed': True, 'reason': str(error) if isinstance(error, D.ConfigError) else 'Checkpoint deployment failed; private evidence retained',
            'recovery': 'Keep all journals. Resume or roll back this same deployment state. Native inputs and cursor remain; no data restore is automatic.'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
