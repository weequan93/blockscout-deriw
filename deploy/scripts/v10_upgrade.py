#!/usr/bin/env python3
"""Stage the reviewed dev v10 schema change and paired backend/NFT activation."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
import time
import uuid

import candidate_build as B
import containerd_maintenance as M
import deploy_check as C
import dev_config as D
import release as R
import v10_candidate as V
import v10_nft as N
import v10_review as Q
import v10_schema as S

ROOT = Path(__file__).resolve().parents[2]
BASE = Path('/data/blockscout-dev-private/v10-upgrades')
FORMAT = 'blockscout-dev-v10-schema-upgrade-v1'
STATE_LABEL = 'org.deriw.v10-upgrade-state'
ROLE_LABEL = 'org.deriw.v10-upgrade-role'
PAIR = ('backend', 'nft_media_handler')
PHASES = {'prepared', 'stopping_pair', 'pair_stopped', 'schema_write_requested', 'schema_complete',
          'activation_requested', 'active', 'needs_attention', 'aborted'}
STAGE = 'preflight'
RECOVERY = {'route': 'operator_manual', 'recovery_verified': False,
            'operator_authorized_without_verification': True, 'decision_date': '2026-11-04'}
RUN_SCRIPT = '''set -eu
umask 077
mkdir -p /tmp/upgrade
tar -xf - -C /tmp/upgrade
jq -e 'type == "object" and all(keys[]; test("^[A-Z][A-Z0-9_]*$")) and all(.[]; type == "string" and (contains("\\u0000") | not))' /tmp/upgrade/environment.json > /dev/null
eval "$(jq -r 'to_entries[] | "export \\(.key)=\\(.value|@sh)"' /tmp/upgrade/environment.json)"
rm /tmp/upgrade/environment.json
exec /app/bin/blockscout eval 'Code.eval_file("/tmp/upgrade/migrate.exs")'
'''


class UpgradeError(D.ConfigError):
    pass


def require(ok, message):
    if not ok:
        raise UpgradeError(message)


def stop_timeout(row):
    value = row.get('Config', {}).get('StopTimeout')
    value = 300 if value is None else value
    require(type(value) is int and 0 < value <= 600, 'Recorded service stop timeout needs review')
    return value


def capacity():
    values = os.statvfs('/data')
    return values.f_bavail * values.f_frsize


def space(state):
    free = capacity()
    start = state.get('space_at_write_start', free)
    require(free >= 100 * S.GIB and start - free <= 20 * S.GIB, 'Initial schema free-space/consumption limit reached')
    return free


def payload(plan, baseline=None, mode='inspect'):
    added = plan['migration_changes']['added']
    upstream = {d: sorted(int(Path(n).name.split('_', 1)[0]) for n in added if n.startswith(d + '/migrations/'))
                for d in ('repo', 'account', 'arbitrum')}
    require([len(upstream[d]) for d in upstream] == [26, 2, 1], 'Reviewed 29-version sequence changed')
    result = {'mode': mode, 'application_name': 'blockscout-v10-schema-client',
              'migration_inventory': plan['target_migration_sha256'], 'upstream_versions': upstream,
              'schema_probe': S.body(), 'presence_probe': Q.presence_expression().rsplit('IO.puts(', 1)[0]}
    if baseline:
        result.update(database_identity=baseline['database_identity'], baseline_applied=baseline['applied_versions'],
                      heap_identity=baseline['heap_identity'], background_statuses=baseline['background_statuses'])
    return result


def archive(environment, data, script):
    values = {k: v for k, v in environment.items() if v is not None}
    require(all(D.ENV_KEY.fullmatch(k) and isinstance(v, str) and '\0' not in v for k, v in values.items()),
            'Invalid migration environment')
    values.update(RELEASE_VERSION='10.2.6', RELEASE_DISTRIBUTION='none', RELEASE_TMP='/tmp',
                  MIGRATION_BACKGROUND_AUTOSTART_DISABLED='true', DISABLE_FILE_LOGGING='true')
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as tar:
        for name, content in (('environment.json', R.encoded(values)), ('payload.json', R.encoded(data)), ('migrate.exs', script)):
            entry = tarfile.TarInfo(name)
            entry.mode = 0o600
            entry.size = len(content)
            tar.addfile(entry, io.BytesIO(content))
    return stream.getvalue()


def client_command(image, network, name, owner):
    return ['docker', 'run', '--rm', '--pull', 'never', '-i', '--name', name, '--label', STATE_LABEL + '=' + owner,
            '--label', ROLE_LABEL + '=schema-client', '--network', network, '--read-only', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges', '--cpus', '2', '--memory', '1g',
            '--tmpfs', '/tmp:rw,mode=1777,size=64m', '--entrypoint', 'sh', image, '-c', RUN_SCRIPT]


def remove_client(api, state_path, state):
    """Stop only this journal's leftover client, before starting a retry."""
    name = state['client_name']
    ids = api.run(['docker', 'ps', '-aq', '--filter', 'name=^/' + name + '$']).split()
    if not ids:
        return
    require(len(ids) == 1, 'Ambiguous migration client')
    row = json.loads(api.run(['docker', 'inspect', ids[0]]))[0]
    labels = row.get('Config', {}).get('Labels') or {}
    require(labels.get(STATE_LABEL) == str(state_path) and labels.get(ROLE_LABEL) == 'schema-client'
            and row['Image'] == state['candidate_image'], 'Migration client ownership changed')
    api.run(['docker', 'rm', '-f', row['Id']], timeout=30)


