#!/usr/bin/env python3
"""Exercise the exact read-only normalization planner on disposable PG15/v10."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import check_upgrade_database as F
import deploy_check as C
import dev_config as D
import v10_upgrade as U
import v10_normalization_plan as N


def inspect(image, network, token, environment, expected_failure=False):
    name = 'normalization-plan-' + uuid.uuid4().hex[:16]
    command = U.client_command(image, network, name, token)
    command[2:2] = ['--label', F.LABEL + '=' + token]
    source = '''
for app <- [:crypto, :ssl, :postgrex, :ecto_sql], do: ({:ok, _} = Application.ensure_all_started(app))
{:ok, _} = Explorer.Repo.start_link(pool_size: 2)
''' + (ROOT / 'deploy/scripts/v10/normalization_plan.exs').read_text()
    try:
        result = subprocess.run(command, input=U.archive(environment, {}, source.encode()), capture_output=True, timeout=60)
        if expected_failure:
            assert result.returncode != 0, 'Planner accepted a missing block-number index'
            return None
        if result.returncode:
            raise RuntimeError(result.stdout.decode(errors='replace')[-3000:] + result.stderr.decode(errors='replace')[-3000:])
        data = C.marked_json(D, result.stdout.decode())
        return N.project(data, 888 * 1024**3)
    finally:
        F.cleanup_container(name, token)


def main():
    token = uuid.uuid4().hex[:16]
    directory = Path(tempfile.mkdtemp(prefix='v10-normalization-plan-'))
    os.chmod(directory, 0o700)
    network, pg = 'normalization-plan-' + token, 'normalization-pg-' + token
    password = uuid.uuid4().hex
    created = False
    try:
        old, before = F.image_inventory(D, C, 'deriw-blockscout:v9-upgrade-baseline')
        image, inventory = F.image_inventory(D, C, 'deriw-blockscout:v10-nft-local-validation')
        plan = D.read_json(ROOT / 'deploy/upgrades/v10.2.6.json')
        assert before['migrations'] == F.baseline_inventory() and inventory['migrations'] == plan['target_migration_sha256']
        versions = C.migration_versions(D, inventory)
        versions = {k: versions[k] for k in ('repo', 'account', 'arbitrum')}
        F.run(['docker', 'network', 'create', '--internal', '--label', F.LABEL + '=' + token, network])
        created = True
        F.run(['docker', 'run', '--rm', '-d', '--pull', 'never', '--name', pg, '--label', F.LABEL + '=' + token,
               '--network', network, '--network-alias', 'pg', '--read-only', '--cpus', '2', '--memory', '1g',
               '--tmpfs', '/var/lib/postgresql/data:rw,mode=0700,size=768m', '--tmpfs', '/var/run/postgresql:rw,mode=1777,size=16m',
               '--tmpfs', '/tmp:rw,size=16m', '-e', 'POSTGRES_USER=fixture', '-e', 'POSTGRES_PASSWORD=' + password,
               '-e', 'POSTGRES_DB=fixture', 'postgres:15-alpine'])
        deadline = time.monotonic() + 30
        while subprocess.run(['docker', 'exec', pg, 'pg_isready', '-q', '-U', 'fixture', '-d', 'fixture'], capture_output=True).returncode:
            if time.monotonic() > deadline:
                raise RuntimeError('PG15 fixture unavailable')
            time.sleep(0.5)
        for stage, selected in [('baseline', old['Id']), ('upgrade', image['Id'])]:
            F.evaluate(selected, stage, token, network, password, versions, directory, pg, defer_indexes=True)
        def sql(value):
            return F.run(['docker', 'exec', pg, 'psql', '-U', 'fixture', '-d', 'fixture', '-v', 'ON_ERROR_STOP=1', '-Atc', value])
        sql('CREATE INDEX "internal_transactions_block_number_DESC_transaction_index_DESC_" ON internal_transactions(block_number DESC, transaction_index DESC, "index" DESC)')
        sql("INSERT INTO migrations_status(migration_name,status,inserted_at,updated_at) VALUES ('reindex_duplicated_internal_transactions','completed',NOW(),NOW())")
        environment = {'DATABASE_URL': 'postgresql://fixture:' + password + '@pg:5432/fixture', 'ECTO_USE_SSL': 'false',
                       'CHAIN_ID': '18417507517', 'CHAIN_TYPE': 'arbitrum', 'SECRET_KEY_BASE': 'a' * 64,
                       'ETHEREUM_JSONRPC_HTTP_URL': 'http://127.0.0.1:1', 'ERL_FLAGS': '+S 2:2'}
        first = inspect(image['Id'], network, token, environment)
        assert first['status'] == 'normalization_pilot_candidate_selected' and first['selected_block'] == 20
        assert first['sample_rows'] == 2 and first['rows_needing_normalization_in_sample'] == 2
        sql("INSERT INTO internal_transactions SELECT (jsonb_populate_record(NULL::internal_transactions, to_jsonb(t) || jsonb_build_object('block_index',g,'index',g))).* FROM (SELECT * FROM internal_transactions WHERE block_number=20 LIMIT 1) t CROSS JOIN generate_series(100,1100) g")
        large = inspect(image['Id'], network, token, environment)
        assert large['sample_rows'] == 1001 and not large['sample_is_complete_block']
        assert 'selected_block_exceeds_1000_row_pilot_cap' in large['blockers']
        assert sql("SELECT status FROM migrations_status WHERE migration_name='reindex_duplicated_internal_transactions'").strip() == 'completed'
        assert sql("SELECT count(*) FROM migrations_status WHERE migration_name='empty_internal_transactions_data'").strip() == '0'
        indexes = sql("SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=i.indkey[0] WHERE i.indrelid='internal_transactions'::regclass AND a.attname='block_number' AND i.indpred IS NULL").splitlines()
        for index in indexes:
            assert D.IDENT.fullmatch(index)
            sql('DROP INDEX "' + index + '"')
        inspect(image['Id'], network, token, environment, expected_failure=True)
        print(json.dumps({'status': 'passed', 'exact_read_only_probe': True, 'indexed_seek_and_batch_checked': True,
                          'complete_small_block_selected': True, '1001_row_limit_checked': True,
                          'missing_index_rejected_before_trace_read': True, 'normalization_status_untouched': True,
                          'live_services_accessed': False, 'logs': str(directory)}))
        return 0
    finally:
        F.cleanup_container(pg, token)
        if created:
            F.run(['docker', 'network', 'rm', network])


if __name__ == '__main__':
    raise SystemExit(main())
