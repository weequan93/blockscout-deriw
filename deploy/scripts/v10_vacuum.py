#!/usr/bin/env python3
"""Prepare or monitor one ordinary-vacuum qualification window on dev."""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import traceback
import uuid

import candidate_build as B
import containerd_maintenance as M
import deploy_check as C
import dev_config as D
import release as R
import upgrade_plan as K
import v10_empty_blocks_hold as E
import v10_metrics_hold as H
import v10_normalization_session as J
import v10_normalization_pilot as P
import v10_upgrade as U

ROOT = Path(__file__).resolve().parents[2]
BASE = Path('/data/blockscout-dev-private/v10-vacuum')
FORMAT = 'blockscout-dev-v10-vacuum-window-v1'
TRANSACTION_GUARD_SCOPE = 'pre_vacuum_clients'
HEALTH_RECHECK_POLICY = 'bounded-v2'
LEGACY_HEALTH_RECHECK_POLICY = 'bounded-v1'
HEALTH_RETRY_MIN_SECONDS = 8
SQL = 'VACUUM (VERBOSE, INDEX_CLEANUP ON, TRUNCATE OFF, PARALLEL 0) public.internal_transactions'
SQL_MD5 = hashlib.md5(SQL.encode()).hexdigest()
LIMITS = dict(duration_seconds=3600, preparation_seconds=600, poll_seconds=10, health_seconds=30,
              lease_seconds=45, free_bytes=200 * 1024**3, space_bytes=4 * 1024**3,
              cluster_wal_bytes=16 * 1024**3, relation_read_bytes=2 * 1024**4,
              oldest_transaction_seconds=300, journal_bytes=64 * 1024**2, log_bytes=16 * 1024**2,
              aggregate_journal_bytes=256 * 1024**2)
INDEX_CLEANUP_LIMITS = {**LIMITS, 'duration_seconds': 14400}
FAST_PROFILE = 'index-cleanup-fast-v1'
PROFILES = ('index-cleanup-v1', FAST_PROFILE)
CONTROLLER_FORMAT = 'blockscout-dev-v10-vacuum-controller-v1'
RPC_SCRIPT = '''set -eu
umask 077
vacuum_probe_file=$(mktemp /tmp/blockscout-v10-vacuum-observe.XXXXXXXX)
trap 'rm -f "$vacuum_probe_file"' EXIT HUP INT TERM
cat > "$vacuum_probe_file"
/app/bin/blockscout rpc "Code.eval_file(\\"$vacuum_probe_file\\"); :ok"
'''


class ResourceReviewError(U.UpgradeError):
    def __init__(self, message, data):
        super().__init__(message)
        self.review = {'transactions': data['transactions'], 'query_diagnostics': data.get('query_diagnostics', [])}


class HealthReviewError(U.UpgradeError):
    def __init__(self, samples):
        stage = samples[-1]['report'].get('verification_failure', {}).get('stage', 'incomplete_runtime_check')
        super().__init__('Runtime health check failed during ' + stage + '; vacuum remains unqualified')
        self.samples = samples


def health_verification(api, upgrade_path, upgrade, manifest, path=None, state=None, monitoring=False):
    budget = LIMITS['health_seconds']
    if monitoring and state and state.get('last_health_at_utc'):
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(state['last_health_at_utc'])).total_seconds()
        P.require(0 <= age < budget, 'Vacuum health evidence expired before verification')
        budget -= age
    # Leave one second for the final identity observation and lease publication.
    deadline = time.monotonic() + max(0, budget - 1)
    policy = state['inputs'].get('health_recheck_policy') if state else HEALTH_RECHECK_POLICY
    attempts = 2 if policy in (LEGACY_HEALTH_RECHECK_POLICY, HEALTH_RECHECK_POLICY) else 1
    samples = []
    for attempt in range(1, attempts + 1):
        if path and state:
            disk_guard(path, state['inputs'])
        report = U.verify(api, upgrade_path, upgrade, manifest, deadline=deadline)
        if time.monotonic() > deadline:
            report = {**report, 'status': 'needs_attention',
                      'verification_failure': {'stage': 'verification_deadline', 'kind': 'deadline_reached'},
                      'verification_probes': {**report.get('verification_probes', {}), 'verification_deadline': False}}
        sample = {'observed_at_utc': datetime.now(timezone.utc).isoformat(), 'attempt': attempt, 'report': report}
        samples.append(sample)
        if path and state:
            fd = os.open(path.parent / 'health-observations.jsonl', os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'ab') as stream:
                stream.write(json.dumps(sample, sort_keys=True, separators=(',', ':')).encode() + b'\n')
                stream.flush()
                os.fsync(stream.fileno())
            state['latest_health'] = sample
            state['health_rechecks'] = state.get('health_rechecks', 0) + (attempt > 1)
            R.update_state(path, state)
        if report['status'] == 'verified' and time.monotonic() <= deadline:
            if attempt > 1:
                print(json.dumps({'phase': 'runtime_health_recovered', 'state': str(path) if path else None,
                                  'attempts': attempt, 'initial_failure': samples[0]['report'].get('verification_failure')}), flush=True)
            return report
        probes = report.get('verification_probes', {})
        services = report.get('services', {})
        hard_failure = any(probes.get(key) is False for key in ('runtime_settings', 'automatic_migrators_absent', 'service_lifecycle'))
        hard_failure |= set(U.PAIR) != set(services) or any(row.get('status') != 'running' or row.get('restart_count') != 0 for row in services.values())
        hard_failure |= report.get('verification_failure', {}).get('kind') == 'identity_mismatch'
        if hard_failure or attempt == attempts or deadline - time.monotonic() < HEALTH_RETRY_MIN_SECONDS:
            break
        # No lease is renewed from a failed sample. The independent watchdog
        # remains active throughout this one short recheck.
        time.sleep(2 if policy == HEALTH_RECHECK_POLICY else 0.25)
    raise HealthReviewError(samples)


