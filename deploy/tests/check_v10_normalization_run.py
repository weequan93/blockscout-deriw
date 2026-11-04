#!/usr/bin/env python3
"""Exercise five real 1000-row transactions through the finite controller."""
from copy import deepcopy
import contextlib
import io
import json
from pathlib import Path
import sys
import types
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import check_v10_normalization_plan as F
import check_upgrade_database as G
import v10_normalization_run as J


def check(image, network, token, environment, sql, pg, directory):
    # Selected legacy blocks plus unrelated normalized rows make an indexed
    # block lookup preferable to walking the transaction-hash index in PG15.
    sql("INSERT INTO internal_transactions SELECT (jsonb_populate_record(NULL::internal_transactions,to_jsonb(t)||jsonb_build_object('block_number',100+g/100,'block_index',10000+g,'index',10000+g))).* FROM generate_series(0,5999) g JOIN internal_transactions t ON t.block_number=10")
    sql("INSERT INTO internal_transactions SELECT (jsonb_populate_record(NULL::internal_transactions,to_jsonb(t)||jsonb_build_object('block_number',200,'block_index',50000,'index',50000))).* FROM internal_transactions t WHERE block_number=10")
    sql("WITH seed AS MATERIALIZED (SELECT to_jsonb(t) AS row FROM internal_transactions t WHERE block_number=10), expanded AS MATERIALIZED (SELECT jsonb_populate_record(NULL::internal_transactions,seed.row||jsonb_build_object('block_number',1,'block_index',1000000+g,'index',1000000+g,'trace_address',NULL,'value',NULL,'call_type',NULL,'call_type_enum','call')) AS r FROM seed CROSS JOIN generate_series(0,99999) g) INSERT INTO internal_transactions SELECT (r).* FROM expanded")
    sql('ANALYZE internal_transactions')
    row = json.loads(G.run(['docker', 'inspect', pg]))[0]
    identity = {'database': 'fixture', 'oid': int(sql("SELECT oid FROM pg_database WHERE datname='fixture'").strip()),
                'address': row['NetworkSettings']['Networks'][network]['IPAddress'], 'port': 5432, 'postgres_major': 15}
    manifest = {'chain_id': '18417507517'}
    runtime = {'backend': {'Id': 'a' * 64, 'Image': image}}
    def run(command, **kwargs):
        return '' if command[0] == 'git' else G.run(command)
    api = types.SimpleNamespace(require=J.P.require, configuration=lambda *a: (manifest, {}, {}), run=run,
                                inspect_project=lambda *a: deepcopy(runtime), ensure_stable=J.D.ensure_stable)
    upgrade = {'candidate_image': image, 'candidate_source_commit': 'a' * 40, 'network': network,
               'backend_environment': environment, 'migration_payload': {'database_identity': identity,
                    'heap_identity': int(sql("SELECT pg_relation_filenode('internal_transactions'::regclass)").strip())},
               'applied_result': {'applied_versions': list(map(int, sql('SELECT version FROM schema_migrations ORDER BY version').splitlines()))}}
    def collect(api, root, path, before_block=None, batch_sample=False, projector=None):
        result = F.inspect(image, network, token, environment, before_block=before_block,
                           batch_sample=batch_sample, projector=projector)
        return {'environment': 'dev', 'chain_id': manifest['chain_id'], 'upgrade_state': str(path),
                'candidate_image': image, 'planning_source_commit': 'a' * 40,
                'automatic_background_migrations_held': True, **result}
    paths = (directory / 'normalization').resolve()
    paths.mkdir(mode=0o700)
    # The real clients/queries, private journals, receipt hashes, cursor logic
    # and commit checkpoints execute. Only host release/runtime health is stubbed.
    with patch.object(J.P, 'BASE', paths / 'batches'), patch.object(J.S, 'BASE', paths / 'samples'), \
            patch.object(J, 'BASE', paths / 'runs'), patch.object(J.B, 'clean_source', return_value=('a' * 40, '10.2.6')), \
            patch.object(J.P, 'context', return_value=(manifest, upgrade)), \
            patch.object(J.P, 'free_bytes', return_value=888 * 1024**3), \
            patch.object(J.S.N, 'collect', side_effect=collect), contextlib.redirect_stdout(io.StringIO()):
        initial = J.P.prepare(api, ROOT, directory / 'upgrade.json', 200, 1)
        initial_path = Path(initial['state'])
        J.P.apply(api, ROOT, initial_path, manifest)
        prepared = J.prepare(api, ROOT, initial_path, 5, 5000)
        path = Path(prepared['state'])
        result = J.execute(api, ROOT, path, manifest)
        assert result['completed_batches'] == 5 and result['selected_rows_verified'] == 5000
        assert result['updated_rows_observed'] == 5000 and result['next_before_block'] == 110
        assert result['stop_reason'] == 'batch_limit_reached' and not result['global_normalization_complete']
        state = J.load(api, path, manifest)
        charged = 0
        for entry in state['batches']:
            child = J.P.load(api, Path(entry['batch_state']), manifest)
            assert entry['rows'] == 1000 and child['phase'] == 'applied'
            assert J.P.wal_limit(child) == J.WAL_BUDGET - charged
            assert child['space_before'] == state['inputs']['space_before']
            charged += entry['charged_wal_bytes']
        assert 0 < charged <= J.WAL_BUDGET
        fingerprint = 'SELECT md5(jsonb_agg(to_jsonb(it) ORDER BY block_number,"index")::text) FROM internal_transactions it WHERE block_number BETWEEN 100 AND 159'
        before = sql(fingerprint)
        resumed = J.execute(api, ROOT, path, manifest)
        assert resumed['completed_batches'] == 5 and sql(fingerprint) == before
        needs = '(trace_address IS NOT NULL OR value=0 OR (call_type_enum IS NULL AND call_type IS NOT NULL) OR error IS NOT NULL OR (created_contract_address_hash IS NOT NULL AND to_address_hash IS NULL)) IS TRUE'
        assert sql('SELECT count(*) FROM internal_transactions WHERE block_number BETWEEN 100 AND 159 AND ' + needs).strip() == '1000'
        assert sql("SELECT count(*) FROM migrations_status WHERE migration_name='empty_internal_transactions_data'").strip() == '0'
        assert sql("SELECT status FROM migrations_status WHERE migration_name='reindex_duplicated_internal_transactions'").strip() == 'completed'
    return {'status': 'passed', 'real_clients_and_database_queries': True, 'batches_committed': 5, 'rows_normalized': 5000,
            'next_cursor_checked': True, 'excluded_boundary_blocks_revisited': True,
            'per_child_remaining_wal_and_shared_space_limits_checked': True,
            'finished_resume_did_not_update_again': True, 'remaining_rows_needing_work_in_seeded_range': 1000,
            'migration_completion_records_unchanged': True, 'global_normalization_marked_complete': False,
            'application_or_worker_started': False, 'host_runtime_health_stubbed': True, 'live_services_accessed': False,
            'synthetic_observed_chunk_wal_sum_bytes': result['observed_chunk_wal_sum_bytes']}


if __name__ == '__main__':
    raise SystemExit(F.main(pilot_check=check))
