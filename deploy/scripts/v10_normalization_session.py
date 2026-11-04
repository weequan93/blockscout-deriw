#!/usr/bin/env python3
"""Prepare or exercise the reusable, vacuum-compatible normalization client."""
from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid

import candidate_build as B
import containerd_maintenance as M
import deploy_check as C
import dev_config as D
import release as R
import v10_normalization_pilot as P
import v10_upgrade as U

ROOT = Path(__file__).resolve().parents[2]
BASE = Path('/data/blockscout-dev-private/v10-normalization-sessions')
FORMAT = 'blockscout-dev-v10-normalization-session-v1'
PROFILE_FORMAT = 'blockscout-dev-v10-normalization-session-v2'
LIMITS = dict(max_batches=25, max_rows=25000, wal_bytes=128 * 1024**2,
              next_wal_reserve_bytes=16 * 1024**2, space_bytes=512 * 1024**2,
              free_bytes=200 * 1024**3, start_seconds=600, health_seconds=30,
              journal_bytes=64 * 1024**2, log_bytes=16 * 1024**2,
              all_session_journal_bytes=256 * 1024**2,
              dead_rows=1000000, additional_dead_rows=100000,
              oldest_transaction_seconds=300, retained_raw_snapshots=2)
# The legacy dictionary is deliberately unchanged: v1 journals freeze it.
# This is one observation epoch, not permission for continuous normalization.
OBSERVATION_LIMITS = {**LIMITS, 'max_batches': 250, 'max_rows': 250000,
                      'wal_bytes': 4 * 1024**3, 'space_bytes': 2 * 1024**3,
                      'start_seconds': 1800, 'additional_dead_rows': 300000,
                      'transaction_wal_bytes': 128 * 1024**2,
                      'session_request_limit': 2000, 'maintenance_max_rows': 250000}


def profile_limits(inputs):
    profile = inputs.get('profile')
    P.require(profile in ('pilot-v1', 'observation-v1') if 'profile' in inputs else profile is None,
              'Unknown session limit profile')
    limits = OBSERVATION_LIMITS if profile == 'observation-v1' else LIMITS
    P.require(inputs['limits'] == limits, 'Frozen session limits changed')
    return limits


def sync_json(path, value):
    R.update_state(path, value)


def owned_directory(path):
    P.require(path.is_dir() and not path.is_symlink() and path.resolve() == path
              and path.stat().st_uid == os.geteuid() and stat.S_IMODE(path.stat().st_mode) == 0o700,
              'Private session directory ownership/permissions changed')


def journal_size(directory):
    total = 0
    for parent, dirs, files in os.walk(directory, followlinks=False):
        for name in dirs:
            owned_directory(Path(parent) / name)
        for name in files:
            path = Path(parent) / name
            P.require(not path.is_symlink() and path.is_file() and path.stat().st_uid == os.geteuid()
                      and stat.S_IMODE(path.stat().st_mode) == 0o600, 'Unexpected session file ownership/permissions')
            total += path.stat().st_size
    return total


def monitor(path, inputs, read_only=False):
    limits = profile_limits(inputs)
    if not read_only:
        free = P.free_bytes()
        P.require(free >= limits['free_bytes'] and inputs['space_before'] - free <= limits['space_bytes'],
                  'Session free-space/consumption limit reached')
    P.require(journal_size(path.parent) <= limits['journal_bytes'], 'Session journal size cap reached')
    P.require(journal_size(BASE) <= limits['all_session_journal_bytes'], 'New session journals reached their aggregate cap; review retention')


def checkpoint_digest(state):
    keys = ('batches', 'rows', 'updated_rows', 'charged_wal_bytes',
            'wal_measurements_complete', 'cursor', 'last_sha256', 'checkpoint_hash')
    if state['format'] == PROFILE_FORMAT:
        keys += ('timing_totals',)
    return R.digest(R.encoded({k: state[k] for k in keys}))


