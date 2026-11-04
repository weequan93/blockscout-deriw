#!/usr/bin/env python3
"""Review the exact v10 candidate and bounded live metadata; never deploy or migrate."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

import candidate_build as B
import deploy_check as C
import dev_config as D
import release as R
import upgrade_plan as U
import v10_candidate as V

ROOT = Path(__file__).resolve().parents[2]
STAGE = 'receipt'
DIRECTORIES = {'repo', 'account', 'arbitrum'}
TABLES = ('account_public_tags_requests', 'multichain_search_db_main_export_queue',
          'multichain_search_db_export_balances_queue', 'multichain_search_db_export_counters_queue',
          'multichain_search_db_export_token_info_queue')


def candidate(api, root, receipt_path, private, before, manifest):
    saved = C.private_receipt(api, receipt_path, root)
    api.require(saved.get('format') == V.FORMAT and saved.get('environment') == 'dev'
                and saved.get('release_version') == '10.2.6' and saved.get('platform') == 'linux/amd64'
                and saved.get('activation_performed') is False and saved.get('automatic_background_migrations_held') is True,
                'Unexpected held v10 receipt')
    api.require(saved.get('private_input_file') == str(private.resolve()), 'Use the candidate original private inputs')
    api.require(saved.get('smoke_checks') == [{'mode': value, 'passed': True} for value in ('unset', 'false', 'true')],
                'Candidate release checks are incomplete')
    api.require(saved.get('baseline_image_ids') == {name: row['Image'] for name, row in before.items()}
                and set(before) == set(manifest['baseline_image_ids']), 'Live service images changed after candidate build')
    commit, version = B.clean_source(api, root)
    original = saved.get('source_commit', '')
    api.require(bool(re.fullmatch('[a-f0-9]{40}', original)) and version == '10.2.6', 'Candidate source/version invalid')
    api.run(['git', '-C', str(root), 'merge-base', '--is-ancestor', original, commit])
    changed = api.run(['git', '-C', str(root), 'diff', '--name-only', original, commit]).splitlines()
    api.require(all(name.startswith(('deploy/', 'docs/upgrades/')) for name in changed),
                'Application/config/recipe changed after candidate build; rebuild required')
    plan = api.read_json(root / 'deploy/upgrades/v10.2.6.json')
    U.source_report(root, plan)
    inventory_hash = hashlib.sha256(json.dumps(plan['target_migration_sha256'], sort_keys=True).encode()).hexdigest()
    api.require(saved.get('migration_inventory_sha256') == inventory_hash, 'Reviewed migration inventory changed')
    identity = {key: saved.get(key) for key in ('format', 'source_commit', 'release_version', 'platform',
                                               'build_arguments', 'migration_inventory_sha256')}
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    api.require(saved.get('build_fingerprint') == fingerprint and saved['build_arguments'].get(V.HOLD) == 'true'
                and saved['build_arguments'].get('RELEASE_VERSION') == '10.2.6'
                and saved['build_arguments'].get('CHAIN_TYPE') == 'arbitrum'
                and saved['build_arguments'].get('BLOCKSCOUT_VERSION') == original, 'Candidate build identity differs')
    api.require(saved.get('candidate_tag') == 'deriw-blockscout:upgrade-v10-' + original[:12] + '-arbitrum-amd64',
                'Unexpected upgrade image tag')
    baseline = B.image_info(api, before['backend']['Image'])
    image = B.image_info(api, saved['candidate_tag'])
    V.validate_image(api, image, {'fingerprint': fingerprint, 'source_commit': original,
                                'baseline_runtime_user': baseline.get('Config', {}).get('User', '')})
    api.require(image['Id'] == saved.get('image_id'), 'Candidate image changed after verification')
    return saved, image, plan, commit


def classify(rows, plan):
    added = {directory: {int(Path(name).name.split('_', 1)[0]) for name in plan['migration_changes']['added']
                         if name.startswith(directory + '/migrations/')} for directory in DIRECTORIES}
    deferred = {int(Path(name).name.split('_', 1)[0]) for name in R.DEFERRED}
    result = []
    D.require(len(rows) == 3 and {row.get('migration_directory') for row in rows} == DIRECTORIES,
              'Expected all three active migration repositories')
    for row in rows:
        D.require(row.get('status') == 'ok', 'Migration version evidence unavailable')
        directory = row['migration_directory']
        pending = set(row['candidate_pending_versions'])
        optional = pending & deferred if directory == 'repo' else set()
        unexpected = pending - added[directory] - optional
        result.append({'repository': row['repository'], 'directory': directory,
                       'upstream_pending_versions': sorted(pending & added[directory]),
                       'deferred_custom_versions': sorted(optional), 'unexpected_pending_versions': sorted(unexpected)})
    return sorted(result, key=lambda row: ('repo', 'account', 'arbitrum').index(row['directory']))


def presence_expression():
    # All identifiers originate from the fixed table list above, never from
    # runtime/private input. EXISTS reads at most one visible row per table.
    return 'tables = ' + json.dumps(list(TABLES)) + ';\n' + '''
data = try do
  repo = Explorer.Repo;
  {:ok, rows} = repo.transaction(fn ->
    Ecto.Adapters.SQL.query!(repo, "SET TRANSACTION READ ONLY", [], timeout: 2000, log: false);
    Ecto.Adapters.SQL.query!(repo, "SET LOCAL statement_timeout = '1500ms'", [], timeout: 2000, log: false);
    Ecto.Adapters.SQL.query!(repo, "SET LOCAL lock_timeout = '500ms'", [], timeout: 2000, log: false);
    Enum.map(tables, fn table ->
      [[relation]] = Ecto.Adapters.SQL.query!(repo, "SELECT to_regclass($1)::text", ["public." <> table], timeout: 2000, log: false).rows;
      if is_nil(relation) do
        %{table: table, exists: false, has_rows: nil}
      else
        [[has_rows]] = Ecto.Adapters.SQL.query!(repo, "SELECT EXISTS (SELECT 1 FROM public." <> table <> " LIMIT 1)", [], timeout: 2000, log: false).rows;
        %{table: table, exists: true, has_rows: has_rows}
      end
    end)
  end, timeout: 5000);
  %{status: "ok", tables: rows}
rescue
  _ -> %{status: "unavailable"}
end;
IO.puts("BLOCKSCOUT_DEPLOY_CHECK=" <> Jason.encode!(data))
'''


def presence(api):
    data = C.marked_json(api, api.run(['docker', 'exec', 'backend', '/app/bin/blockscout', 'rpc', presence_expression()], timeout=15))
    api.require(data.get('status') == 'ok' and isinstance(data.get('tables'), list)
                and len(data['tables']) == len(TABLES), 'Migration table presence evidence unavailable')
    api.require({row.get('table') for row in data['tables']} == set(TABLES), 'Unexpected migration table evidence')
    for row in data['tables']:
        api.require(type(row.get('exists')) is bool and (type(row.get('has_rows')) is bool if row['exists']
                    else row.get('has_rows') is None), 'Invalid migration table flags')
    return [{key: row[key] for key in ('table', 'exists', 'has_rows')} for row in data['tables']]


def review(api, root, receipt_path, private):
    global STAGE
    STAGE = 'runtime_model'
    manifest, requirements, _profile = api.configuration(root, 'dev')
    values = api.load_private(private, requirements)
    B.approved_preflight(api, api.plan(root, manifest, requirements, values, V.LIVE_DIRECTORY))
    before = api.inspect_project(manifest)
    STAGE = 'receipt'
    saved, image, plan, review_commit = candidate(api, root, receipt_path, private, before, manifest)
    STAGE = 'image_migrations'
    inventory = C.image_inventory(api, image['Id'])
    api.require(inventory['migrations'] == plan['target_migration_sha256'], 'Built image migration bytes differ from reviewed source')
    STAGE = 'live_migration_versions'
    migrations = classify(C.pending_migrations(api, inventory), plan)
    STAGE = 'bounded_table_presence'
    tables = presence(api)
    STAGE = 'history_metadata'
    history = U.history_report(root, api)
    STAGE = 'runtime_prerequisites'
    model = api.managed_model(root, manifest, values)
    mounts = C.mount_access(api, model['services']['backend'], before['backend'], inventory['identity'])
    health = C.live_health(api, before)
    capacity = V.storage_ready(Path(saved['storage_state']))
    api.ensure_stable(before, api.inspect_project(manifest), 'Live containers changed during v10 candidate review')
    api.require(api.load_private(private, requirements) == values, 'Private inputs changed during candidate review')
    api.require(C.private_receipt(api, receipt_path, root) == saved
                and B.image_info(api, saved['candidate_tag'])['Id'] == image['Id'], 'Candidate receipt/tag changed during review')
    api.require(B.clean_source(api, root) == (review_commit, '10.2.6'), 'Review source changed during metadata reads')
    blockers = ['snapshot_recovery_unverified', 'migration_working_space_unverified']
    if any(row['unexpected_pending_versions'] for row in migrations):
        blockers.append('pending_versions_outside_reviewed_sequence')
    if not all(health.values()) or not mounts['numeric_user_and_groups_match']:
        blockers.append('runtime_prerequisites_need_review')
    if any(row['table'] == 'account_public_tags_requests' and row['has_rows'] is True for row in tables):
        blockers.append('account_public_tags_drop_would_remove_rows')
    return {'status': 'v10_candidate_review_collected', 'read_only': True, 'environment': 'dev',
            'candidate_image': image['Id'], 'candidate_source_commit': saved['source_commit'],
            'review_source_commit': review_commit,
            'automatic_background_migrations_held': True, 'deployment_permitted': False, 'snapshot_verified': False,
            'blockers': blockers, 'migration_sequence': migrations,
            'upstream_pending_count': sum(len(row['upstream_pending_versions']) for row in migrations),
            'deferred_custom_count': sum(len(row['deferred_custom_versions']) for row in migrations),
            'selected_table_presence': tables, 'history': history, 'mount_access': mounts, **health, 'storage': capacity,
            'scope': 'Receipt/image/source checks, isolated image file inventory, existing-Repo bounded read-only metadata/EXISTS reads and current v9 health. No candidate app startup, schema/data migration, index build, count/duplicate scan or service change.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--private-file', type=Path, required=True)
    args = parser.parse_args()
    try:
        D.require(sys.platform.startswith('linux') and os.geteuid() == 0, 'Run on the Linux dev server as root')
        with V.M.global_lock(False):
            result = review(D, ROOT, args.receipt, args.private_file)
        print(json.dumps(result, indent=2))
        return 0
    except Exception:
        print(json.dumps({'status': 'stopped', 'stage': STAGE, 'raw_values_suppressed': True,
                          'deployment_permitted': False, 'services_changed': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
