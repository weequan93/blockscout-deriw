"""Bound the initial v10 schema phase separately from held historical backfills."""
import json

import deploy_check as C
import dev_config as D

GIB = 1024**3
SMALL_TABLES = ('tokens', 'token_instances', 'bridged_tokens', 'contract_methods',
                'proxy_implementations', 'signed_authorizations', 'migrations_status',
                'address_current_token_balances', 'account_public_tags_requests',
                'multichain_search_db_main_export_queue', 'multichain_search_db_export_balances_queue',
                'multichain_search_db_export_counters_queue', 'multichain_search_db_export_token_info_queue')
EMPTY_TABLES = ('account_public_tags_requests', 'multichain_search_db_main_export_queue',
                'multichain_search_db_export_balances_queue', 'multichain_search_db_export_counters_queue',
                'multichain_search_db_export_token_info_queue')
REQUIRED = {'tokens', 'token_instances', 'contract_methods', 'proxy_implementations',
            'signed_authorizations', 'migrations_status', 'address_current_token_balances'}
EXPECTED_TYPES = {'trace_address': 'integer[]', 'value': 'numeric(100,0)'}


def body():
    return '''
repo = Explorer.Repo;
{:ok, data} = repo.transaction(fn ->
  q = fn sql, params -> Ecto.Adapters.SQL.query!(repo, sql, params, timeout: 2000, log: false).rows end;
  q.("SET TRANSACTION READ ONLY", []);
  q.("SET LOCAL statement_timeout = '1500ms'", []);
  q.("SET LOCAL lock_timeout = '500ms'", []);
  types = q.("SELECT attname, format_type(atttypid, atttypmod) FROM pg_attribute WHERE attrelid='public.internal_transactions'::regclass AND attname IN ('trace_address','value') AND NOT attisdropped ORDER BY attname", []);
  [[heap]] = q.("SELECT pg_relation_filenode('public.internal_transactions'::regclass)::bigint", []);
  relations = q.("SELECT c.relname, pg_total_relation_size(c.oid) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind='r' AND c.relname=ANY($1::text[]) ORDER BY c.relname", [TABLES]);
  %{types: types, internal_transactions_heap_identity: heap, relations: relations}
end, timeout: 5000);
data
'''.replace('[TABLES]', '[' + json.dumps(list(SMALL_TABLES)) + ']')


def metadata(api):
    expression = 'data = (fn ->\n' + body() + '\nend).();\nIO.puts("' + C.MARKER + '" <> Jason.encode!(data))'
    return C.marked_json(api, api.run(['docker', 'exec', 'backend', '/app/bin/blockscout', 'rpc', expression], timeout=15))


def budget(data, capacity, presence):
    """Point-in-time eligibility; repeat after clients stop, before any writes."""
    D.require(isinstance(data, dict), 'Schema resource metadata unavailable')
    types = data.get('types')
    D.require(isinstance(types, list) and len(types) == 2 and all(
        isinstance(row, list) and len(row) == 2 and all(isinstance(value, str) for value in row) for row in types),
        'Internal transaction type metadata unavailable')
    D.require(type(data.get('internal_transactions_heap_identity')) is int
              and data['internal_transactions_heap_identity'] > 0, 'Internal transaction heap identity unavailable')
    relations = data.get('relations')
    D.require(isinstance(relations, list) and len(relations) <= len(SMALL_TABLES), 'Schema relation sizes unavailable')
    sizes = {}
    for row in relations:
        D.require(isinstance(row, list) and len(row) == 2 and row[0] in SMALL_TABLES and row[0] not in sizes
                  and type(row[1]) is int and row[1] >= 0, 'Invalid schema relation size')
        sizes[row[0]] = row[1]
    D.require(REQUIRED <= sizes.keys(), 'Required schema relations unavailable')
    D.require(isinstance(presence, list) and len(presence) == len(EMPTY_TABLES)
              and {row.get('table') for row in presence} == set(EMPTY_TABLES), 'Empty-table evidence unavailable')
    D.require(all(type(row.get('exists')) is bool and (type(row.get('has_rows')) is bool if row['exists']
                  else row.get('has_rows') is None) for row in presence), 'Invalid empty-table evidence')
    empty = all(row['has_rows'] is not True for row in presence)
    matching = dict(types) == EXPECTED_TYPES
    small = sum(sizes.values()) <= GIB
    available = capacity['data_available_bytes']
    D.require(type(available) is int and available >= 0, 'Data filesystem capacity unavailable')
    return {'status': 'eligible_for_initial_schema_phase' if matching and small and empty and available >= 120 * GIB
            else 'initial_schema_resources_require_review',
            'internal_transaction_types_match_no_rewrite_path': matching,
            'small_relations_total_bytes': sum(sizes.values()), 'small_relations_limit_bytes': GIB,
            'selected_drop_and_export_tables_empty': empty,
            'data_available_bytes': available, 'planned_schema_consumption_limit_bytes': 20 * GIB,
            'minimum_remaining_free_bytes': 100 * GIB,
            'historical_backfill_space_verified': False,
            'recheck_after_stopping_clients_required': True,
            'scope': 'Catalog sizes/types and bounded empty-table probes only. Budget covers the reviewed 29 schema versions; it does not authorize backfills, heavy indexes or PK conversion.'}