def client(api, state_path, state, mode, directory=None, monitor=None, progress_phase=None, timeout_seconds=None):
    directory = directory or state_path.parent
    script = (directory / 'migrate.exs').read_bytes()
    data = deepcopy(state['migration_payload'])
    data['mode'] = mode
    body = archive(state['backend_environment'], data, script)
    command = client_command(state['candidate_image'], state['network'], state['client_name'], str(state_path))
    log_path = directory / (mode + '-' + uuid.uuid4().hex[:8] + '.log.private')
    process = None
    remove_client(api, state_path, state)
    try:
        with B.private_log(log_path) as log:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=log, stderr=log)
            process.stdin.write(body)
            process.stdin.close()
            started = time.monotonic()
            announced = started
            while process.poll() is None:
                require(time.monotonic() - started < (timeout_seconds or (1800 if mode == 'migrate' else 90)), 'Schema client exceeded time limit')
                if mode == 'migrate':
                    (monitor or space)(state)
                if time.monotonic() - announced >= 20:
                    print(json.dumps({'phase': progress_phase or ('applying_reviewed_schema' if mode == 'migrate' else 'checking_stopped_database'),
                                      'elapsed_seconds': int(time.monotonic() - started)}), flush=True)
                    announced = time.monotonic()
                time.sleep(1)
            require(process.returncode == 0, 'Schema client failed; inspect its server-only log')
        data = C.marked_json(api, log_path.read_text(errors='replace'))
        require(data.get('mode') == mode and data.get('application_started') is False, 'Schema client result incomplete')
        return data
    finally:
        if process is not None and process.poll() is None:
            remove_client(api, state_path, state)
            B.terminate(process)
        remove_client(api, state_path, state)


def private(path):
    require(path.is_file() and not path.is_symlink() and path.stat().st_uid == os.geteuid()
            and stat.S_IMODE(path.stat().st_mode) == 0o600, 'Private upgrade file unavailable or permissions changed')
    return path.read_bytes()


