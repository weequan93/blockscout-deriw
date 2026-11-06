#!/usr/bin/env python3
"""Supervise the native Blockscout normalization worker with durable DB cursors."""
from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

import candidate_build as B
import containerd_maintenance as M
import dev_config as D
import release as R
import v10_normalization_session as J
import v10_normalization_reuse as Q
import v10_upgrade as U
import v10_vacuum as V

ROOT = Path(__file__).resolve().parents[2]
BASE = Path('/data/blockscout-dev-private/v10-native-normalization')
FORMAT = 'blockscout-dev-v10-native-normalization-v1'
UNIT = 'blockscout-v10-native-normalization.service'
MIN_FREE = 200 * 1024**3
PAUSE_FREE = 210 * 1024**3
RESUME_FREE = 220 * 1024**3
POLL_SECONDS = 10
LEASE_MS = 30000
TERMINAL = ('completed', 'needs_attention')
STOP_REQUESTED = False
WORKER_PROCESSES = {}

METADATA = r'''
true = Explorer.Migrator.StartupControl.disabled?()
{:ok, result} = Explorer.Repo.transaction(fn ->
  q = fn sql, args -> Explorer.Repo.query!(sql, args, log: false, timeout: 2000).rows end
  q.("SET TRANSACTION READ ONLY", [])
  q.("SET LOCAL statement_timeout='1500ms'", [])
  [[result]] = q.(~S"""
  SELECT jsonb_build_object('observed_at',clock_timestamp()::text,
    'native_status',(SELECT status FROM migrations_status WHERE migration_name='empty_internal_transactions_data'),
    'native_meta',(SELECT meta FROM migrations_status WHERE migration_name='empty_internal_transactions_data'),
    'dead_rows',(SELECT n_dead_tup FROM pg_stat_user_tables WHERE relid='public.internal_transactions'::regclass),
    'autovacuum_running',(SELECT count(*) FROM pg_stat_progress_vacuum WHERE relid='public.internal_transactions'::regclass),
    'long_client_transactions',(SELECT count(*) FROM pg_stat_activity
      WHERE datname=current_database() AND backend_type='client backend'
        AND pid<>pg_backend_pid() AND application_name<>$1
        AND xact_start<clock_timestamp()-interval '300 seconds'),
    'autovacuum_enabled',current_setting('autovacuum')='on' AND current_setting('track_counts')='on'
      AND NOT COALESCE((SELECT 'autovacuum_enabled=false'=ANY(reloptions) FROM pg_class
        WHERE oid='public.internal_transactions'::regclass),false))
  """, [NATIVE_APPLICATION_NAME])
  result
end, timeout: 5000)
IO.puts("BLOCKSCOUT_DEPLOY_CHECK=" <> Jason.encode!(result))
'''


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, state, phase=None, reason=None):
    if phase:
        state['phase'] = phase
    state['reason'] = reason
    state['observed_at_utc'] = now()
    R.update_state(path, state)


def load(path):
    J.P.require(path.is_absolute() and path.resolve() == path and path.name == 'state.json'
                and path.parent.parent == BASE
                and re.fullmatch(r'job-[0-9TZ-]+-[a-f0-9]{8}', path.parent.name), 'Unexpected native job path')
    J.owned_directory(path.parent)
    state = json.loads(U.private(path))
    frozen = U.private(path.parent / 'inputs.json')
    inputs = json.loads(frozen)
    J.P.require(state['format'] == FORMAT and state['inputs'] == inputs
                and state['inputs_sha256'] == R.digest(frozen), 'Frozen native job inputs changed')
    J.P.require(inputs['min_free_bytes'] == MIN_FREE and inputs['pause_free_bytes'] == PAUSE_FREE
                and inputs['resume_free_bytes'] == RESUME_FREE
                and inputs['cumulative_growth_cap_bytes'] is None
                and inputs['maximum_batch_blocks'] == 1000
                and re.fullmatch(r'[a-f0-9]{16}', inputs['payload']['token'])
                and inputs['client_name'] == 'blockscout-v10-native-' + inputs['payload']['token'],
                'Native job policy changed')
    J.P.require(R.digest(U.private(path.parent / 'native.exs')) == inputs['script_sha256'],
                'Frozen native worker source changed')
    J.P.require(state['phase'] in ('prepared', 'launch_requested', 'running', 'waiting', 'paused', *TERMINAL)
                and type(state['batch_blocks']) is int and 1 <= state['batch_blocks'] <= 1000,
                'Invalid native job checkpoint')
    return state