class Client:
    """One isolated Repo and mailbox; waits never retain a database transaction."""
    def __init__(self, api, path, inputs, read_only=False):
        P.require(type(read_only) is bool, 'Invalid client observation mode')
        self.api, self.path, self.inputs = api, path, inputs
        self.read_only = read_only
        self.process = self.log = None
        self.name = 'blockscout-v10-schema-' + uuid.uuid4().hex[:16]

    def __enter__(self):
        channel = self.path.parent / 'channel'
        channel.mkdir(mode=0o700, exist_ok=True)
        owned_directory(channel)
        # Callers first refuse a still-active client. No other process owns this
        # mailbox; stale responses are disposable, never commit checkpoints.
        for name in ('request.json', 'response.json', 'response.tmp'):
            candidate = channel / name
            if candidate.exists():
                U.private(candidate)
                candidate.unlink()
        state = json.loads(U.private(self.path))
        state['client_name'] = self.name
        sync_json(self.path, state)
        command = U.client_command(self.inputs['candidate_image'], self.inputs['network'], self.name, str(self.path))
        log_path = self.path.parent / 'client.log.private'
        # A single bounded log per session. Existing bytes are retained on retry.
        descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        self.log = os.fdopen(descriptor, 'ab', buffering=0)
        initial_log_size = os.fstat(self.log.fileno()).st_size
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=self.log, stderr=self.log)
            self.process.stdin.write(U.archive(self.inputs['backend_environment'],
                                              {**self.inputs['payload'], 'session': True,
                                               'session_request_limit': profile_limits(self.inputs).get('session_request_limit', 200)},
                                              U.private(self.path.parent / 'migrate.exs')))
            self.process.stdin.close()
            started = time.monotonic()
            while b'BLOCKSCOUT_SESSION_READY' not in U.private(log_path)[initial_log_size:]:
                P.require(self.process.poll() is None and time.monotonic() - started < 60, 'Session client startup failed')
                monitor(self.path, self.inputs, read_only=self.read_only)
                time.sleep(0.1)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def request(self, command, payload=None):
        P.require(command in ('inspect', 'resources', 'sample', 'migrate'), 'Unsupported session command')
        P.require(not self.read_only or command in ('inspect', 'resources', 'sample'),
                  'Read-only observation clients cannot request normalization writes')
        # Check the applicable budget before publishing any request, especially
        # before a command that can write. Observation never resets that budget.
        monitor(self.path, self.inputs, read_only=self.read_only)
        channel = self.path.parent / 'channel'
        response = channel / 'response.json'
        if response.exists():
            U.private(response)
            response.unlink()
        identifier = uuid.uuid4().hex
        request = {'id': identifier, 'command': command, 'payload': payload or {}}
        sync_json(channel / 'request.json', request)
        # Pass data as stdin, never shell text. Exec accesses the running tmpfs;
        # no host volume or persistent database connection is exposed to it.
        self.transport(['docker', 'exec', '-i', self.name, 'sh', '-c',
                      'set -eu; umask 077; cat > /tmp/channel/request.tmp; rm -f /tmp/channel/response.json; mv /tmp/channel/request.tmp /tmp/channel/request.json'],
                     input_text=R.encoded(request).decode(), timeout=10)
        started = time.monotonic()
        while True:
            P.require(self.process.poll() is None, 'Session client stopped; inspect its private log')
            P.require(time.monotonic() - started < 60, 'Session command exceeded 60 seconds')
            P.require(os.fstat(self.log.fileno()).st_size <= profile_limits(self.inputs)['log_bytes'], 'Session log cap reached')
            monitor(self.path, self.inputs, read_only=self.read_only)
            raw = self.transport(['docker', 'exec', self.name, 'sh', '-c',
                                'if test -f /tmp/channel/response.json; then cat /tmp/channel/response.json; fi'], timeout=10)
            if raw:
                sync_json(response, json.loads(raw))
                break
            time.sleep(0.1)
        value = json.loads(U.private(response))
        P.require(value.get('id') == identifier and value.get('ok') is True,
                  'Session transaction/read failed; retain the pending checkpoint and inspect its private log')
        return value['data']

    def transport(self, command, **kwargs):
        try:
            return self.api.run(command, **kwargs)
        except D.ConfigError:
            # An aborted SQL request can end the --rm container before the host
            # retrieves its reply. It is still an uncertain pending transaction.
            raise U.UpgradeError('Session transport stopped; retain the pending checkpoint and inspect its private log') from None

    def __exit__(self, *args):
        try:
            if self.process is not None:
                if self.process.poll() is None:
                    try:
                        U.remove_client(self.api, self.path, {'client_name': self.name, 'candidate_image': self.inputs['candidate_image']})
                    except D.ConfigError:
                        # A --rm client may vanish between list and inspect.
                        # Suppress only confirmed absence, never ownership drift.
                        P.require(not self.api.run(['docker', 'ps', '-aq', '--filter', 'name=^/' + self.name + '$']).split(),
                                  'Unable to confirm session client cleanup; observe it before resuming')
                B.terminate(self.process)
        finally:
            if self.log:
                self.log.close()


def active(api, path, state):
    name = state.get('client_name')
    if not name:
        return False
    P.require(re.fullmatch(r'blockscout-v10-schema-[a-f0-9]{16}', name), 'Invalid session client name')
    return bool(api.run(['docker', 'ps', '-q', '--filter', 'name=^/' + name + '$']).split())


def target(state):
    return {k: state[k] for k in ('blocks', 'block_rows', 'expected_rows', 'baseline')}


def observed(target, result):
    saved, current = target['baseline'], P.baseline(result)
    P.check_block_rows({'format': P.BATCH_FORMAT, 'block_rows': target['block_rows']}, current)
    P.require(len(current['rows']) == target['expected_rows'] and current['semantic_hashes'] == saved['semantic_hashes']
              and current['statuses'] == saved['statuses'], 'Selected rows/semantics/migration records changed')
    if current['needs_normalization'] == 0:
        return 'normalized'
    P.require(current['full_hashes'] == saved['full_hashes'], 'Original rows changed; no overwrite is permitted')
    return 'original'


def compact(snapshot):
    return {k: snapshot[k] for k in ('full_hashes', 'semantic_hashes', 'statuses')}


def resource_report(data):
    settings = {name: value for name, value, unit in data['settings']}
    options = dict(v.split('=', 1) for v in data['table']['options'])
    effective = lambda name: options.get(name, settings[name])
    estimated = max(0, data['table']['estimated_rows'])
    trigger = float(effective('autovacuum_vacuum_threshold')) + float(effective('autovacuum_vacuum_scale_factor')) * estimated
    return {**data, 'effective_update_delete_vacuum_trigger_estimate': trigger,
            'autovacuum_effectively_enabled': settings.get('autovacuum') == 'on'
                and settings.get('track_counts') == 'on' and options.get('autovacuum_enabled', 'true') not in ('false', 'off'),
            'sustained_ready': False,
            'vacuum_plan': {'proposed_table_vacuum_threshold': 1000000, 'proposed_table_vacuum_scale_factor': 0,
                            'apply_available': False, 'requires_vacuum_io_and_reclamation_measurement': True},
            'note': 'Catalog/statistics and bounded aggregates only. Dead rows are estimates. Ordinary vacuum makes space reusable; it generally does not return it to the filesystem. No vacuum, tuning, session termination or rewrite was performed.'}


