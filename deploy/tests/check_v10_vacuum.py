#!/usr/bin/env python3
"""Exercise the ordinary-vacuum socket and watchdog on disposable PG15."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shlex
import select
import subprocess
import sys
import time
import types
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import check_v10_normalization_plan as F
import check_upgrade_database as G
import v10_vacuum as V


SETUP = '''
true = Bitwise.band(File.stat!(__ENV__.file).mode, 0o777) == 0o600
for app <- [:crypto, :ssl, :postgrex, :ecto_sql], do: ({:ok, _} = Application.ensure_all_started(app))
{:ok, _} = Explorer.Repo.start_link(pool_size: 2)
{:ok, _} = Supervisor.start_link([], strategy: :one_for_one, name: Explorer.Supervisor)
defmodule VacuumFixtureChild do
  def start_link(_), do: Task.start_link(fn -> Process.sleep(:infinity) end)
end
for {parent, worker} <- [{BlockScoutWeb.Supervisor, Indexer.Prometheus.Metrics}, {Indexer.Fetcher.EmptyBlocksSanitizer.Supervisor, Indexer.Fetcher.EmptyBlocksSanitizer}] do
  spec = %{id: worker, start: {VacuumFixtureChild, :start_link, [nil]}}
  {:ok, _} = Supervisor.start_link([spec], strategy: :one_for_one, name: parent)
  :ok = Supervisor.terminate_child(parent, worker)
end
'''


def check(image, network, token, environment, sql, pg, directory):
    row = json.loads(G.run(['docker', 'inspect', pg]))[0]
    identity = {'database': 'fixture', 'oid': int(sql("SELECT oid FROM pg_database WHERE datname='fixture'").strip()),
        'address': row['NetworkSettings']['Networks'][network]['IPAddress'], 'port': 5432, 'postgres_major': 15}
    versions = list(map(int, sql('SELECT version FROM schema_migrations ORDER BY version').splitlines()))
    heap = int(sql("SELECT pg_relation_filenode('internal_transactions'::regclass)").strip())
    def run(command, **kwargs):
        if command[0] == 'git':
            return ''
        if command[:4] == ['docker', 'exec', '-i', 'backend']:
            name = 'vacuum-observer-' + uuid.uuid4().hex[:16]
            assert command[-1] == V.RPC_SCRIPT and len(command[-1]) < 4096
            code = kwargs['input_text']
            # Exercise the actual stdin/private-file/cleanup shell transport.
            # Only rpc becomes eval because this disposable node supplies the
            # backend Repo and held supervisor fixtures rather than a full app.
            entry = V.U.RUN_SCRIPT.rsplit('exec /app/bin/blockscout eval', 1)[0]
            entry += 'cat /tmp/upgrade/migrate.exs | sh -c ' + shlex.quote(V.RPC_SCRIPT.replace('/app/bin/blockscout rpc', '/app/bin/blockscout eval')) + '\n'
            entry += 'for vacuum_probe_leftover in /tmp/blockscout-v10-vacuum-observe.*; do test ! -e "$vacuum_probe_leftover"; done\n'
            client_command = V.U.client_command(image, network, name, token)
            client_command[-1] = entry
            try:
                observed = subprocess.run(client_command,
                    input=V.U.archive(environment, {}, (SETUP + code).encode()), capture_output=True, timeout=30)
                if observed.returncode:
                    raise RuntimeError((observed.stdout + observed.stderr).decode(errors='replace')[-5000:])
                return observed.stdout.decode()
            finally:
                G.cleanup_container(name, token)
        return V.D.run(command, **kwargs)
    manifest = {'chain_id': '18417507517'}
    runtime = {'backend': {'Id': 'a' * 64, 'Image': image}}
    api = types.SimpleNamespace(require=V.P.require, configuration=lambda *a: (manifest, {}, {}), run=run,
        inspect_project=lambda *a: deepcopy(runtime), ensure_stable=V.D.ensure_stable)
    upgrade = {'candidate_image': image, 'candidate_source_commit': 'a' * 40, 'network': network,
        'backend_environment': environment, 'migration_payload': {'database_identity': identity, 'heap_identity': heap},
        'applied_result': {'applied_versions': versions}}
    base = (directory / 'vacuum-check').resolve()
    base.mkdir(mode=0o700)
    readers = []
    def reader():
        process = subprocess.Popen(['docker', 'exec', '-i', pg, 'psql', '-U', 'fixture', '-d', 'fixture', '-Atq', '-v', 'ON_ERROR_STOP=1'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        readers.append(process)
        process.stdin.write(b'BEGIN ISOLATION LEVEL REPEATABLE READ;\nSELECT pg_backend_pid(),count(*) FROM internal_transactions WHERE block_number=20;\n')
        process.stdin.flush()
        assert select.select([process.stdout], [], [], 10)[0]
        pid = int(process.stdout.readline().decode().split('|')[0])
        return process, pid
    def stop_reader(process):
        if process.poll() is None:
            process.stdin.write(b'ROLLBACK;\n\\q\n')
            process.stdin.flush()
            process.stdin.close()
            process.wait(timeout=5)
    with patch.object(V, 'BASE', base / 'vacuum'), patch.object(V.P, 'BASE', base / 'pilot'), \
         patch.object(V.B, 'clean_source', return_value=('a' * 40, '10.2.6')), \
         patch.object(V.P, 'context', return_value=(manifest, upgrade)), \
         patch.object(V.P, 'free_bytes', return_value=888 * 1024**3), \
         patch.object(V.P.N, 'collect', return_value={'blockers': [], 'selected_block': 20, 'sample_rows': 2}):
        pilot = V.P.prepare(api, ROOT, directory / 'upgrade.json', 20, 2)
        pilot_path = Path(pilot['state'])
        V.P.apply(api, ROOT, pilot_path, manifest)
        pilot_state = V.P.load(api, pilot_path, manifest)
        sql("WITH seed AS MATERIALIZED (SELECT to_jsonb(t) AS row FROM internal_transactions t WHERE block_number=20 LIMIT 1), expanded AS MATERIALIZED (SELECT jsonb_populate_record(NULL::internal_transactions,seed.row||jsonb_build_object('block_number',30,'block_index',10000+g,'index',10000+g)) AS r FROM generate_series(0,999) g CROSS JOIN seed) INSERT INTO internal_transactions SELECT (r).* FROM expanded")
        sql('ANALYZE internal_transactions')
        large_state = deepcopy(pilot_state)
        large_state['client_name'] = 'blockscout-v10-schema-' + uuid.uuid4().hex[:16]
        large_state['migration_payload'].update(blocks=[30], block_rows=[{'block': 30, 'rows': 1000}], expected_rows=1000)
        large_state['migration_payload'].pop('baseline', None)
        large_state['migration_payload'].pop('block', None)
        snapshot = V.P.baseline(V.U.client(api, pilot_path, large_state, 'inspect'))
        anchor = {'blocks': [30], 'block_rows': [{'block': 30, 'rows': 1000}], 'expected_rows': 1000,
                  'baseline': V.J.compact(snapshot)}
        parent_path = base / 'normalization-state.json'
        V.R.write_new(parent_path, b'{}')
        parent = {'phase': 'finished', 'pending': None, 'rows': 1000, 'cursor': 30,
            'stop_reason': 'next_complete_batch_requires_vacuum_review', 'last': anchor,
            'inputs': {'upgrade_state': str(directory / 'upgrade.json'), 'runtime_before': runtime,
                'maintenance_rows_before': 0, 'maintenance_origin': {'fixture': True},
                'payload': {'database_identity': identity, 'heap_identity': heap, 'applied_versions': versions}}}
        hold = {'phase': 'held', 'inputs': {'upgrade_state': parent['inputs']['upgrade_state'], 'runtime_before': runtime}}
        fixture_root = base / 'source'
        scripts = fixture_root / 'deploy/scripts/v10'
        scripts.mkdir(mode=0o700, parents=True)
        for name in ('normalization_pilot.exs', 'normalization_diagnostics.exs', 'vacuum.exs'):
            source = (ROOT / 'deploy/scripts/v10' / name).read_bytes()
            if name == 'vacuum.exs':
                flush = b'Postgrex.query!(conn, "SELECT pg_stat_force_next_flush()", [], timeout: 2000)'
                assert source.count(flush) == 1
                # Keep the completed VACUUM as the socket's last statement long
                # enough to prove its actual server query_start deterministically.
                source = source.replace(flush, b'Process.sleep(5000)\n          ' + flush)
            if name == 'normalization_diagnostics.exs':
                source = source.replace(b"interval '60 seconds'", b"interval '1 second'")
            (scripts / name).write_bytes(source)
        old_reader, old_pid = reader()
        later = {}
        with patch.object(V.J, 'load', return_value=parent), patch.object(V, 'no_normalizer'), \
             patch.object(V.H, 'load', return_value=hold), patch.object(V.E, 'load', return_value=hold):
            prepared = V.prepare(api, fixture_root, parent_path, Path('/metrics'), Path('/empty'), profile='index-cleanup-v1')
            path = Path(prepared['state'])
            assert not prepared['database_writes']
            assert prepared['resources']['transactions']['oldest_transaction_seconds'] < 300
            frozen = V.load(path)['inputs']
            assert frozen['limits'] == {**V.LIMITS, 'duration_seconds': 14400}
            assert frozen['payload']['duration_seconds'] == 14400
            sql("INSERT INTO migrations_status(migration_name,status,inserted_at,updated_at) VALUES ('empty_internal_transactions_data','started',NOW(),NOW())")
            try:
                V.observe(api, V.U.private(path.parent / 'observer.exs'), frozen['payload'])
                raise AssertionError('Appearance of a held migration record was accepted')
            except RuntimeError as error:
                assert 'no match of right hand side value' in str(error)
            finally:
                sql("DELETE FROM migrations_status WHERE migration_name='empty_internal_transactions_data'")
            # Poll quickly only in the disposable fixture; the frozen production
            # limits and actual session-local PostgreSQL settings stay unchanged.
            real_sleep = V.time.sleep
            real_capture = V.capture_start
            def capture_then_start_reader(path, state, data):
                real_capture(path, state, data)
                if state.get('vacuum_query_started_at') and not later:
                    later['process'], later['pid'] = reader()
            with patch.object(V.time, 'sleep', side_effect=lambda seconds: real_sleep(min(seconds, 0.1))), \
                 patch.object(V, 'capture_start', side_effect=capture_then_start_reader):
                result = V.execute(api, fixture_root, path)
            assert result['vacuum_sql_completed'] and result['completion_verified']
            assert result['target']['settings']['statement_timeout'] == '4h'
            assert later
            assert not result['normalization_gate_reopened']
            before = json.loads(V.U.private(path.parent / 'resources-before.json'))
            after = json.loads(V.U.private(path.parent / 'resources-after.json'))
            assert after['table']['vacuum_count'] > before['table']['vacuum_count']
            verbose = V.marker(path, 'BLOCKSCOUT_VACUUM_VERBOSE=')
            assert verbose and any('vacuum' in str(message).lower() for message in verbose)
            assert V.execute(api, fixture_root, path, resume=True)['completion_verified']
            finished = V.load(path)
            boundary = finished['vacuum_query_started_at']
            payload = {**finished['inputs']['payload'], 'oldest_transaction_seconds': 1}
            policy_inputs = {**finished['inputs'], 'limits': {**finished['inputs']['limits'], 'oldest_transaction_seconds': 1},
                             'target': finished['target'], 'vacuum_query_started_at': boundary}
            observed = V.observe(api, V.U.private(path.parent / 'observer.exs'), payload, finished['target'],
                                 vacuum_query_started_at=boundary, runtime=runtime)
            assert observed['transactions']['oldest_pre_vacuum_transaction_seconds'] > 1
            assert observed['transactions']['post_vacuum_client_transactions_over_limit'] >= 1
            assert {old_pid, later['pid']} <= {row['pid'] for row in observed['query_diagnostics']}
            try:
                V.check_resources(observed, policy_inputs)
                raise AssertionError('pre-existing old snapshot was accepted')
            except V.ResourceReviewError as error:
                assert error.review['query_diagnostics']
            stop_reader(old_reader)
            observed = V.observe(api, V.U.private(path.parent / 'observer.exs'), payload, finished['target'],
                                 vacuum_query_started_at=boundary, runtime=runtime)
            assert observed['transactions']['oldest_pre_vacuum_transaction_seconds'] == 0
            assert observed['transactions']['oldest_transaction_seconds'] > 1
            V.check_resources(observed, policy_inputs)
            try:
                V.check_resources(observed, {**policy_inputs, 'transaction_guard_scope': None})
                raise AssertionError('legacy all-client guard was relaxed')
            except V.ResourceReviewError:
                pass
            stop_reader(later['process'])
            assert sql('SHOW vacuum_cost_delay').strip() == '0'
            assert sql('SHOW vacuum_cost_limit').strip() == '200'
            assert sql('SELECT count(*) FROM internal_transactions').strip() == '1005'
            assert sql("SELECT count(*) FROM migrations_status WHERE migration_name='empty_internal_transactions_data'").strip() == '0'

        # A delayed statement runs on this fixture's own socket. Losing lease
        # renewal exits the VM, closes that socket and leaves a foreign lock alone.
        foreign = subprocess.Popen(['docker', 'exec', '-i', pg, 'psql', '-U', 'fixture', '-d', 'fixture', '-v', 'ON_ERROR_STOP=1'],
                                   stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            foreign.stdin.write(b'SELECT pg_advisory_lock(918273);\n')
            foreign.stdin.flush()
            until = time.monotonic() + 5
            while sql("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND objid=918273").strip() != '1':
                assert time.monotonic() < until
                time.sleep(0.1)
            frozen = V.load(path)['inputs']
            payload = {**frozen['payload'], 'token': uuid.uuid4().hex[:16]}
            payload['application_name'] = 'blockscout-v10-vacuum-' + payload['token']
            script = (ROOT / 'deploy/scripts/v10/vacuum.exs').read_bytes()
            script = script.replace(b'@lease_ms 45000', b'@lease_ms 1000').replace(V.SQL.encode(), b'SELECT pg_sleep(5)')
            name = 'blockscout-v10-schema-' + payload['token']
            log_path = base / 'watchdog.log.private'
            with log_path.open('wb') as log:
                os.chmod(log_path, 0o600)
                process = subprocess.Popen(V.U.client_command(image, network, name, token), stdin=subprocess.PIPE, stdout=log, stderr=log)
                try:
                    process.stdin.write(V.U.archive(environment, payload, script))
                    process.stdin.close()
                    until = time.monotonic() + 30
                    while 'BLOCKSCOUT_VACUUM_READY=' not in log_path.read_text():
                        assert process.poll() is None and time.monotonic() < until
                        time.sleep(0.1)
                    ready = next(json.loads(line.split('=', 1)[1]) for line in log_path.read_text().splitlines() if line.startswith('BLOCKSCOUT_VACUUM_READY='))
                    V.publish(api, {'client_name': name}, 'lease', {'token': payload['token'], 'sequence': 1})
                    V.publish(api, {'client_name': name}, 'start', {'token': payload['token']})
                    process.wait(timeout=10)
                    assert process.returncode != 0
                finally:
                    G.cleanup_container(name, token)
                    V.B.terminate(process)
            stopped = next(json.loads(line.split('=', 1)[1]) for line in log_path.read_text().splitlines() if line.startswith('BLOCKSCOUT_VACUUM_RESULT='))
            assert stopped['reason'] == 'host_lease_expired'
            until = time.monotonic() + 4
            while sql('SELECT count(*) FROM pg_stat_activity WHERE pid=' + str(ready['pid'])).strip() != '0':
                assert time.monotonic() < until, 'Owned socket did not stop its statement after lease loss'
                time.sleep(0.1)
            assert sql("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND objid=918273").strip() == '1'
        finally:
            foreign.stdin.close()
            foreign.wait(timeout=5)
    return {'status': 'passed', 'ordinary_vacuum_completed': True, 'vacuum_counter_advanced': True,
        'verbose_result_retained': True, 'connection_local_settings_verified': True, 'four_hour_profile_timeout_verified': True,
        'other_profile_limits_unchanged': True, 'normalization_anchor_unchanged': True,
        'migration_completion_records_unchanged': True, 'completion_resume_did_not_relaunch': True,
        'appearance_of_absent_migration_record_rejected': True,
        'observer_code_streamed_through_private_file': True, 'observer_temporary_file_removed': True,
        'maximum_1000_row_anchor_verified': True,
        'actual_vacuum_query_start_proved': True, 'pre_existing_snapshot_guard_retained': True,
        'post_start_transaction_observed_without_stopping_vacuum': True, 'legacy_all_client_guard_retained': True,
        'query_diagnostics_captured_before_rejection': True,
        'lost_lease_closed_only_owned_socket': True, 'foreign_lock_preserved': True,
        'normalization_gate_reopened': False, 'host_health_and_runtime_stubbed': True, 'live_services_accessed': False}


if __name__ == '__main__':
    raise SystemExit(F.main(pilot_check=check))
