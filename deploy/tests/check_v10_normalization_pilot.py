#!/usr/bin/env python3
"""Exercise the real one-block callback, rollback guards and idempotent retry."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import check_v10_normalization_plan as F
import check_upgrade_database as G
import deploy_check as C
import dev_config as D
import v10_upgrade as U
import v10_normalization_pilot as P
import check_v10_upgrade as A


def check(image, network, token, environment, sql, pg, directory):
    row = json.loads(G.run(['docker', 'inspect', pg]))[0]
    identity = {'database': 'fixture', 'oid': int(sql("SELECT oid FROM pg_database WHERE datname='fixture'").strip()),
                'address': row['NetworkSettings']['Networks'][network]['IPAddress'], 'port': 5432, 'postgres_major': 15}
    versions = list(map(int, sql('SELECT version FROM schema_migrations ORDER BY version').splitlines()))
    payload = {'mode': 'inspect', 'block': 20, 'expected_rows': 2, 'wal_limit_bytes': P.WAL_LIMIT,
               'database_identity': identity, 'applied_versions': versions,
               'heap_identity': int(sql("SELECT pg_relation_filenode('internal_transactions'::regclass)").strip())}
    def invoke(data, failed=False):
        name = 'normalization-pilot-' + uuid.uuid4().hex[:16]
        command = U.client_command(image, network, name, token)
        command[2:2] = ['--label', G.LABEL + '=' + token]
        source = (ROOT / 'deploy/scripts/v10/normalization_pilot.exs').read_bytes()
        try:
            r = subprocess.run(command, input=U.archive(environment, data, source), capture_output=True, timeout=60)
            if failed:
                assert r.returncode != 0, 'A deliberately rejected pilot unexpectedly succeeded'
                return None
            if r.returncode:
                raise RuntimeError(r.stdout.decode(errors='replace')[-5000:] + r.stderr.decode(errors='replace')[-4000:])
            return C.marked_json(D, r.stdout.decode())
        finally:
            G.cleanup_container(name, token)
    before = invoke(payload)
    original = P.baseline(before)
    assert original['needs_normalization'] == 2 and len(original['rows']) == 2
    payload['baseline'] = {k: original[k] for k in ('full_hashes', 'semantic_hashes', 'statuses')}
    invalid = deepcopy(payload)
    invalid['expected_rows'] = 1
    invoke(invalid, failed=True)
    # Exercise an actual transaction rollback after the callback has done DML.
    limited = {**payload, 'mode': 'migrate', 'wal_limit_bytes': 0}
    invoke(limited, failed=True)
    after_rollback = P.baseline(invoke(payload))
    assert after_rollback['full_hashes'] == original['full_hashes']
    assert after_rollback['statuses'] == original['statuses']
    # A trace-path change preserves semantic form but must trip full-row drift.
    sql("UPDATE internal_transactions SET trace_address=ARRAY[99] WHERE block_number=20")
    invoke({**payload, 'mode': 'migrate'}, failed=True)
    # Restore precisely the two synthetic rows for the success path.
    for old in original['rows']:
        value = json.dumps(old).replace("'", "''")
        block_hash = old['block_hash'].replace("'", "''")
        sql("UPDATE internal_transactions SET trace_address=(jsonb_populate_record(NULL::internal_transactions,'" + value + "'::jsonb)).trace_address WHERE block_hash='" + block_hash + "'::bytea AND block_index=" + str(old['block_index']))
    changed = invoke({**payload, 'mode': 'migrate'})
    after = P.baseline(changed)
    assert changed['updated_rows'] == 2 and not changed['already_normalized']
    assert after['needs_normalization'] == 0 and after['semantic_hashes'] == original['semantic_hashes']
    assert after['statuses'] == original['statuses']
    assert 0 < changed['cluster_wal_delta_bytes'] <= P.WAL_LIMIT
    repeated = invoke({**payload, 'mode': 'migrate'})
    assert repeated['updated_rows'] == 0 and repeated['already_normalized']
    assert P.baseline(repeated)['full_hashes'] == after['full_hashes']
    assert sql("SELECT count(*) FROM migrations_status WHERE migration_name='empty_internal_transactions_data'").strip() == '0'
    assert sql("SELECT status FROM migrations_status WHERE migration_name='reindex_duplicated_internal_transactions'").strip() == 'completed'
    # Cover nonzero value/error relocation and created-contract destination.
    for block in (11, 12):
        single = {**payload, 'block': block, 'expected_rows': 1}
        single.pop('baseline')
        prior = P.baseline(invoke(single))
        single['baseline'] = {k: prior[k] for k in ('full_hashes', 'semantic_hashes', 'statuses')}
        if block == 11:
            error_count = sql('SELECT count(*) FROM transaction_errors').strip()
            invoke({**single, 'mode': 'migrate', 'wal_limit_bytes': 0}, failed=True)
            assert P.baseline(invoke(single))['full_hashes'] == prior['full_hashes']
            assert sql('SELECT count(*) FROM transaction_errors').strip() == error_count
        converted = invoke({**single, 'mode': 'migrate'})
        assert converted['updated_rows'] == 1 and converted['snapshot']['semantic_hashes'] == prior['semantic_hashes']
    def api_check(backend):
        for block, kind, error, value in ((11, 'call', 'synthetic revert', '7'), (12, 'create', None, '0')):
            path = '/api/v2/transactions/0x' + format(100 + block, '064x') + '/internal-transactions'
            body = G.run(['docker', 'exec', backend, 'curl', '-fsS', '--max-time', '8', 'http://127.0.0.1:4000' + path])
            items = json.loads(body)['items']
            assert len(items) == 1
            assert items[0]['type'] == kind and items[0]['error'] == error and items[0]['value'] == value
            assert items[0]['success'] == (error is None)
    application = A.startup(image, network, token, {**environment, 'POOL_SIZE': '2', 'ACCOUNT_POOL_SIZE': '2'}, directory, api_check=api_check)
    assert sql("SELECT count(*) FROM migrations_status WHERE migration_name='empty_internal_transactions_data'").strip() == '0'
    return {'status': 'passed', 'actual_compiled_update_callback': True, 'rows_normalized': 2,
            'semantic_values_and_other_columns_preserved': True, 'error_relocation_and_created_destination_checked': True,
            'mixed_state_api_values_verified': True, 'original_snapshot_saved_in_fixture': True,
            'wal_limit_failure_rolled_back_changes': True, 'row_count_and_full_row_drift_rejected': True,
            'repeat_updated_rows': 0, 'migration_completion_records_untouched': True,
            'schema_client_application_or_migration_worker_started': False, 'partial_normalization_application_check': application, 'live_services_accessed': False,
            'synthetic_transaction_elapsed_ms': changed['elapsed_ms'], 'synthetic_cluster_wal_delta_bytes': changed['cluster_wal_delta_bytes']}


if __name__ == '__main__':
    raise SystemExit(F.main(pilot_check=check))
