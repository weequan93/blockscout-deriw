#!/usr/bin/env python3
"""Inspect the existing DeriW test v9 database before the v10 release.

Run from a Git-fetched copy; the existing checkout and services stay unchanged.
Private configuration snapshots are not a database backup or migration approval.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import traceback
import types

SOURCE = 'f0f40cd352d26db5c1f4743f18ad110e85a3c4af'
CHAIN = '2885'
MARKER = 'DERIW_TEST_PREFLIGHT='
SMALL = ('tokens', 'token_instances', 'bridged_tokens', 'contract_methods',
         'proxy_implementations', 'signed_authorizations', 'migrations_status',
         'address_current_token_balances', 'account_public_tags_requests',
         'multichain_search_db_main_export_queue', 'multichain_search_db_export_balances_queue',
         'multichain_search_db_export_counters_queue', 'multichain_search_db_export_token_info_queue')
EMPTY = ('account_public_tags_requests', 'multichain_search_db_main_export_queue',
         'multichain_search_db_export_balances_queue', 'multichain_search_db_export_counters_queue',
         'multichain_search_db_export_token_info_queue')
CUTOFFS = {'repo': 20260220073250, 'account': 20251029115930, 'arbitrum': 20250731001757}


class PreflightError(Exception):
    pass


def require(ok, message):
    if not ok:
        raise PreflightError(message)


def private_write(path, data):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as stream:
        stream.write(data if isinstance(data, bytes) else data.encode())


def run(command, timeout=30):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        raise PreflightError('A read-only command was unavailable or timed out.') from None
    require(result.returncode == 0 and len(result.stdout) <= 16 * 1024**2,
            'A read-only command failed; raw output withheld.')
    return result.stdout


def env_map(row):
    return dict(item.split('=', 1) for item in row['Config'].get('Env', []) if '=' in item)


def select_project(backend):
    row = json.loads(run(['docker', 'inspect', backend]))[0]
    env = env_map(row)
    require(env.get('CHAIN_ID') == CHAIN and env.get('CHAIN_TYPE') == 'arbitrum',
            'This inspection requires the DeriW test Arbitrum backend, chain 2885.')
    require(row['State']['Running'], 'Keep the test backend running for this read-only inspection.')
    project = (row['Config'].get('Labels') or {}).get('com.docker.compose.project')
    require(isinstance(project, str) and re.fullmatch(r'[A-Za-z0-9_.-]+', project),
            'The backend Compose project was not identified.')
    ids = run(['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=' + project]).split()
    rows = json.loads(run(['docker', 'inspect', *ids]))
    services = {}
    for item in rows:
        labels = item['Config'].get('Labels') or {}
        service = labels.get('com.docker.compose.service')
        require(isinstance(service, str) and re.fullmatch(r'[A-Za-z0-9_.-]+', service)
                and service not in services, 'The Compose service inventory is ambiguous.')
        services[service] = item
    require(services.get('backend', {}).get('Id') == row['Id'] and 'db' in services,
            'Expected the inspected backend and its database in one Compose project.')
    return project, services


def load_primitives(repo):
    # Reuse the exact, previously tested read-only queries and projection from
    # the qualified application revision, without checking that revision out.
    source = run(['git', '-C', str(repo), 'show', SOURCE + ':deploy/scripts/in_place_preflight.py'])
    module = types.ModuleType('deriw_test_preflight_primitives')
    exec(compile(source, 'qualified-in-place-preflight.py', 'exec'), module.__dict__)
    return module


def expression(primitives):
    extra = '''
repo = Explorer.Repo
{:ok, schema} = repo.transaction(fn ->
  q = fn sql, args -> repo.query!(sql, args, timeout: 2000, log: false).rows end
  q.("SET TRANSACTION READ ONLY", [])
  q.("SET LOCAL statement_timeout = '1500ms'", [])
  q.("SET LOCAL lock_timeout = '250ms'", [])
  versions = q.("SELECT version FROM public.schema_migrations ORDER BY version LIMIT 2049", []) |> List.flatten()
  true = length(versions) <= 2048
  types = q.("SELECT attname, format_type(atttypid, atttypmod) FROM pg_attribute WHERE attrelid='public.internal_transactions'::regclass AND attname IN ('trace_address','value') AND NOT attisdropped ORDER BY attname", [])
  columns = q.("SELECT attname, format_type(atttypid, atttypmod), attnotnull FROM pg_attribute WHERE attrelid='public.internal_transactions'::regclass AND attname IN ('block_hash','block_index','index','block_number','transaction_index') AND NOT attisdropped ORDER BY attname", [])
  indexes = q.("SELECT c.relname, i.indisvalid, i.indisready, i.indisunique, i.indisprimary, ARRAY(SELECT COALESCE(a.attname::text, '<expression>') FROM unnest(i.indkey) WITH ORDINALITY AS k(attnum, n) LEFT JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum WHERE k.n<=i.indnkeyatts ORDER BY k.n) FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid WHERE i.indrelid='public.internal_transactions'::regclass ORDER BY c.relname LIMIT 65", [])
  sizes = q.("SELECT c.relname, pg_total_relation_size(c.oid) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind='r' AND c.relname=ANY($1::text[]) ORDER BY c.relname", [SMALL_TABLES])
  empty = for table <- EMPTY_TABLES do
    [[exists]] = q.("SELECT to_regclass($1) IS NOT NULL", ["public." <> table])
    has_rows = if exists do
      [[present]] = q.("SELECT EXISTS(SELECT 1 FROM public." <> table <> " LIMIT 1)", [])
      present
    else
      nil
    end
    %{table: table, exists: exists, has_rows: has_rows}
  end
  statuses = q.("SELECT migration_name, status FROM public.migrations_status WHERE migration_name=ANY($1::text[]) ORDER BY migration_name", [
    ~w(empty_internal_transactions_data reindex_duplicated_internal_transactions heavy_indexes_create_internal_transactions_block_number_transaction_index_index_index heavy_indexes_create_internal_transactions_not_null_constraints heavy_indexes_create_internal_transactions_pkey heavy_indexes_create_arbitrum_batch_l2_blocks_unconfirmed_blocks_index)
  ])
  %{applied_versions: versions, types: types, columns: columns, indexes: indexes,
    relation_sizes: sizes, presence: empty, background_statuses: statuses}
end, timeout: 10000)
bridge = for {name, module, task} <- [
  {:deposits, Indexer.Fetcher.Arbitrum.TrackingMessagesOnL1, :check_new},
  {:withdrawals, Indexer.Fetcher.Arbitrum.TrackingBatchesStatuses, :new_executions}
] do
  pid = Process.whereis(module)
  if is_pid(pid) do
    try do
      state = :sys.get_state(pid, 500).callback_module_state
      config = state.config
      range = if name == :deposits, do: config[:l1_rpc_block_range], else: get_in(config, [:l1_rpc, :logs_block_range])
      %{tracker: name, running: true, state_read_ok: true,
        next_parent_block: get_in(state, [:task_data, task, :start_block]),
        blocks_per_request: range, interval_ms: state.intervals[task]}
    rescue
      _ -> %{tracker: name, running: true, state_read_ok: false}
    catch
      _, _ -> %{tracker: name, running: true, state_read_ok: false}
    end
  else
    %{tracker: name, running: false}
  end
end
%{metadata: metadata, schema: schema, bridge: bridge,
  runtime_version: to_string(Application.spec(:explorer, :vsn))}
'''.replace('[SMALL_TABLES]', '[' + json.dumps(list(SMALL)) + ']')
    extra = extra.replace('EMPTY_TABLES', json.dumps(list(EMPTY)))
    return '''
result = try do
  "2885" = System.fetch_env!("CHAIN_ID")
  metadata = (fn ->
''' + primitives.body() + '''
  end).()
''' + extra + '''
rescue
  error ->
    kind = case error do
      %{__struct__: Postgrex.Error, postgres: %{code: :query_canceled}} -> "statement_timeout_or_cancel"
      %{__struct__: Postgrex.Error, postgres: %{code: :insufficient_privilege}} -> "permission_denied"
      %{__struct__: Postgrex.Error, postgres: %{code: :lock_not_available}} -> "lock_unavailable"
      %{__struct__: DBConnection.ConnectionError} -> "connection_unavailable"
      _ -> "check_failed"
    end
    %{status: "unavailable", stage: "schema_metadata", error_kind: kind}
catch
  {:in_place_preflight_failed, stage, kind} -> %{status: "unavailable", stage: stage, error_kind: kind}
  _, _ -> %{status: "unavailable", stage: "backend_rpc", error_kind: "check_failed"}
end
IO.puts("''' + MARKER + '''" <> Jason.encode!(result))
'''


def schema_summary(plan, raw, routes):
    versions = raw['applied_versions']
    require(isinstance(versions, list) and all(type(v) is int and v > 0 for v in versions),
            'Applied migration versions are invalid.')
    pending = {}
    for directory, cutoff in CUTOFFS.items():
        expected_repo = {'repo': 'Explorer.Repo', 'account': 'Explorer.Repo.Account',
                         'arbitrum': 'Explorer.Repo.Arbitrum'}[directory]
        shared = any(r['repository'] == expected_repo and r['shares_main_configuration'] for r in routes)
        wanted = sorted(int(Path(p).name.split('_', 1)[0]) for p in plan['target_migration_sha256']
                        if p.startswith(directory + '/migrations/'))
        pending[directory] = [v for v in wanted if v <= cutoff and v not in versions] if shared else None
    types_map = dict(raw['types'])
    size_bytes = sum(row[1] for row in raw['relation_sizes'])
    return {
        'pending_initial_schema_versions': pending,
        'deferred_index_versions': [20260721000000, 20260812000000],
        'internal_transaction_types': types_map,
        'internal_transaction_columns': raw['columns'],
        'internal_transaction_indexes': raw['indexes'],
        'types_match_reviewed_schema_path': types_map == {'trace_address': 'integer[]', 'value': 'numeric(100,0)'},
        'selected_relations_total_bytes': size_bytes,
        'selected_relations_within_reviewed_1_gib_budget': size_bytes <= 1024**3,
        'selected_drop_and_export_tables': raw['presence'],
        'selected_drop_and_export_tables_empty': all(r['has_rows'] is not True for r in raw['presence']),
        'background_statuses': [[name, status if status in ('started', 'completed') else 'other']
                                for name, status in raw['background_statuses']],
    }


def compose_snapshot(repo, row, project, saved):
    labels = row['Config']['Labels']
    files = labels.get('com.docker.compose.project.config_files', '').split(',')
    directory = labels.get('com.docker.compose.project.working_dir', '')
    if not directory or not all(Path(p).is_absolute() and Path(p).is_file() for p in files):
        return {'captured': False, 'reason': 'original_compose_files_unavailable'}
    command = ['docker', 'compose', '-p', project, '--project-directory', directory]
    for path in files:
        command += ['-f', path]
    model = json.loads(run(command + ['config', '--format', 'json']))
    private_write(saved / 'compose.private.json', json.dumps(model, indent=2))
    for index, path in enumerate(files):
        private_write(saved / ('original-compose-' + str(index) + '.private.yml'), Path(path).read_bytes())
    actual = env_map(row)
    differences = sorted(key for key, value in model.get('services', {}).get('backend', {}).get('environment', {}).items()
                         if value is not None and str(value) != actual.get(key))
    return {'captured': True, 'directory_basename': Path(directory).name,
            'backend_environment_differences': differences,
            'private_test_directory_exists': (repo / 'docker-compose.test').is_dir()}


def collect(repo, backend, private_base):
    project, rows = select_project(backend)
    require(Path(run(['git', '-C', str(repo), 'rev-parse', '--show-toplevel']).strip()).resolve() == repo.resolve(),
            'Select the existing Blockscout repository root.')
    # All target checks precede creation of configuration snapshots.
    private_base = private_base.absolute()
    require(not private_base.is_symlink() and private_base.resolve() != repo.resolve()
            and repo.resolve() not in private_base.resolve().parents,
            'Private snapshots must be outside the source checkout.')
    private_base.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(private_base.is_dir() and private_base.stat().st_uid == os.geteuid()
            and private_base.stat().st_mode & 0o077 == 0, 'Private snapshot directory must be owned and mode 0700.')
    saved = Path(tempfile.mkdtemp(prefix='preflight-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-'),
                                  dir=str(private_base)))
    private_write(saved / 'containers.private.json', json.dumps(rows, indent=2))
    try:
        primitives = load_primitives(repo)
        plan = json.loads(run(['git', '-C', str(repo), 'show', SOURCE + ':deploy/upgrades/v10.2.6.json']))
        compose = compose_snapshot(repo, rows['backend'], project, saved)
        private_write(saved / 'tracked-local-changes.private.patch',
                      run(['git', '-C', str(repo), 'diff', '--binary', 'HEAD']))
        source_status = run(['git', '-C', str(repo), 'status', '--porcelain', '--untracked-files=all',
                             '--', 'apps', 'config', 'docker', 'mix.exs', 'mix.lock'])
        private_write(saved / 'source-status.private.txt', source_status)
        raw = run(['docker', 'exec', rows['backend']['Id'], 'bin/blockscout', 'rpc', expression(primitives)], timeout=30)
        private_write(saved / 'database-observation.private.txt', raw)
        found = [json.loads(line[len(MARKER):]) for line in raw.splitlines() if line.startswith(MARKER)]
        require(len(found) == 1, 'The database inspection did not return one observation.')
        data = found[0]
        if data.get('status') == 'unavailable':
            stage = data.get('stage') if data.get('stage') in (*primitives.STAGES, 'schema_metadata') else 'backend_rpc'
            kind = data.get('error_kind') if data.get('error_kind') in primitives.ERROR_KINDS else 'check_failed'
            raise PreflightError('Database inspection unavailable: ' + stage + '/' + kind + '; private observations saved.')
        database, (address, port), tablespaces = primitives.safe_database(data['metadata'])
        db = rows['db']
        networks = db.get('NetworkSettings', {}).get('Networks') or {}
        shared = set(networks) & set(rows['backend'].get('NetworkSettings', {}).get('Networks') or {})
        direct = port == 5432 and address in {
            values.get(key) for name, values in networks.items() if name in shared
            for key in ('IPAddress', 'GlobalIPv6Address')}
        storage = []
        paths = [('data', database['data_directory']), ('wal', database['data_directory'] + '/pg_wal')]
        paths += [('tablespace_' + str(oid), path) for oid, path in tablespaces]
        if direct:
            for kind, path in paths:
                resolved = run(['docker', 'exec', db['Id'], 'readlink', '-f', path], timeout=5).strip()
                entry = primitives.mount_for(resolved, db.get('Mounts') or [])
                item = {'kind': kind, 'status': entry['status']}
                if entry['status'] == 'mapped':
                    host = Path(entry['host_path']).resolve(strict=True)
                    fs = os.statvfs(host)
                    item.update(filesystem_device=host.stat().st_dev,
                                total_bytes=fs.f_blocks * fs.f_frsize, available_bytes=fs.f_bavail * fs.f_frsize)
                storage.append(item)
        database.pop('data_directory', None)
        image = json.loads(run(['docker', 'image', 'inspect', rows['backend']['Image']]))[0]
        private_write(saved / 'backend-image.private.json', json.dumps(image, indent=2))
        current = json.loads(run(['docker', 'inspect', rows['backend']['Id'], db['Id']]))
        require(all(c['Id'] == rows[name]['Id'] and c['Image'] == rows[name]['Image']
                    and c['Mounts'] == rows[name]['Mounts'] for name, c in zip(('backend', 'db'), current)),
                'The test backend/database changed during inspection.')
        report = {
            'status': 'test_v10_preflight_collected', 'chain_id': CHAIN, 'target_version': '10.2.6',
            'target_application_revision': SOURCE, 'runtime_version': data['runtime_version'],
            'compose_project': project, 'compose': compose, 'platform': image['Os'] + '/' + image['Architecture'],
            'backend_runtime_user': rows['backend']['Config'].get('User', ''),
            'database': database, 'database_route_matches_inspected_container': direct, 'storage': storage,
            'schema': schema_summary(plan, data['schema'], database['repository_routes']),
            'bridge_trackers': data.get('bridge', []),
            'backend_flags': {key: value if value in ('true', 'false', None, '') else 'other'
                              for key in ('INDEXER_ARBITRUM_BRIDGE_MESSAGES_TRACKING_ENABLED',
                                          'INDEXER_ARBITRUM_BATCHES_TRACKING_ENABLED',
                                          'INDEXER_ARBITRUM_BATCHES_TRACKING_L1_FINALIZATION_CHECK_ENABLED',
                                          'SHRINK_INTERNAL_TRANSACTIONS_ENABLED', 'NFT_MEDIA_HANDLER_ENABLED',
                                          'NFT_MEDIA_HANDLER_REMOTE_DISPATCHER_NODE_MODE_ENABLED',
                                          'NFT_MEDIA_HANDLER_IS_WORKER', 'DISABLE_INDEXER')
                              for value in [env_map(rows['backend']).get(key)]},
            'parent_rpc_configured': bool(env_map(rows['backend']).get('INDEXER_ARBITRUM_L1_RPC')),
            'services': {name: {'image': c['Config']['Image'], 'image_id': c['Image'],
                                'state': c['State']['Status'], 'restart_count': c.get('RestartCount', 0)}
                         for name, c in rows.items()},
            'private_configuration_snapshot': str(saved), 'source_checkout_changed': False,
            'source_has_local_edits': bool(source_status.strip()),
            'source_backup_scope': 'tracked diff and filename inventory; untracked file contents not copied',
            'database_backup_created': False, 'schema_writes': False, 'services_restarted': False,
            'deployment_ready': False,
        }
        private_write(saved / 'report.json', json.dumps(report, indent=2))
        return report
    except Exception:
        private_write(saved / 'error.private.log', traceback.format_exc())
        print('Private configuration snapshots:', saved, file=sys.stderr)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path('/data/blockscout'))
    parser.add_argument('--backend', default='backend')
    parser.add_argument('--private-dir', type=Path, default=Path('/data/blockscout-test-private'))
    args = parser.parse_args()
    os.umask(0o077)
    try:
        report = collect(args.repo, args.backend, args.private_dir)
        print(json.dumps(report, indent=2))
        return 0
    except PreflightError as error:
        print('STOP:', error, file=sys.stderr)
        return 1
    except Exception:
        print('STOP: test preflight did not complete; no services or database schema were changed.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