def runtime_context(api, root, upgrade_path, path=None, state=None, monitoring=False):
    return P.context(api, root, upgrade_path,
        verifier=lambda api, upgrade_path, upgrade, manifest: health_verification(
            api, upgrade_path, upgrade, manifest, path, state, monitoring))


def observer_source(root):
    source = (root / 'deploy/scripts/v10/normalization_pilot.exs').read_text()
    marker = 'true = Explorer.Migrator.StartupControl.disabled?()\n\nfor app'
    P.require(source.count(marker) == 1, 'Normalization observation marker changed')
    source = source.split(marker)[0].replace('BlockscoutNormalizationCheck', 'BlockscoutV10VacuumAnchor')
    source = source.replace('BlockscoutNormalizationSession', 'BlockscoutV10VacuumResources')
    activity_marker = '  # All waits for the host occur outside transactions.'
    P.require(source.count(activity_marker) == 1, 'Vacuum activity SQL marker changed')
    source = source.replace(activity_marker, '  def activity_sql, do: @transaction_activity_sql\n\n' + activity_marker)
    # This observer runs in the existing backend. It invokes only the read-only
    # inspect path, with automatic migrators still held; it starts no application.
    P.require(source.count('nil = Process.whereis(Explorer.Supervisor)') == 1, 'Anchor application guard changed')
    source = source.replace('nil = Process.whereis(Explorer.Supervisor)', 'true = is_pid(Process.whereis(Explorer.Supervisor))')
    P.require(source.count("'phase',phase,'heap_blocks_total'") == 1, 'Vacuum progress projection changed')
    source = source.replace("'phase',phase,'heap_blocks_total'", "'pid',pid,'phase',phase,'heap_blocks_total'")
    vacuum = (root / 'deploy/scripts/v10/vacuum.exs').read_text()
    diagnostics = (root / 'deploy/scripts/v10/normalization_diagnostics.exs').read_text()
    diagnostics = diagnostics.replace('BlockscoutNormalizationDiagnostics', 'BlockscoutV10VacuumDiagnostics')
    P.require(vacuum.count('# VACUUM_CLIENT_ENTRYPOINT') == 1, 'Vacuum entrypoint marker changed')
    return (source + diagnostics + vacuum.split('# VACUUM_CLIENT_ENTRYPOINT')[0]).encode()


def observe(api, source, payload, target=None, verify_anchor=False, vacuum_query_started_at=None, runtime=None):
    encoded = base64.b64encode(R.encoded({**payload, 'target': target, 'verify_anchor': verify_anchor,
                                        'vacuum_query_started_at': vacuum_query_started_at})).decode()
    code = source.decode() + '\nresult = BlockscoutV10Vacuum.observe(Jason.decode!(Base.decode64!("' + encoded + '")))\n'
    code += 'IO.puts("BLOCKSCOUT_DEPLOY_CHECK=" <> Jason.encode!(result))\n'
    # A 1,000-row anchor carries two hash lists. On 4 KiB-page Linux hosts each
    # argument is limited to 128 KiB, independently of ARG_MAX. Keep RPC short;
    # pass code/data through stdin into a private, automatically removed file.
    result = C.marked_json(api, api.run(['docker', 'exec', '-i', 'backend', 'sh', '-c', RPC_SCRIPT],
                                      input_text=code, timeout=15))
    queries = J.query_diagnostics(result.get('query_diagnostics', []), runtime or {})
    result['query_diagnostics'] = [row for row in queries if not target or
        row.get('pid') != target['pid'] or row.get('backend_started_at') != target['backend_start']]
    if verify_anchor:
        P.require(J.observed(payload['anchor'], result['anchor'], row_limit=payload.get('single_block_rows', 1000)) == 'normalized', 'Normalization anchor no longer verifies')
        result.pop('anchor')  # Bounded raw rows remain private in the source journal.
    return result


def profile_limits(inputs):
    profile = inputs.get('profile')
    P.require(profile is None or profile in PROFILES, 'Unknown vacuum window profile')
    limits = INDEX_CLEANUP_LIMITS if profile else LIMITS
    P.require(inputs['limits'] == limits, 'Frozen vacuum limits changed')
    P.require(inputs['payload'].get('duration_seconds', 3600) == limits['duration_seconds'],
              'Frozen vacuum duration changed')
    cost = inputs['payload'].get('vacuum_cost_limit', 200)
    P.require(type(cost) is int and cost == (400 if profile == FAST_PROFILE else 200),
              'Frozen vacuum cost limit changed')
    return limits