def public(path, state):
    inputs = state['inputs']
    meta = (state.get('database') or {}).get('native_meta') or {}
    return {'status': 'native_normalization_' + state['phase'], 'state': str(path),
            'reason': state.get('reason'), 'observed_at_utc': state.get('observed_at_utc'),
            'native_cursor': meta.get('max_block_number'),
            'native_migration_status': (state.get('database') or {}).get('native_status'),
            'manual_phase_rows_verified': inputs['manual_phase_rows'],
            'manual_maintenance_selected_rows': inputs['maintenance_selected_rows'],
            'starting_before_block': inputs['payload']['start_before_block'],
            'current_data_free_bytes': state.get('free_bytes'),
            'minimum_data_free_bytes': MIN_FREE, 'cumulative_growth_cap_bytes': None,
            'native_worker': state.get('worker'), 'batch_blocks': state['batch_blocks'],
            'database_checkpoint_persisted_by': 'Explorer.Migrator.FillingMigration',
            'normalization_complete': state['phase'] == 'completed',
            'heavy_index_and_primary_key_migrations_started': False,
            'note': 'Native batching and database checkpoints. Automatic vacuum is configured only for internal_transactions. Other migrators remain held.'}


def rpc(api, code):
    return V.C.marked_json(api, api.run(
        ['docker', 'exec', '-i', 'backend', 'sh', '-c', V.RPC_SCRIPT], input_text=code, timeout=20))


def owned(api, path, inputs):
    ids = api.run(['docker', 'ps', '-aq', '--filter', 'name=^/' + inputs['client_name'] + '$']).split()
    if not ids:
        return None
    J.P.require(len(ids) == 1, 'Ambiguous native worker container')
    row = json.loads(api.run(['docker', 'inspect', ids[0]]))[0]
    labels = row.get('Config', {}).get('Labels') or {}
    J.P.require(row['Image'] == inputs['candidate_image']
                and labels.get(U.STATE_LABEL) == str(path)
                and labels.get(U.ROLE_LABEL) == 'native-normalizer', 'Native container ownership changed')
    return row


def stop_client(api, path, inputs):
    row = owned(api, path, inputs)
    if row:
        try:
            tail = api.run(['sh', '-c', 'exec docker logs --tail 100 "$1" 2>&1',
                            'native-log', row['Id']], timeout=10)
            R.update_state(path.parent / 'worker-log-tail.json.private', {'log': tail[-65536:]})
        except (D.ConfigError, OSError):
            pass
        api.run(['docker', 'rm', '-f', row['Id']], timeout=30)
    process = WORKER_PROCESSES.pop(inputs['client_name'], None)
    if process:
        B.terminate(process)


def reserve_sources(path, state):
    inputs = state['inputs']
    entries = (
        (Path(inputs['parent_state']), inputs['parent_sha256'], 'normalization-cycle.json',
         {'format': Q.CYCLE_FORMAT, 'parent_inputs_sha256': inputs['parent_inputs_sha256'],
          'state': str(path), 'inputs_sha256': state['inputs_sha256']}),
        (Path(inputs['vacuum_state']), inputs['vacuum_sha256'], 'normalization-reuse.json',
         {'format': Q.FORMAT, 'vacuum_inputs_sha256': inputs['vacuum_inputs_sha256'],
          'state': str(path), 'inputs_sha256': state['inputs_sha256']}))
    for parent, digest, name, record in entries:
        J.P.require(R.digest(U.private(parent)) == digest, 'Native handoff source changed')
        reservation = parent.parent / name
        if reservation.exists():
            J.P.require(json.loads(U.private(reservation)) == record, 'Handoff reserved by another job')
        else:
            Q.reserve(parent, digest, name, path, record)


def lease(api, inputs, run, batch_blocks=1000, valid_ms=LEASE_MS):
    J.P.require(type(batch_blocks) is int and 1 <= batch_blocks <= 1000
                and type(valid_ms) is int and 0 < valid_ms <= LEASE_MS, 'Invalid native lease')
    data = {'token': inputs['payload']['token'], 'mode': 'run' if run else 'paused',
            'batch_blocks': batch_blocks,
            'valid_until_unix_ms': int(time.time() * 1000) + valid_ms}
    api.run(['docker', 'exec', '-i', inputs['client_name'], 'sh', '-c',
             'set -eu; umask 077; cat > /tmp/native-lease.tmp; mv /tmp/native-lease.tmp /tmp/native-lease.json'],
            input_text=R.encoded(data).decode(), timeout=10)