def load_state(api, path, manifest):
    require(path.is_absolute() and path.resolve() == path and path.name == 'state.json'
            and path.parent.parent == BASE and re.fullmatch(r'upgrade-[0-9TZ-]+-[a-f0-9]{8}', path.parent.name),
            'Unexpected private upgrade journal location')
    require(stat.S_IMODE(path.parent.stat().st_mode) == 0o700 and path.parent.stat().st_uid == os.geteuid(),
            'Upgrade directory permissions changed')
    state = json.loads(private(path))
    require(state.get('format') == FORMAT and state.get('environment') == 'dev'
            and state.get('chain_id') == manifest['chain_id'] and state.get('compose_project') == manifest['compose_project']
            and state.get('phase') in PHASES and type(state.get('schema_writes_may_have_started')) is bool,
            'Unexpected upgrade journal')
    require(state.get('recovery_policy') == RECOVERY and state.get('snapshot_verified') is False, 'Recovery policy changed')
    require(set(state['before']) == set(manifest['baseline_image_ids']), 'Saved service inventory changed')
    require(re.fullmatch(r'sha256:[a-f0-9]{64}', state['candidate_image'])
            and re.fullmatch(r'blockscout-v10-schema-[a-f0-9]{16}', state['client_name']), 'Invalid candidate/client identity')
    for name, digest in state['file_hashes'].items():
        require(name in ('previous.compose.json', 'forward.compose.json', 'migrate.exs')
                and R.digest(private(path.parent / name)) == digest, 'Saved upgrade file changed')
    require(set(state['file_hashes']) == {'previous.compose.json', 'forward.compose.json', 'migrate.exs'}, 'Incomplete saved upgrade files')
    require(state['migration_payload']['mode'] == 'inspect', 'Saved migration payload changed')
    return state


def rows(api, manifest, allow_missing=False):
    return api.inspect_project(manifest, require_running=False, allow_missing_services=PAIR if allow_missing else ())


def others(api, state, current):
    old = {n: r for n, r in state['before'].items() if n not in PAIR}
    new = {n: r for n, r in current.items() if n not in PAIR}
    # Definitions/identity/running state stay fixed; normal network runtime IDs
    # are irrelevant for services whose daemons remain running.
    M.validate_containers(old, new, policies='original', running={n for n, r in old.items() if r['State']['Running']})


def original_pair(state, current, disabled=False, stopped=False):
    old = {n: state['before'][n] for n in PAIR}
    new = {n: current[n] for n in PAIR}
    M.validate_containers(old, new, policies='disabled' if disabled else 'either',
                          running=set() if stopped else None)


def forward_model(model, before, image, source, path, nft):
    result = deepcopy(model)
    for name, service in result['services'].items():
        R.freeze_null_environment(service, before[name])
        service.pop('build', None)
        service['image'] = before[name]['Image']
        service['pull_policy'] = 'never'
    for name in PAIR:
        service = result['services'][name]
        service['image'] = image
        service['command'] = R.START
        env = service.setdefault('environment', {})
        env.update(RELEASE_VERSION='10.2.6', BLOCKSCOUT_VERSION=source, MIGRATION_BACKGROUND_AUTOSTART_DISABLED='true')
        if name == 'backend':
            env.update(nft['backend_environment'])
        labels = service.setdefault('labels', {})
        labels.pop(R.STATE_LABEL, None)
        labels.pop(R.SIDE_LABEL, None)
        labels.update({STATE_LABEL: str(path), ROLE_LABEL: name})
    return result


def preflight(api, root, private_path, receipt_path):
    review = Q.review(api, root, receipt_path, private_path)
    require(not review['blockers'] and review['upstream_pending_count'] == 29 and review['deferred_custom_count'] == 2,
            'Fresh review must establish exactly the 29 upstream and two deferred versions')
    manifest, requirements, _ = api.configuration(root, 'dev')
    values = api.load_private(private_path, requirements)
    before = api.inspect_project(manifest)
    receipt = C.private_receipt(api, receipt_path, root)
    source = B.clean_source(api, root)
    current_files = api.live_files(V.LIVE_DIRECTORY, before['backend'], manifest)
    current = api.compose_model(V.LIVE_DIRECTORY, manifest, values, current_files, True)
    managed = api.managed_model(root, manifest, values)
    nft = N.plan(api, managed)
    require(all(before[n]['State']['Running'] for n in PAIR), 'Both upgrade services must be running before preparation')
    for name in PAIR:
        stop_timeout(before[name])
    networks = set(before['backend']['NetworkSettings']['Networks']) & set(before['db']['NetworkSettings']['Networks'])
    require(len(networks) == 1, 'Expected exactly one shared backend/database network')
    identity = C.image_inventory(api, receipt['image_id'])['identity']
    # Validate worker mount access and numeric runtime identity too; no writes.
    worker = before['nft_media_handler']
    require(all(m.get('Type') == 'bind' and m.get('Destination') == '/app/logs' for m in worker['Mounts']),
            'Worker mount layout needs review')
    text = api.run(['docker', 'exec', worker['Id'], 'sh', '-c',
                    C.IMAGE_SCRIPT.split('find ', 1)[0] + 'test -d /app/logs && test -r /app/logs && test -w /app/logs\n'], timeout=10)
    require(C.parse_identity(api, text.strip()) == identity, 'Candidate and existing NFT worker numeric identity differ')
    api.ensure_stable(before, api.inspect_project(manifest), 'Deployment changed during upgrade preparation')
    return manifest, before, receipt, managed, current, nft, source, next(iter(networks)), review


