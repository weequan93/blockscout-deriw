#!/usr/bin/env python3
"""Build a dev v10 candidate with automatic Explorer migrators held; never deploy."""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys

import candidate_build as B
import containerd_maintenance as M
import dev_config as D
import upgrade_plan as U
import v10_nft as N

ROOT = Path(__file__).resolve().parents[2]
FORMAT = 'blockscout-v10-upgrade-candidate-v1'
HOLD = 'MIGRATION_BACKGROUND_AUTOSTART_DISABLED'
STAGE = 'source'
DEFAULT_STORAGE = Path('/data/blockscout-dev-private/containerd-storage/plan-4xiv4wmt/state.json')
DEFAULT_OUTPUT = Path('/data/blockscout-dev-private/v10-candidates')
LIVE_DIRECTORY = Path('/data/blockscout/docker-compose')


def held_expression():
    return ('true = Explorer.Migrator.StartupControl.disabled?(); '
            '[Explorer.Repo, Explorer.Chain.Cache.BackgroundMigrations] = '
            'Explorer.Migrator.StartupControl.filter([Explorer.Repo, Explorer.Chain.Cache.BackgroundMigrations, '
            'Explorer.Migrator.EmptyInternalTransactionsData, Explorer.Migrator.ReindexDuplicatedInternalTransactions, '
            'Explorer.Migrator.HeavyDbIndexOperation.CreateInternalTransactionsBlockNumberTransactionIndexIndexUniqueIndex, '
            'Explorer.Migrator.HeavyDbIndexOperation.ValidateInternalTransactionsBlockNumberTransactionIndexNotNull, '
            'Explorer.Migrator.HeavyDbIndexOperation.UpdateInternalTransactionsPrimaryKey, '
            'Explorer.Migrator.SwitchPendingOperations, Explorer.TokenInstanceOwnerAddressMigration.Supervisor]); ')


def storage_ready(path):
    maintenance = M.Maintenance(path)
    maintenance.identities()
    retirement = maintenance.source_retirement()
    D.require(retirement is not None and retirement['phase'] == 'complete', 'Root-store retirement must be complete')
    maintenance.target_configuration()
    rows = maintenance.rows()
    M.validate_containers(maintenance.plan['before'], rows, policies='original',
                          running=set(maintenance.plan['public_plan']['running_services']))
    maintenance.verify_backend_path()
    root = M.S.capacity(M.S.SOURCE)
    data = M.S.capacity(M.S.TARGET)
    D.require(root['available_bytes'] >= 10 * 1024**3 and data['available_bytes'] >= 150 * 1024**3,
              'Candidate build needs 10 GiB root and 150 GiB data free; migration space is separate')
    # A candidate build legitimately adds image/tag bindings. Preserve the old
    # storage inventory instead of rebasing it; verify current container images,
    # mounts and the active backend overlay directly for this operation.
    return {'root_available_bytes': root['available_bytes'], 'data_available_bytes': data['available_bytes'],
            'active_containerd_root': str(M.S.TARGET), 'migration_space_verified': False}


def source_plan(api, root, manifest, model, before):
    commit, version = B.clean_source(api, root)
    api.require(version == '10.2.6', 'Upgrade candidate requires committed v10.2.6 source')
    reviewed = api.read_json(root / 'deploy/upgrades/v10.2.6.json')
    U.source_report(root, reviewed)
    profile = model['services']['backend'].get('build', {}).get('args') or {}
    api.require(profile.get('RELEASE_VERSION') == '9.0.1' and profile.get('CHAIN_TYPE') == 'arbitrum',
                'Expected captured v9 Arbitrum profile; regular dev deployment remains unchanged')
    api.require(api.env_map(before['backend']).get('RELEASE_VERSION') == '9.0.1', 'Live backend must remain v9.0.1')
    candidate_model = deepcopy(model)
    candidate_model['services']['backend']['build']['args']['RELEASE_VERSION'] = version
    source = B.source_plan(api, root, manifest, candidate_model, before)
    api.require(source['platform'] == 'linux/amd64', 'This server candidate requires the verified AMD64 baseline')
    source['build_arguments'][HOLD] = 'true'
    inventory = hashlib.sha256(json.dumps(reviewed['target_migration_sha256'], sort_keys=True).encode()).hexdigest()
    identity = {'format': FORMAT, 'source_commit': commit, 'release_version': version, 'platform': source['platform'],
                'build_arguments': source['build_arguments'], 'migration_inventory_sha256': inventory}
    return {**source, **identity, 'fingerprint': hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest(),
            'tag': 'deriw-blockscout:upgrade-v10-' + commit[:12] + '-arbitrum-amd64'}


def prepare(api, root, private, output, storage):
    global STAGE
    STAGE = 'profile'
    B.validate_state_directory(api, output, root)
    api.require(root.resolve() not in private.resolve().parents
                and not any((p / '.git').exists() for p in private.resolve().parents), 'Private inputs must remain outside Git checkouts')
    manifest, requirements, _profile = api.configuration(root, 'dev')
    values = api.load_private(private, requirements)
    STAGE = 'runtime_model'
    report = api.plan(root, manifest, requirements, values, LIVE_DIRECTORY)
    B.approved_preflight(api, report)
    before = api.inspect_project(manifest)
    model = api.managed_model(root, manifest, values)
    STAGE = 'source_version'
    source = source_plan(api, root, manifest, model, before)
    environment = model['services']['backend']['environment']
    N.plan(api, model)
    api.require(HOLD not in environment, 'Captured profile must not override the candidate image migration hold')
    STAGE = 'market_provider'
    market = B.market_policy().provider_check(api, environment)
    api.require(market['status'] != 'unavailable', 'ETH provider unavailable; no build started')
    STAGE = 'storage'
    capacity = storage_ready(storage)
    return manifest, requirements, values, before, model, source, market, capacity


