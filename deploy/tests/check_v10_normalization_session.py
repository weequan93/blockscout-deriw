#!/usr/bin/env python3
"""Exercise the persistent protocol, checkpoint rotation and lost commit on PG15."""
import contextlib
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import io
import json
from pathlib import Path
import subprocess
import sys
import time
import types
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import check_v10_normalization_plan as F
import check_upgrade_database as G
import v10_normalization_session as J


def check(image, network, token, environment, sql, pg, directory):
    constraint = sql("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='internal_transactions'::regclass AND conname='call_has_error_id_or_result'").strip()
    sql('ALTER TABLE internal_transactions DROP CONSTRAINT call_has_error_id_or_result')
    sql("WITH seeds AS MATERIALIZED (SELECT block_number,to_jsonb(t) AS row FROM internal_transactions t WHERE block_number IN (10,11,12)), expanded AS MATERIALIZED (SELECT jsonb_populate_record(NULL::internal_transactions,seeds.row||jsonb_build_object('block_number',100+g/100,'block_index',10000+g,'index',10000+g)) AS r FROM generate_series(0,5999) g JOIN seeds ON seeds.block_number=CASE g WHEN 0 THEN 11 WHEN 1 THEN 12 ELSE 10 END) INSERT INTO internal_transactions SELECT (r).* FROM expanded")
    sql('ALTER TABLE internal_transactions ADD CONSTRAINT call_has_error_id_or_result ' + constraint)
    sql("INSERT INTO internal_transactions SELECT (jsonb_populate_record(NULL::internal_transactions,to_jsonb(t)||jsonb_build_object('block_number',200,'block_index',50000,'index',50000))).* FROM internal_transactions t WHERE block_number=10")
    sql('ANALYZE internal_transactions')
    row = json.loads(G.run(['docker', 'inspect', pg]))[0]
    identity = {'database': 'fixture', 'oid': int(sql("SELECT oid FROM pg_database WHERE datname='fixture'").strip()),
                'address': row['NetworkSettings']['Networks'][network]['IPAddress'], 'port': 5432, 'postgres_major': 15}
    manifest = {'chain_id': '18417507517'}
    runtime = {'backend': {'Id': 'a' * 64, 'Image': image}}
    def run(command, **kwargs):
        return '' if command[0] == 'git' else J.D.run(command, **kwargs)
    api = types.SimpleNamespace(require=J.P.require, configuration=lambda *a: (manifest, {}, {}), run=run,
                                inspect_project=lambda *a: deepcopy(runtime), ensure_stable=J.D.ensure_stable)
    upgrade = {'candidate_image': image, 'candidate_source_commit': 'a' * 40, 'network': network,
               'backend_environment': environment, 'migration_payload': {'database_identity': identity,
                    'heap_identity': int(sql("SELECT pg_relation_filenode('internal_transactions'::regclass)").strip())},
               'applied_result': {'applied_versions': list(map(int, sql('SELECT version FROM schema_migrations ORDER BY version').splitlines()))}}
    def collect(*args, **kwargs):
        return {'blockers': [], 'selected_block': 200, 'sample_rows': 1}
    paths = (directory / 'session-check').resolve()
    paths.mkdir(mode=0o700)
    with patch.object(J.P, 'BASE', paths / 'batches'), patch.object(J, 'BASE', paths / 'sessions'), \
            patch.object(J.B, 'clean_source', return_value=('a' * 40, '10.2.6')), \
            patch.object(J.P, 'context', return_value=(manifest, upgrade)), \
            patch.object(J.P, 'free_bytes', return_value=888 * 1024**3), \
            patch.object(J.P.N, 'collect', side_effect=collect), \
            patch.dict(J.LIMITS, max_batches=5, max_rows=5000), contextlib.redirect_stdout(io.StringIO()):
        initial = J.P.prepare(api, ROOT, directory / 'upgrade.json', 200, 1)
        initial_path = Path(initial['state'])
        J.P.apply(api, ROOT, initial_path, manifest)
        prepared = J.prepare(api, ROOT, initial_path)
        path = Path(prepared['state'])
        assert prepared['database_writes'] is False
        assert prepared['resources']['autovacuum_effectively_enabled']
        assert not prepared['resources']['sustained_ready']
        result = J.execute(api, ROOT, path, manifest)
        assert result['completed_batches'] == 5 and result['selected_rows_verified'] == 5000
        assert result['updated_rows_observed'] == 5000 and result['next_before_block'] == 110
        assert result['stop_reason'] == 'batch_limit_reached' and not result['global_normalization_complete']
        assert len(list(path.parent.glob('raw-*.json'))) == 2
        assert J.journal_size(path.parent) < J.LIMITS['journal_bytes']
        state = J.load(path, manifest)
        checkpoint = state['checkpoint_hash']
        assert len(state['recent']) == 5
        before = sql('SELECT md5(jsonb_agg(to_jsonb(it) ORDER BY block_number,"index")::text) FROM internal_transactions it WHERE block_number BETWEEN 100 AND 159')
        J.execute(api, ROOT, path, manifest)
        assert sql('SELECT md5(jsonb_agg(to_jsonb(it) ORDER BY block_number,"index")::text) FROM internal_transactions it WHERE block_number BETWEEN 100 AND 159') == before

        # The next epoch copies only the latest verified checkpoint. It must not
        # depend on any rotated historical raw snapshot to continue/reconcile.
        prepared2 = J.prepare(api, ROOT, path, after_session=True)
        next_path = Path(prepared2['state'])
        original_request = J.Client.request
        lose = [True]
        writes = [0]
        def interrupted(client, command, payload=None):
            result = original_request(client, command, payload)
            if command == 'migrate':
                writes[0] += 1
                if lose[0]:
                    lose[0] = False
                    raise J.U.UpgradeError('simulated lost commit response')
            return result
        with patch.object(J.Client, 'request', interrupted):
            try:
                J.execute(api, ROOT, next_path, manifest)
                raise AssertionError('lost response was not simulated')
            except J.U.UpgradeError:
                pass
            assert J.load(next_path, manifest)['cursor'] == 110
            resumed = J.execute(api, ROOT, next_path, manifest)
        assert writes[0] == 1
        assert resumed['completed_batches'] == 1 and resumed['selected_rows_verified'] == 1000
        assert resumed['next_before_block'] == 100
        assert resumed['charged_chunk_wal_bytes'] == J.LIMITS['wal_bytes']
        assert not resumed['wal_measurements_complete'] and not resumed['pending_transaction']
        assert J.load(path, manifest)['checkpoint_hash'] == checkpoint

        # Hold the very lock used by ordinary vacuum in another real session.
        # The new transaction must finish while that lock is still held.
        inputs = J.load(next_path, manifest)['inputs']
        with J.Client(api, next_path, inputs) as client:
            spec = {'blocks': [12], 'block_rows': [{'block': 12, 'rows': 1}], 'expected_rows': 1,
                    'wal_limit_bytes': 128 * 1024**2}
            prior = J.P.baseline(client.request('inspect', spec))
            spec['baseline'] = J.compact(prior)
            locker = subprocess.Popen(['docker', 'exec', pg, 'psql', '-U', 'fixture', '-d', 'fixture', '-v', 'ON_ERROR_STOP=1',
                '-Atc', 'BEGIN; LOCK TABLE internal_transactions IN SHARE UPDATE EXCLUSIVE MODE; SELECT pg_sleep(15); COMMIT;'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                # Confirm lock acquisition, rather than relying on a fixed sleep.
                deadline = time.monotonic() + 5
                while sql("SELECT count(*) FROM pg_locks WHERE relation='internal_transactions'::regclass AND mode='ShareUpdateExclusiveLock' AND granted").strip() != '1':
                    assert time.monotonic() < deadline
                    time.sleep(0.1)
                converted = client.request('migrate', {**spec, 'health_valid_until_unix_ms': int((time.time() + 25) * 1000)})
                assert converted['snapshot']['needs_normalization'] == 0 and locker.poll() is None
            finally:
                locker.communicate(timeout=20)

        # A fixture-only pause after the first snapshot makes races deterministic.
        # All identity, callback, isolation and rollback code remains unchanged.
        source_path = next_path.parent / 'migrate.exs'
        original_source = J.U.private(source_path)
        assert original_source.count(b'before = snapshot.()') == 1
        source_path.write_bytes(original_source.replace(b'before = snapshot.()',
            b'before = snapshot.()\n          if mode == "migrate", do: q.("SELECT pg_sleep(1.5)", [])'))
        def during_snapshot():
            deadline = time.monotonic() + 5
            while sql("SELECT count(*) FROM pg_stat_activity WHERE application_name='blockscout-v10-normalization-pilot' AND wait_event='PgSleep'").strip() != '1':
                assert time.monotonic() < deadline
                time.sleep(0.05)
        try:
            with J.Client(api, next_path, inputs) as client, ThreadPoolExecutor(max_workers=1) as pool:
                def spec_for(block):
                    spec = {'blocks': [block], 'block_rows': [{'block': block, 'rows': 1}], 'expected_rows': 1,
                            'wal_limit_bytes': 128 * 1024**2, 'health_valid_until_unix_ms': int((time.time() + 25) * 1000)}
                    spec['baseline'] = J.compact(J.P.baseline(client.request('inspect', spec)))
                    return spec
                late_spec = spec_for(11)
                future = pool.submit(client.request, 'migrate', late_spec)
                during_snapshot()
                sql("INSERT INTO internal_transactions SELECT (jsonb_populate_record(NULL::internal_transactions,to_jsonb(t)||jsonb_build_object('block_number',11,'block_index',999999,'index',999999))).* FROM internal_transactions t WHERE block_number=10")
                late_result = future.result(timeout=15)
                assert late_result['updated_rows'] == 1 and len(late_result['snapshot']['rows']) == 1
                assert sql('SELECT count(*) FROM internal_transactions WHERE block_number=11 AND trace_address IS NOT NULL').strip() == '1'
                assert sql('SELECT count(*) FROM internal_transactions WHERE block_number=11').strip() == '2'

                # A concurrent update of an existing selected row must cause a
                # serialization failure, never overwrite that row's new value.
                drift_spec = spec_for(10)
                future = pool.submit(client.request, 'migrate', drift_spec)
                during_snapshot()
                sql('UPDATE internal_transactions SET trace_address=ARRAY[99] WHERE block_number=10')
                try:
                    future.result(timeout=15)
                    raise AssertionError('concurrent update should abort normalization')
                except J.U.UpgradeError:
                    pass
                log = J.U.private(next_path.parent / 'client.log.private').decode(errors='replace')
                assert '40001' in log or 'could not serialize access' in log
                assert sql('SELECT trace_address::text FROM internal_transactions WHERE block_number=10').strip() == '{99}'
                assert sql('SELECT value::text FROM internal_transactions WHERE block_number=10').strip() == '0'
        finally:
            source_path.write_bytes(original_source)

        assert sql("SELECT count(*) FROM migrations_status WHERE migration_name='empty_internal_transactions_data'").strip() == '0'
        assert sql("SELECT status FROM migrations_status WHERE migration_name='reindex_duplicated_internal_transactions'").strip() == 'completed'
    return {'status': 'passed', 'persistent_repo_protocol': True, 'rows_normalized_in_first_session': 5000,
            'lost_commit_response_reconciled_without_second_update': True,
            'unknown_wal_consumed_remaining_allowance': True, 'two_raw_snapshot_slots_only': True,
            'constant_size_latest_checkpoint_continuation': True, 'ordinary_vacuum_lock_coexistence_verified': True,
            'late_insert_excluded_by_repeatable_read_snapshot': True,
            'concurrent_update_caused_serialization_rollback': True,
            'finished_resume_did_not_update_again': True, 'migration_completion_records_unchanged': True,
            'full_table_run_enabled': False, 'host_runtime_health_stubbed': True, 'live_services_accessed': False}


if __name__ == '__main__':
    raise SystemExit(F.main(pilot_check=check))