def resource_guard(data, baseline=None, limits=None):
    limits = LIMITS if limits is None else limits
    report = resource_report(data)
    P.require(report['autovacuum_effectively_enabled'], 'Autovacuum/track_counts is disabled')
    P.require(0 <= data['table']['dead_rows'] <= limits['dead_rows'], 'Estimated dead-row ceiling reached; review vacuum')
    P.require(data['transactions']['oldest_transaction_seconds'] <= limits['oldest_transaction_seconds'],
              'An existing long transaction needs review (observed ' + str(data['transactions']['oldest_transaction_seconds'])
              + ' seconds, limit ' + str(limits['oldest_transaction_seconds']) + '); no session was terminated')
    if baseline:
        P.require(data['table']['stats_reset'] == baseline['table']['stats_reset']
                  and data['wal']['stats_reset'] == baseline['wal']['stats_reset'], 'Statistics reset during session')
        P.require(data['table']['dead_rows'] - baseline['table']['dead_rows'] <= limits['additional_dead_rows'],
                  'Dead-row growth ceiling reached; review vacuum')
        for key in ('total_bytes',):
            P.require(data['table'][key] - baseline['table'][key] <= limits['space_bytes'], 'Table/index growth ceiling reached')
        for key in ('directory_bytes', 'retained_slot_bytes'):
            P.require(data['wal'][key] - baseline['wal'][key] <= limits['space_bytes'], 'Retained WAL growth ceiling reached')
        report['changes_since_preparation'] = {
            'table': {k: data['table'][k] - baseline['table'][k] for k in
                      ('heap_bytes', 'index_bytes', 'total_bytes', 'dead_rows', 'updates', 'hot_updates', 'vacuum_count', 'autovacuum_count')
                      if k in data['table'] and k in baseline['table']},
            'wal': {k: data['wal'][k] - baseline['wal'][k] for k in ('directory_bytes', 'retained_slot_bytes')},
            'io': {k: data['io'][k] - baseline['io'][k] for k in data.get('io', {})},
        }
    return report


def session_resources(state, data):
    """Carry a fixed maintenance baseline across successor session journals."""
    origin = state['inputs'].get('maintenance_origin')
    maintenance = resource_guard(data, origin['resources'], OBSERVATION_LIMITS) if origin else None
    report = resource_guard(data, state.get('resources_before'), profile_limits(state['inputs']))
    if maintenance:
        report['changes_since_maintenance_origin'] = maintenance['changes_since_preparation']
    return report


def load(path, manifest, check_resources=True):
    P.require(path.is_absolute() and path.resolve() == path and path.parent.parent == BASE and path.name == 'state.json'
              and re.fullmatch(r'session-[0-9TZ-]+-[a-f0-9]{8}', path.parent.name), 'Unexpected private session path')
    owned_directory(path.parent)
    state = json.loads(U.private(path))
    frozen = U.private(path.parent / 'inputs.json')
    inputs = json.loads(frozen)
    limits = profile_limits(inputs)
    P.require(state['format'] == (PROFILE_FORMAT if 'profile' in inputs else FORMAT)
              and inputs == state['inputs'] and R.digest(frozen) == state['inputs_sha256']
              and inputs['chain_id'] == manifest['chain_id']
              and inputs['environment'] == 'dev' and state['phase'] in ('prepared', 'running', 'write_requested', 'finished'),
              'Session identity, inputs or limits changed')
    P.require(state['checkpoint_sha256'] == checkpoint_digest(state), 'Committed aggregate checkpoint changed')
    P.require(R.digest(U.private(path.parent / 'migrate.exs')) == inputs['script_sha256'], 'Frozen session script changed')
    P.require(type(state['batches']) is int and 0 <= state['batches'] <= limits['max_batches']
              and type(state['rows']) is int and 0 <= state['rows'] <= limits['max_rows']
              and type(state['charged_wal_bytes']) is int and 0 <= state['charged_wal_bytes'] <= limits['wal_bytes']
              and type(state['cursor']) is int and 0 <= state['cursor'] <= inputs['start_cursor'], 'Invalid session accounting')
    origin = inputs.get('maintenance_origin')
    P.require(inputs.get('profile') != 'observation-v1' or origin is not None, 'Missing maintenance baseline')
    if origin is not None:
        P.require('profile' in inputs and type(inputs['maintenance_rows_before']) is int
                  and 0 <= inputs['maintenance_rows_before'] <= OBSERVATION_LIMITS['maintenance_max_rows']
                  and inputs['maintenance_rows_before'] + state['rows'] <= OBSERVATION_LIMITS['maintenance_max_rows']
                  and type(origin['space_before']) is int and inputs['space_before'] == origin['space_before']
                  and isinstance(origin['resources'], dict), 'Maintenance baseline/accounting changed')
    if state['format'] == PROFILE_FORMAT:
        timing = state['timing_totals']
        P.require(set(timing) == {'transaction_ms', 'transaction_measurements', 'host_attempt_ms', 'host_attempt_batches'}
                  and all(type(v) is int and v >= 0 for v in timing.values())
                  and timing['host_attempt_batches'] == state['batches']
                  and timing['transaction_measurements'] <= state['batches'], 'Invalid aggregate timing measurements')
    if state['last']:
        last = state['last']
        P.require(P.block_rows(last['block_rows']) == last['expected_rows'] and last['block_rows'][-1]['block'] == state['cursor']
                  and R.digest(R.encoded(last)) == state['last_sha256'], 'Last committed checkpoint changed')
    else:
        P.require(state['batches'] == state['rows'] == state['charged_wal_bytes'] == 0
                  and state['cursor'] == inputs['start_cursor'], 'Missing committed checkpoint')
    if state['pending']:
        pending = state['pending']
        P.require(pending['slot'] == 'raw-' + str(state['batches'] % 2) + '.json', 'Invalid pending snapshot slot')
        raw = U.private(path.parent / pending['slot'])
        P.require(R.digest(raw) == pending['sha256'], 'Pending raw snapshot changed')
        saved = json.loads(raw)
        P.require(P.block_rows(saved['block_rows']) == saved['expected_rows'] <= limits['max_rows'] - state['rows']
                  and saved['block_rows'][0]['block'] < state['cursor']
                  and saved['blocks'] == [r['block'] for r in saved['block_rows']]
                  and compact(saved['baseline']) == saved['payload_baseline'], 'Pending target/hash accounting changed')
        if origin:
            P.require(saved['expected_rows'] + inputs['maintenance_rows_before'] + state['rows']
                      <= OBSERVATION_LIMITS['maintenance_max_rows'], 'Pending rows exceed maintenance allowance')
        P.baseline({'snapshot': saved['baseline'], 'statuses': saved['baseline']['statuses']})
        P.check_block_rows({'format': P.BATCH_FORMAT, 'block_rows': saved['block_rows']}, saved['baseline'])
    P.require(state['phase'] != 'finished' or state['pending'] is None, 'A finished session cannot have pending writes')
    if check_resources:
        monitor(path, inputs)
    else:
        journal_size(path.parent)  # Still check private file permissions.
    return state