def read_worker(api, inputs):
    text = api.run(['docker', 'exec', inputs['client_name'], 'sh', '-c',
                    'if test -f /tmp/native-status.json; then cat /tmp/native-status.json; fi'], timeout=10)
    if not text:
        return {'phase': 'starting'}
    report = json.loads(text)
    J.P.require(report.get('token') == inputs['payload']['token'], 'Native worker token changed')
    return {k: report[k] for k in ('phase', 'reason', 'last_batch_updated_rows',
                                 'last_batch_wal_bytes', 'last_batch_rows') if k in report}


def launch(api, path, state):
    inputs = state['inputs']
    existing = owned(api, path, inputs)
    if existing:
        if existing['State']['Running']:
            return
        stop_client(api, path, inputs)
    save(path, state, 'launch_requested')
    command = U.client_command(inputs['candidate_image'], inputs['network'], inputs['client_name'], str(path))
    command.remove('--rm')
    command[command.index(U.ROLE_LABEL + '=schema-client')] = U.ROLE_LABEL + '=native-normalizer'
    command[2:2] = ['--log-driver', 'json-file', '--log-opt', 'max-size=8m', '--log-opt', 'max-file=2']
    payload = {**inputs['payload'], 'batch_blocks': state['batch_blocks']}
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    WORKER_PROCESSES[inputs['client_name']] = process
    try:
        process.stdin.write(U.archive(inputs['backend_environment'], payload, U.private(path.parent / 'native.exs')))
        process.stdin.close()
    except BaseException:
        B.terminate(process)
        raise
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if STOP_REQUESTED:
            return
        if owned(api, path, inputs):
            if read_worker(api, inputs).get('phase') != 'starting':
                return
        J.P.require(process.poll() is None, 'Native worker exited during startup; inspect its private Docker log')
        time.sleep(0.2)
    raise U.UpgradeError('Native worker startup exceeded 60 seconds')