def load(path):
    P.require(path.is_absolute() and path.resolve() == path and path.parent.parent == BASE and path.name == 'state.json'
              and re.fullmatch(r'vacuum-[0-9TZ-]+-[a-f0-9]{8}', path.parent.name), 'Unexpected vacuum journal path')
    J.owned_directory(path.parent)
    state = json.loads(U.private(path))
    frozen = U.private(path.parent / 'inputs.json')
    inputs = json.loads(frozen)
    profile_limits(inputs)
    P.require(state['format'] == FORMAT and inputs == state['inputs'] and state['inputs_sha256'] == R.digest(frozen)
              and inputs['sql'] == SQL
              and state['phase'] in ('prepared', 'launch_requested', 'vacuum_requested', 'finished'), 'Vacuum state/inputs changed')
    for name, digest in inputs['files'].items():
        P.require(name in ('migrate.exs', 'observer.exs') and R.digest(U.private(path.parent / name)) == digest,
                  'Frozen vacuum source changed')
    P.require(set(inputs['files']) == {'migrate.exs', 'observer.exs'}
              and re.fullmatch(r'blockscout-v10-schema-[a-f0-9]{16}', inputs['client_name'])
              and inputs['payload']['application_name'] == 'blockscout-v10-vacuum-' + inputs['payload']['token'],
              'Vacuum client identity changed')
    scope = inputs.get('transaction_guard_scope')
    P.require(scope is None or scope == TRANSACTION_GUARD_SCOPE
              and inputs['payload'].get('transaction_guard_scope') == scope
              and inputs['payload'].get('oldest_transaction_seconds') == inputs['limits']['oldest_transaction_seconds'],
              'Frozen vacuum transaction guard changed')
    P.require(inputs.get('health_recheck_policy') in (None, LEGACY_HEALTH_RECHECK_POLICY, HEALTH_RECHECK_POLICY),
              'Frozen vacuum health policy changed')
    if state.get('vacuum_query_started_at'):
        proof = U.private(path.parent / 'vacuum-start.json')
        P.require(R.digest(proof) == state['vacuum_start_sha256']
                  and json.loads(proof) == {**state['target'], 'query_start': state['vacuum_query_started_at']},
                  'Vacuum query-start proof changed')
    return state


def marker(path, label):
    log = path.parent / 'client.log.private'
    if not log.exists():
        return None
    values = [line[len(label):] for line in U.private(log).decode(errors='replace').splitlines() if line.startswith(label)]
    P.require(len(values) <= 1, 'Repeated vacuum client marker; retain the private log')
    return json.loads(values[0]) if values else None


def owned_client(api, path, inputs):
    ids = api.run(['docker', 'ps', '-aq', '--filter', 'name=^/' + inputs['client_name'] + '$']).split()
    if not ids:
        return False
    P.require(len(ids) == 1, 'Ambiguous vacuum client')
    row = json.loads(api.run(['docker', 'inspect', ids[0]]))[0]
    labels = row.get('Config', {}).get('Labels') or {}
    P.require(row['Image'] == inputs['candidate_image'] and labels.get(U.STATE_LABEL) == str(path)
              and labels.get(U.ROLE_LABEL) == 'schema-client', 'Vacuum client ownership changed')
    return row['State']['Running']


def no_normalizer(api, manifest):
    if not J.BASE.exists():
        return
    for directory in J.BASE.iterdir():
        other = J.load(directory / 'state.json', manifest, check_resources=False)
        P.require(not J.active(api, directory / 'state.json', other) and other['pending'] is None
                  and other['phase'] not in ('running', 'write_requested'), 'Reconcile active/pending normalization before vacuum')


def check_resources(data, inputs, baseline=None, free=None):
    limits = inputs['limits']
    free = P.free_bytes() if free is None else free
    P.require(free >= limits['free_bytes'] and inputs['space_before'] - free <= limits['space_bytes'],
              'Vacuum filesystem budget reached')
    reviewed = data
    if inputs.get('transaction_guard_scope') == TRANSACTION_GUARD_SCOPE:
        boundary = inputs.get('vacuum_query_started_at')
        P.require(data.get('vacuum_query_started_at') == boundary, 'Vacuum transaction review boundary changed')
        if boundary:
            reviewed = {**data, 'transactions': {**data['transactions'],
                'oldest_transaction_seconds': data['transactions']['oldest_pre_vacuum_transaction_seconds']}}
    try:
        J.resource_guard(reviewed, limits={**J.OBSERVATION_LIMITS, 'oldest_transaction_seconds': limits['oldest_transaction_seconds']})
    except U.UpgradeError as error:
        raise ResourceReviewError(str(error), data) from None
    P.require(data['transactions']['prepared_transactions'] == data['transactions']['slots_with_xmin'] == 0,
              'Prepared transaction or replication snapshot needs review before vacuum')
    P.require(all(v['pid'] == (inputs.get('target') or {}).get('pid') for v in data['vacuum']),
              'Another vacuum is operating on internal_transactions')
    if baseline:
        P.require(data['table']['stats_reset'] == baseline['table']['stats_reset']
                  and data['wal']['stats_reset'] == baseline['wal']['stats_reset'], 'Statistics reset during vacuum observation')
        P.require(data['table']['total_bytes'] - baseline['table']['total_bytes'] <= limits['space_bytes'], 'Vacuum table-growth budget reached')
        for key in ('directory_bytes', 'retained_slot_bytes'):
            P.require(data['wal'][key] - baseline['wal'][key] <= limits['space_bytes'], 'Vacuum retained-WAL budget reached')
        wal = int(data['wal']['bytes_since_stats_reset']) - int(baseline['wal']['bytes_since_stats_reset'])
        P.require(0 <= wal <= limits['cluster_wal_bytes'], 'Vacuum cluster-WAL observation budget reached')
        reads = sum(data['io'][key] - baseline['io'][key] for key in ('heap_blocks_read', 'index_blocks_read'))
        P.require(0 <= reads * 8192 <= limits['relation_read_bytes'], 'Vacuum relation-read observation budget reached')
    return {**data, 'data_free_bytes': free}