def retention_plan(api, manifest):
    """Inventory only this format; never remove or rewrite any journal."""
    if not BASE.exists():
        return {'status': 'session_retention_inventory', 'sessions': [], 'deletion_performed': False}
    owned_directory(BASE)
    directories = sorted(BASE.iterdir(), reverse=True)
    P.require(len(directories) <= 512, 'Review the session directory inventory manually; more than 512 entries')
    entries = []
    for directory in directories:
        state = load(directory / 'state.json', manifest, check_resources=False)
        busy = active(api, directory / 'state.json', state)
        entries.append({'state': str(directory / 'state.json'), 'phase': state['phase'], 'bytes': journal_size(directory),
                        'pending': state['pending'] is not None, 'client_in_progress': busy,
                        'archive_candidate': len(entries) >= 2 and state['phase'] == 'finished' and not busy and state['pending'] is None})
    return {'status': 'session_retention_inventory', 'aggregate_bytes': sum(e['bytes'] for e in entries),
            'aggregate_cap_bytes': LIMITS['all_session_journal_bytes'], 'sessions': entries, 'deletion_performed': False,
            'policy': 'Protect all unfinished/pending sessions and the newest two session directories. Older finished sessions are archival candidates only. Latest inputs/checkpoint are self-contained. Preserve all legacy pilot/batch/run journals separately.'}


def public(path, state):
    return {'status': 'normalization_session_' + state['phase'], 'state': str(path), 'stop_reason': state.get('stop_reason'),
            'preparation_complete': state.get('preparation_complete', 'resources_before' in state),
            'completed_batches': state['batches'], 'selected_rows_verified': state['rows'],
            'updated_rows_observed': state['updated_rows'], 'charged_chunk_wal_bytes': state['charged_wal_bytes'],
            'wal_measurements_complete': state['wal_measurements_complete'], 'next_before_block': state['cursor'],
            'pending_transaction': state['pending'] is not None, 'limits': profile_limits(state['inputs']),
            'profile': state['inputs'].get('profile', 'pilot-v1'),
            'maintenance_selected_rows': state['inputs'].get('maintenance_rows_before', 0) + state['rows'],
            'vacuum_review_row_limit': OBSERVATION_LIMITS['maintenance_max_rows'] if state['inputs'].get('maintenance_origin') else None,
            'timing_totals': state.get('timing_totals'), 'latest_attempt': state.get('latest_attempt'),
            'automatic_background_migrations_held': True, 'global_normalization_complete': False,
            'migration_completion_records_unchanged': True, 'historical_rpc_used': False,
            'vacuum_or_tuning_performed': False, 'sustained_full_table_run_enabled': False,
            'retention': 'Two rotating raw snapshots for this new session; the pending slot is retained until commit reconciliation. Earlier pilot journals are retained. Compact latest checkpoint and aggregate totals survive rotation; older per-row rollback snapshots are not retained.'}


def finish(path, state, reason):
    state.update(phase='finished', stop_reason=reason)
    if state.get('latest_attempt'):
        state['latest_attempt'].update(finished_at=datetime.now(timezone.utc).isoformat(), finishing_rows=state['rows'])
    sync_json(path, state)
    return public(path, state)


def transaction_activity_sql(root):
    """Use exactly the same metadata query as newly frozen session clients."""
    source = (root / 'deploy/scripts/v10/normalization_pilot.exs').read_text()
    matches = re.findall(r'^  @transaction_activity_sql ~S"""\n(.*?)^  """$', source, re.M | re.S)
    P.require(len(matches) == 1 and matches[0].lstrip().startswith('WITH clients AS MATERIALIZED'),
              'Transaction metadata query marker changed')
    return matches[0]


