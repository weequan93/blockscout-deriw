#!/usr/bin/env python3
"""Verify sequential native block repairs on disposable local PG15 only."""
import json
from pathlib import Path
import subprocess
import time
import uuid

import check_v10_normalization_plan as F
import check_upgrade_database as G
import v10_native_oversized_block_repair as R

ROOT = Path(__file__).resolve().parents[2]
U = F.U


def check(image, network, token, environment, sql, pg, directory):
    def clone(block, count, offset):
        sql("INSERT INTO internal_transactions SELECT (jsonb_populate_record(NULL::internal_transactions,"
            "to_jsonb(t)||jsonb_build_object('block_number'," + str(block) + ", 'block_index',g,'index',g))).* "
            "FROM (SELECT * FROM internal_transactions WHERE block_number=20 LIMIT 1) t CROSS JOIN "
            f"generate_series({offset},{offset + count - 1}) g")
    existing = int(sql('SELECT count(*) FROM internal_transactions WHERE block_number=20').strip())
    clone(20, 10147 - existing, 100000)
    clone(19, 10147, 200000)
    clone(18, 12001, 300000)
    sql('CREATE INDEX native_repair_fixture_block ON internal_transactions(block_number)')
    sql("CREATE FUNCTION native_repair_reject_overflow() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "IF NEW.block_number IN (18,19) THEN RAISE EXCEPTION 'unapproved_callback_update'; END IF; RETURN NEW; END $$")
    sql('CREATE TRIGGER native_repair_reject_overflow BEFORE UPDATE ON internal_transactions '
        'FOR EACH ROW EXECUTE FUNCTION native_repair_reject_overflow()')
    row = json.loads(G.run(['docker', 'inspect', pg]))[0]
    payload = {
        'token': token, 'batch_blocks': 1, 'start_before_block': 21,
        'application_name': 'blockscout-v10-native-' + token,
        'database_identity': {'database': 'fixture',
            'oid': int(sql("SELECT oid FROM pg_database WHERE datname='fixture'").strip()),
            'address': row['NetworkSettings']['Networks'][network]['IPAddress'],
            'port': 5432, 'postgres_major': 15},
        'heap_identity': int(sql("SELECT pg_relation_filenode('internal_transactions'::regclass)").strip()),
        'applied_versions': list(map(int, sql('SELECT version FROM schema_migrations ORDER BY version').splitlines())),
        'native_status_before': None,
        'table_options_before': json.loads(sql("SELECT COALESCE(to_jsonb(reloptions),'[]'::jsonb) "
                                               "FROM pg_class WHERE oid='internal_transactions'::regclass"))}
    original = (ROOT / 'deploy/scripts/v10/native_normalization.exs').read_text()
    names, processes = [], []

    def exercise(block):
        name = 'native-repair-fixture-' + uuid.uuid4().hex[:12]
        names.append(name)
        log = (directory / (name + '.log.private')).open('wb')
        command = U.client_command(image, network, name, token)
        command[2:2] = ['--label', G.LABEL + '=' + token]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=log, stderr=log)
        processes.append((process, log))
        source = R.repair_source(original, block).encode()
        process.stdin.write(U.archive(environment, payload, source))
        process.stdin.close()
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('Repair fixture exited; inspect ' + str(log.name))
            probe = subprocess.run(['docker', 'exec', name, 'cat', '/tmp/native-status.json'],
                                   capture_output=True, text=True, timeout=5)
            if probe.returncode == 0 and probe.stdout:
                status = json.loads(probe.stdout)
                if status['phase'] == 'needs_attention':
                    G.cleanup_container(name, token)
                    return status
                lease = {'token': token, 'mode': 'run', 'batch_blocks': 1,
                         'valid_until_unix_ms': int(time.time() * 1000) + 30000}
                G.run(['docker', 'exec', '-i', name, 'sh', '-c',
                       'umask 077; cat > /tmp/native-lease.tmp; mv /tmp/native-lease.tmp /tmp/native-lease.json'],
                      input=json.dumps(lease))
            time.sleep(0.5)
        raise RuntimeError('Repair fixture timed out; inspect ' + str(log.name))

    def remaining(block):
        return int(sql(f'SELECT count(*) FROM internal_transactions WHERE block_number={block} '
                       'AND (trace_address IS NOT NULL OR value=0 OR '
                       '(call_type_enum IS NULL AND call_type IS NOT NULL) OR error IS NOT NULL OR '
                       '(created_contract_address_hash IS NOT NULL AND to_address_hash IS NULL))').strip())

    def cursor():
        return int(sql("SELECT meta->>'max_block_number' FROM migrations_status "
                       "WHERE migration_name='empty_internal_transactions_data'").strip())

    try:
        first = exercise(20)
        assert first['reason'] == 'native_batch_requires_smaller_range', first
        assert remaining(20) == 0 and remaining(19) == 10147 and cursor() == 19
        sql("CREATE OR REPLACE FUNCTION native_repair_reject_overflow() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            "IF NEW.block_number=18 THEN RAISE EXCEPTION 'unapproved_callback_update'; END IF; RETURN NEW; END $$")
        second = exercise(19)
        assert second['reason'] == 'native_batch_requires_smaller_range', second
        assert remaining(19) == 0 and remaining(18) == 12001 and cursor() == 18
        third = exercise(18)
        assert third['reason'] == 'native_batch_requires_smaller_range', third
        assert remaining(18) == 12001 and cursor() == 18
        return {'status': 'passed', 'local_pg15_only': True, 'live_services_accessed': False,
                'first_10147_row_block_committed_and_checkpointed': True,
                'second_10147_row_block_committed_and_checkpointed': True,
                'unapproved_block_above_10000_rejected_before_update': True,
                'approved_block_above_12000_rejected_before_update': True,
                'rejected_block_rows_and_cursor_unchanged': True}
    finally:
        for name in names:
            G.cleanup_container(name, token)
        for process, log in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            log.close()


if __name__ == '__main__':
    raise SystemExit(F.main(pilot_check=check))