def prepare(api, root, private_path, receipt_path, check=False):
    global STAGE
    STAGE = 'preflight'
    manifest, before, receipt, managed, current, nft, source, network, review = preflight(api, root, private_path, receipt_path)
    public = {'status': 'v10_upgrade_plan_ready', 'environment': 'dev', 'candidate_image': receipt['image_id'],
              'upstream_migrations': 29, 'deferred_custom_versions': sorted(R.VERSIONS), 'replace_services': list(PAIR),
              'automatic_background_migrations_held': True, 'recovery_policy': RECOVERY, 'snapshot_verified': False,
              'services_changed': False, 'effect': 'Apply stops backend and NFT worker, applies only reviewed schema versions, and starts both on v10. PostgreSQL and the other services remain running. No automatic v9 rollback after schema writes.'}
    if check:
        return public
    B.validate_state_directory(api, BASE, root)
    BASE.mkdir(parents=True, mode=0o700, exist_ok=True)
    directory = BASE / ('upgrade-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8])
    directory.mkdir(mode=0o700)
    path = directory / 'state.json'
    model = forward_model(managed, before, receipt['image_id'], receipt['source_commit'], path, nft)
    previous = R.snapshot(current, before, before['backend']['Image'], path, 'rollback')
    hashes = {}
    for name, content in (('previous.compose.json', R.snapshot_bytes(previous)),
                          ('forward.compose.json', R.snapshot_bytes(model)),
                          ('migrate.exs', (root / 'deploy/scripts/v10/migrate.exs').read_bytes())):
        R.write_new(directory / name, content)
        hashes[name] = R.digest(content)
    R.check_snapshot(api, directory / 'forward.compose.json', model, manifest, V.LIVE_DIRECTORY)
    state = {'format': FORMAT, 'environment': 'dev', 'chain_id': manifest['chain_id'], 'compose_project': manifest['compose_project'],
             'phase': 'prepared', 'schema_writes_may_have_started': False, 'snapshot_verified': False, 'recovery_policy': RECOVERY,
             'candidate_image': receipt['image_id'], 'candidate_tag': receipt['candidate_tag'],
             'candidate_source_commit': receipt['source_commit'], 'tool_source_commit': source[0],
             'before': before, 'source_environment': {n: managed['services'][n].get('environment', {}) for n in PAIR},
             'source_images': {n: s.get('image') for n, s in managed['services'].items()},
             'image_ids': {n: s['image'] for n, s in model['services'].items()}, 'file_hashes': hashes,
             'network': network, 'nft_role_plan': nft, 'backend_environment': model['services']['backend']['environment'],
             'client_name': 'blockscout-v10-schema-' + uuid.uuid4().hex[:16],
             'migration_payload': payload(api.read_json(root / 'deploy/upgrades/v10.2.6.json'))}
    print(json.dumps({'phase': 'upgrade_inputs_frozen', 'private_directory': str(directory), 'services_changed': False}), flush=True)
    STAGE = 'candidate_database_route'
    inspected = client(api, path, state, 'inspect')
    db_addresses = {a.get('IPAddress') for a in before['db']['NetworkSettings']['Networks'].values()}
    require(inspected['database_identity']['address'] in db_addresses and inspected['database_identity']['port'] == 5432,
            'Candidate database route does not match the inspected PostgreSQL container')
    budget = S.budget(inspected['schema'], {'data_available_bytes': capacity()}, inspected['tables'])
    require(budget['status'] == 'eligible_for_initial_schema_phase', 'Initial schema resource bounds no longer hold')
    state['migration_payload'] = payload(api.read_json(root / 'deploy/upgrades/v10.2.6.json'), inspected)
    api.ensure_stable(before, api.inspect_project(manifest), 'Deployment changed while freezing the upgrade')
    require(B.clean_source(api, root) == source and B.image_info(api, receipt['candidate_tag'])['Id'] == receipt['image_id'],
            'Source/candidate changed while freezing the upgrade')
    R.write_new(path, R.encoded(state))
    return {**public, 'status': 'v10_upgrade_prepared', 'state': str(path), 'candidate_database_route_verified': True,
            'initial_schema_budget': budget, 'note': 'Private journal/configuration retained on this server. Preparation did not stop services or run migrations.'}


def save(path, state, phase):
    global STAGE
    STAGE = phase
    state['phase'] = phase
    R.update_state(path, state)
    print(json.dumps({'phase': phase, 'state': str(path)}), flush=True)


def compose_pair(api, path, manifest, log_path):
    command = ['docker', 'compose', '-p', manifest['compose_project'], '--project-directory', str(V.LIVE_DIRECTORY),
               '--env-file', os.devnull, '-f', str(path.parent / 'forward.compose.json'), 'up', '-d', '--no-deps',
               '--no-build', '--pull', 'never', *PAIR]
    with B.private_log(log_path) as log:
        try:
            process = subprocess.Popen(command, stdout=log, stderr=log, env=api.compose_environment({}))
            started = time.monotonic()
            while process.poll() is None:
                require(time.monotonic() - started < 240, 'Paired activation timed out')
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    print(json.dumps({'phase': 'recreating_v10_pair', 'elapsed_seconds': int(time.monotonic() - started)}), flush=True)
            require(process.returncode == 0, 'Paired activation failed; use status/resume and private logs')
        finally:
            if 'process' in locals():
                B.terminate(process)


def runtime_expression():
    return B.market_policy().smoke_expression() + V.held_expression() + '''
children = Supervisor.which_children(Explorer.Supervisor);
held = Enum.all?(children, fn {id, _, _, _} ->
  name = if is_atom(id), do: Atom.to_string(id), else: "";
  not String.starts_with?(name, "Elixir.Explorer.Migrator.") and id != Explorer.TokenInstanceOwnerAddressMigration.Supervisor
end);
true = held;
"10.2.6" = Application.spec(:explorer, :vsn) |> to_string();
true = Application.get_env(:explorer, Explorer.Chain.Cache.Counters.TransactionsCount)[:approximate?];
60000 = Application.get_env(:indexer, :contract_code_retry_delay);
1 = Application.get_env(:indexer, :contract_code_concurrency);
"18417507517" = Application.get_env(:block_scout_web, :chain_id);
"176114083..latest" = Application.get_env(:indexer, :trace_block_ranges);
true = Application.get_env(:indexer, Indexer.Fetcher.InternalTransaction.Supervisor)[:disabled?];
false = is_pid(Process.whereis(Indexer.Temporary.UncatalogedTokenTransfers));
true = Indexer.Temporary.UncatalogedTokenTransfers.Supervisor.disabled?();
true = Application.get_env(:nft_media_handler, :r2_folder) == System.get_env("NFT_MEDIA_HANDLER_BUCKET_FOLDER");
market_price_ready = case Explorer.Market.Fetcher.Coin.get_coin_exchange_rate() do
  %{fiat_value: %Decimal{} = price} = coin ->
    rate = Explorer.Market.apply_native_coin_policy(coin);
    Decimal.compare(price, Decimal.new(0)) == :gt and is_nil(rate.market_cap) and is_nil(rate.total_supply) and is_nil(rate.available_supply)
  _ -> false
end;
IO.puts("BLOCKSCOUT_DEPLOY_CHECK=" <> Jason.encode!(%{settings_ok: true, automatic_migrators_absent: true, market_price_ready: market_price_ready}))
'''


def nft_expression():
    return '''
producer = :"producer@backend.blockscout.internal";
true = producer in Node.list();
true = is_pid(:rpc.call(producer, Process, :whereis, [Indexer.NFTMediaHandler.Queue], 5000));
{[], folder} = :rpc.call(producer, Indexer.NFTMediaHandler.Queue, :get_urls_to_fetch, [0], 5000);
true = folder == "/folder_1";
true = is_pid(Process.whereis(NFTMediaHandler.DispatcherInterface));
IO.puts("BLOCKSCOUT_DEPLOY_CHECK=" <> Jason.encode!(%{connected: true, v10_queue_protocol_ok: true, folder_preserved: true}))
'''


def verify(api, path, state, manifest, wait=False):
    model = api.compose_model(V.LIVE_DIRECTORY, manifest, {}, [path.parent / 'forward.compose.json'], True)
    ready = False
    result = {}
    for attempt in range(60 if wait else 1):
        current = rows(api, manifest, allow_missing=True)
        others(api, state, current)
        if not set(PAIR) <= current.keys():
            if wait:
                time.sleep(2)
                continue
            break
        for name in PAIR:
            row, service = current[name], model['services'][name]
            require(row['Image'] == state['candidate_image'] and row['Config'].get('Cmd') == R.START,
                    'Paired image/startup command differs')
            require((row['Config'].get('Labels') or {}).get(STATE_LABEL) == str(path)
                    and (row['Config'].get('Labels') or {}).get(ROLE_LABEL) == name, 'Another release owns the paired service')
            require(not api.live_environment_differences({'services': {name: service}}, {name: row}), 'Paired runtime environment changed')
            old = state['before'][name]
            require(api.inspect_mounts(old.get('Mounts')) == api.inspect_mounts(row.get('Mounts'))
                    and old['HostConfig'].get('PortBindings') == row['HostConfig'].get('PortBindings')
                    and set(old['NetworkSettings']['Networks']) == set(row['NetworkSettings']['Networks']),
                    'Paired service mounts/ports/networks changed')
        try:
            port = api.env_map(current['backend']).get('PORT', '4000')
            require(re.fullmatch(r'[0-9]{1,5}', port) and 0 < int(port) < 65536, 'Unexpected readiness port')
            readiness = api.run(['docker', 'exec', 'backend', 'curl', '-q', '--noproxy', '*', '-fsS', '--max-time', '5',
                                 '--max-filesize', '4096', 'http://127.0.0.1:' + port + '/api/health/readiness'], timeout=10).strip() == 'OK'
            health = {'backend_readiness_ok': readiness}
            runtime = C.marked_json(api, api.run(['docker', 'exec', 'backend', '/app/bin/blockscout', 'rpc', runtime_expression()], timeout=20))
            nft = C.marked_json(api, api.run(['docker', 'exec', 'nft_media_handler', '/app/bin/blockscout', 'rpc', nft_expression()], timeout=20))
            ready = all(health.values()) and runtime.get('settings_ok') is True and nft.get('v10_queue_protocol_ok') is True
            ready = ready and runtime.get('market_price_ready') is True
            ready = ready and all(current[n]['State']['Running'] and current[n]['RestartCount'] == 0 for n in PAIR)
            if ready:
                api.rpc_chain(manifest)
                for route in ('blocks', 'transactions', 'tokens'):
                    body = api.run(['docker', 'exec', 'backend', 'curl', '-q', '--noproxy', '*', '-fsS', '--max-time', '5',
                                    '--max-filesize', '2097152', 'http://127.0.0.1:' + port + '/api/v2/' + route], timeout=10)
                    require(isinstance(json.loads(body).get('items'), list), 'Representative API result unavailable')
            result = {**health, **runtime, **nft}
        except D.ConfigError:
            ready = False
            result = {'backend_readiness_ok': False, 'runtime_checks_completed': False}
        if ready or not wait:
            break
        if attempt % 5 == 0:
            print(json.dumps({'phase': 'waiting_for_v10_pair', 'attempt': attempt + 1}), flush=True)
        time.sleep(2)
    return {'status': 'verified' if ready else 'needs_attention', 'state': str(path), 'recorded_phase': state['phase'],
            'candidate_image': state['candidate_image'], 'schema_writes_may_have_started': state['schema_writes_may_have_started'],
            'deferred_custom_versions': sorted(R.VERSIONS), 'unrelated_containers_preserved': True,
            'automatic_background_migrations_held': True, 'snapshot_verified': False,
            'services': {n: api.lifecycle_summary(current[n]) for n in PAIR if n in current}, **result}


def resume(api, root, path, manifest, initial=False):
    state = load_state(api, path, manifest)
    require(state['phase'] != 'aborted' and (not initial or state['phase'] == 'prepared'), 'Use resume for an existing upgrade attempt')
    if state['phase'] == 'active':
        return verify(api, path, state, manifest)
    source = B.clean_source(api, root)
    require(source[1] == '10.2.6', 'Upgrade tool source version changed')
    api.run(['git', '-C', str(root), 'merge-base', '--is-ancestor', state['candidate_source_commit'], source[0]])
    changed = api.run(['git', '-C', str(root), 'diff', '--name-only', state['candidate_source_commit'], source[0]]).splitlines()
    require(all(n.startswith(('deploy/', 'docs/upgrades/')) for n in changed), 'Application/config changed after the saved candidate')
    require(B.image_info(api, state['candidate_image'])['Id'] == state['candidate_image'], 'Saved image unavailable')
    current = rows(api, manifest, allow_missing=state['phase'] == 'activation_requested')
    others(api, state, current)
    remove_client(api, path, state)
    if state['phase'] != 'activation_requested':
        original_pair(state, current)
        save(path, state, 'stopping_pair')
        for name in PAIR:
            api.run(['docker', 'update', '--restart=no', state['before'][name]['Id']], timeout=15)
        for name in reversed(PAIR):
            grace = stop_timeout(state['before'][name])
            print(json.dumps({'phase': 'stopping_service', 'service': name, 'grace_seconds': grace}), flush=True)
            api.run(['docker', 'stop', '--time', str(grace), state['before'][name]['Id']], timeout=grace + 15)
        current = rows(api, manifest)
        others(api, state, current)
        original_pair(state, current, disabled=True, stopped=True)
        save(path, state, 'pair_stopped')
        inspected = client(api, path, state, 'inspect')
        budget = S.budget(inspected['schema'], {'data_available_bytes': capacity()}, inspected['tables'])
        require(budget['status'] == 'eligible_for_initial_schema_phase', 'Stopped-database schema resource checks failed')
        if 'space_at_write_start' not in state:
            state['space_at_write_start'] = capacity()
        space(state)
        state['schema_writes_may_have_started'] = True
        save(path, state, 'schema_write_requested')
        migrated = client(api, path, state, 'migrate')
        state['applied_result'] = migrated
        save(path, state, 'schema_complete')
        others(api, state, rows(api, manifest))
        save(path, state, 'activation_requested')
    compose_pair(api, path, manifest, path.parent / ('activate-' + uuid.uuid4().hex[:8] + '.log.private'))
    result = verify(api, path, state, manifest, wait=True)
    if result['status'] == 'verified':
        save(path, state, 'active')
        result['recorded_phase'] = 'active'
    return result


def abort(api, path, manifest):
    state = load_state(api, path, manifest)
    require(state['schema_writes_may_have_started'] is False, 'Schema writes may have started; operator manual recovery is required for v9')
    current = rows(api, manifest)
    others(api, state, current)
    original_pair(state, current)
    remove_client(api, path, state)
    for name in PAIR:
        api.run(['docker', 'start', state['before'][name]['Id']], timeout=60)
        api.run(['docker', 'update', '--restart=' + M.policy(state['before'][name]), state['before'][name]['Id']], timeout=15)
    save(path, state, 'aborted')
    return {'status': 'aborted_before_schema_writes', 'state': str(path), 'schema_writes_may_have_started': False}


def status(api, path, manifest):
    state = load_state(api, path, manifest)
    if state['phase'] in ('active', 'activation_requested'):
        return verify(api, path, state, manifest)
    current = rows(api, manifest)
    others(api, state, current)
    return {'status': 'prepared' if state['phase'] == 'prepared' else 'needs_attention', 'state': str(path),
            'recorded_phase': state['phase'], 'schema_writes_may_have_started': state['schema_writes_may_have_started'],
            'running_pair': sorted(n for n in PAIR if current[n]['State']['Running']),
            'automatic_v9_rollback_available': not state['schema_writes_may_have_started'],
            'recovery_policy': RECOVERY, 'snapshot_verified': False}


def active_context(api, backend, manifest):
    labels = backend.get('Config', {}).get('Labels') or {}
    path = Path(labels[STATE_LABEL])
    state = load_state(api, path, manifest)
    require(state['schema_writes_may_have_started'] and state['phase'] in ('activation_requested', 'active')
            and backend['Image'] == state['candidate_image'] and labels.get(ROLE_LABEL) == 'backend', 'Upgrade context differs')
    require(labels.get('com.docker.compose.project.config_files') == str(path.parent / 'forward.compose.json'),
            'Upgrade Compose label changed')
    return {'upgrade': True, 'path': path, 'state': state, 'side': 'forward',
            'file': path.parent / 'forward.compose.json', 'image_ids': state['image_ids']}


def active_model(api, model, context):
    state = context['state']
    result = forward_model(model, state['before'], state['candidate_image'], state['candidate_source_commit'],
                           context['path'], state['nft_role_plan'])
    for name, service in model['services'].items():
        if service.get('image') != state['source_images'][name]:
            result['services'][name]['image'] = service.get('image')
        if name in PAIR:
            keys = {'RELEASE_VERSION', 'BLOCKSCOUT_VERSION', V.HOLD}
            if name == 'backend':
                keys.add('NFT_MEDIA_HANDLER_BUCKET_FOLDER')
            for key in keys:
                source = service.get('environment', {})
                if source.get(key) != state['source_environment'][name].get(key):
                    if key in source:
                        result['services'][name]['environment'][key] = source[key]
                    else:
                        result['services'][name]['environment'].pop(key, None)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    for name in ('check', 'prepare', 'apply', 'resume', 'abort', 'status'):
        group.add_argument('--' + name, action='store_true')
    parser.add_argument('--private-file', type=Path)
    parser.add_argument('--receipt', type=Path)
    parser.add_argument('--state', type=Path)
    args = parser.parse_args()
    try:
        require(sys.platform.startswith('linux') and os.geteuid() == 0, 'Run on the Linux dev server as root')
        manifest, _, _ = D.configuration(ROOT, 'dev')
        with M.global_lock(not (args.check or args.status)), B.build_lock(D, D.MANAGED_RELEASES, ROOT):
            if args.check or args.prepare:
                require(args.private_file is not None and args.receipt is not None and args.state is None, 'Provide private file and receipt')
                result = prepare(D, ROOT, args.private_file, args.receipt, check=args.check)
            else:
                require(args.state is not None and args.private_file is None and args.receipt is None, 'Provide the saved upgrade state only')
                if args.status:
                    result = status(D, args.state, manifest)
                elif args.abort:
                    result = abort(D, args.state, manifest)
                else:
                    result = resume(D, ROOT, args.state, manifest, initial=args.apply)
        print(json.dumps(result, indent=2))
        return 1 if result.get('status') == 'needs_attention' else 0
    except Exception as error:
        print(json.dumps({'status': 'stopped', 'stage': STAGE, 'raw_values_suppressed': True,
                          'reason': str(error) if isinstance(error, UpgradeError) else 'Upgrade check failed; inspect the named stage and private logs',
                          'state': str(args.state) if args.state else None,
                          'recovery': 'Use --status and the server-only journal/logs. --resume uses the same image and actual schema versions. --abort is available only before schema writes. After writes, v9 recovery is operator-managed.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