def diagnose(api, root, path, manifest):
    """Read saved evidence and existing backend metadata, with no new client."""
    state = load(path, manifest, check_resources=False)
    inputs, saved = state['inputs'], state.get('resources_before') or {}
    limits = profile_limits(inputs)
    current = P.free_bytes()
    used = max(0, inputs['space_before'] - current)
    report = {'status': 'normalization_session_diagnostics', 'state': str(path), 'read_only': True,
              'raw_values_suppressed': True, 'new_client_started': False, 'database_writes': False,
              'phase': state['phase'], 'preparation_complete': state.get('preparation_complete', 'resources_before' in state),
              'verified_rows': state['rows'], 'pending_transaction': state['pending'] is not None,
              'saved_observed_at': saved.get('observed_at'), 'saved_transactions': saved.get('transactions'),
              'starting_data_free_bytes': inputs['space_before'], 'current_data_free_bytes': current,
              'observed_net_consumption_bytes': used, 'current_free_space_floor_met': current >= limits['free_bytes'],
              'old_session_consumption_limit_exceeded': used > limits['space_bytes'],
              'write_limits_unchanged': True}
    # The filesystem evidence remains returnable even if the running backend
    # is unavailable or has changed. Never start an alternate application.
    try:
        before = api.inspect_project(manifest)
        P.require(before['backend']['Image'] == inputs['candidate_image'], 'Backend image differs from saved session')
        sql = json.dumps(transaction_activity_sql(root))
        code = '''
true = Explorer.Migrator.StartupControl.disabled?()
{:ok, report} = Explorer.Repo.transaction(fn ->
  q = fn sql -> Explorer.Repo.query!(sql, [], log: false, timeout: 2000).rows end
  q.("SET TRANSACTION READ ONLY")
  q.("SET LOCAL statement_timeout='1500ms'")
  q.("SET LOCAL lock_timeout='500ms'")
  [[identity]] = q.("SELECT jsonb_build_object('database',current_database(),'oid',(SELECT oid::bigint FROM pg_database WHERE datname=current_database()),'address',host(inet_server_addr()),'port',inet_server_port(),'postgres_major',current_setting('server_version_num')::integer/10000)")
  [[result]] = q.(SQL)
  %{database_identity: identity, transactions: result}
end, timeout: 5000)
IO.puts("BLOCKSCOUT_DEPLOY_CHECK=" <> Jason.encode!(report))
'''.replace('q.(SQL)', 'q.(' + sql + ')')
        raw = api.run(['docker', 'exec', 'backend', '/app/bin/blockscout', 'rpc', code], timeout=15)
        checked = C.marked_json(api, raw)
        P.require(checked['database_identity'] == inputs['payload']['database_identity'],
                  'Backend database differs from saved session')
        api.ensure_stable(before, api.inspect_project(manifest), 'Runtime changed during transaction diagnosis')
        report['live_backend_transactions'] = checked['transactions']
    except Exception as error:
        report['live_backend_error'] = str(error) if isinstance(error, U.UpgradeError) else 'Backend metadata check unavailable; raw values suppressed'
    return report