def disk_guard(path, inputs):
    P.require(J.journal_size(path.parent) <= inputs['limits']['journal_bytes'], 'Vacuum journal cap reached')
    P.require(J.journal_size(BASE) <= inputs['limits']['aggregate_journal_bytes'], 'Vacuum journal aggregate cap reached; review retention')
    for name in ('client.log.private', 'controller.log.private'):
        log = path.parent / name
        P.require(not log.exists() or len(U.private(log)) <= inputs['limits']['log_bytes'], 'Vacuum private log cap reached')


def record(path, value):
    target = path.parent / 'observations.jsonl'
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'ab') as stream:
        stream.write(json.dumps(value, sort_keys=True, separators=(',', ':')).encode() + b'\n')
        stream.flush()
        os.fsync(stream.fileno())
    R.update_state(path.parent / 'resources-latest.json', value)


def public(path, state):
    return {'status': 'vacuum_window_' + state['phase'], 'state': str(path), 'stop_reason': state.get('stop_reason'),
            'sql': SQL, 'limits': state['inputs']['limits'], 'profile': state['inputs'].get('profile', 'one-hour'),
            'target': state.get('target'),
            'vacuum_sql_completed': state.get('vacuum_sql_completed', False),
            'completion_verified': state.get('completion_verified', False),
            'observations': state.get('observations', 0), 'latest': state.get('latest'),
            'transaction_guard_scope': state['inputs'].get('transaction_guard_scope', 'all_other_clients'),
            'vacuum_query_started_at': state.get('vacuum_query_started_at'),
            'health_recheck_policy': state['inputs'].get('health_recheck_policy', 'single_check'),
            'latest_health': state.get('latest_health'), 'health_rechecks': state.get('health_rechecks', 0),
            'controller': controller_status(path, state),
            'normalization_cursor': state['inputs']['normalization_cursor'],
            'maintenance_selected_rows': state['inputs']['maintenance_selected_rows'],
            'normalization_gate_reopened': False, 'table_or_global_settings_changed': False,
            'global_normalization_complete': False, 'sustained_full_table_run_enabled': False,
            'note': 'One cost-limited ordinary-vacuum window. Interrupted work can leave useful partial cleanup but does not qualify a completed cycle. Completion still requires review of index cleanup, dead-row reduction and subsequent reuse.'}


def controller_status(path, state):
    record_path = path.parent / 'controller.json'
    if not record_path.exists():
        return None
    record = json.loads(U.private(record_path))
    P.require(record['format'] == CONTROLLER_FORMAT and record['state'] == str(path)
              and record['inputs_sha256'] == state['inputs_sha256']
              and (record.get('pid') is None or type(record['pid']) is int and record['pid'] > 0),
              'Detached vacuum controller identity changed')
    return {'mode': 'detached', 'requested_at_utc': record['requested_at_utc'],
            'pid': record.get('pid'), 'log': str(path.parent / 'controller.log.private')}


def reserve_background(path, state):
    P.require(state['phase'] == 'prepared', 'Detach only a newly prepared vacuum window')
    disk_guard(path, state['inputs'])
    R.write_new(path.parent / 'controller.json', R.encoded({
        'format': CONTROLLER_FORMAT, 'state': str(path), 'inputs_sha256': state['inputs_sha256'],
        'requested_at_utc': datetime.now(timezone.utc).isoformat()}))


def launch_background(api, root, path):
    # Called only after the parent's operation/build locks have been released.
    # The child acquires those same locks and runs the original one-shot state
    # machine. No retry or service restart is installed.
    state = load(path)
    P.require(state['phase'] == 'prepared' and controller_status(path, state)['pid'] is None,
              'Detached vacuum launch was already requested; inspect its saved state/log')
    P.require(B.clean_source(api, root) == (state['inputs']['tool_source_commit'], '10.2.6'),
              'Use the vacuum creation commit')
    disk_guard(path, state['inputs'])
    with B.private_log(path.parent / 'controller.log.private') as log:
        process = subprocess.Popen([sys.executable, '-u', str(root / 'deploy/scripts/v10_vacuum.py'),
                                    '--resume', '--state', str(path)],
            cwd=str(root), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True)
    record = json.loads(U.private(path.parent / 'controller.json'))
    record['pid'] = process.pid
    R.update_state(path.parent / 'controller.json', record)
    return {**public(path, state), 'status': 'vacuum_controller_dispatched',
            'database_work_finished': False}