def prepare(api, root, parent_path, vacuum_path):
    manifest, _, _ = api.configuration(root, 'dev')
    source = B.clean_source(api, root)
    J.P.require(source[1] == '10.2.6', 'Source release changed')
    previous = J.load(parent_path, manifest, check_resources=False)
    review, cycle = Q.prepare_cycle_review(api, vacuum_path, parent_path, previous, manifest)
    J.P.require(review['normalization_cursor'] > 0, 'Historical range already exhausted; review completion')
    J.P.require(J.P.free_bytes() >= MIN_FREE, 'Less than 200 GiB data free')
    B.validate_state_directory(api, BASE, root)
    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    for directory in BASE.iterdir():
        J.P.require(load(directory / 'state.json')['phase'] == 'completed',
                    'Resume the existing native job instead of preparing another')
    upgrade_path = Path(previous['inputs']['upgrade_state'])
    upgrade = U.load_state(api, upgrade_path, manifest)
    api.run(['git', '-C', str(root), 'merge-base', '--is-ancestor', upgrade['candidate_source_commit'], source[0]])
    changed = api.run(['git', '-C', str(root), 'diff', '--name-only', upgrade['candidate_source_commit'], source[0]]).splitlines()
    J.P.require(all(p.startswith(('deploy/', 'docs/upgrades/')) for p in changed), 'Application/config changed after pinned image')
    token = uuid.uuid4().hex[:16]
    statuses = previous['last']['baseline']['statuses']
    original_status = next((s for s in statuses if s['migration_name'] == 'empty_internal_transactions_data'), None)
    inputs = {'tool_source_commit': source[0], 'chain_id': manifest['chain_id'],
              'upgrade_state': str(upgrade_path), 'candidate_image': upgrade['candidate_image'],
              'network': upgrade['network'], 'backend_environment': upgrade['backend_environment'],
              'runtime_before': previous['inputs']['runtime_before'],
              'source_root': str(root), 'python_executable': str(Path(sys.executable).resolve()),
              'min_free_bytes': MIN_FREE, 'pause_free_bytes': PAUSE_FREE, 'resume_free_bytes': RESUME_FREE,
              'cumulative_growth_cap_bytes': None, 'maximum_batch_blocks': 1000,
              'manual_phase_rows': previous['rows'],
              'maintenance_selected_rows': review['maintenance_selected_rows'],
              'maintenance_origin': previous['inputs']['maintenance_origin'],
              'parent_state': str(parent_path), 'parent_sha256': R.digest(U.private(parent_path)),
              'parent_inputs_sha256': cycle['inputs_sha256'],
              'vacuum_state': str(vacuum_path), 'vacuum_sha256': R.digest(U.private(vacuum_path)),
              'vacuum_inputs_sha256': review['vacuum_inputs_sha256'],
              'client_name': 'blockscout-v10-native-' + token,
              'payload': {**review['database_anchor'],
                          'applied_versions': previous['inputs']['payload']['applied_versions'],
                          'token': token, 'application_name': 'blockscout-v10-native-' + token,
                          'start_before_block': review['normalization_cursor'],
                          'native_status_before': original_status,
                          'table_options_before': review['resources_after']['table']['options']}}
    script = (root / 'deploy/scripts/v10/native_normalization.exs').read_bytes()
    inputs['script_sha256'] = R.digest(script)
    directory = BASE / ('job-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8])
    directory.mkdir(mode=0o700)
    path = directory / 'state.json'
    R.write_new(directory / 'inputs.json', R.encoded(inputs))
    R.write_new(directory / 'native.exs', script)
    state = {'format': FORMAT, 'inputs': inputs, 'inputs_sha256': R.digest(R.encoded(inputs)),
             'phase': 'prepared', 'batch_blocks': 1000, 'reason': None, 'observed_at_utc': now()}
    R.write_new(path, R.encoded(state))
    print(json.dumps({'phase': 'native_inputs_saved', 'state': str(path), 'database_writes': False}), flush=True)
    try:
        reserve_sources(path, state)
    except Exception as error:
        error.state_path = path
        raise
    return public(path, state)


def gate(state, free, database, health_ok):
    if free < PAUSE_FREE or (state.get('reason') == 'low_space' and free < RESUME_FREE):
        return 'low_space'
    if not health_ok:
        return 'application_health'
    if not database.get('autovacuum_enabled'):
        return 'autovacuum_disabled'
    if database.get('long_client_transactions', 0):
        return 'long_transaction'
    if database.get('dead_rows', 0) > 10000000:
        return 'waiting_for_autovacuum'
    return None


def run(api, root, path):
    global STOP_REQUESTED
    state = load(path)
    if state['phase'] in TERMINAL:
        return public(path, state)
    inputs = state['inputs']
    J.P.require(B.clean_source(api, root) == (inputs['tool_source_commit'], '10.2.6'), 'Use native job creation commit')
    reserve_sources(path, state)
    manifest, _, _ = api.configuration(root, 'dev')
    upgrade_path = Path(inputs['upgrade_state'])
    upgrade = U.load_state(api, upgrade_path, manifest)
    last_health = 0
    health = {'status': 'needs_attention'}
    try:
        while not STOP_REQUESTED and not (path.parent / 'pause.request').exists():
            try:
                runtime = api.inspect_project(manifest)
            except D.ConfigError:
                save(path, state, 'waiting', 'runtime_observation_unavailable')
                time.sleep(POLL_SECONDS)
                continue
            api.ensure_stable(inputs['runtime_before'], runtime, 'Runtime changed; native cursor retained')
            Q.verify_holds(api)
            try:
                if time.monotonic() - last_health >= 15:
                    started = time.monotonic()
                    health = U.verify(api, upgrade_path, upgrade, manifest, deadline=started + 25)
                    last_health = started
                    state['health'] = health
                name = json.dumps(inputs['payload']['application_name']).replace('#{', r'\#{')
                database = rpc(api, METADATA.replace('NATIVE_APPLICATION_NAME', name))
            except D.ConfigError:
                save(path, state, 'waiting', 'backend_observation_unavailable')
                time.sleep(POLL_SECONDS)
                continue
            previous_meta = (state.get('database') or {}).get('native_meta') or {}
            state.update(database=database, free_bytes=J.P.free_bytes())
            if STOP_REQUESTED or (path.parent / 'pause.request').exists():
                break
            meta = database.get('native_meta') or {}
            if meta.get('v10_native_job') not in (None, inputs['payload']['token']):
                raise U.UpgradeError('Native migration belongs to another job')
            if meta.get('v10_native_job') == inputs['payload']['token']:
                cursor = meta.get('max_block_number')
                J.P.require(type(cursor) is int and -1 <= cursor < inputs['payload']['start_before_block'],
                            'Native cursor is outside its frozen range')
                if previous_meta.get('v10_native_job') == inputs['payload']['token']:
                    J.P.require(cursor <= previous_meta['max_block_number'], 'Native cursor moved backwards')
                    if cursor < previous_meta['max_block_number']:
                        state['transient_retries'] = 0
                        state['worker_crash_retries'] = 0
            # Completion needs no write authorization or disk-space recovery.
            if database.get('native_status') == 'completed':
                J.P.require(meta.get('v10_native_job') == inputs['payload']['token'], 'Unowned completion record')
                J.P.require(meta.get('max_block_number') == -1, 'Native completion has an unfinished cursor')
                try:
                    rpc(api, 'true = Explorer.Migrator.EmptyInternalTransactionsData.migration_finished?()\nExplorer.Migrator.EmptyInternalTransactionsData.update_cache()\nIO.puts("BLOCKSCOUT_DEPLOY_CHECK=" <> Jason.encode!(%{cache_refreshed: true}))\n')
                except D.ConfigError:
                    save(path, state, 'waiting', 'completion_cache_refresh')
                    time.sleep(POLL_SECONDS)
                    continue
                stop_client(api, path, inputs)
                save(path, state, 'completed')
                return public(path, state)
            fresh = time.monotonic() - last_health < 25
            reason = gate(state, state['free_bytes'], database, fresh and health.get('status') == 'verified')
            if reason:
                container = owned(api, path, inputs)
                if container and container['State']['Running']:
                    lease(api, inputs, False)
                save(path, state, 'waiting', reason)
            else:
                container = owned(api, path, inputs)
                if container and not container['State']['Running']:
                    state['worker_crash_retries'] = state.get('worker_crash_retries', 0) + 1
                    J.P.require(state['worker_crash_retries'] <= 5,
                                'Native worker repeatedly exited without cursor progress; private diagnostics retained')
                launch(api, path, state)
                if STOP_REQUESTED:
                    break
                state['worker'] = read_worker(api, inputs)
                if state['worker'].get('phase') == 'needs_attention':
                    reason = state['worker'].get('reason')
                    if reason in ('native_batch_requires_smaller_range', 'native_transaction_wal_limit') and state['batch_blocks'] > 1:
                        stop_client(api, path, inputs)
                        state['batch_blocks'] = max(1, state['batch_blocks'] // 2)
                        save(path, state, 'waiting', 'reducing_native_batch')
                        continue
                    if reason == 'native_lease_expired_before_update':
                        stop_client(api, path, inputs)
                        save(path, state, 'waiting', 'refreshing_native_authorization')
                        continue
                    if reason == 'native_transient_database_error' and state.get('transient_retries', 0) < 5:
                        state['transient_retries'] = state.get('transient_retries', 0) + 1
                        stop_client(api, path, inputs)
                        save(path, state, 'waiting', 'retrying_native_database_batch')
                        time.sleep(POLL_SECONDS)
                        continue
                    save(path, state, 'needs_attention', state['worker'].get('reason'))
                    stop_client(api, path, inputs)
                    return public(path, state)
                if state['worker'].get('last_batch_rows', 10001) <= 2000:
                    state['batch_blocks'] = min(1000, state['batch_blocks'] * 2)
                valid_ms = min(LEASE_MS, int((25 - (time.monotonic() - last_health)) * 1000))
                if valid_ms <= 0:
                    save(path, state, 'waiting', 'application_health')
                    continue
                lease(api, inputs, True, state['batch_blocks'], valid_ms)
                save(path, state, 'running')
            time.sleep(POLL_SECONDS)
        save(path, state, 'paused', 'operator_requested')
        stop_client(api, path, inputs)
        return public(path, state)
    except Exception as error:
        # Lease expiry prevents additional batches even if Docker is unavailable.
        save(path, state, 'needs_attention', str(error) if isinstance(error, D.ConfigError)
             else 'Native controller check failed; private state retained')
        try:
            stop_client(api, path, inputs)
        except Exception:
            pass
        return public(path, state)


def service_text(path):
    J.P.require(str(path).startswith('/data/') and not re.search(r'[\s"\\%]', str(path)), 'Unexpected service state path')
    inputs = load(path)['inputs']
    J.P.require(inputs['source_root'] == str(ROOT), 'Use native job source directory')
    executable = Path(inputs['python_executable'])
    J.P.require(executable.is_absolute() and not re.search(r'[\s"\\%]', str(executable))
                and not re.search(r'[\s"\\%]', str(ROOT)), 'Unexpected service executable/source path')
    return ('[Unit]\nDescription=Blockscout native normalization\nAfter=docker.service\nRequires=docker.service\n'
            'RequiresMountsFor=/data\n\n[Service]\nType=simple\nUser=root\nWorkingDirectory=' + str(ROOT) + '\n'
            'ExecStart=' + str(executable) + ' -u ' + str(ROOT / 'deploy/scripts/v10_native_normalization.py') +
            ' --resume --state ' + str(path) + '\nRestart=on-failure\nRestartSec=30\nRestartPreventExitStatus=2\n'
            'TimeoutStopSec=60\nKillMode=process\nUMask=0077\n\n[Install]\nWantedBy=multi-user.target\n')


def install_service(api, path):
    state = load(path)
    J.P.require(B.clean_source(api, ROOT) == (state['inputs']['tool_source_commit'], '10.2.6'), 'Use native job creation commit')
    target = Path('/etc/systemd/system') / UNIT
    data = service_text(path).encode()
    if target.exists():
        J.P.require(not target.is_symlink() and target.read_bytes() == data, 'Another native service definition exists')
    else:
        R.write_new(target, data)
        os.chmod(target, 0o644)
    api.run(['systemctl', 'daemon-reload'], timeout=15)
    api.run(['systemctl', 'enable', '--now', UNIT], timeout=30)
    return {**public(path, state), 'service': UNIT, 'service_start_requested': True}


def require_service(path):
    target = Path('/etc/systemd/system') / UNIT
    J.P.require(target.is_file() and not target.is_symlink()
                and target.read_text() == service_text(path), 'Native service belongs to another job')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    for name in ('prepare', 'run', 'resume', 'status', 'install-service', 'pause', 'start'):
        modes.add_argument('--' + name, action='store_true')
    parser.add_argument('--state', type=Path)
    parser.add_argument('--after-session', type=Path)
    parser.add_argument('--after-vacuum', type=Path)
    args = parser.parse_args()
    try:
        J.P.require(sys.platform.startswith('linux') and os.geteuid() == 0, 'Run on the Linux dev server as root')
        if args.prepare or args.run:
            J.P.require(args.state is None and args.after_session and args.after_vacuum, 'Provide the finished session and qualified vacuum')
            with M.global_lock(True), B.build_lock(D, D.MANAGED_RELEASES, ROOT):
                result = prepare(D, ROOT, args.after_session, args.after_vacuum)
            args.state = Path(result['state'])
            if args.run:
                result = install_service(D, args.state)
        else:
            J.P.require(args.state and not args.after_session and not args.after_vacuum, 'Use only the saved native job state')
            state = load(args.state)
            if args.status:
                result = public(args.state, state)
            elif args.pause:
                require_service(args.state)
                request = args.state.parent / 'pause.request'
                if not request.exists():
                    R.write_new(request, b'operator requested\n')
                D.run(['systemctl', 'stop', UNIT], timeout=65)
                result = public(args.state, load(args.state))
            elif args.start:
                require_service(args.state)
                if state['phase'] == 'completed':
                    print(json.dumps(public(args.state, state), indent=2))
                    return 0
                if state['phase'] in ('prepared', 'paused', 'needs_attention'):
                    with M.global_lock(True):
                        state = load(args.state)
                        stop_client(D, args.state, state['inputs'])
                        state['transient_retries'] = 0
                        state['worker_crash_retries'] = 0
                        save(args.state, state, 'prepared')
                request = args.state.parent / 'pause.request'
                if request.exists():
                    U.private(request)
                    request.unlink()
                D.run(['systemctl', 'start', UNIT], timeout=30)
                result = public(args.state, state)
            elif args.install_service:
                result = install_service(D, args.state)
            else:
                def stopping(*_):
                    global STOP_REQUESTED
                    STOP_REQUESTED = True
                signal.signal(signal.SIGTERM, stopping)
                signal.signal(signal.SIGINT, stopping)
                with M.global_lock(True), B.build_lock(D, D.MANAGED_RELEASES, ROOT):
                    result = run(D, ROOT, args.state)
        print(json.dumps(result, indent=2))
        return 2 if result['status'] == 'native_normalization_needs_attention' else 0
    except Exception as error:
        print(json.dumps({'status': 'stopped', 'raw_values_suppressed': True,
                          'state': str(getattr(error, 'state_path', args.state)) if getattr(error, 'state_path', args.state) else None,
                          'reason': str(error) if isinstance(error, D.ConfigError) else 'Native job check failed; retain its private files'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
