#!/usr/bin/env python3
"""Validate diagnostic SQL/templates/privacy against disposable PG15/v10."""
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
import v10_normalization_session as J
import v10_upgrade as U


def check(image, network, token, environment, sql, pg, directory):
    # Thirty active transactions below the 300-second write guard. The early
    # diagnostic must find the oldest twenty without waiting for that guard.
    fake = "SELECT 1000000+g AS pid,'active'::text AS state,clock_timestamp()-make_interval(secs=>120+g) AS xact_start,clock_timestamp()-interval '1 hour' AS backend_start,clock_timestamp()-make_interval(secs=>120+g) AS query_start,'3'::xid AS backend_xmin,NULL::xid AS backend_xid,'IO'::text AS wait_event_type,'DataFileRead'::text AS wait_event,current_user AS usename,'private-application-marker'::text AS application_name,'172.77.9.9'::inet AS client_addr,current_setting('test.diagnostic_query') AS query,current_database() AS datname,'client backend'::text AS backend_type FROM generate_series(1,30) g"
    expression = J.diagnosis_expression(ROOT).replace('FROM pg_stat_activity', 'FROM (' + fake + ') fake_activity')
    source = '''
for app <- [:crypto,:ssl,:postgrex,:ecto_sql], do: {:ok,_} = Application.ensure_all_started(app)
{:ok,_} = Explorer.Repo.start_link(pool_size: 1)
import Ecto.Query, only: [from: 2]
{sql,_args} = Ecto.Adapters.SQL.to_sql(:all,Explorer.Repo,from(b in Explorer.Chain.Block,select: count(b.hash),where: b.consensus == true))
Explorer.Repo.query!("SELECT set_config('test.diagnostic_query',$1,false)",[sql],log: false)
''' + expression
    name = 'normalization-diagnosis-' + uuid.uuid4().hex[:16]
    command = U.client_command(image, network, name, token)
    command[2:2] = ['--label', G.LABEL + '=' + token]
    before = sql('SELECT to_jsonb(s) FROM migrations_status s ORDER BY migration_name')
    try:
        result = subprocess.run(command, input=U.archive(environment, {}, source.encode()), capture_output=True, timeout=60)
        (directory / 'diagnosis.log').write_bytes(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError('Diagnostic fixture failed: ' + (result.stdout + result.stderr).decode(errors='replace')[-3500:])
        report = C.marked_json(D, result.stdout.decode())
        assert report['transactions']['client_transactions_over_300s'] == 0
        assert report['transactions']['oldest_transaction_seconds'] >= 150
        rows = report['query_diagnostics']
        assert len(rows) == 20 and rows[0]['pid'] == 1000030
        assert all(r['source_query_matches'] == ['blocks_consensus_count'] for r in rows)
        assert all(r['mentioned_relations'] == ['blocks'] and r['fingerprints']['count_aggregate'] for r in rows)
        runtime = {'stats': {'NetworkSettings': {'Networks': {'fixture': {'IPAddress': '172.77.9.9'}}}}}
        public = J.query_diagnostics(rows, runtime)
        assert all(r['client_service_candidates'] == ['stats'] and r['client_service_mapping'] == 'unique' for r in public)
        encoded = json.dumps(public)
        assert '172.77.9.9' not in encoded and 'private-application-marker' not in encoded
        assert 'SELECT count' not in encoded and 'client_address' not in encoded
        assert sql('SELECT to_jsonb(s) FROM migrations_status s ORDER BY migration_name') == before
        return {'status': 'passed', 'real_pg15_metadata_queries': True,
                'compiled_source_sql_template_match': True, 'early_60_second_transactions_observed': True,
                'twenty_detail_limit_enforced': True, 'connection_mapped_to_service': True,
                'sql_addresses_parameters_suppressed': True, 'migration_records_unchanged': True,
                'fixture_activity_rows_injected': True, 'application_supervisors_started': False,
                'live_services_accessed': False}
    finally:
        G.cleanup_container(name, token)


if __name__ == '__main__':
    raise SystemExit(F.main(pilot_check=check))