def prepare(api, root, after_path, after_session=False, profile=None):
    manifest, _, _ = api.configuration(root, 'dev')
    source = B.clean_source(api, root)
    P.require(source[1] == '10.2.6', 'Source release changed')
    P.require(profile in (None, 'pilot-v1', 'observation-v1'), 'Unknown session limit profile')
    P.require(profile != 'observation-v1' or after_session,
              'The observation profile requires a verified finished reusable session')
    limits = OBSERVATION_LIMITS if profile == 'observation-v1' else LIMITS
    origin, maintenance_rows = None, 0
    if after_session:
        # A finished journal is proof, not an old write-budget lease. A new
        # epoch must pass its own guards and preserve any maintenance baseline.
        previous = load(after_path, manifest, check_resources=False)
        P.require(previous['phase'] == 'finished' and not active(api, after_path, previous),
                  'Continue only a finished session')
        ancestor = previous['inputs']
        upgrade_path, cursor = Path(ancestor['upgrade_state']), previous['cursor']
        anchor = target(previous['last']) if previous['last'] else ancestor['anchor']
        if ancestor.get('maintenance_origin'):
            origin = ancestor['maintenance_origin']
            maintenance_rows = ancestor['maintenance_rows_before'] + previous['rows']
            profile = profile or 'pilot-v1'
        elif profile == 'observation-v1':
            P.require(previous['rows'] > 0 and previous['wal_measurements_complete']
                      and previous.get('preparation_complete', False), 'A verified measured predecessor is required')
            origin = {'state': str(after_path), 'state_sha256': R.digest(U.private(after_path)),
                      'space_before': ancestor['space_before'], 'resources': previous['resources_before']}
        if origin:
            P.require(previous.get('stop_reason') not in ('vacuum_review_required', 'next_complete_batch_requires_vacuum_review'),
                      'Maintenance row allowance exhausted; measure and review vacuum before another epoch')
            P.require(maintenance_rows < OBSERVATION_LIMITS['maintenance_max_rows'],
                      'Maintenance row allowance exhausted; measure and review vacuum before another epoch')
    else:
        previous = P.load(api, after_path, manifest)
        cursor, upgrade_path = P.completed_cursor(previous), Path(previous['upgrade_state'])
        result = P.status(api, root, after_path, manifest)
        P.require(result['status'] == 'verified', 'Starting pilot no longer verifies')
        blocks = previous.get('block_rows') or [{'block': previous['block'], 'rows': previous['expected_rows']}]
        anchor = {'blocks': [r['block'] for r in blocks], 'block_rows': blocks, 'expected_rows': previous['expected_rows'],
                  'baseline': compact(previous['baseline'])}
    _, upgrade = P.context(api, root, upgrade_path)
    previous_image = previous['inputs']['candidate_image'] if after_session else previous['candidate_image']
    P.require(previous_image == upgrade['candidate_image'], 'Starting checkpoint belongs to another image')
    api.run(['git', '-C', str(root), 'merge-base', '--is-ancestor', upgrade['candidate_source_commit'], source[0]])
    changed = api.run(['git', '-C', str(root), 'diff', '--name-only', upgrade['candidate_source_commit'], source[0]]).splitlines()
    P.require(all(n.startswith(('deploy/', 'docs/upgrades/')) for n in changed), 'Application/config changed after the pinned image')
    B.validate_state_directory(api, BASE, root)
    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    P.require(journal_size(BASE) <= limits['all_session_journal_bytes'] - limits['journal_bytes'],
              'Reserve another full session journal before preparing; review retention of new finished sessions')
    directory = BASE / ('session-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8])
    directory.mkdir(mode=0o700)
    path = directory / 'state.json'
    script = (root / 'deploy/scripts/v10/normalization_pilot.exs').read_bytes()
    inputs = {'environment': 'dev', 'chain_id': manifest['chain_id'], 'limits': limits, 'tool_source_commit': source[0],
              'script_sha256': R.digest(script), 'upgrade_state': str(upgrade_path), 'start_cursor': cursor,
              'start_parent': str(after_path), 'start_parent_sha256': R.digest(U.private(after_path)), 'anchor': anchor,
              'candidate_image': upgrade['candidate_image'], 'network': upgrade['network'],
              'backend_environment': upgrade['backend_environment'], 'runtime_before': api.inspect_project(manifest),
              'created_epoch': time.time(), 'space_before': P.free_bytes(),
              'payload': {k: upgrade['migration_payload'][k] for k in ('database_identity', 'heap_identity')}}
    if profile:
        inputs['profile'] = profile
    if origin:
        inputs.update(maintenance_origin=origin, maintenance_rows_before=maintenance_rows,
                      space_before=origin['space_before'])
    inputs['payload']['applied_versions'] = upgrade['applied_result']['applied_versions']
    R.write_new(directory / 'migrate.exs', script)
    R.write_new(directory / 'inputs.json', R.encoded(inputs))
    state = {'format': PROFILE_FORMAT if profile else FORMAT,
             'inputs': inputs, 'inputs_sha256': R.digest(R.encoded(inputs)), 'phase': 'prepared',
             'batches': 0, 'rows': 0, 'updated_rows': 0, 'charged_wal_bytes': 0, 'wal_measurements_complete': True,
             'cursor': cursor, 'pending': None, 'last': None, 'last_sha256': None, 'recent': [],
             'checkpoint_hash': R.digest(R.encoded(inputs)), 'preparation_complete': False}
    if profile:
        state['timing_totals'] = {'transaction_ms': 0, 'transaction_measurements': 0,
                                  'host_attempt_ms': 0, 'host_attempt_batches': 0}
    state['checkpoint_sha256'] = checkpoint_digest(state)
    R.write_new(path, R.encoded(state))
    print(json.dumps({'phase': 'session_inputs_saved', 'state': str(path), 'database_writes': False}), flush=True)
    # A read-only preparation also validates permissions and the exact protocol.
    try:
        with Client(api, path, inputs) as client:
            state['client_name'] = client.name
            P.require(observed(anchor, client.request('inspect', anchor)) == 'normalized', 'Anchor no longer normalized')
            resources = client.request('resources')
            # Retain the evidence even when a resource guard rejects it. A
            # stopped preparation still has an inspectable private state path.
            report = resource_report(resources)
            sync_json(directory / 'resources-before.json', report)
            state['resources_before'] = resources
            sync_json(path, state)
            report = session_resources(state, resources)
            sync_json(directory / 'resources-before.json', report)
            sampled = client.request('sample', {'before_block': cursor})
            P.require(sampled['selected_rows'] > 0 and sampled['selected_bytes'] <= 16 * 1024**2,
                      'Next block is oversized or the non-null range is exhausted; review instead of skipping')
        state = json.loads(U.private(path))
        monitor(path, inputs)
        state['preparation_complete'] = True
        sync_json(path, state)
    except Exception as error:
        error.state_path = path
        raise
    return {**public(path, state), 'database_writes': False, 'resources': report,
            'next_sample': sampled, 'note': 'Prepared only. This finite epoch preserves transaction limits and any inherited maintenance baseline. Vacuum must be measured and reviewed before continuous normalization.'}


def limit_reason(state):
    limits = profile_limits(state['inputs'])
    if state['inputs'].get('maintenance_origin') and state['inputs']['maintenance_rows_before'] + state['rows'] >= OBSERVATION_LIMITS['maintenance_max_rows']:
        return 'vacuum_review_required'
    if state['batches'] >= limits['max_batches']:
        return 'batch_limit_reached'
    if state['rows'] >= limits['max_rows']:
        return 'row_limit_reached'
    if limits['wal_bytes'] - state['charged_wal_bytes'] < limits['next_wal_reserve_bytes']:
        return 'next_batch_wal_reserve_reached'
    if time.time() - state['inputs']['created_epoch'] >= limits['start_seconds']:
        return 'new_write_window_expired'


def execute(api, root, path, manifest):
    state = load(path, manifest, check_resources=False)
    if active(api, path, state):
        return {**public(path, state), 'status': 'client_in_progress'}
    if state['phase'] == 'finished':
        return public(path, state)
    inputs = state['inputs']
    limits = profile_limits(inputs)
    monitor(path, inputs)
    P.require(B.clean_source(api, root) == (inputs['tool_source_commit'], '10.2.6'), 'Resume from the session creation commit')
    P.require(state.get('preparation_complete', 'resources_before' in state) and 'resources_before' in state,
              'Preparation was incomplete; retain it and prepare a new session')
    P.context(api, root, Path(inputs['upgrade_state']))
    health_at = time.monotonic()
    state['latest_attempt'] = {'started_at': datetime.now(timezone.utc).isoformat(), 'starting_rows': state['rows']}
    sync_json(path, state)
    with Client(api, path, inputs) as client:
        state['client_name'] = client.name
        anchor = target(state['last']) if state['last'] else inputs['anchor']
        P.require(observed(anchor, client.request('inspect', anchor)) == 'normalized', 'Latest checkpoint no longer verifies')
        while True:
            batch_started = time.monotonic()
            pending = state['pending']
            if not pending:
                reason = limit_reason(state)
                if reason:
                    P.context(api, root, Path(inputs['upgrade_state']))
                    sync_json(path.parent / 'resources-latest.json', session_resources(state, client.request('resources')))
                    return finish(path, state, reason)
                monitor(path, inputs)
                resources = client.request('resources')
                report = session_resources(state, resources)
                sync_json(path.parent / 'resources-latest.json', report)
                sampled = client.request('sample', {'before_block': state['cursor']})
                if sampled['selected_rows'] == 0:
                    reason = 'oversized_boundary_block' if sampled['excluded_boundary_block'] is not None else 'non_null_range_exhausted_needs_completion_review'
                    return finish(path, state, reason)
                count = P.block_rows(sampled['block_rows'])
                P.require(count == sampled['selected_rows'] and sampled['selected_bytes'] <= 16 * 1024**2,
                          'Sample exceeds transaction budget')
                if inputs.get('maintenance_origin') and count + state['rows'] + inputs['maintenance_rows_before'] > OBSERVATION_LIMITS['maintenance_max_rows']:
                    return finish(path, state, 'next_complete_batch_requires_vacuum_review')
                if count + state['rows'] > limits['max_rows']:
                    return finish(path, state, 'next_complete_batch_exceeds_remaining_rows')
                spec = {'blocks': [r['block'] for r in sampled['block_rows']], 'block_rows': sampled['block_rows'], 'expected_rows': count}
                spec['baseline'] = P.baseline(client.request('inspect', {**spec, 'wal_limit_bytes': limits.get('transaction_wal_bytes', limits['wal_bytes'])}))
                spec['payload_baseline'] = compact(spec['baseline'])
                slot = 'raw-' + str(state['batches'] % 2) + '.json'
                sync_json(path.parent / slot, spec)
                state['pending'] = pending = {'slot': slot, 'sha256': R.digest(U.private(path.parent / slot)), 'result': None}
                state['phase'] = 'running'
                sync_json(path, state)
            saved = json.loads(U.private(path.parent / pending['slot']))
            spec = {**target(saved), 'baseline': saved['payload_baseline'],
                    'wal_limit_bytes': min(limits.get('transaction_wal_bytes', limits['wal_bytes']),
                                           limits['wal_bytes'] - state['charged_wal_bytes'])}
            checked = client.request('inspect', spec)
            if observed(spec, checked) == 'original':
                reason = limit_reason(state)
                if reason:
                    # Reconciled unchanged original rows may be left for the
                    # next session. Keep their raw slot, without moving cursor.
                    state.update(abandoned=pending, pending=None)
                    return finish(path, state, reason)
                if time.monotonic() - health_at >= limits['health_seconds']:
                    P.context(api, root, Path(inputs['upgrade_state']))
                    health_at = time.monotonic()
                session_resources(state, client.request('resources'))
                monitor(path, inputs)
                P.require(B.clean_source(api, root) == (inputs['tool_source_commit'], '10.2.6'), 'Tool source changed before a write')
                api.ensure_stable(inputs['runtime_before'], api.inspect_project(manifest), 'Runtime changed before a write')
                if time.monotonic() - health_at >= limits['health_seconds']:
                    P.context(api, root, Path(inputs['upgrade_state']))
                    health_at = time.monotonic()
                spec['health_valid_until_unix_ms'] = int((time.time() + limits['health_seconds'] - (time.monotonic() - health_at)) * 1000)
                # This fsynced record and raw slot precede the command that can
                # write. Failure after commit keeps this exact target pending.
                state['phase'] = 'write_requested'
                sync_json(path, state)
                result = client.request('migrate', spec)
                P.require(observed(spec, result) == 'normalized', 'Committed result did not preserve semantics')
                metrics = {k: result[k] for k in ('updated_rows', 'elapsed_ms', 'cluster_wal_delta_bytes')}
                P.require(type(metrics['updated_rows']) is int and 0 <= metrics['updated_rows'] <= saved['expected_rows']
                          and type(metrics['elapsed_ms']) is int and 0 <= metrics['elapsed_ms'] <= 10000
                          and type(metrics['cluster_wal_delta_bytes']) is int and 0 <= metrics['cluster_wal_delta_bytes'] <= spec['wal_limit_bytes'],
                          'Unexpected committed transaction measurements')
                pending['result'] = metrics
                sync_json(path, state)
            api.ensure_stable(inputs['runtime_before'], api.inspect_project(manifest), 'Runtime changed during transaction')
            if time.monotonic() - health_at >= limits['health_seconds']:
                P.context(api, root, Path(inputs['upgrade_state']))
                health_at = time.monotonic()
            monitor(path, inputs)
            sync_json(path.parent / 'resources-latest.json', session_resources(state, client.request('resources')))
            metrics = pending['result']
            if metrics is None and saved['baseline']['needs_normalization'] == 0:
                metrics = {'updated_rows': 0, 'elapsed_ms': 0, 'cluster_wal_delta_bytes': 0}
            # A lost commit measurement consumes the entire remaining epoch
            # allowance, even though a transaction has its own lower WAL cap.
            charged = metrics['cluster_wal_delta_bytes'] if metrics else limits['wal_bytes'] - state['charged_wal_bytes']
            entry = {'sequence': state['batches'] + 1, 'rows': saved['expected_rows'], 'oldest_block': spec['blocks'][-1],
                     'snapshot_sha256': pending['sha256'], 'observed_wal_bytes': metrics['cluster_wal_delta_bytes'] if metrics else None,
                     'charged_wal_bytes': charged, 'elapsed_ms': metrics['elapsed_ms'] if metrics else None,
                     'host_attempt_ms': int((time.monotonic() - batch_started) * 1000)}
            state['last'] = {**target(spec), 'receipt': entry}
            state['last_sha256'] = R.digest(R.encoded(state['last']))
            state['checkpoint_hash'] = R.digest(R.encoded([state['checkpoint_hash'], entry]))
            state['batches'] += 1
            state['rows'] += saved['expected_rows']
            state['updated_rows'] = state['updated_rows'] + metrics['updated_rows'] if metrics and state['updated_rows'] is not None else None
            state['wal_measurements_complete'] &= metrics is not None
            state['charged_wal_bytes'] += charged
            state['cursor'] = spec['blocks'][-1]
            state['pending'] = None
            state['recent'] = (state['recent'] + [entry])[-25:]
            if state['format'] == PROFILE_FORMAT:
                timing = state['timing_totals']
                timing['transaction_ms'] += metrics['elapsed_ms'] if metrics else 0
                timing['transaction_measurements'] += metrics is not None
                timing['host_attempt_ms'] += entry['host_attempt_ms']
                timing['host_attempt_batches'] += 1
            state['phase'] = 'running'
            state['checkpoint_sha256'] = checkpoint_digest(state)
            sync_json(path, state)
            print(json.dumps({'phase': 'session_checkpoint', 'state': str(path), 'completed_batches': state['batches'],
                              'selected_rows_verified': state['rows'], 'next_before_block': state['cursor']}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    for name in ('prepare', 'run', 'resume', 'status', 'retention-plan', 'diagnose'):
        modes.add_argument('--' + name, action='store_true')
    start = parser.add_mutually_exclusive_group()
    start.add_argument('--after-state', type=Path)
    start.add_argument('--after-session', type=Path)
    parser.add_argument('--state', type=Path)
    parser.add_argument('--profile', choices=('pilot-v1', 'observation-v1'))
    args = parser.parse_args()
    try:
        P.require(sys.platform.startswith('linux') and os.geteuid() == 0, 'Run on the Linux dev server as root')
        P.require(args.profile is None or args.prepare or args.run,
                  'Select a limit profile only when preparing a new session; saved budgets cannot be replaced')
        manifest, _, _ = D.configuration(ROOT, 'dev')
        with M.global_lock(not (args.status or args.retention_plan or args.diagnose)), B.build_lock(D, D.MANAGED_RELEASES, ROOT):
            if args.diagnose:
                P.require(args.state is not None and args.after_state is None and args.after_session is None,
                          'Diagnosis requires only the saved session state')
                result = diagnose(D, ROOT, args.state, manifest)
            elif args.retention_plan:
                P.require(args.state is None and args.after_state is None and args.after_session is None, 'Retention inventory needs no state argument')
                result = retention_plan(D, manifest)
            elif args.prepare or args.run:
                P.require(args.state is None and (args.after_state or args.after_session), 'Provide a verified predecessor')
                result = prepare(D, ROOT, args.after_state or args.after_session,
                                 after_session=args.after_session is not None, profile=args.profile)
                args.state = Path(result['state'])
                if args.run:
                    print(json.dumps(result, indent=2), flush=True)
                    result = execute(D, ROOT, args.state, manifest)
            else:
                P.require(args.state is not None and args.after_state is None and args.after_session is None, 'Use only the saved session state')
                state = load(args.state, manifest, check_resources=False)
                result = public(args.state, state) if args.status else execute(D, ROOT, args.state, manifest)
                if args.status:
                    result['client_in_progress'] = active(D, args.state, state)
                    result['database_observation_performed'] = False
        print(json.dumps(result, indent=2))
        return 0
    except Exception as error:
        failed_path = getattr(error, 'state_path', args.state)
        print(json.dumps({'status': 'stopped', 'state': str(failed_path) if failed_path else None, 'raw_values_suppressed': True,
                          'reason': str(error) if isinstance(error, U.UpgradeError) else 'Session check failed; retain its private journal/log',
                          'recovery': 'Use status to inspect the saved checkpoint, then resume from the creation commit. A live orphan client is left alone and exits after at most 60 seconds idle. Resume reconciles pending rows before any update; budgets are never reset.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
