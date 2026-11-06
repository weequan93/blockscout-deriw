#!/usr/bin/env python3
"""Exercise native DB checkpoints, lease pauses and crash recovery on local PG15."""
import json
from pathlib import Path
import subprocess
import time
import uuid

import check_v10_normalization_plan as F
import check_upgrade_database as G

ROOT = Path(__file__).resolve().parents[2]
U = F.U


def check(image, network, token, environment, sql, pg, directory):
    other_statuses = json.loads(sql("SELECT COALESCE(jsonb_agg(to_jsonb(s) ORDER BY migration_name),'[]'::jsonb) FROM migrations_status s WHERE migration_name<>'empty_internal_transactions_data'"))
    sql("INSERT INTO internal_transactions SELECT (jsonb_populate_record(NULL::internal_transactions,to_jsonb(t)||jsonb_build_object('block_index',10000+g,'index',10000+g))).* FROM (SELECT * FROM internal_transactions WHERE block_number=20 LIMIT 1) t CROSS JOIN generate_series(0,3500) g")
    row = json.loads(G.run(['docker', 'inspect', pg]))[0]
    payload = {
        'token': token, 'batch_blocks': 10, 'start_before_block': 21,
        'application_name': 'blockscout-v10-native-' + token,
        'database_identity': {'database': 'fixture',
            'oid': int(sql("SELECT oid FROM pg_database WHERE datname='fixture'").strip()),
            'address': row['NetworkSettings']['Networks'][network]['IPAddress'],
            'port': 5432, 'postgres_major': 15},
        'heap_identity': int(sql("SELECT pg_relation_filenode('internal_transactions'::regclass)").strip()),
        'applied_versions': list(map(int, sql('SELECT version FROM schema_migrations ORDER BY version').splitlines())),
        'native_status_before': None,
        'table_options_before': json.loads(sql("SELECT COALESCE(to_jsonb(reloptions),'[]'::jsonb) FROM pg_class WHERE oid='internal_transactions'::regclass"))}
    expected = int(sql("SELECT count(*) FROM internal_transactions WHERE trace_address IS NOT NULL OR value=0 OR (call_type_enum IS NULL AND call_type IS NOT NULL) OR error IS NOT NULL OR (created_contract_address_hash IS NOT NULL AND to_address_hash IS NULL)").strip())
    sql("CREATE TABLE native_fixture_updates(block_hash bytea, block_index integer)")
    sql("CREATE FUNCTION native_fixture_updated() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN INSERT INTO native_fixture_updates VALUES(NEW.block_hash,NEW.block_index); RETURN NEW; END $$")
    sql("CREATE TRIGGER native_fixture_updated AFTER UPDATE ON internal_transactions FOR EACH ROW EXECUTE FUNCTION native_fixture_updated()")
    sql("CREATE FUNCTION native_fixture_checkpoint() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF (NEW.meta->>'max_block_number')::bigint < (OLD.meta->>'max_block_number')::bigint THEN PERFORM pg_sleep(20); END IF; RETURN NEW; END $$")
    sql("CREATE TRIGGER native_fixture_checkpoint BEFORE UPDATE OF meta ON migrations_status FOR EACH ROW EXECUTE FUNCTION native_fixture_checkpoint()")
    names = []
    processes = []

    def start():
        name = 'native-fixture-' + uuid.uuid4().hex[:16]
        names.append(name)
        log = (directory / (name + '.log.private')).open('wb')
        command = U.client_command(image, network, name, token)
        command[2:2] = ['--label', G.LABEL + '=' + token]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=log, stderr=log)
        processes.append((process, log))
        process.stdin.write(U.archive(environment, payload, (ROOT / 'deploy/scripts/v10/native_normalization.exs').read_bytes()))
        process.stdin.close()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('Native fixture exited; inspect private fixture log ' + str(log.name))
            p = subprocess.run(['docker', 'exec', name, 'sh', '-c',
                'test ! -f /tmp/native-status.json || cat /tmp/native-status.json'], capture_output=True, text=True)
            if p.returncode == 0 and p.stdout:
                status = json.loads(p.stdout)
                if status['phase'] == 'needs_attention':
                    raise RuntimeError('Native fixture requires review: ' + str(status.get('reason')))
                return name
            time.sleep(0.2)
        raise RuntimeError('Native fixture startup timed out')

    def grant(name):
        data = {'token': token, 'mode': 'run', 'valid_until_unix_ms': int(time.time() * 1000) + 30000}
        G.run(['docker', 'exec', '-i', name, 'sh', '-c',
               'umask 077; cat > /tmp/native-lease.json'], input=json.dumps(data))

    def stop(name):
        G.cleanup_container(name, token)

    def status(name):
        return json.loads(G.run(['docker', 'exec', name, 'cat', '/tmp/native-status.json']))

    try:
        first = start()
        deadline = time.monotonic() + 5
        while status(first)['phase'] != 'waiting':
            if time.monotonic() > deadline:
                raise RuntimeError('Native fixture did not wait for its initial lease')
            time.sleep(0.1)
        assert sql('SELECT count(*) FROM native_fixture_updates').strip() == '0'
        assert sql("SELECT count(*) FROM pg_stat_activity WHERE application_name='" + payload['application_name'] + "' AND (backend_xid IS NOT NULL OR backend_xmin IS NOT NULL)").strip() == '0'
        assert sql("SELECT meta->>'max_block_number' FROM migrations_status WHERE migration_name='empty_internal_transactions_data'").strip() == '20'
        grant(first)
        deadline = time.monotonic() + 15
        while sql('SELECT count(*) FROM native_fixture_updates').strip() == '0':
            if time.monotonic() > deadline:
                raise RuntimeError('Native fixture did not update its first block range')
            time.sleep(0.2)
        # The callback committed, while the native cursor UPDATE is sleeping.
        assert sql("SELECT meta->>'max_block_number' FROM migrations_status WHERE migration_name='empty_internal_transactions_data'").strip() == '20'
        stop(first)
        sql('DROP TRIGGER native_fixture_checkpoint ON migrations_status')
        updates_before_restart = int(sql('SELECT count(*) FROM native_fixture_updates').strip())
        assert updates_before_restart > 0
        # A row above the transferred cursor must prevent a false completion.
        sql("INSERT INTO internal_transactions SELECT (jsonb_populate_record(NULL::internal_transactions,to_jsonb(t)||jsonb_build_object('block_number',9999,'block_index',900001,'index',900001,'value',0))).* FROM internal_transactions t WHERE block_number=10 LIMIT 1")
        second = start()
        assert sql("SELECT meta->>'max_block_number' FROM migrations_status WHERE migration_name='empty_internal_transactions_data'").strip() == '20'
        assert int(sql('SELECT count(*) FROM native_fixture_updates').strip()) == updates_before_restart
        grant(second)
        deadline = time.monotonic() + 30
        while status(second)['phase'] != 'needs_attention':
            grant(second)
            if time.monotonic() > deadline:
                raise RuntimeError('Native fixture did not reach its residual review')
            time.sleep(0.2)
        assert status(second)['reason'] == 'native_residual_rows_require_review'
        assert sql("SELECT status FROM migrations_status WHERE migration_name='empty_internal_transactions_data'").strip() == 'started'
        assert sql("SELECT meta->>'max_block_number' FROM migrations_status WHERE migration_name='empty_internal_transactions_data'").strip() == '-1'
        assert int(sql('SELECT count(*) FROM native_fixture_updates').strip()) == expected
        assert sql('SELECT count(*) FROM (SELECT block_hash,block_index FROM native_fixture_updates GROUP BY 1,2 HAVING count(*)>1) s').strip() == '0'
        stop(second)
        sql('DELETE FROM internal_transactions WHERE block_number=9999')
        third = start()
        grant(third)
        deadline = time.monotonic() + 20
        while status(third)['phase'] != 'completed':
            if time.monotonic() > deadline:
                raise RuntimeError('Native fixture did not complete after residual correction')
            time.sleep(0.2)
        assert sql("SELECT status FROM migrations_status WHERE migration_name='empty_internal_transactions_data'").strip() == 'completed'
        assert int(sql('SELECT count(*) FROM native_fixture_updates').strip()) == expected
        assert sql('SELECT count(*) FROM internal_transactions WHERE block_number=11 AND error IS NULL AND error_id IS NOT NULL').strip() == '1'
        assert sql('SELECT count(*) FROM internal_transactions WHERE block_number=12 AND to_address_hash=created_contract_address_hash').strip() == '1'
        assert json.loads(sql("SELECT COALESCE(jsonb_agg(to_jsonb(s) ORDER BY migration_name),'[]'::jsonb) FROM migrations_status s WHERE migration_name<>'empty_internal_transactions_data'")) == other_statuses
        options = json.loads(sql("SELECT to_jsonb(reloptions) FROM pg_class WHERE oid='internal_transactions'::regclass"))
        assert {'autovacuum_vacuum_scale_factor=0', 'autovacuum_vacuum_threshold=5000000', 'autovacuum_vacuum_cost_limit=400', 'autovacuum_vacuum_cost_delay=2'} <= set(options)
        return {'status': 'passed', 'native_filling_migration_used': True,
                'native_update_callback_used': True, 'missing_lease_pauses_before_updates': True,
                'committed_data_with_lost_cursor_checkpoint_replayed_without_second_update': True,
                'database_cursor_preserved_across_container_restart': True,
                'residual_rows_prevented_completion': True,
                'completion_reached_through_native_framework_after_residual_correction': True,
                'table_autovacuum_configured': True, 'other_heavy_migrations_not_started': True,
                'live_services_accessed': False}
    finally:
        for name in names:
            stop(name)
        for process, log in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            log.close()


if __name__ == '__main__':
    raise SystemExit(F.main(pilot_check=check))
