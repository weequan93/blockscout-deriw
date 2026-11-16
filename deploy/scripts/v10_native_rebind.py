#!/usr/bin/env python3
"""Bind the existing native job to the current deriw-dev backend runtime."""
import argparse
import json
import os
from pathlib import Path
import sys

import dev_config as D
import release as R
import v10_native_normalization as N
import v10_infrastructure_checks as K

ROOT = Path(__file__).resolve().parents[2]


def require(condition, message):
    if not condition:
        raise D.ConfigError(message)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    args = parser.parse_args(argv)
    require(os.geteuid() == 0 and sys.platform.startswith('linux'), 'Run on the dev Linux server as root')
    path = args.state.resolve(strict=True)
    native = N.load(path)
    require(native['phase'] in ('paused', 'needs_attention'),
            'Native job must be paused or needs_attention before rebind')
    require(N.owned(D, path, native['inputs']) is None, 'The native worker must be stopped before rebind')
    manifest, _, _ = D.configuration(ROOT, 'dev')
    runtime = D.inspect_project(manifest)
    backend_env = D.env_map(runtime['backend'])
    require(backend_env.get('BLOCKSCOUT_VERSION') == 'f0f40cd352d26db5c1f4743f18ad110e85a3c4af',
            'Current backend application revision is not the qualified v10 revision')
    require(backend_env.get('MIGRATION_BACKGROUND_AUTOSTART_DISABLED') == 'true',
            'Current backend does not hold automatic migrations')
    anchor = K.stable_database_anchor(D)
    payload = native['inputs']['payload']
    require(anchor['database_identity'] == payload['database_identity']
            and anchor['heap_identity'] == payload['heap_identity']
            and anchor['versions'] == payload['applied_versions']
            and anchor['native_status']['meta']['v10_native_job'] == payload['token'],
            'Database identity or native checkpoint does not match the saved job')
    commit = D.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD']).strip()
    binding = {
        'format': N.CURRENT_RUNTIME_FORMAT,
        'native_inputs_sha256': native['inputs_sha256'],
        'native_worker_script_sha256': native['inputs']['script_sha256'],
        'source_root': str(ROOT),
        'tool_source_commit': commit,
        'compose_project': manifest['compose_project'],
        'runtime_before': runtime,
        'database_anchor': anchor,
    }
    target = path.parent / 'current-runtime-binding.json'
    data = R.encoded(binding)
    if target.exists():
        previous = json.loads(target.read_text())
        require(previous.get('format') == N.CURRENT_RUNTIME_FORMAT
                and previous.get('runtime_before') == runtime
                and previous.get('database_anchor') == anchor,
                'Existing current runtime binding differs from the live runtime')
        temporary = target.with_name('.current-runtime-binding-' + commit[:12] + '.tmp')
        R.write_new(temporary, data)
        os.replace(temporary, target)
    else:
        R.write_new(target, data)
    native.update(runtime_binding_file=target.name,
                  runtime_binding_sha256=R.digest(data),
                  database={'native_status': anchor['native_status']['status'],
                            'native_meta': anchor['native_status']['meta']},
                  transient_retries=0, worker_crash_retries=0,
                  phase='paused', reason=None)
    N.runtime_binding(path, native)
    R.update_state(path, native)
    print(json.dumps({'status': 'native_runtime_rebind_prepared', 'state': str(path),
                      'source_root': str(ROOT), 'tool_source_commit': commit,
                      'backend_image': runtime['backend']['Image'],
                      'database_anchor_verified': True,
                      'native_cursor': anchor['native_status']['meta']['max_block_number']}, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'status': 'native_runtime_rebind_stopped',
                          'reason': str(error) if isinstance(error, D.ConfigError) else 'Rebind failed; private state unchanged'}))
        raise SystemExit(2)
