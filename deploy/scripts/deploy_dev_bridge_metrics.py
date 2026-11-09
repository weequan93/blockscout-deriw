#!/usr/bin/env python3
"""Deploy committed bridge verification metrics to the already-upgraded dev backend.

Run after a clean Git fast-forward on the existing dev server. This is a v10.2.6
backend-only rebuild with private backups and start-only startup. It does not
migrate databases, resume normalization or repair bridge scan checkpoints.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / 'docker-compose.dev'
PRIVATE = Path('/data/blockscout-dev-private/native-metrics-deployments')
NATIVE = Path('/data/blockscout-dev-private/v10-native-normalization/'
              'job-20261106T103901Z-59bb4dc6/state.json')
PROJECT = 'docker-compose'
SECTIONS = ('deposits', 'withdrawals', 'rollup', 'published', 'confirmed')
VERIFICATION = ('deposits', 'withdrawals', 'batches', 'parent_latest', 'parent_safe', 'parent_finalized', 'batch_head')
BUILD_ARGUMENTS = {
    'RELEASE_VERSION', 'CHAIN_TYPE', 'BLOCKSCOUT_VERSION',
    'MIGRATION_BACKGROUND_AUTOSTART_DISABLED', 'DISABLE_API',
    'API_V1_READ_METHODS_DISABLED', 'API_V1_WRITE_METHODS_DISABLED',
    'BRIDGED_TOKENS_ENABLED', 'API_GRAPHQL_MAX_COMPLEXITY', 'MUD_INDEXER_ENABLED',
}


class DeploymentError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise DeploymentError(message)


def emit(phase, **fields):
    print(json.dumps({'phase': phase, **fields}), flush=True)


def private_write(path, data):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def run(command, timeout=30, **kwargs):
    completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, **kwargs)
    require(completed.returncode == 0, 'A required command failed; raw output suppressed.')
    return completed.stdout


def paused():
    require(NATIVE.is_file() and json.loads(NATIVE.read_text()).get('phase') in ('paused', 'completed'),
            'Keep native normalization paused before deploying this backend.')


def project_rows():
    ids = run(['docker', 'ps', '-aq', '--no-trunc', '--filter',
               'label=com.docker.compose.project=' + PROJECT]).split()
    require(ids, 'The existing Compose project was not found.')
    return {row['Config']['Labels']['com.docker.compose.service']: row
            for row in json.loads(run(['docker', 'inspect', *ids]))}


def prepare_model(before, revision, nft_image):
    result = deepcopy(before)
    require(result.get('name') == PROJECT, 'Unexpected Compose project name.')
    backend = result['services']['backend']
    env = backend.get('environment') or {}
    build = backend.get('build') or {}
    args = build.get('args') or {}
    require(env.get('CHAIN_ID') == '18417507517' and env.get('CHAIN_TYPE') == 'arbitrum',
            'This package targets the existing DeriW dev backend only.')
    require(backend.get('command') == ['bin/blockscout', 'start'],
            'Expected start-only backend startup; migrations must remain separate.')
    require(env.get('MIGRATION_BACKGROUND_AUTOSTART_DISABLED') == 'true'
            and args.get('MIGRATION_BACKGROUND_AUTOSTART_DISABLED') == 'true',
            'Automatic migration holds are missing from the existing configuration.')
    require(env.get('INDEXER_METRICS_ENABLED') == 'false'
            and env.get('INDEXER_DISABLE_EMPTY_BLOCKS_SANITIZER') == 'true',
            'Expected the existing metrics and empty-block scan holds.')
    require(args.get('CHAIN_TYPE') == 'arbitrum' and args.get('RELEASE_VERSION') == '10.2.6'
            and backend.get('platform') == 'linux/amd64', 'Unexpected backend build selection.')
    require(set(args) <= BUILD_ARGUMENTS, 'An unfamiliar build argument needs review.')
    require(Path(build.get('context', '')).resolve() == ROOT.resolve()
            and build.get('dockerfile') in ('docker/Dockerfile', './docker/Dockerfile'),
            'Expected the original source root and Dockerfile.')
    for key, value in args.items():
        if key not in ('CHAIN_TYPE', 'RELEASE_VERSION', 'BLOCKSCOUT_VERSION', 'API_GRAPHQL_MAX_COMPLEXITY'):
            require(value in ('', 'true', 'false'), 'An unexpected boolean build option needs review.')
        elif key == 'API_GRAPHQL_MAX_COMPLEXITY':
            require(str(value).isdigit() or value == '', 'Unexpected GraphQL build setting.')

    backend['image'] = 'deriw-blockscout:dev-bridge-verification-' + revision[:12]
    backend['pull_policy'] = 'build'
    env.update(ARBITRUM_BRIDGE_METRICS_ENABLED='true', BLOCKSCOUT_VERSION=revision)
    args['BLOCKSCOUT_VERSION'] = revision
    build.setdefault('labels', {})['org.opencontainers.image.revision'] = revision
    # This release updates backend only. Hold the existing NFT image so a later
    # whole-stack --build cannot rebuild it from new source with its old SHA.
    nft = result['services']['nft_media_handler']
    require(nft_image.startswith('sha256:') and len(nft_image) == 71, 'Expected the running NFT image ID.')
    nft['image'] = nft_image
    nft['pull_policy'] = 'never'
    nft.pop('build', None)
    nft_before = deepcopy(before['services']['nft_media_handler'])
    for key in ('image', 'pull_policy', 'build'):
        nft_before.pop(key, None)
    require({k: v for k, v in nft.items() if k not in ('image', 'pull_policy', 'build')} == nft_before,
            'An NFT worker runtime setting would change.')
    require(all(result['services'][name] == original for name, original in before['services'].items()
                if name not in ('backend', 'nft_media_handler')), 'Another service configuration would change.')
    return result


def metrics_ready(text):
    lines = set(text.splitlines())
    return 'deriw_bridge_exporter_up 1' in lines and all(
        'deriw_bridge_query_success{section="' + section + '"} 1' in lines for section in SECTIONS) and all(
        'deriw_bridge_verification_success{section="' + section + '"} 1' in lines for section in VERIFICATION)


def main():
    require(os.geteuid() == 0 and ROOT.is_dir() and COMPOSE.is_dir(),
            'Run on the existing dev Linux server as root.')
    revision = run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD']).strip()
    require(len(revision) == 40, 'Expected a committed source revision.')
    require(not any(k.startswith('COMPOSE_') and k != 'COMPOSE_PROJECT_NAME' for k in os.environ)
            and os.environ.get('COMPOSE_PROJECT_NAME', PROJECT) == PROJECT,
            'Unset shell COMPOSE_* overrides before running this script.')
    paused()
    require(run(['git', '-C', str(ROOT), 'branch', '--show-current']).strip() == 'deriw-dev',
            'The source checkout must be on deriw-dev.')
    changes = run(['git', '-C', str(ROOT), 'diff', '--name-only', 'HEAD']).splitlines()
    require(set(changes) <= {'.dockerignore'}, 'Commit or preserve tracked source edits before rebuilding.')
    initial = project_rows()
    require(initial['backend']['State']['Running'], 'The current backend must be running before this update.')
    other_ids = {name: row['Id'] for name, row in initial.items() if name != 'backend'}

    PRIVATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    saved = Path(tempfile.mkdtemp(prefix='native-metrics-', dir=str(PRIVATE)))
    original_bytes = (COMPOSE / 'docker-compose.yml').read_bytes()
    private_write(saved / 'before-docker-compose.yml', original_bytes)
    private_write(saved / 'containers.private.json', json.dumps(initial, indent=2).encode())
    try:
        emit('source_selected', source_revision=revision)

        sys.path.insert(0, str(ROOT / 'deploy/scripts'))
        import dev_config as D
        import release as R
        contract = {'compose_project': PROJECT}
        before = D.compose_model(COMPOSE, contract, {}, [COMPOSE / 'docker-compose.yml'], True)
        candidate = prepare_model(before, revision, initial['nft_media_handler']['Image'])
        selected = saved / 'candidate.compose.json'
        private_write(selected, R.snapshot_bytes(candidate))
        resolved = D.compose_model(COMPOSE, contract, {}, [selected], True)
        require(not D.model_differences(candidate, resolved), 'Compose changed the proposed settings.')
        backend = resolved['services']['backend']

        emit('building_backend', image=backend['image'])
        command = ['docker', 'build', '--platform', 'linux/amd64', '-f', 'docker/Dockerfile',
                   '-t', backend['image']]
        for key, value in sorted(backend['build'].get('labels', {}).items()):
            command += ['--label', key + '=' + str(value)]
        for key, value in sorted(backend['build']['args'].items()):
            command += ['--build-arg', key + '=' + str(value)]
        # Only committed source is sent to Docker. Private Compose data and saved
        # untracked repair directories never enter the build context.
        archive = subprocess.Popen(['git', '-C', str(ROOT), 'archive', '--format=tar', revision],
                                   stdout=subprocess.PIPE)
        try:
            built = subprocess.Popen(command + ['-'], stdin=archive.stdout)
            archive.stdout.close()
            require(built.wait() == 0 and archive.wait() == 0, 'Backend image build failed.')
        finally:
            if archive.poll() is None:
                archive.terminate()
                archive.wait()

        smoke = '''
true = Code.ensure_loaded?(Indexer.Prometheus.ArbitrumBridgeMetrics)
true = Code.ensure_loaded?(Indexer.Prometheus.ArbitrumBridgeMetrics.Source)
true = Code.ensure_loaded?(Indexer.Prometheus.ArbitrumBridgeMetrics.Verification)
true = Code.ensure_loaded?(Indexer.Prometheus.ArbitrumBridgeMetrics.RPC)
true = Code.ensure_loaded?(Indexer.Prometheus.ArbitrumBridgeMetrics.Collector)
true = Application.get_env(:indexer, Indexer.Prometheus.ArbitrumBridgeMetrics)[:enabled]
true = Explorer.Migrator.StartupControl.disabled?()
nil = Process.whereis(Explorer.Repo)
IO.puts("NATIVE_METRICS_IMAGE_OK")
'''
        checked = run(['docker', 'run', '--rm', '--pull', 'never', '--network', 'none',
                       '--read-only', '--cap-drop', 'ALL', '--tmpfs', '/tmp:rw,mode=1777',
                       '-e', 'RELEASE_DISTRIBUTION=none', '-e', 'RELEASE_TMP=/tmp',
                       '-e', 'ARBITRUM_BRIDGE_METRICS_ENABLED=true',
                       '-e', 'MIGRATION_BACKGROUND_AUTOSTART_DISABLED=true',
                       '-e', 'DISABLE_FILE_LOGGING=true', '-e', 'ERL_FLAGS=+S 2:2',
                       backend['image'], 'bin/blockscout', 'eval', smoke])
        require('NATIVE_METRICS_IMAGE_OK' in checked, 'The new image did not pass the isolated check.')

        paused()
        require(run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD']).strip() == revision,
                'The selected source commit changed while building.')
        require((COMPOSE / 'docker-compose.yml').read_bytes() == original_bytes,
                'Compose changed while building; the running backend was left unchanged.')
        require({name: row['Id'] for name, row in project_rows().items() if name != 'backend'} == other_ids,
                'Another service changed while building.')
        rendered = D.run(['docker', 'compose', '-p', PROJECT, '--project-directory', str(COMPOSE),
                          '--env-file', os.devnull, '-f', str(selected), 'config'],
                         environment=D.compose_environment({}))
        staged = COMPOSE / '.native-metrics-prepared.yml'
        require(not staged.exists(), 'A previous staged configuration needs review.')
        private_write(staged, ('# Private DeriW dev configuration; active environment values are here.\n' + rendered).encode())
        actual = D.compose_model(COMPOSE, contract, {}, [staged], True)
        require(not D.model_differences(resolved, actual), 'Rendered configuration differs from the candidate.')
        os.replace(staged, COMPOSE / 'docker-compose.yml')

        emit('recreating_backend')
        run(['docker', 'compose', '-p', PROJECT, '--project-directory', str(COMPOSE),
             '--env-file', os.devnull, '-f', str(COMPOSE / 'docker-compose.yml'),
             'up', '-d', '--no-deps', '--no-build', '--pull', 'never', 'backend'],
            env=D.compose_environment({}), timeout=360)
        deadline = time.monotonic() + 180
        last_notice = 0
        text = ''
        while time.monotonic() < deadline:
            try:
                result = subprocess.run(['docker', 'exec', 'backend', 'curl', '-fsS', '--max-time', '10',
                                         'http://127.0.0.1:4000/metrics'], capture_output=True, text=True, timeout=15)
                text = result.stdout
                ready = result.returncode == 0 and metrics_ready(text)
            except subprocess.TimeoutExpired:
                ready = False
            if ready:
                break
            if time.monotonic() - last_notice >= 20:
                emit('waiting_for_native_metrics')
                last_notice = time.monotonic()
            time.sleep(3)
        else:
            private_write(saved / 'last-metrics.private.txt', text.encode())
            raise DeploymentError('Metrics did not pass the database and on-chain verification checks; inspect the saved snapshot.')

        after = project_rows()
        require({name: row['Id'] for name, row in after.items() if name != 'backend'} == other_ids,
                'A service other than backend was recreated.')
        env = D.env_map(after['backend'])
        require(env.get('BLOCKSCOUT_VERSION') == revision and env.get('ARBITRUM_BRIDGE_METRICS_ENABLED') == 'true',
                'Running backend source or native flag differs from the candidate.')
        paused()
        for line in text.splitlines():
            if line.startswith(('deriw_bridge_exporter_up ', 'deriw_bridge_query_success{',
                                'deriw_bridge_sample_missing_origination{', 'deriw_bridge_tracker_next_parent_block{',
                                'deriw_bridge_verification_success{', 'deriw_bridge_deposit_sample_incomplete_relayed ',
                                'deriw_bridge_withdrawal_sample_missing_relay ', 'deriw_bridge_tracker_lag_blocks{',
                                'deriw_rollup_latest_batch_', 'deriw_rollup_batch_sample_finality_mismatch ',
                                'deriw_rollup_batch_sample_publication_mismatch ', 'deriw_rollup_batch_indexing_lag ')):
                print(line)
        emit('bridge_verification_metrics_deployed', source_revision=revision, image=backend['image'],
             configuration_backup=str(saved), only_recreated_service='backend',
             native_normalization_resumed=False, old_exporter_stopped=False)
        return 0
    except Exception as error:
        private_write(saved / 'error.private.log', traceback.format_exc().encode())
        if isinstance(error, DeploymentError):
            print('STOP:', error, file=sys.stderr)
        else:
            print('STOP: deployment did not complete; raw diagnostics remain private.', file=sys.stderr)
        print('Private backups and diagnostics:', saved, file=sys.stderr)
        print('The old metrics exporter was not disabled.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except DeploymentError as error:
        print('STOP:', error, file=sys.stderr)
        raise SystemExit(1)