def prepare(api, root, after_path, metrics_path, empty_path, profile=None):
    P.require(profile is None or profile in PROFILES, 'Unknown vacuum window profile')
    limits = INDEX_CLEANUP_LIMITS if profile else LIMITS
    manifest, _, _ = api.configuration(root, 'dev')
    previous = J.load(after_path, manifest, check_resources=False)
    measured_capacity_expiry = (J.Q.capacity_profile(previous['inputs'])
        and previous['stop_reason'] == 'new_write_window_expired'
        and previous['rows'] >= 200000 and previous.get('wal_measurements_complete') is True
        and type(previous.get('updated_rows')) is int and 200000 <= previous['updated_rows'] <= previous['rows'])
    P.require(previous['phase'] == 'finished' and previous['pending'] is None and previous['rows'] > 0
              and (previous['stop_reason'] in ('vacuum_review_required', 'next_complete_batch_requires_vacuum_review')
                   or (J.Q.capacity_profile(previous['inputs'])
                       and previous['stop_reason'] in ('capacity_review_required', 'next_complete_batch_requires_capacity_review'))
                   or measured_capacity_expiry),
              'Vacuum qualification starts from a finished normalization review checkpoint')
    no_normalizer(api, manifest)
    ancestor = previous['inputs']
    _, upgrade = runtime_context(api, root, Path(ancestor['upgrade_state']))
    source = B.clean_source(api, root)
    P.require(source[1] == '10.2.6', 'Tool source release changed')
    api.run(['git', '-C', str(root), 'merge-base', '--is-ancestor', upgrade['candidate_source_commit'], source[0]])
    changes = api.run(['git', '-C', str(root), 'diff', '--name-only', upgrade['candidate_source_commit'], source[0]]).splitlines()
    P.require(all(name.startswith(('deploy/', 'docs/upgrades/')) for name in changes), 'Application/config changed after the pinned image')
    before = api.inspect_project(manifest)
    api.ensure_stable(ancestor['runtime_before'], before, 'Runtime changed since normalization')
    for module, hold_path in ((H, metrics_path), (E, empty_path)):
        held = module.load(hold_path)
        P.require(held['phase'] == 'held' and held['inputs']['upgrade_state'] == ancestor['upgrade_state'], 'Keep both reviewed runtime holds active')
        api.ensure_stable(held['inputs']['runtime_before'], before, 'Runtime changed since worker hold')
    token = uuid.uuid4().hex[:16]
    script = (root / 'deploy/scripts/v10/vacuum.exs').read_bytes()
    observer = observer_source(root)
    inputs = {'limits': dict(limits), 'sql': SQL, 'tool_source_commit': source[0], 'chain_id': manifest['chain_id'],
              'transaction_guard_scope': TRANSACTION_GUARD_SCOPE,
              'health_recheck_policy': HEALTH_RECHECK_POLICY,
              'candidate_image': upgrade['candidate_image'], 'network': upgrade['network'],
              'backend_environment': upgrade['backend_environment'], 'runtime_before': before,
              'upgrade_state': ancestor['upgrade_state'], 'normalization_state': str(after_path),
              'runtime_hold_states': [str(metrics_path), str(empty_path)],
              'normalization_state_sha256': R.digest(U.private(after_path)), 'normalization_cursor': previous['cursor'],
              'maintenance_selected_rows': ancestor['maintenance_rows_before'] + previous['rows'],
              'maintenance_origin': ancestor['maintenance_origin'], 'space_before': P.free_bytes(),
              'prepared_at_utc': datetime.now(timezone.utc).isoformat(), 'client_name': 'blockscout-v10-schema-' + token,
              'files': {'migrate.exs': R.digest(script), 'observer.exs': R.digest(observer)},
              'payload': {**ancestor['payload'], 'anchor': J.target(previous['last']),
                          'statuses': previous['last']['baseline']['statuses'],
                          'transaction_guard_scope': TRANSACTION_GUARD_SCOPE,
                          'oldest_transaction_seconds': limits['oldest_transaction_seconds'],
                          'duration_seconds': limits['duration_seconds'],
                          'vacuum_cost_limit': 400 if profile == FAST_PROFILE else 200,
                          'application_name': 'blockscout-v10-vacuum-' + token, 'token': token}}
    # Include absent completion records: appearance of a held worker's record
    # must be detected just as a change to an existing record would be.
    inputs['payload']['status_names'] = list(K.HISTORY_WORKERS)
    if profile:
        inputs['profile'] = profile
    data = check_resources(observe(api, observer, inputs['payload'], verify_anchor=True, runtime=before), inputs)
    B.validate_state_directory(api, BASE, root)
    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    P.require(J.journal_size(BASE) <= LIMITS['aggregate_journal_bytes'] - LIMITS['journal_bytes'],
              'Reserve a full vacuum journal before preparing another window; review retention')
    entries = list(BASE.iterdir())
    P.require(len(entries) < 32, 'Review vacuum journal retention before another window')
    for entry in entries:
        earlier = load(entry / 'state.json')
        P.require(earlier['phase'] in ('prepared', 'finished'), 'Reconcile the earlier vacuum window before preparing another')
        P.require(not (entry / 'controller.json').exists() or earlier['phase'] == 'finished',
                  'Reconcile the earlier detached vacuum controller before preparing another')
    directory = BASE / ('vacuum-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8])
    directory.mkdir(mode=0o700)
    for name, content in (('inputs.json', R.encoded(inputs)), ('migrate.exs', script), ('observer.exs', observer), ('resources-before.json', R.encoded(data))):
        R.write_new(directory / name, content)
    state = {'format': FORMAT, 'phase': 'prepared', 'inputs': inputs, 'inputs_sha256': R.digest(R.encoded(inputs)),
             'resources_before_sha256': R.digest(R.encoded(data)), 'observations': 0}
    path = directory / 'state.json'
    R.write_new(path, R.encoded(state))
    api.ensure_stable(before, api.inspect_project(manifest), 'Runtime changed during vacuum preparation')
    return {**public(path, state), 'database_writes': False, 'resources': data}


def publish(api, inputs, name, value):
    P.require(name in ('lease', 'start'), 'Unexpected vacuum protocol file')
    api.run(['docker', 'exec', '-i', inputs['client_name'], 'sh', '-c',
             'set -eu; umask 077; cat > /tmp/channel/' + name + '.tmp; mv /tmp/channel/' + name + '.tmp /tmp/channel/' + name + '.json'],
            input_text=R.encoded(value).decode(), timeout=5)


def validate_target(target, payload):
    cost = payload.get('vacuum_cost_limit', 200)
    P.require(type(cost) is int and cost in (200, 400), 'Vacuum cost limit unavailable')
    P.require(isinstance(target, dict) and type(target['pid']) is int and target['pid'] > 0
              and isinstance(target['backend_start'], str) and target['application_name'] == payload['application_name']
              and target['query_md5'] == SQL_MD5 and target['database_oid'] == payload['database_identity']['oid']
              and type(target['role_oid']) is int
              and target['settings'] == {'maintenance_work_mem': '512MB', 'vacuum_cost_limit': str(cost),
                  'vacuum_cost_delay': '2ms', 'lock_timeout': '500ms',
                  'statement_timeout': {3600: '1h', 14400: '4h'}[payload.get('duration_seconds', 3600)],
                  'client_connection_check_interval': '1s'},
              'Vacuum socket identity/settings unavailable')


def validate_activity(data, target):
    P.require(target['role_oid'] == data['observer_role_oid'], 'Vacuum connection role differs from the backend')
    for session in data['target_sessions']:
        P.require(all(session[k] == target[k] for k in ('pid', 'backend_start', 'application_name'))
                  and session['same_role'], 'Vacuum backend identity changed')


def capture_start(path, state, data):
    validate_activity(data, state['target'])
    boundary = data.get('vacuum_query_started_at')
    if not boundary:
        P.require(not state.get('vacuum_query_started_at'), 'Previously proved vacuum boundary disappeared')
        return
    if state.get('vacuum_query_started_at'):
        P.require(boundary == state['vacuum_query_started_at'], 'Vacuum query start changed')
        return
    P.require(any(row['state'] in ('active', 'idle') and row['query_md5'] == SQL_MD5 and row['query_start'] == boundary
                  for row in data['target_sessions']), 'Vacuum start requires its observed, exact statement')
    proof = R.encoded({**state['target'], 'query_start': boundary})
    proof_path = path.parent / 'vacuum-start.json'
    if proof_path.exists():
        P.require(U.private(proof_path) == proof, 'Earlier vacuum query-start proof belongs to another statement')
    else:
        R.write_new(proof_path, proof)
    state.update(vacuum_query_started_at=boundary, vacuum_start_sha256=R.digest(proof))
    R.update_state(path, state)


def record_observation(path, state, data):
    data = {**data, 'data_free_bytes': P.free_bytes()}
    record(path, data)
    state['latest'] = {key: data[key] for key in ('observed_at', 'table', 'wal', 'data_free_bytes', 'transactions')}
    state['latest'].update(vacuum=data.get('vacuum', []), query_diagnostics=data.get('query_diagnostics', []))
    R.update_state(path, state)
    return data


def validate_result(result, state):
    P.require(result['token'] == state['inputs']['payload']['token'] and result['identity'] == state.get('target')
              and result['mode'] in ('completed', 'interrupted'), 'Vacuum result identity changed')
    if result['mode'] == 'completed':
        P.require(result.get('command') == 'vacuum' and result.get('index_cleanup_requested') is True
                  and type(result.get('elapsed_ms')) is int
                  and 0 <= result['elapsed_ms'] <= state['inputs']['limits']['duration_seconds'] * 1000,
                  'Vacuum completion receipt is incomplete or exceeds the window')


def renew(api, path, state):
    now = datetime.now(timezone.utc)
    if state.get('last_health_at_utc'):
        gap = (now - datetime.fromisoformat(state['last_health_at_utc'])).total_seconds()
        P.require(0 <= gap <= state['inputs']['limits']['health_seconds'], 'Vacuum health observation gap exceeded 30 seconds')
    state['last_health_at_utc'] = now.isoformat()
    state['lease_sequence'] = state.get('lease_sequence', 0) + 1
    R.update_state(path, state)
    publish(api, state['inputs'], 'lease', {'token': state['inputs']['payload']['token'], 'sequence': state['lease_sequence']})


def finish(api, path, state, reason, result=None):
    inputs = state['inputs']
    cleanup(api, path, inputs)
    if result is not None:
        validate_result(result, state)
        state['vacuum_sql_completed'] = result['mode'] == 'completed'
    state.update(phase='finished', stop_reason=reason, result=result, finished_at_utc=datetime.now(timezone.utc).isoformat())
    R.update_state(path, state)
    return public(path, state)


def cleanup(api, path, inputs):
    try:
        U.remove_client(api, path, {'client_name': inputs['client_name'], 'candidate_image': inputs['candidate_image']})
    except D.ConfigError:
        P.require(not api.run(['docker', 'ps', '-aq', '--filter', 'name=^/' + inputs['client_name'] + '$']).split(),
                  'Unable to confirm vacuum client cleanup; its watchdog retains the fixed deadline')


def execute(api, root, path, resume=False):
    state = load(path)
    inputs = state['inputs']
    if state['phase'] == 'finished':
        return public(path, state)
    P.require(resume or state['phase'] == 'prepared', 'Resume the saved vacuum window; never launch it twice')
    P.require(B.clean_source(api, root) == (inputs['tool_source_commit'], '10.2.6'), 'Use the vacuum creation commit')
    manifest, _ = runtime_context(api, root, Path(inputs['upgrade_state']), path, state)
    no_normalizer(api, manifest)
    api.ensure_stable(inputs['runtime_before'], api.inspect_project(manifest), 'Runtime changed before vacuum')
    P.require(R.digest(U.private(Path(inputs['normalization_state']))) == inputs['normalization_state_sha256'], 'Normalization checkpoint changed')
    baseline = json.loads(U.private(path.parent / 'resources-before.json'))
    P.require(R.digest(R.encoded(baseline)) == state['resources_before_sha256'], 'Vacuum resource baseline changed')
    observer = U.private(path.parent / 'observer.exs')
    process = log = None
    try:
        if state['phase'] == 'prepared':
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(inputs['prepared_at_utc'])).total_seconds()
            P.require(0 <= age <= inputs['limits']['preparation_seconds'], 'Vacuum preparation expired; retain it and prepare a new window')
            data = check_resources(observe(api, observer, inputs['payload'], verify_anchor=True, runtime=inputs['runtime_before']), inputs, baseline)
            record(path, data)
            state.update(phase='launch_requested', launch_requested_at_utc=datetime.now(timezone.utc).isoformat())
            R.update_state(path, state)  # A lost launch response never authorizes relaunch.
            fd = os.open(path.parent / 'client.log.private', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            log = os.fdopen(fd, 'ab', buffering=0)
            process = subprocess.Popen(U.client_command(inputs['candidate_image'], inputs['network'], inputs['client_name'], str(path)),
                                       stdin=subprocess.PIPE, stdout=log, stderr=log)
            process.stdin.write(U.archive(inputs['backend_environment'], inputs['payload'], U.private(path.parent / 'migrate.exs')))
            process.stdin.close()
            started = time.monotonic()
            while not marker(path, 'BLOCKSCOUT_VACUUM_READY='):
                P.require(process.poll() is None and time.monotonic() - started < 60, 'Vacuum socket startup failed; retain its private log')
                disk_guard(path, inputs)
                time.sleep(0.2)
            target = marker(path, 'BLOCKSCOUT_VACUUM_READY=')
            validate_target(target, inputs['payload'])
            state.update(target=target, phase='vacuum_requested', requested_at_utc=datetime.now(timezone.utc).isoformat(), lease_sequence=0)
            R.update_state(path, state)
            runtime_context(api, root, Path(inputs['upgrade_state']), path, state)
            api.ensure_stable(inputs['runtime_before'], api.inspect_project(manifest), 'Runtime changed before vacuum dispatch')
            data = check_resources(observe(api, observer, inputs['payload'], target, runtime=inputs['runtime_before']), {**inputs, 'target': target}, baseline)
            validate_activity(data, target)
            record(path, data)
            renew(api, path, state)
            publish(api, inputs, 'start', {'token': inputs['payload']['token']})
        else:
            P.require(state['phase'] == 'vacuum_requested' or not owned_client(api, path, inputs),
                      'Socket launch outcome is uncertain; let its 60-second start window expire before reconciliation')
        while True:
            disk_guard(path, inputs)
            result = marker(path, 'BLOCKSCOUT_VACUUM_RESULT=')
            if result:
                validate_result(result, state)
                state['vacuum_sql_completed'] = result['mode'] == 'completed'
                R.update_state(path, state)
                break
            P.require(owned_client(api, path, inputs), 'Vacuum client exited without a completion receipt; no relaunch is permitted')
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(state['requested_at_utc'])).total_seconds()
            P.require(0 <= elapsed <= inputs['limits']['duration_seconds'], 'Vacuum duration budget reached')
            api.ensure_stable(inputs['runtime_before'], api.inspect_project(manifest), 'Runtime changed during vacuum')
            data = observe(api, observer, inputs['payload'], state['target'],
                           vacuum_query_started_at=state.get('vacuum_query_started_at'), runtime=inputs['runtime_before'])
            validate_activity(data, state['target'])
            capture_start(path, state, data)
            # Preserve the rejecting observation too, before a guard can raise.
            data = record_observation(path, state, data)
            data = check_resources(data, {**inputs, 'target': state['target'],
                                          'vacuum_query_started_at': state.get('vacuum_query_started_at')}, baseline)
            state['observations'] += 1
            R.update_state(path, state)
            runtime_context(api, root, Path(inputs['upgrade_state']), path, state, monitoring=True)
            api.ensure_stable(inputs['runtime_before'], api.inspect_project(manifest), 'Runtime changed during health verification')
            print(json.dumps({'phase': 'observing_vacuum', 'state': str(path), 'elapsed_seconds': int(elapsed),
                              'progress': data['vacuum'], 'transaction_review': data['transactions'],
                              'query_diagnostics': data.get('query_diagnostics', [])}), flush=True)
            # Renew only after health/runtime/resource observations succeed.
            if not marker(path, 'BLOCKSCOUT_VACUUM_RESULT='):
                renew(api, path, state)
                time.sleep(inputs['limits']['poll_seconds'])
        # Completion is a SQL receipt plus fresh identity/anchor/resource checks.
        cleanup(api, path, inputs)
        runtime_context(api, root, Path(inputs['upgrade_state']), path, state)
        after = observe(api, observer, inputs['payload'], state.get('target'), verify_anchor=True,
                        vacuum_query_started_at=state.get('vacuum_query_started_at'), runtime=inputs['runtime_before'])
        after = record_observation(path, state, after)
        after = check_resources(after, {**inputs, 'target': state.get('target'),
                                       'vacuum_query_started_at': state.get('vacuum_query_started_at')}, baseline)
        api.ensure_stable(inputs['runtime_before'], api.inspect_project(manifest), 'Runtime changed after vacuum')
        P.require(not after['target_sessions'], 'Maintenance socket remains active after cleanup')
        R.update_state(path.parent / 'resources-after.json', after)
        state['completion_verified'] = state['vacuum_sql_completed']
        return finish(api, path, state, 'vacuum_completed_needs_review' if state['completion_verified'] else result['reason'], result)
    except BaseException as error:
        reason = str(error) if isinstance(error, U.UpgradeError) else 'Vacuum monitoring interrupted; inspect its private journal'
        finish(api, path, state, reason, marker(path, 'BLOCKSCOUT_VACUUM_RESULT='))
        raise
    finally:
        if process:
            B.terminate(process)
        if log:
            log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    for name in ('prepare', 'run', 'resume', 'status'):
        modes.add_argument('--' + name, action='store_true')
    parser.add_argument('--after-session', type=Path)
    parser.add_argument('--metrics-hold', type=Path)
    parser.add_argument('--empty-blocks-hold', type=Path)
    parser.add_argument('--state', type=Path)
    parser.add_argument('--profile', choices=PROFILES)
    parser.add_argument('--background', action='store_true')
    args = parser.parse_args()
    try:
        P.require(sys.platform.startswith('linux') and os.geteuid() == 0, 'Run on the Linux dev server as root')
        P.require(not args.background or args.run, 'Use --background only with a new --run window')
        if args.status:
            P.require(args.state and not any((args.after_session, args.metrics_hold, args.empty_blocks_hold, args.profile)), 'Use only the saved vacuum state')
            print(json.dumps(public(args.state, load(args.state)), indent=2))
            return 0
        with M.global_lock(True), B.build_lock(D, D.MANAGED_RELEASES, ROOT):
            if args.prepare or args.run:
                P.require(args.state is None and args.after_session and args.metrics_hold and args.empty_blocks_hold,
                          'Prepare requires the finished normalization checkpoint and both hold journals')
                result = prepare(D, ROOT, args.after_session, args.metrics_hold, args.empty_blocks_hold, args.profile)
                args.state = Path(result['state'])
                if args.run:
                    print(json.dumps(result, indent=2), flush=True)
                    if args.background:
                        reserve_background(args.state, load(args.state))
                    else:
                        result = execute(D, ROOT, args.state)
            else:
                P.require(args.state and not any((args.after_session, args.metrics_hold, args.empty_blocks_hold, args.profile)), 'Use only the saved vacuum state')
                result = execute(D, ROOT, args.state, resume=True)
        if args.background:
            result = launch_background(D, ROOT, args.state)
        print(json.dumps(result, indent=2))
        return 0 if args.prepare or args.background or result.get('completion_verified') else 1
    except Exception as error:
        print(json.dumps({'status': 'stopped', 'state': str(args.state) if args.state else None, 'raw_values_suppressed': True,
                          **error_report(error),
                          'recovery': 'Keep normalization paused. Status reads the saved journal; resume only monitors/reconciles the same requested window and never relaunches VACUUM. Interrupted vacuum does not qualify completion.'}))
        return 1


def error_report(error):
    # ConfigError messages already suppress command output and sensitive values.
    # Other exception messages can contain SQL, row data or environment values.
    report = {'reason': str(error) if isinstance(error, D.ConfigError) else 'Vacuum operation failed; raw exception values suppressed',
            'error_type': type(error).__name__,
            'source_locations': [{'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name}
                                 for frame in traceback.extract_tb(error.__traceback__)
                                 if Path(frame.filename).name in ('v10_vacuum.py', 'v10_normalization_session.py',
                                     'v10_metrics_hold.py', 'v10_empty_blocks_hold.py', 'dev_config.py',
                                     'v10_normalization_pilot.py', 'v10_upgrade.py', 'candidate_build.py', 'deploy_check.py')]}
    context = error.__cause__ or error.__context__
    if context:
        report['context_type'] = type(context).__name__
    if isinstance(context, OSError):
        report['system_errno'] = context.errno
    if isinstance(error, ResourceReviewError):
        report['transaction_review'] = error.review
    if isinstance(error, HealthReviewError):
        report['health_observations'] = error.samples
    return report


if __name__ == '__main__':
    raise SystemExit(main())
