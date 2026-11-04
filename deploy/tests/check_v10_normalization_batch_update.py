#!/usr/bin/env python3
"""Exercise a real 959-row/30-block callback and whole-transaction rollback."""
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
import v10_normalization_batch as S
import v10_normalization_pilot as P
import v10_upgrade as U


def check(image, network, token, environment, sql, pg, directory):
    # Exactly the live workload size, with legacy errors/nonzero values and a
    # created-contract destination mixed into the complete selected blocks.
    # Seed legacy error rows as if they predated v10's NOT VALID constraint;
    # restore its exact definition before exercising any normalization client.
    constraint = sql("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='internal_transactions'::regclass AND conname='call_has_error_id_or_result'").strip()
    assert constraint.endswith('NOT VALID')
    sql('ALTER TABLE internal_transactions DROP CONSTRAINT call_has_error_id_or_result')
    sql("INSERT INTO internal_transactions SELECT (jsonb_populate_record(NULL::internal_transactions,to_jsonb(t)||jsonb_build_object('block_number',100+g/32,'block_index',10000+g,'index',10000+g))).* FROM generate_series(0,958) g JOIN internal_transactions t ON t.block_number=CASE g WHEN 0 THEN 11 WHEN 1 THEN 12 ELSE 10 END")
    sql('ALTER TABLE internal_transactions ADD CONSTRAINT call_has_error_id_or_result ' + constraint)
    sql("INSERT INTO internal_transactions SELECT (jsonb_populate_record(NULL::internal_transactions,to_jsonb(t)||jsonb_build_object('block_number',90,'block_index',20000+g,'index',20000+g))).* FROM generate_series(0,99) g JOIN internal_transactions t ON t.block_number=10")
    sample = F.inspect(image, network, token, environment, before_block=200, batch_sample=True, projector=S.project)
    assert sample['selected_rows'] == 959 and sample['selected_block_count'] == 30
    assert sample['excluded_boundary_block'] == 90 and sample['excluded_boundary_rows'] == 42
    targets = [{'block': r['block'], 'rows': r['rows']} for r in sample['selected_blocks']]
    row = json.loads(G.run(['docker', 'inspect', pg]))[0]
    identity = {'database': 'fixture', 'oid': int(sql("SELECT oid FROM pg_database WHERE datname='fixture'").strip()),
                'address': row['NetworkSettings']['Networks'][network]['IPAddress'], 'port': 5432, 'postgres_major': 15}
    payload = {'mode': 'inspect', 'blocks': [r['block'] for r in targets], 'block_rows': targets,
               'expected_rows': 959, 'wal_limit_bytes': P.WAL_LIMIT, 'database_identity': identity,
               'applied_versions': list(map(int, sql('SELECT version FROM schema_migrations ORDER BY version').splitlines())),
               'heap_identity': int(sql("SELECT pg_relation_filenode('internal_transactions'::regclass)").strip())}

    def invoke(data, failed=False):
        name = 'normalization-batch-' + uuid.uuid4().hex[:16]
        command = U.client_command(image, network, name, token)
        command[2:2] = ['--label', G.LABEL + '=' + token]
        source = (ROOT / 'deploy/scripts/v10/normalization_pilot.exs').read_bytes()
        try:
            result = subprocess.run(command, input=U.archive(environment, data, source), capture_output=True, timeout=60)
            if failed:
                assert result.returncode != 0, 'A deliberately rejected batch unexpectedly succeeded'
                return
            if result.returncode:
                raise RuntimeError(result.stdout.decode(errors='replace')[-5000:] + result.stderr.decode(errors='replace')[-4000:])
            return C.marked_json(D, result.stdout.decode())
        finally:
            G.cleanup_container(name, token)

    original = P.baseline(invoke(payload))
    assert len(original['rows']) == 959 and original['needs_normalization'] == 959
    payload['baseline'] = {k: original[k] for k in ('full_hashes', 'semantic_hashes', 'statuses')}
    untouched_query = 'SELECT md5(jsonb_agg(to_jsonb(it) ORDER BY block_number,block_index)::text) FROM internal_transactions it WHERE block_number NOT BETWEEN 100 AND 129'
    untouched = sql(untouched_query)
    errors = sql('SELECT count(*) FROM transaction_errors')
    # An after-DML WAL failure must undo every block and error dictionary insert.
    invoke({**payload, 'mode': 'migrate', 'wal_limit_bytes': 0}, failed=True)
    rolled_back = P.baseline(invoke(payload))
    assert rolled_back['full_hashes'] == original['full_hashes'] and rolled_back['statuses'] == original['statuses']
    assert sql('SELECT count(*) FROM transaction_errors') == errors and sql(untouched_query) == untouched
    # The total remains 959, but swapping counts between two blocks must fail.
    wrong = deepcopy(payload)
    wrong['block_rows'][0]['rows'] += 1
    wrong['block_rows'][1]['rows'] -= 1
    invoke({**wrong, 'mode': 'migrate'}, failed=True)
    # Even a semantically harmless trace path change prevents overwriting drift.
    sql('UPDATE internal_transactions SET trace_address=ARRAY[99] WHERE block_index=10958')
    invoke({**payload, 'mode': 'migrate'}, failed=True)
    saved = next(r for r in original['rows'] if r['block_index'] == 10958)
    encoded = json.dumps(saved).replace("'", "''")
    sql("UPDATE internal_transactions SET trace_address=(jsonb_populate_record(NULL::internal_transactions,'" + encoded + "'::jsonb)).trace_address WHERE block_index=10958")
    changed = invoke({**payload, 'mode': 'migrate'})
    after = P.baseline(changed)
    assert changed['updated_rows'] == 959 and not changed['already_normalized']
    assert after['needs_normalization'] == 0 and after['semantic_hashes'] == original['semantic_hashes']
    assert after['statuses'] == original['statuses'] and sql(untouched_query) == untouched
    repeated = invoke({**payload, 'mode': 'migrate'})
    assert repeated['updated_rows'] == 0 and repeated['already_normalized']
    assert P.baseline(repeated)['full_hashes'] == after['full_hashes']
    # The shared client must retain the original one-block protocol, including
    # existing frozen pilot journals which have no batch fields.
    single = {k: v for k, v in payload.items() if k not in ('blocks', 'block_rows', 'baseline')}
    single.update(block=20, expected_rows=2)
    prior = P.baseline(invoke(single))
    single['baseline'] = {k: prior[k] for k in ('full_hashes', 'semantic_hashes', 'statuses')}
    single_result = invoke({**single, 'mode': 'migrate'})
    assert single_result['updated_rows'] == 2 and single_result['snapshot']['semantic_hashes'] == prior['semantic_hashes']
    assert sql("SELECT count(*) FROM migrations_status WHERE migration_name='empty_internal_transactions_data'").strip() == '0'
    assert sql("SELECT status FROM migrations_status WHERE migration_name='reindex_duplicated_internal_transactions'").strip() == 'completed'
    return {'status': 'passed', 'actual_compiled_update_callback': True, 'rows_normalized': 959, 'complete_blocks': 30,
            'partial_boundary_block_untouched': True, 'other_stored_rows_untouched': True,
            'all_blocks_and_error_inserts_rolled_back_on_wal_failure': True, 'per_block_count_and_row_drift_rejected': True,
            'semantic_values_and_other_columns_preserved': True, 'repeat_updated_rows': 0,
            'original_one_block_protocol_checked': True,
            'migration_completion_records_unchanged': True, 'application_or_worker_started': False,
            'live_services_accessed': False, 'synthetic_transaction_elapsed_ms': changed['elapsed_ms'],
            'synthetic_cluster_wal_delta_bytes': changed['cluster_wal_delta_bytes']}


if __name__ == '__main__':
    raise SystemExit(F.main(pilot_check=check))
