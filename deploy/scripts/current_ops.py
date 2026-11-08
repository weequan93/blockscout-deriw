#!/usr/bin/env python3
"""Read-only dev v10 status, bridge progress, component inventory and source checks."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess

import dev_config as D

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = ROOT / 'deploy/audits/dev-components-2026-11-06.json'
PRODUCTION_REF = 'origin/deriw-prod'
UPSTREAM_V9 = '1106bfa688deea0a3c26750949fad9e5fedf65be'
NATIVE_STATE = Path('/data/blockscout-dev-private/v10-native-normalization/job-20261106T103901Z-59bb4dc6/state.json')
VERSION = re.compile(r'(?:[A-Za-z0-9_.-]+/)?v?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9_.-]+)?')
DIGEST = re.compile(r'sha256:[a-f0-9]{64}')
REVISION = re.compile(r'[a-f0-9]{40,64}')
FRONTEND_PROBE = """const fs=require('fs');let r={node:process.version};
for(const p of ['/app/package.json','/app/apps/frontend/package.json','/app/.next/standalone/package.json']){
try{let d=JSON.parse(fs.readFileSync(p,'utf8'));r.package_version=d.version;
for(const n of ['next','react','react-server-dom-webpack','react-server-dom-parcel','react-server-dom-turbopack']){
try{r[n]=require('module').createRequire(p)(n+'/package.json').version}catch{}}
break}catch{}}process.stdout.write(JSON.stringify(r));"""


def git(*args, root=ROOT):
    return D.run(['git', '-C', str(root), *args]).strip()


def source_check(root=ROOT, production_ref=PRODUCTION_REF):
    """Compare locally fetched refs; never fetch or modify a ref automatically."""
    production = git('rev-parse', '--verify', production_ref + '^{commit}', root=root)
    release = git('rev-parse', '--verify', 'HEAD^{commit}', root=root)
    D.require(REVISION.fullmatch(production) and REVISION.fullmatch(release), 'Invalid source identity')
    merge_base = git('merge-base', 'HEAD', production, root=root)
    D.require(REVISION.fullmatch(merge_base), 'Invalid merge-base identity')
    missing = git('rev-list', 'HEAD..' + production, root=root).splitlines()
    custom = git('rev-list', '--reverse', production, '^' + UPSTREAM_V9, root=root).splitlines()
    table = (root / 'docs/upgrades/v10.2.6-custom-changes.md').read_text()
    classified = re.findall(r'^\| `([a-f0-9]{9,40})` \|', table, re.M)
    unclassified = [sha for sha in custom if not any(sha.startswith(short) for short in classified)]
    return {'status': 'source_consolidated' if not missing and not unclassified else 'source_review_required',
            'read_only': True, 'production_ref': production_ref, 'production_commit': production,
            'release_commit': release, 'merge_base': merge_base,
            'production_commits_missing_from_release': len(missing),
            'custom_production_commits': len(custom), 'unclassified_custom_commits': unclassified,
            'custom_changes_guide': 'docs/upgrades/v10.2.6-custom-changes.md',
            'note': 'Local fetched refs only. Retained, adapted, replaced and reverted changes are classified in the guide.'}


def safe_version(value):
    return value if isinstance(value, str) and VERSION.fullmatch(value) else None


def image_record(service, row, image, snapshot):
    labels = image.get('Config', {}).get('Labels') or {}
    revision = labels.get('org.opencontainers.image.revision')
    identity = row.get('Image', '')
    D.require(DIGEST.fullmatch(identity) and image.get('Id') == identity, 'Invalid image identity')
    known = snapshot['images'].get(service, {})
    # A revision label identifies source, not a reproducible binary. Require the
    # observed image digest as well before reusing the dated release mapping.
    mapped = known.get('release') if (known.get('image_id') == identity and
                                    known.get('revision') == revision) else None
    created = image.get('Created', '')[:10]
    return {'service': service, 'image_id': identity,
            'created': created if re.fullmatch(r'\d{4}-\d\d-\d\d', created) else None,
            'oci_version': safe_version(labels.get('org.opencontainers.image.version')),
            'revision': revision if isinstance(revision, str) and REVISION.fullmatch(revision) else None,
            'mapped_release_as_of_2026_11_06': mapped,
            'matches_recorded_image': known.get('image_id') == identity}


def version_probes(api):
    commands = {
        'postgresql': ['docker', 'exec', 'db', 'postgres', '--version'],
        'stats_postgresql': ['docker', 'exec', 'stats-db', 'postgres', '--version'],
        'redis': ['docker', 'exec', 'redis-db', 'redis-server', '--version'],
        'nginx': ['docker', 'exec', 'proxy', 'sh', '-c', 'exec nginx -v 2>&1']}
    result = {}
    for name, command in commands.items():
        try:
            found = re.search(r'\b\d+\.\d+(?:\.\d+)?\b', api.run(command, timeout=10))
            result[name] = found.group(0) if found else None
        except D.ConfigError:
            result[name] = None
    try:
        data = json.loads(api.run(['docker', 'exec', 'frontend', 'node', '-e', FRONTEND_PROBE], timeout=10))
        D.require(isinstance(data, dict), 'Invalid frontend version probe')
        keys = ('node', 'package_version', 'next', 'react', 'react-server-dom-webpack',
                'react-server-dom-parcel', 'react-server-dom-turbopack')
        result['frontend'] = {key: safe_version(data.get(key)) for key in keys}
    except (D.ConfigError, ValueError):
        result['frontend'] = None
    return result


def components(api=D):
    manifest, _, _ = api.configuration(ROOT, 'dev')
    before = api.inspect_project(manifest)
    identities = sorted({row['Image'] for row in before.values()})
    D.require(all(DIGEST.fullmatch(identity) for identity in identities), 'Invalid live image identity')
    images = json.loads(api.run(['docker', 'image', 'inspect', *identities]))
    images = {row['Id']: row for row in images}
    D.require(set(images) == set(identities), 'Image inventory changed')
    snapshot = json.loads(SNAPSHOT.read_text())
    rows = [image_record(name, row, images[row['Image']], snapshot) for name, row in sorted(before.items())]
    versions = version_probes(api)
    api.ensure_stable(before, api.inspect_project(manifest), 'Runtime changed during component inventory')
    return {'status': 'component_inventory', 'read_only': True, 'raw_values_suppressed': True,
            'observed_at_utc': datetime.now(timezone.utc).isoformat(), 'images': rows, 'versions': versions,
            'note': 'No image pulls or restarts. Missing package versions do not prove package absence. Release mappings are dated evidence.'}


def service_status():
    try:
        proc = subprocess.run(['systemctl', 'is-active', 'blockscout-v10-native-normalization.service'],
                              capture_output=True, text=True, timeout=10)
        value = proc.stdout.strip()
        return value if value in ('active', 'inactive', 'failed', 'activating', 'deactivating') else 'unknown'
    except (OSError, subprocess.SubprocessError):
        return 'unavailable'


def observe(action, path, api=D):
    import v10_native_normalization as N
    import v10_bridge_deploy as P
    import v10_normalization_session as J
    import v10_frontend_deploy as F
    state = N.load(path)
    if action == 'status':
        return {'read_only': True, 'service': service_status(), 'normalization': N.public(path, state)}
    manifest, _, _ = api.configuration(ROOT, 'dev')
    before = api.inspect_project(manifest)
    context = N.runtime_binding(path, state) or state['inputs']
    api.ensure_stable(context['runtime_before'], before, 'Runtime changed from the verified native binding')
    if action == 'bridge':
        # The controller tools and backend may have different source revisions.
        # Read the backend revision only after authenticating its runtime binding.
        source = api.env_map(before['backend']).get('BLOCKSCOUT_VERSION', '')
        D.require(re.fullmatch(r'[a-f0-9]{40}', source), 'Missing verified backend source identity')
        result = {'bridge': P.bridge_status(api, source)}
    elif action == 'config-check':
        D.require(context.get('release_state'), 'A qualified deployment snapshot is required')
        files = F.current_compose_files(api, before, manifest, context)
        model = api.compose_model(P.V.LIVE_DIRECTORY, manifest, {}, files, True)
        D.require(not api.live_environment_differences(model, before),
                  'Live settings differ from the authenticated Compose snapshot')
        sources = {mount['Source'] for row in before.values() for mount in row.get('Mounts', [])
                   if mount.get('Type') == 'bind'}
        D.require(all(Path(source).exists() for source in sources), 'A required bind source is missing')
        D.require(api.compose_model(P.V.LIVE_DIRECTORY, manifest, {}, files, True) == model,
                  'Selected configuration changed during observation')
        result = {'configuration': {'snapshot_authenticated': True, 'runtime_matches_binding': True,
                  'declared_environment_matches': True, 'bind_sources_present': len(sources),
                  'configuration_files_changed': False,
                  'note': 'Checks the saved selection, runtime identity and bind-source presence. This is not a backup or a database health check.'}}
    else:
        checked = N.rpc(api, J.diagnosis_expression(ROOT))
        D.require(checked['database_identity'] == state['inputs']['payload']['database_identity'],
                  'Database identity changed')
        result = {'transactions': checked['transactions'],
                  'query_diagnostics': J.query_diagnostics(checked.get('query_diagnostics', []), before)}
    api.ensure_stable(before, api.inspect_project(manifest), 'Runtime changed during observation')
    return {'read_only': True, 'database_writes': False, 'raw_values_suppressed': True,
            **result, 'normalization': N.public(path, N.load(path))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('source-check', 'components', 'status', 'bridge', 'diagnose', 'config-check'))
    parser.add_argument('--state', type=Path, default=NATIVE_STATE,
                        help='Existing native job only; defaults to the deployed dev job')
    args = parser.parse_args()
    try:
        if args.action == 'source-check':
            result = source_check()
        elif args.action == 'components':
            result = components()
        else:
            result = observe(args.action, args.state)
        print(json.dumps(result, indent=2))
        return 2 if result.get('status') == 'source_review_required' else 0
    except (D.ConfigError, OSError, ValueError, KeyError, TypeError):
        print(json.dumps({'status': 'observation_unavailable', 'read_only': True,
                          'raw_values_suppressed': True,
                          'reason': 'Check the dev server, saved state path and verified runtime binding; raw error suppressed'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
