#!/usr/bin/env python3
"""Exercise real vacuum qualification, bounded reuse and reconciliation on PG15."""
import contextlib
from copy import deepcopy
import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import check_v10_normalization_plan as F
import check_upgrade_database as G
from check_v10_vacuum import SETUP
import v10_normalization_session as J
import v10_vacuum as V


def check(image, network, token, environment, sql, pg, directory):
    sql("WITH seed AS MATERIALIZED (SELECT to_jsonb(t) AS row FROM internal_transactions t WHERE block_number=10 LIMIT 1), expanded AS MATERIALIZED (SELECT jsonb_populate_record(NULL::internal_transactions,seed.row||jsonb_build_object('block_number',100+g/100,'block_index',10000+g,'index',10000+g)) AS r FROM generate_series(0,2999) g CROSS JOIN seed) INSERT INTO internal_transactions SELECT (r).* FROM expanded")
    sql("INSERT INTO internal_transactions SELECT (jsonb_populate_record(NULL::internal_transactions,to_jsonb(t)||jsonb_build_object('block_number',200,'block_index',50000,'index',50000))).* FROM internal_transactions t WHERE block_number=10")
    sql('ANALYZE internal_transactions')
    row = json.loads(G.run(['docker', 'inspect', pg]))[0]
    identity = {'database': 'fixture', 'oid': int(sql("SELECT oid FROM pg_database WHERE datname='fixture'").strip()),
                'address': row['NetworkSettings']['Networks'][network]['IPAddress'], 'port': 5432, 'postgres_major': 15}
    manifest = {'chain_id': '18417507517'}
    runtime = {'backend': {'Id': 'a' * 64, 'Image': image}}

    def run(command, **kwargs):
        if command[0] == 'git':
            return ''
        if command[:4] == ['docker', 'exec', '-i', 'backend']:
            name = 'reuse-observer-' + uuid.uuid4().hex[:16]
            assert command[-1] == V.RPC_SCRIPT
            entry = V.U.RUN_SCRIPT.rsplit('exec /app/bin/blockscout eval', 1)[0]
            entry += 'cat /tmp/upgrade/migrate.exs | sh -c ' + shlex.quote(V.RPC_SCRIPT.replace('/app/bin/blockscout rpc', '/app/bin/blockscout eval')) + '\n'
            client_command = V.U.client_command(image, network, name, token)
            client_command[-1] = entry
            try:
                observed = subprocess.run(client_command,
                    input=V.U.archive(environment, {}, (SETUP + kwargs['input_text']).encode()), capture_output=True, timeout=30)
                if observed.returncode:
                    raise RuntimeError((observed.stdout + observed.stderr).decode(errors='replace')[-5000:])
                return observed.stdout.decode()
            finally:
                G.cleanup_container(name, token)
        return J.D.run(command, **kwargs)

    api = type('FixtureAPI', (), {})()
    api.require = J.P.require
    api.configuration = lambda *a: (manifest, {}, {})
    api.run, api.inspect_project, api.ensure_stable = run, lambda *a: deepcopy(runtime), J.D.ensure_stable
    upgrade = {'candidate_image': image, 'candidate_source_commit': 'a' * 40, 'network': network,
               'backend_environment': environment, 'migration_payload': {'database_identity': identity,
                    'heap_identity': int(sql("SELECT pg_relation_filenode('internal_transactions'::regclass)").strip())},
               'applied_result': {'applied_versions': list(map(int, sql('SELECT version FROM schema_migrations ORDER BY version').splitlines()))}}
    base = (directory / 'reuse-check').resolve()
    base.mkdir(mode=0o700)
    hold = {'phase': 'held', 'inputs': {'upgrade_state': str(directory / 'upgrade.json'), 'runtime_before': runtime}}
    with patch.object(J, 'BASE', base / 'sessions'), patch.object(V, 'BASE', base / 'vacuum'), \
         patch.object(J.P, 'BASE', base / 'pilot'), \
         patch.object(J.B, 'clean_source', return_value=('a' * 40, '10.2.6')), \
         patch.object(J.P, 'context', return_value=(manifest, upgrade)), \
         patch.object(J.P, 'free_bytes', return_value=888 * 1024**3), \
         patch.object(J.P.N, 'collect', return_value={'blockers': [], 'selected_block': 200, 'sample_rows': 1}), \
         patch.object(V.H, 'load', return_value=hold), patch.object(V.E, 'load', return_value=hold), \
         patch.dict(J.LIMITS, max_batches=1, max_rows=1000), \
         patch.dict(J.OBSERVATION_LIMITS, max_batches=1, max_rows=1000, maintenance_max_rows=1000), \
         patch.dict(J.REUSE_LIMITS, max_batches=1, max_rows=1000), contextlib.redirect_stdout(io.StringIO()):
        pilot = J.P.prepare(api, ROOT, directory / 'upgrade.json', 200, 1)
        pilot_path = Path(pilot['state'])
        J.P.apply(api, ROOT, pilot_path, manifest)
        first = J.prepare(api, ROOT, pilot_path)
        first_path = Path(first['state'])
        assert J.execute(api, ROOT, first_path, manifest)['selected_rows_verified'] == 1000
        observation = J.prepare(api, ROOT, first_path, after_session=True, profile='observation-v1')
        parent_path = Path(observation['state'])
        result = J.execute(api, ROOT, parent_path, manifest)
        assert result['stop_reason'] == 'vacuum_review_required' and result['next_before_block'] == 110
        parent_bytes = J.U.private(parent_path)
        prepared = V.prepare(api, ROOT, parent_path, Path('/metrics'), Path('/empty'), profile=V.FAST_PROFILE)
        vacuum_path = Path(prepared['state'])
        real_sleep = V.time.sleep
        with patch.object(V.time, 'sleep', side_effect=lambda seconds: real_sleep(min(seconds, 0.1))):
            vacuum = V.execute(api, ROOT, vacuum_path)
        assert vacuum['vacuum_sql_completed'] and vacuum['completion_verified']
        reuse = J.prepare(api, ROOT, parent_path, after_session=True, profile=J.Q.PROFILE, after_vacuum=vacuum_path)
        reuse_path = Path(reuse['state'])
        assert reuse['reuse_review']['cleanup']['dead_item_identifiers_removed'] > 0
        assert reuse['execution_profile'] == 'lean-v1' and reuse['maintenance_selected_rows'] == 1000
        reservation = J.U.private(vacuum_path.parent / 'normalization-reuse.json')
        assert json.loads(reservation)['state'] == str(reuse_path)
        try:
            J.prepare(api, ROOT, parent_path, after_session=True, profile=J.Q.PROFILE, after_vacuum=vacuum_path)
            raise AssertionError('vacuum grant was reused')
        except J.U.UpgradeError as error:
            assert 'already reserved' in str(error)
        original = J.Client.request
        writes = [0]

        def lose_commit_reply(client, command, payload=None):
            result = original(client, command, payload)
            if command == 'migrate':
                writes[0] += 1
                raise J.U.UpgradeError('fixture lost commit response')
            return result

        with patch.object(J.Client, 'request', lose_commit_reply):
            try:
                J.execute(api, ROOT, reuse_path, manifest)
                raise AssertionError('lost commit reply was not injected')
            except J.U.UpgradeError:
                pass
            pending = J.load(reuse_path, manifest)
            assert pending['pending'] is not None and pending['cursor'] == 110
            recovered = J.execute(api, ROOT, reuse_path, manifest)
        assert writes[0] == 1 and recovered['selected_rows_verified'] == 1000
        assert recovered['maintenance_selected_rows'] == 2000 and recovered['next_before_block'] == 100
        assert recovered['stop_reason'] == 'reuse_review_required'
        assert recovered['charged_chunk_wal_bytes'] == J.REUSE_LIMITS['wal_bytes']
        assert not recovered['pending_transaction'] and not recovered['wal_measurements_complete']
        assert J.U.private(parent_path) == parent_bytes
        assert J.U.private(vacuum_path.parent / 'normalization-reuse.json') == reservation
        assert sql('SELECT count(*) FROM internal_transactions WHERE block_number BETWEEN 100 AND 109 AND trace_address IS NOT NULL').strip() == '0'
        assert sql("SELECT count(*) FROM migrations_status WHERE migration_name='empty_internal_transactions_data'").strip() == '0'
        assert sql("SELECT status FROM migrations_status WHERE migration_name='reindex_duplicated_internal_transactions'").strip() == 'completed'
        report = json.loads(J.U.private(reuse_path.parent / 'resources-latest.json'))
        assert 'changes_since_post_vacuum' in report and 'changes_since_maintenance_origin' in report
        try:
            J.prepare(api, ROOT, reuse_path, after_session=True)
            raise AssertionError('finite reuse phase created another successor')
        except J.U.UpgradeError as error:
            assert 'requires review' in str(error)
    return {'status': 'passed', 'actual_pg15_vacuum_and_main_table_cleanup_verified': True,
            'actual_lean_reuse_transaction_verified': True, 'durable_single_use_grant_checked': True,
            'lost_commit_reconciled_without_second_update': True, 'original_baseline_and_rows_preserved': True,
            'post_vacuum_deltas_recorded': True, 'sustained_run_enabled': False,
            'host_runtime_health_stubbed': True, 'live_services_accessed': False}


if __name__ == '__main__':
    raise SystemExit(F.main(pilot_check=check))