def validate_image(api, image, source):
    labels = image.get('Config', {}).get('Labels') or {}
    api.require(labels.get(B.LABEL) == source['fingerprint']
                and labels.get('org.opencontainers.image.revision') == source['source_commit'], 'Candidate labels differ')
    api.require(image.get('Os') == 'linux' and image.get('Architecture') == 'amd64', 'Candidate platform differs')
    environment = api.env_map(image)
    api.require(environment.get(HOLD) == 'true' and environment.get('RELEASE_VERSION') == '10.2.6',
                'Candidate must inherit the v10 migration hold')
    api.require(image['Config'].get('User', '') == source['baseline_runtime_user'], 'Candidate runtime user differs')


def build(api, root, private, output, storage, check=False, reuse=False):
    global STAGE
    STAGE = 'preflight'
    manifest, requirements, values, before, model, source, market, capacity = prepare(api, root, private, output, storage)
    existing = B.image_info(api, source['tag'], missing=True)
    summary = {'environment': 'dev', 'source_commit': source['source_commit'], 'release_version': '10.2.6',
               'platform': source['platform'], 'candidate_tag': source['tag'], 'automatic_background_migrations_held': True,
               'activation_performed': False, 'deployment_permitted': False, 'storage': capacity, 'native_coin_market': market,
               'paired_activation_services': ['backend', 'nft_media_handler']}
    if check:
        return {'status': 'v10_candidate_build_ready', **summary, 'candidate_already_exists': existing is not None}
    api.require((existing is not None) == reuse, 'Existing candidate requires --reuse; otherwise omit --reuse')
    if existing:
        validate_image(api, existing, source)
    directory = B.private_directory(api, output, root)
    print(json.dumps({'phase': 'v10_candidate_prepared', **summary, 'private_run_directory': str(directory),
                      'note': 'Private build/eval logs remain on the server. No application startup or database migration.'}), flush=True)
    STAGE = 'build'
    if not reuse:
        B.compile_image(api, root, source, directory / 'build.log.private')
    STAGE = 'release_config'
    image = B.image_info(api, source['tag'])
    validate_image(api, image, source)
    nft_plan = N.plan(api, model)
    backend_environment = {**model['services']['backend']['environment'], **nft_plan['backend_environment']}
    checks = B.smoke_image(api, image['Id'], source, backend_environment, directory,
                           extra_expression=held_expression() +
                           'true = Application.get_env(:nft_media_handler, :r2_folder) == System.get_env("NFT_MEDIA_HANDLER_BUCKET_FOLDER"); ')
    nft_checks = N.smoke_worker(api, image['Id'], source['release_version'],
                                model['services']['nft_media_handler']['environment'], directory)
    STAGE = 'post_build'
    api.require(B.clean_source(api, root) == (source['source_commit'], '10.2.6'), 'Source changed during build')
    api.require(api.load_private(private, requirements) == values, 'Private input changed during build')
    api.ensure_stable(before, api.inspect_project(manifest), 'Live containers changed during candidate build')
    B.approved_preflight(api, api.plan(root, manifest, requirements, values, LIVE_DIRECTORY))
    storage_ready(storage)
    api.require(B.image_info(api, source['tag'])['Id'] == image['Id'], 'Candidate tag changed during validation')
    result = {'format': FORMAT, **summary, 'image_id': image['Id'], 'build_fingerprint': source['fingerprint'],
              'build_arguments': source['build_arguments'], 'migration_inventory_sha256': source['migration_inventory_sha256'],
              'smoke_checks': checks, 'baseline_image_ids': {name: row['Image'] for name, row in before.items()},
              'nft_role_plan': nft_plan, 'nft_worker_release_config_checks': nft_checks,
              'private_input_file': str(private), 'storage_state': str(storage),
              'scope': 'Committed AMD64 build, backend and NFT worker release configuration, preserved bucket folder and selected repair/hold checks. No full application startup, live migration, background-worker execution or deployment.'}
    path = directory / 'candidate.json'
    with B.private_log(path) as stream:
        stream.write((json.dumps(result, indent=2, sort_keys=True) + '\n').encode())
    return {'status': 'v10_candidate_verified', **summary, 'image_id': image['Id'], 'receipt': str(path), 'scope': result['scope']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--build', action='store_true')
    parser.add_argument('--private-file', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--storage-state', type=Path, default=DEFAULT_STORAGE)
    parser.add_argument('--reuse', action='store_true')
    args = parser.parse_args()
    try:
        D.require(sys.platform.startswith('linux') and os.geteuid() == 0, 'Run on the Linux dev server as root')
        M.load_plan(args.storage_state)
        with M.global_lock(args.build):
            if args.check:
                result = build(D, ROOT, args.private_file, args.output, args.storage_state, check=True)
            else:
                with B.build_lock(D, args.output, ROOT):
                    result = build(D, ROOT, args.private_file, args.output, args.storage_state, reuse=args.reuse)
        print(json.dumps(result, indent=2))
        return 0
    except Exception:
        print(json.dumps({'status': 'stopped', 'stage': STAGE, 'raw_values_suppressed': True,
                          'activation_performed': False, 'deployment_permitted': False,
                          'note': 'No deployment or database migration. Any private build/eval logs remain under the candidate directory.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
