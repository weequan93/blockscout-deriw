#!/usr/bin/env python3
"""Verify bounded row transactions, partial replay and semantics on local PG15."""
import json
from pathlib import Path
import subprocess
import time
import uuid

import check_v10_normalization_plan as F
import check_upgrade_database as G
import v10_native_row_chunk_repair as C

ROOT = Path(__file__).resolve().parents[2]
U = F.U


def check(image, network, token, environment, sql, pg, directory):
    def clone(block, count, offset):
        sql("INSERT INTO internal_transactions SELECT (jsonb_populate_record(NULL::internal_transactions,"
            "to_jsonb(t)||jsonb_build_object('block_number'," + str(block) + ", 'block_index',g,'index',g))).* "
            "FROM (SELECT * FROM internal_transactions WHERE block_number=20 LIMIT 1) t CROSS JOIN "
            f"generate_series({offset},{offset + count - 1}) g")
    # A valid transitional error row: the legacy text takes precedence over
    # its old error_id. The callback must create/map the text's error record,
    # and output must be NULL both before and after that mapping.
    sql("INSERT INTO transaction_errors(message,inserted_at) VALUES('native_row_fixture_old_error',now())")
    sql("UPDATE internal_transactions SET error='native_row_fixture_error',output=NULL, "
        "error_id=(SELECT id FROM transaction_errors WHERE message='native_row_fixture_old_error') "
        "WHERE block_number=20")
    existing = int(sql('SELECT count(*) FROM internal_transactions WHERE block_number=20').strip())
    clone(20, 25001 - existing, 100000)
    clone(19, 30001, 200000)
    sql('CREATE INDEX native_row_fixture_block ON internal_transactions(block_number)')
    others = json.loads(sql("SELECT COALESCE(jsonb_agg(to_jsonb(s) ORDER BY migration_name),'[]'::jsonb) "
                           "FROM migrations_status s WHERE migration_name<>'empty_internal_transactions_data'"))
    semantic = """(to_jsonb(it) - ARRAY['trace_address','value','error','error_id','call_type','call_type_enum','to_address_hash']
      || jsonb_build_object('value',CASE WHEN it.value=0 THEN NULL ELSE it.value END,
      'effective_error',COALESCE(it.error,te.message),'effective_call_type',COALESCE(it.call_type_enum::text,it.call_type),
      'to_address_hash',COALESCE(it.to_address_hash,it.created_contract_address_hash)::text))"""
    def digest():
        return sql("SELECT md5(string_agg(md5((" + semantic + ")::text),',' ORDER BY it.block_hash,it.block_index)) "
                   "FROM internal_transactions it LEFT JOIN transaction_errors te ON te.id=it.error_id").strip()
    before = digest()
    errors_before = sql('SELECT count(*) FROM transaction_errors').strip()
    needs = ("trace_address IS NOT NULL OR value=0 OR (call_type_enum IS NULL AND call_type IS NOT NULL) "
             "OR error IS NOT NULL OR (created_contract_address_hash IS NOT NULL AND to_address_hash IS NULL)")
    expected = int(sql('SELECT count(*) FROM internal_transactions WHERE ' + needs).strip())
    sql('CREATE TABLE native_row_fixture_updates(block_hash bytea, block_index integer, transaction_id bigint)')
    sql("CREATE FUNCTION native_row_fixture_updated() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "INSERT INTO native_row_fixture_updates VALUES(NEW.block_hash,NEW.block_index,txid_current()); RETURN NEW; END $$")
    sql('CREATE TRIGGER native_row_fixture_updated AFTER UPDATE ON internal_transactions '
        'FOR EACH ROW EXECUTE FUNCTION native_row_fixture_updated()')
    sql("CREATE FUNCTION native_row_fixture_fail() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "IF NEW.index=100100 THEN RAISE EXCEPTION 'synthetic_chunk_failure'; END IF; RETURN NEW; END $$")
    sql('CREATE TRIGGER native_row_fixture_fail BEFORE UPDATE ON internal_transactions '
        'FOR EACH ROW EXECUTE FUNCTION native_row_fixture_fail()')
    row = json.loads(G.run(['docker', 'inspect', pg]))[0]
    payload = {'token': token, 'batch_blocks': 1, 'start_before_block': 21,
        'application_name': 'blockscout-v10-native-' + token,
        'database_identity': {'database': 'fixture',
            'oid': int(sql("SELECT oid FROM pg_database WHERE datname='fixture'").strip()),
            'address': row['NetworkSettings']['Networks'][network]['IPAddress'], 'port': 5432, 'postgres_major': 15},
        'heap_identity': int(sql("SELECT pg_relation_filenode('internal_transactions'::regclass)").strip()),
        'applied_versions': list(map(int, sql('SELECT version FROM schema_migrations ORDER BY version').splitlines())),
        'native_status_before': None,
        'table_options_before': json.loads(sql("SELECT COALESCE(to_jsonb(reloptions),'[]'::jsonb) FROM pg_class "
                                               "WHERE oid='internal_transactions'::regclass"))}
    source = C.source((ROOT / 'deploy/scripts/v10/native_normalization.exs').read_text()).encode()
    names, processes = [], []

    def status(name):
        return json.loads(G.run(['docker', 'exec', name, 'cat', '/tmp/native-status.json']))

    def start(batch_blocks=1, worker_source=source):
        name = 'native-row-fixture-' + uuid.uuid4().hex[:12]
        names.append(name)
        log = (directory / (name + '.log.private')).open('wb')
        command = U.client_command(image, network, name, token)
        command[2:2] = ['--label', G.LABEL + '=' + token]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=log, stderr=log)
        processes.append((process, log))
        process.stdin.write(U.archive(environment, {**payload, 'batch_blocks': batch_blocks}, worker_source))
        process.stdin.close()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('Row fixture exited; inspect ' + str(log.name))
            probe = subprocess.run(['docker', 'exec', name, 'cat', '/tmp/native-status.json'],
                                   capture_output=True, text=True, timeout=5)
            if probe.returncode == 0 and probe.stdout:
                value = json.loads(probe.stdout)
                if value['phase'] == 'needs_attention':
                    raise RuntimeError('Row fixture failed: ' + str(value) + '; inspect ' + str(log.name))
                if value['phase'] == 'waiting':
                    return name
            time.sleep(0.2)
        raise RuntimeError('Row fixture startup timed out; inspect ' + str(log.name))

    def grant(name, milliseconds=30000, batch_blocks=1):
        lease = {'token': token, 'mode': 'run', 'batch_blocks': batch_blocks,
                 'valid_until_unix_ms': int(time.time() * 1000) + milliseconds}
        G.run(['docker', 'exec', '-i', name, 'sh', '-c',
               'umask 077; cat > /tmp/native-lease.tmp; mv /tmp/native-lease.tmp /tmp/native-lease.json'],
              input=json.dumps(lease))

    def cursor():
        return int(sql("SELECT meta->>'max_block_number' FROM migrations_status "
                       "WHERE migration_name='empty_internal_transactions_data'").strip())

    def updates():
        return int(sql('SELECT count(*) FROM native_row_fixture_updates').strip())

    def wait_for(name, phase, seconds=30, renew=False):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = status(name)
            if value['phase'] == phase:
                return value
            if value['phase'] == 'needs_attention':
                raise RuntimeError('Row fixture stopped: ' + str(value) + '; logs ' + str(directory))
            if renew:
                grant(name)
            time.sleep(0.2)
        raise RuntimeError('Row fixture phase timed out: ' + str(value) + '; logs ' + str(directory))

    try:
        multi = start(batch_blocks=2)
        grant(multi, batch_blocks=2)
        bounded = wait_for(multi, 'needs_attention')
        assert bounded['reason'] == 'native_batch_requires_smaller_range', bounded
        assert updates() == 0 and cursor() == 20 and digest() == before
        G.cleanup_container(multi, token)
        first = start()
        assert updates() == 0 and cursor() == 20
        grant(first)
        failure = wait_for(first, 'needs_attention')
        assert failure['reason'] == 'native_database_error', failure
        assert updates() == 0 and cursor() == 20 and digest() == before
        assert sql('SELECT count(*) FROM transaction_errors').strip() == errors_before
        G.cleanup_container(first, token)
        sql('DROP TRIGGER native_row_fixture_fail ON internal_transactions')
        # Force only this local fixture's post-callback WAL check to fail. This
        # proves the nested native callback cannot commit outside the outer
        # chunk transaction when a resource guard rejects its completed work.
        wal_guard = b'unless wal <= 134217728, do: Guard.fail!("native_transaction_wal_limit")'
        assert source.count(wal_guard) == 1
        forced = start(worker_source=source.replace(wal_guard, wal_guard.replace(b'134217728', b'0')))
        grant(forced)
        rejected = wait_for(forced, 'needs_attention')
        assert rejected['reason'] == 'native_transaction_wal_limit', rejected
        assert updates() == 0 and cursor() == 20 and digest() == before
        assert sql('SELECT count(*) FROM transaction_errors').strip() == errors_before
        G.cleanup_container(forced, token)
        sql("CREATE FUNCTION native_row_fixture_delay() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            "PERFORM pg_sleep(0.001); RETURN NEW; END $$")
        sql('CREATE TRIGGER native_row_fixture_delay BEFORE UPDATE ON internal_transactions '
            'FOR EACH ROW EXECUTE FUNCTION native_row_fixture_delay()')
        second = start()
        grant(second, 5000)
        deadline = time.monotonic() + 20
        while updates() == 0:
            if time.monotonic() > deadline:
                raise RuntimeError('Row fixture did not commit its first chunk; logs ' + str(directory))
            time.sleep(0.2)
        wait_for(second, 'waiting')
        assert updates() == 5000 and cursor() == 20
        assert sql("SELECT count(*) FROM internal_transactions WHERE block_number=20 AND (" + needs + ")").strip() == '20001'
        assert sql("SELECT count(*) FROM pg_stat_activity WHERE application_name='" + payload['application_name'] +
                   "' AND (backend_xid IS NOT NULL OR backend_xmin IS NOT NULL)").strip() == '0'
        G.cleanup_container(second, token)
        sql('DROP TRIGGER native_row_fixture_delay ON internal_transactions')
        third = start()
        assert updates() == 5000 and cursor() == 20
        grant(third)
        wait_for(third, 'completed', seconds=90, renew=True)
        assert cursor() == -1
        assert sql("SELECT status FROM migrations_status WHERE migration_name='empty_internal_transactions_data'").strip() == 'completed'
        assert updates() == expected and digest() == before
        assert sql('SELECT count(*) FROM (SELECT block_hash,block_index FROM native_row_fixture_updates '
                   'GROUP BY 1,2 HAVING count(*)>1) s').strip() == '0'
        assert int(sql('SELECT max(n) FROM (SELECT count(*) n FROM native_row_fixture_updates '
                       'GROUP BY transaction_id) s').strip()) == 5000
        assert sql('SELECT count(*) FROM internal_transactions WHERE ' + needs).strip() == '0'
        assert sql('SELECT count(*) FROM internal_transactions WHERE block_number=11 AND error IS NULL AND error_id IS NOT NULL').strip() == '1'
        assert sql('SELECT count(*) FROM internal_transactions WHERE block_number=12 AND to_address_hash=created_contract_address_hash').strip() == '1'
        assert json.loads(sql("SELECT COALESCE(jsonb_agg(to_jsonb(s) ORDER BY migration_name),'[]'::jsonb) "
                              "FROM migrations_status s WHERE migration_name<>'empty_internal_transactions_data'")) == others
        return {'status': 'passed', 'local_pg15_only': True, 'live_services_accessed': False,
                'maximum_updated_rows_per_transaction': 5000,
                'multi_block_10000_row_guard_retained': True,
                'failed_chunk_rolled_back_rows_errors_and_cursor': True,
                'post_callback_wal_rejection_rolled_back_rows_errors_and_cursor': True,
                'expired_lease_waits_without_a_database_snapshot': True,
                'partially_committed_block_kept_native_cursor_until_drained': True,
                'partial_block_restart_did_not_repeat_updates': True,
                'successive_25001_and_30001_row_blocks_completed_without_new_exceptions': True,
                'effective_values_errors_call_types_destinations_and_other_columns_preserved': True,
                'completed_via_native_framework_and_residual_check': True,
                'other_migration_states_unchanged': True}
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
