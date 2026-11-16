#!/usr/bin/env python3
"""Build and apply the bounded testnet v9 -> v10 release, preserving live data."""
import argparse
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

# The Git-delivered tool directory is private and contains these reviewed tools.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import candidate_build as B
import dev_config as D
import release as R
import testnet_v10_preflight as P
import v10_upgrade as U

SOURCE = P.SOURCE
FORMAT = 'deriw-test-v10-upgrade-v1'
BASE = Path('/data/blockscout-test-private')
LABEL = 'org.deriw.test-v10-state'
MARKER = 'TEST_V10_CHECK='
HOLDS = {
    'MIGRATION_BACKGROUND_AUTOSTART_DISABLED': 'true',
    'INDEXER_METRICS_ENABLED': 'false',
    'INDEXER_DISABLE_EMPTY_BLOCKS_SANITIZER': 'true',
    'INDEXER_UNCATALOGED_TOKEN_TRANSFERS_ENABLED': 'false',
    'ARBITRUM_BRIDGE_METRICS_ENABLED': 'true',
}


def emit(phase, **fields):
    print(json.dumps({'phase': phase, **fields}), flush=True)


def require(ok, message):
    if not ok:
        raise P.PreflightError(message)


def save(path, state, phase=None):
    if phase:
        state['phase'] = phase
    R.update_state(path, state)


def observation(raw, marker=MARKER):
    lines = [line[len(marker):] for line in raw.splitlines() if line.startswith(marker)]
    require(len(lines) == 1, 'One authenticated release observation was expected; raw output remains private.')
    return json.loads(lines[0])


def git_source(repo, name):
    return P.run(['git', '-C', str(repo), 'show', SOURCE + ':' + name])


def current(project):
    ids = P.run(['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=' + project]).split()
    require(ids, 'The original test project is unavailable.')
    items = json.loads(P.run(['docker', 'inspect', *ids]))
    rows = {c['Config']['Labels']['com.docker.compose.service']: c for c in items}
    require(len(rows) == len(items), 'Multiple containers claim the same test service.')
    return rows


def stable(state, rows):
    before = state['before']
    require(set(rows) == set(before), 'The test service inventory changed.')
    for name in before:
        if name != 'backend':
            require(rows[name]['Id'] == before[name]['Id'] and rows[name]['Image'] == before[name]['Image']
                    and D.inspect_mounts(rows[name]['Mounts']) == D.inspect_mounts(before[name]['Mounts'])
                    and rows[name]['Config'] == before[name]['Config']
                    and rows[name]['HostConfig'] == before[name]['HostConfig'], 'Another test service changed.')
    require(D.env_map(rows['backend']).get('CHAIN_ID') == '2885', 'The current backend is not test chain 2885.')


def validate_topology(model, row):
    """Reject stale backend mounts, addresses, ports and host settings before stopping it."""
    service = model['services']['backend']
    host = row['HostConfig']
    require(not any(service.get(k) for k in ('network_mode', 'volumes_from', 'configs', 'secrets', 'links', 'devices')),
            'Backend topology uses an unsupported special attachment; review before rollout.')
    require(service.get('container_name', row['Name'].lstrip('/')) == row['Name'].lstrip('/'),
            'The backend container name differs from the captured Compose.')
    expected = []
    for volume in service.get('volumes') or []:
        kind = volume['type']
        require(kind in ('bind', 'volume'), 'Review the backend mount type before rollout.')
        source = volume.get('source')
        if kind == 'volume':
            require(source in model.get('volumes', {}), 'Anonymous backend volumes need review.')
            source = model['volumes'][source].get('name', source)
        expected.append((kind, source, volume['target'].rstrip('/'), not volume.get('read_only', False)))
    actual = [(v['Type'], v.get('Name') if v['Type'] == 'volume' else v['Source'],
               v['Destination'].rstrip('/'), v['RW']) for v in row['Mounts']]
    require(sorted(expected) == sorted(actual), 'Backend mounts differ from the captured Compose.')
    requested = {}
    for key, settings in (service.get('networks') or {}).items():
        name = model['networks'][key]['name']
        requested[name] = settings or {}
    attached = row['NetworkSettings']['Networks']
    require(set(requested) == set(attached), 'Backend network membership differs from the captured Compose.')
    for name, settings in requested.items():
        live = attached[name]
        ipam = live.get('IPAMConfig') or {}
        for field, actual_field in (('ipv4_address', 'IPv4Address'), ('ipv6_address', 'IPv6Address')):
            require((settings.get(field) or '') == (ipam.get(actual_field) or ''),
                    'Backend static addresses differ from the captured Compose.')
        require(set(settings.get('aliases') or []) <= set(live.get('Aliases') or []),
                'Backend network aliases differ from the captured Compose.')
    ports = host.get('PortBindings') or {}
    expected_ports = {}
    for port in service.get('ports') or []:
        require(str(port.get('published', '')).isdecimal(), 'Dynamic backend host ports need review.')
        key = str(port['target']) + '/' + port.get('protocol', 'tcp')
        expected_ports.setdefault(key, []).append({'HostIp': port.get('host_ip') or '', 'HostPort': str(port['published'])})
    def canonical_ports(values):
        return {key: sorted(set((('any' if p['HostIp'] in ('', '0.0.0.0', '::') else p['HostIp']), p['HostPort'])
                                for p in bindings or [])) for key, bindings in values.items()}
    require(canonical_ports(ports) == canonical_ports(expected_ports),
            'Backend published ports differ from the captured Compose.')
    restart = service.get('restart') or 'no'
    name, _, maximum = restart.partition(':')
    require(host['RestartPolicy'].get('Name', 'no') in (name, '' if name == 'no' else name)
            and host['RestartPolicy'].get('MaximumRetryCount', 0) == int(maximum or 0),
            'Backend restart policy differs from the captured Compose.')
    extra = service.get('extra_hosts') or {}
    if isinstance(extra, list):
        extra = dict(re.split('[:=]', item, maxsplit=1) for item in extra)
    actual_extra = dict(re.split('[:=]', item, maxsplit=1) for item in host.get('ExtraHosts') or [])
    require(extra == actual_extra, 'Backend host aliases differ from the captured Compose.')
    for field, setting, default in (
        ('Privileged', 'privileged', False), ('ReadonlyRootfs', 'read_only', False),
        ('Memory', 'mem_limit', 0), ('MemoryReservation', 'mem_reservation', 0),
        ('CpuShares', 'cpu_shares', 0), ('CpusetCpus', 'cpuset', ''),
        ('PidMode', 'pid', ''), ('Dns', 'dns', []), ('DnsSearch', 'dns_search', []),
        ('DnsOptions', 'dns_opt', []), ('SecurityOpt', 'security_opt', []),
        ('CapAdd', 'cap_add', []), ('CapDrop', 'cap_drop', []),
    ):
        require((host.get(field) or default) == (service.get(setting) or default),
                'Backend host settings differ from Compose: ' + setting)
    require(not host.get('Devices') and not host.get('VolumesFrom') and not host.get('Links'),
            'Undeclared backend attachments need review.')


def cursor_expression():
    # v9 deposits use a GenServer state.data map; withdrawals already use BufferedTask.
    return '''
"2885" = System.fetch_env!("CHAIN_ID")
"9.0.1" = to_string(Application.spec(:explorer, :vsn))
rows = for {name, module, task, prefix} <- [
  {:deposits, Indexer.Fetcher.Arbitrum.TrackingMessagesOnL1, :check_new, "arbitrum_to_l2_scan_v1:"},
  {:withdrawals, Indexer.Fetcher.Arbitrum.TrackingBatchesStatuses, :new_executions, "arbitrum_to_l1_execution_scan_v1:"}
] do
  raw = :sys.get_state(Process.whereis(module), 5000)
  state = Map.get(raw, :callback_module_state, raw)
  config = state.config
  next = if name == :deposits and Map.has_key?(state, :data),
    do: get_in(state, [:data, :new_msg_to_l2_start_block]),
    else: get_in(state, [:task_data, task, :start_block])
  true = is_integer(next) and next >= 0
  {args, contract, interval, range} = if name == :deposits,
    do: {config.json_l1_rpc_named_arguments, config.l1_bridge_address, config.recheck_interval, config.l1_rpc_block_range},
    else: {config.l1_rpc.json_rpc_named_arguments, config.l1_outbox_address, state.intervals[task], config.l1_rpc.logs_block_range}
  {:ok, parent} = EthereumJSONRPC.fetch_chain_id(args)
  true = is_integer(parent) and parent >= 0
  addresses = [config.l1_rollup_address, contract]
  true = Enum.all?(addresses, &Regex.match?(~r/^0x[0-9a-fA-F]{40}$/, &1))
  identity = Enum.join([parent | Enum.map(addresses, &String.downcase/1)], ":")
  key = prefix <> Base.encode16(:crypto.hash(:sha256, identity), case: :lower)
  [[value]] = Explorer.Repo.query!(~S"""
  INSERT INTO last_fetched_counters(counter_type,value,inserted_at,updated_at)
  VALUES($1,$2,timezone('UTC',now()),timezone('UTC',now()))
  ON CONFLICT(counter_type) DO UPDATE SET value=GREATEST(last_fetched_counters.value,EXCLUDED.value),updated_at=EXCLUDED.updated_at
  RETURNING value
  """, [key, Decimal.new(next)], log: false, timeout: 5000).rows
  saved = Decimal.to_integer(value)
  true = saved >= next and Decimal.equal?(value, Decimal.new(saved))
  %{tracker: name, counter_key: key, saved_next_parent_block: saved,
    live_next_parent_block: next, interval_ms: interval, blocks_per_request: range, parent_chain_id: parent}
end
IO.puts("''' + MARKER + '''" <> Jason.encode!(%{cursors: rows}))
'''


def probe_expression(repo):
    code = git_source(repo, 'deploy/scripts/probe_dev_bridge_rpc.exs')
    code = code.rsplit('\nDeriW.DevBridgeRPCProbe.run(', 1)[0]
    code = code.replace('DeriW.DevBridgeRPCProbe', 'DeriW.TestnetBridgeRPCProbe')
    return '''
"2885" = System.fetch_env!("CHAIN_ID")
''' + code + '''
{:ok, anchors} = Explorer.Repo.transaction(fn ->
  q = fn sql -> Explorer.Repo.query!(sql, [], timeout: 2000, log: false).rows end
  q.("SET TRANSACTION READ ONLY")
  q.("SET LOCAL statement_timeout = '1500ms'")
  a = q.("SELECT originating_transaction_block_number FROM arbitrum_crosslevel_messages WHERE direction='to_l2' AND originating_transaction_block_number IS NOT NULL ORDER BY message_id DESC LIMIT 1")
  b = q.("SELECT t.block_number FROM arbitrum_l1_executions e JOIN arbitrum_lifecycle_l1_transactions t ON t.id=e.execution_id ORDER BY e.message_id DESC LIMIT 1")
  [a, b]
end, timeout: 5000)
contexts = for {name, module, [[anchor]], topic} <- [
  {:deposits, Indexer.Fetcher.Arbitrum.TrackingMessagesOnL1, Enum.at(anchors,0), EthereumJSONRPC.Arbitrum.Constants.Events.message_delivered()},
  {:withdrawals, Indexer.Fetcher.Arbitrum.TrackingBatchesStatuses, Enum.at(anchors,1), EthereumJSONRPC.Arbitrum.Constants.Events.outbox_transaction_executed()}
], anchor >= 5000 do
  raw = :sys.get_state(Process.whereis(module), 5000)
  config = Map.get(raw, :callback_module_state, raw).config
  %{name: name, anchor: anchor, topic: topic,
    address: if(name == :deposits, do: config.l1_bridge_address, else: config.l1_outbox_address),
    args: if(name == :deposits, do: config.json_l1_rpc_named_arguments, else: config.l1_rpc.json_rpc_named_arguments)}
end
passed = length(contexts) == 2 and DeriW.TestnetBridgeRPCProbe.run(contexts) == :ok
IO.puts("''' + MARKER + '''" <> Jason.encode!(%{passed: passed}))
'''


def candidate_model(before, rows, image, state_path, wide):
    result = deepcopy(before)
    backend = result['services']['backend']
    R.freeze_null_environment(backend, rows['backend'])
    # Retain image-provided runtime configuration too; new image defaults must
    # not silently remove an existing test setting.
    env = {**D.env_map(rows['backend']), **backend.get('environment', {})}
    backend['environment'] = env
    require(env.get('CHAIN_ID') == '2885' and env.get('CHAIN_TYPE') == 'arbitrum', 'Expected the captured test environment.')
    require(D.env_map(rows['backend']).get('NFT_MEDIA_HANDLER_ENABLED') == 'false',
            'This backend-only rollout requires the captured disabled NFT worker mode.')
    env.update(HOLDS)
    env.update(RELEASE_VERSION='10.2.6', BLOCKSCOUT_VERSION=SOURCE)
    if wide:
        env.update(INDEXER_ARBITRUM_DEPOSITS_L1_BLOCKS_RANGE='10000', INDEXER_ARBITRUM_WITHDRAWALS_L1_BLOCKS_RANGE='10000')
    backend.update(image=image, command=['bin/blockscout', 'start'], pull_policy='never')
    backend['entrypoint'] = rows['backend']['Config'].get('Entrypoint') or []
    backend['working_dir'] = rows['backend']['Config'].get('WorkingDir') or '/app'
    backend['stop_signal'] = rows['backend']['Config'].get('StopSignal') or 'SIGTERM'
    if rows['backend']['Config'].get('Healthcheck'):
        require('healthcheck' in backend, 'An image-provided backend healthcheck needs review.')
    backend.pop('build', None)
    backend.setdefault('labels', {})[LABEL] = str(state_path)
    require(all(result['services'][name] == service for name, service in before['services'].items() if name != 'backend'),
            'Another service declaration changed.')
    return result


def rpc(row, code, log):
    result = subprocess.run(['docker', 'exec', row['Id'], 'bin/blockscout', 'rpc', code],
                            text=True, capture_output=True, timeout=150)
    P.private_write(log, result.stdout + result.stderr)
    require(result.returncode == 0, 'A backend observation failed; inspect its private log.')
    return observation(result.stdout)


def change(command, path, operation, timeout=30):
    """Keep mutation output private while returning a useful journal reference."""
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    P.private_write(path.parent / (operation + '-' + uuid.uuid4().hex[:8] + '.private.log'), result.stdout + result.stderr)
    require(result.returncode == 0, 'Deployment operation failed: ' + operation + '; its output is in the private journal.')


def schema_space(state):
    available = os.statvfs(state['data_host_path'])
    free = available.f_bavail * available.f_frsize
    require(free >= 100 * 1024**3 and state.get('schema_start_free_bytes', free) - free <= 20 * 1024**3,
            'Schema filesystem reserve/consumption limit reached.')


def remove_clients(path):
    ids = P.run(['docker', 'ps', '-aq', '--filter', 'label=' + LABEL + '=' + str(path)]).split()
    for identity in ids:
        row = json.loads(P.run(['docker', 'inspect', identity]))[0]
        if row['Name'].startswith('/deriw-test-v10-schema-'):
            require((row['Config'].get('Labels') or {}).get(LABEL) == str(path), 'Schema-client ownership changed.')
            P.run(['docker', 'rm', '-f', identity])


def client(state, path, mode, baseline=None):
    remove_clients(path)
    repo = Path(state['repo'])
    plan = json.loads(git_source(repo, 'deploy/upgrades/v10.2.6.json'))
    payload = U.payload(plan, baseline, mode)
    script = b'"2885" = System.fetch_env!("CHAIN_ID")\n' + git_source(repo, 'deploy/scripts/v10/migrate.exs').encode()
    data = U.archive(state['environment'], payload, script)
    name = 'deriw-test-v10-schema-' + uuid.uuid4().hex[:12]
    command = U.client_command(state['image_id'], state['network'], name, str(path))
    command[2:2] = ['--label', LABEL + '=' + str(path)]
    log = path.parent / (mode + '-' + uuid.uuid4().hex[:8] + '.private.log')
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        # The bounded schema sequence is allowed 30 minutes; each SQL statement
        # has the qualified client's 120s server deadline and 2s lock deadline.
        started = time.monotonic()
        try:
            out, err = process.communicate(data, timeout=30)
        except subprocess.TimeoutExpired:
            while True:
                require(time.monotonic() - started < 1800, 'Schema client exceeded its execution budget.')
                emit('waiting_for_schema_client', mode=mode, elapsed_seconds=int(time.monotonic() - started))
                schema_space(state)
                try:
                    out, err = process.communicate(timeout=30)
                    break
                except subprocess.TimeoutExpired:
                    pass
        P.private_write(log, out + err)
        schema_space(state)
        require(process.returncode == 0, 'The bounded schema client failed; review the private log and journal before recovery.')
        result = observation(out.decode(), 'BLOCKSCOUT_DEPLOY_CHECK=')
        P.private_write(path.parent / (mode + '-result-' + uuid.uuid4().hex[:8] + '.private.json'), R.encoded(result))
        return result
    finally:
        try:
            B.terminate(process)
        finally:
            remove_clients(path)
            if not log.exists():
                out, err = process.communicate(timeout=5)
                P.private_write(log, out + err)


def ensure_private_config(repo, directory):
    require(not directory.is_symlink() and directory.parent.resolve() == repo.resolve(), 'Unexpected private test Compose path.')
    require(not P.run(['git', '-C', str(repo), 'ls-files', '--', 'docker-compose.test']).strip(),
            'The private test configuration path is tracked.')
    ignored = subprocess.run(['git', '-C', str(repo), 'check-ignore', '-q', '--', 'docker-compose.test/'], capture_output=True)
    if ignored.returncode != 0:
        exclude = Path(P.run(['git', '-C', str(repo), 'rev-parse', '--git-path', 'info/exclude']).strip())
        if not exclude.is_absolute():
            exclude = repo / exclude
        with exclude.open('a') as stream:
            stream.write('\n/docker-compose.test/\n')
    directory.mkdir(mode=0o700, exist_ok=True)
    require(directory.stat().st_uid == os.geteuid() and directory.stat().st_mode & 0o077 == 0,
            'The private test directory must be owned and mode 0700.')


def prepare(repo, preflight, backup, journal=None):
    require(backup, 'Record the existing test database backup with --existing-backup.')
    require(preflight.parent.resolve() == BASE.resolve() and not preflight.is_symlink()
            and preflight.stat().st_uid == os.geteuid() and preflight.stat().st_mode & 0o077 == 0,
            'Select the original private test preflight directory.')
    report = json.loads((preflight / 'report.json').read_text())
    require(report['chain_id'] == '2885' and report['runtime_version'] == '9.0.1'
            and report['target_application_revision'] == SOURCE and report['platform'] == 'linux/amd64'
            and report['database_route_matches_inspected_container'], 'The qualified test preflight does not match this rollout.')
    require(report['schema']['types_match_reviewed_schema_path']
            and report['schema']['selected_relations_within_reviewed_1_gib_budget']
            and report['schema']['selected_drop_and_export_tables_empty']
            and [len(report['schema']['pending_initial_schema_versions'][d]) for d in ('repo', 'account', 'arbitrum')] == [26, 2, 1],
            'The bounded initial 29-version schema path needs review.')
    project, rows = P.select_project('backend')
    before = json.loads((preflight / 'containers.private.json').read_text())
    require(set(rows) == set(before) and all(rows[n]['Id'] == before[n]['Id'] and rows[n]['Image'] == before[n]['Image']
            and D.inspect_mounts(rows[n]['Mounts']) == D.inspect_mounts(before[n]['Mounts'])
            and rows[n]['Config'] == before[n]['Config'] and rows[n]['HostConfig'] == before[n]['HostConfig']
            for n in rows), 'Test containers/settings changed after the supplied preflight.')
    require(not any(k.startswith('COMPOSE_') for k in os.environ), 'Unset shell COMPOSE_* overrides for this rollout.')
    captured = (preflight / 'compose.private.json').read_text()
    if D.compose_json_escapes_dollars():
        captured = captured.replace('$$', '$')
    model = json.loads(captured)
    require(not D.live_environment_differences(model, rows), 'The captured Compose environment differs from the running stack.')
    validate_topology(model, rows['backend'])
    directory = repo / 'docker-compose.test'
    require(not (directory / 'docker-compose.yml').exists(), 'A private test Compose already exists; review it before the initial v10 rollout.')
    raw = observation((preflight / 'database-observation.private.txt').read_text(), P.MARKER)
    primitives = P.load_primitives(repo)
    data_directory = raw['metadata']['server'][0][1]
    resolved = P.run(['docker', 'exec', rows['db']['Id'], 'readlink', '-f', data_directory]).strip()
    mapped = primitives.mount_for(resolved, rows['db']['Mounts'])
    require(mapped['status'] == 'mapped', 'The original database data path must remain mounted.')
    host = Path(mapped['host_path']).resolve(strict=True)
    free = os.statvfs(host)
    require(free.f_bavail * free.f_frsize >= 120 * 1024**3, 'Keep at least 120 GiB available before the initial schema phase.')
    docker_root = Path(json.loads(P.run(['docker', 'info', '--format', '{{json .DockerRootDir}}'])))
    docker_space = os.statvfs(docker_root)
    require(docker_space.f_bavail * docker_space.f_frsize >= 10 * 1024**3, 'Docker build storage needs at least 10 GiB available.')
    root = Path(tempfile.mkdtemp(prefix='upgrade-', dir=str(BASE)))
    path = root / 'state.json'
    state = {'format': FORMAT, 'repo': str(repo), 'preflight': str(preflight), 'project': project, 'source': SOURCE,
             'before': before, 'phase': 'preparing', 'existing_backup_recorded': True,
             'data_host_path': str(host), 'compose_directory': str(directory), 'schema_complete': False,
             'tool_script': str(Path(__file__).resolve())}
    save(path, state)
    if journal is not None:
        journal.update(path=path, state=state)
    emit('test_release_preparing', state=str(path), source_revision=SOURCE)
    oldenv = D.env_map(rows['backend'])
    uid = P.run(['docker', 'exec', rows['backend']['Id'], 'id', '-u']).strip()
    gid = P.run(['docker', 'exec', rows['backend']['Id'], 'id', '-g']).strip()
    require(uid.isdecimal() and gid.isdecimal(), 'The existing application user could not be identified.')
    args = {'CHAIN_TYPE': 'arbitrum', 'RELEASE_VERSION': '10.2.6', 'BLOCKSCOUT_VERSION': SOURCE,
            'MIGRATION_BACKGROUND_AUTOSTART_DISABLED': 'true',
            'BLOCKSCOUT_UID': uid if uid != '0' else '10001', 'BLOCKSCOUT_GID': gid if gid != '0' else '10001'}
    for key in ('DISABLE_API', 'API_V1_READ_METHODS_DISABLED', 'API_V1_WRITE_METHODS_DISABLED', 'BRIDGED_TOKENS_ENABLED', 'MUD_INDEXER_ENABLED'):
        value = oldenv.get(key)
        if value is not None:
            require(value in ('', 'true', 'false'), 'A compile-time boolean setting needs review.')
            args[key] = value
    if oldenv.get('API_GRAPHQL_MAX_COMPLEXITY'):
        require(oldenv['API_GRAPHQL_MAX_COMPLEXITY'].isdecimal(), 'The GraphQL build setting needs review.')
        args['API_GRAPHQL_MAX_COMPLEXITY'] = oldenv['API_GRAPHQL_MAX_COMPLEXITY']
    source = {'source_commit': SOURCE, 'platform': report['platform'], 'build_arguments': args,
              'tag': 'deriw-blockscout:test-v10.2.6-' + SOURCE[:12]}
    source['fingerprint'] = hashlib.sha256(R.encoded(source)).hexdigest()
    B.compile_image(D, repo, source, root / 'build.private.log')
    image = json.loads(P.run(['docker', 'image', 'inspect', source['tag']]))[0]
    require(image['Config']['Labels']['org.opencontainers.image.revision'] == SOURCE
            and image['Config']['Labels']['org.deriw.build-fingerprint'] == source['fingerprint'],
            'The built image differs from the selected source.')
    state.update(image_id=image['Id'], image_tag=source['tag'], build_arguments=args)
    # This probe is bounded and read-only. A provider that rejects wide reads
    # keeps its original log range; it does not block the schema upgrade.
    try:
        wide = rpc(rows['backend'], probe_expression(repo), root / 'parent-rpc-probe.private.log')['passed'] is True
    except Exception:
        wide = False
    emit('test_rpc_probe_complete', ten_thousand_block_reads_verified=wide)
    candidate = candidate_model(model, rows, source['tag'], path, wide)
    candidate['services']['backend']['user'] = uid + ':' + gid
    state['environment'] = {k: v for k, v in candidate['services']['backend']['environment'].items() if v is not None}
    common_networks = set(rows['backend']['NetworkSettings']['Networks']) & set(rows['db']['NetworkSettings']['Networks'])
    require(len(common_networks) == 1, 'One original shared database network is required for the schema client.')
    state['network'] = next(iter(common_networks))
    state['wide_reads_verified'] = wide
    selected = root / 'candidate.compose.json'
    R.write_new(selected, R.snapshot_bytes(candidate))
    state['candidate_sha256'] = R.digest(selected.read_bytes())
    resolved_model = D.compose_model(directory, {'compose_project': project}, {}, [selected], True)
    require(not D.model_differences(candidate, resolved_model), 'Compose changed the proposed test settings.')
    expression = '''
"10.2.6" = to_string(Application.spec(:explorer, :vsn))
"2885" = Application.get_env(:block_scout_web, :chain_id)
true = Explorer.Migrator.StartupControl.disabled?()
true = Application.get_env(:indexer, Indexer.Prometheus.ArbitrumBridgeMetrics)[:enabled]
false = Application.get_env(:indexer, Indexer.Prometheus.Metrics)[:enabled]
true = Code.ensure_loaded?(Indexer.Fetcher.Arbitrum.Utils.Db.MessageScanCursor)
true = function_exported?(Indexer.Fetcher.Arbitrum.Utils.Db.MessageScanCursor, :execution_key, 3)
nil = Process.whereis(Explorer.Repo)
IO.puts("TEST_V10_IMAGE_OK")
'''
    smoke_env = {**state['environment'], 'RELEASE_DISTRIBUTION': 'none', 'RELEASE_TMP': '/tmp',
                 'DISABLE_FILE_LOGGING': 'true', 'ERL_FLAGS': '+S 2:2'}
    command = ['docker', 'run', '--rm', '-i', '--pull', 'never', '--network', 'none', '--read-only',
               '--cap-drop', 'ALL', '--tmpfs', '/tmp:rw,mode=1777', '--memory', '1g', '--cpus', '2',
               '--entrypoint', 'sh', state['image_id'], '-c', B.eval_script(expression)]
    checked = subprocess.run(command, input=json.dumps(smoke_env), capture_output=True, text=True, timeout=120)
    P.private_write(root / 'image-smoke.private.log', checked.stdout + checked.stderr)
    require(checked.returncode == 0 and 'TEST_V10_IMAGE_OK' in checked.stdout, 'The isolated test image check failed.')
    state['schema_before'] = client(state, path, 'inspect')
    stable(state, current(project))
    state['preparation_complete'] = True
    save(path, state, 'prepared')
    emit('test_release_prepared', state=str(path), image=source['tag'], schema_versions=29,
         source_checkout_changed=False, schema_writes=False, services_restarted=False)
    return path, state


def runtime_expression(state):
    return '''
"10.2.6" = to_string(Application.spec(:explorer, :vsn))
"2885" = Application.get_env(:block_scout_web, :chain_id)
"''' + SOURCE + '''" = Application.get_env(:block_scout_web, :version)
true = Explorer.Migrator.StartupControl.disabled?()
false = Application.get_env(:indexer, Indexer.Prometheus.Metrics)[:enabled]
true = Enum.all?(Supervisor.which_children(Explorer.Supervisor), fn {id, _, _, _} ->
  not (is_atom(id) and String.starts_with?(Atom.to_string(id), "Elixir.Explorer.Migrator.")) and
    id != Explorer.TokenInstanceOwnerAddressMigration.Supervisor
end)
handoff = Jason.decode!(~S|''' + json.dumps(state['handoff'], separators=(',', ':')) + '''|)
rows = for row <- handoff["cursors"] do
  {module, task, field} = case row["tracker"] do
    "deposits" -> {Indexer.Fetcher.Arbitrum.TrackingMessagesOnL1, :check_new, :l1_messages_scan_cursor_key}
    "withdrawals" -> {Indexer.Fetcher.Arbitrum.TrackingBatchesStatuses, :new_executions, :l1_executions_scan_cursor_key}
  end
  data = :sys.get_state(Process.whereis(module), 5000).callback_module_state
  true = data.config[field] == row["counter_key"]
  next = get_in(data, [:task_data, task, :start_block])
  true = is_integer(next) and next >= row["saved_next_parent_block"]
  saved = Explorer.Chain.Cache.Counters.LastFetchedCounter.get(row["counter_key"], nullable: true)
  true = Decimal.compare(saved, Decimal.new(row["saved_next_parent_block"])) in [:eq, :gt]
  range = if row["tracker"] == "deposits", do: data.config.l1_rpc_block_range,
    else: data.config.l1_executions_block_range
  true = data.intervals[task] == row["interval_ms"]
  true = range == ''' + ('10000' if state['wide_reads_verified'] else 'row["blocks_per_request"]') + '''
  %{tracker: row["tracker"], next_parent_block: next, durable_next_parent_block: Decimal.to_integer(saved),
    blocks_per_request: range, interval_ms: data.intervals[task]}
end
IO.puts("''' + MARKER + '''" <> Jason.encode!(%{version: "10.2.6", chain_id: "2885",
  background_migrators_held: true, bridge_cursors: rows}))
'''


def apply(path, state):
    require(state.get('preparation_complete') is True, 'Preparation did not complete; do not activate this journal.')
    project = state['project']
    rows = current(project)
    stable(state, rows)
    image = json.loads(P.run(['docker', 'image', 'inspect', state['image_tag']]))[0]
    require(image['Id'] == state['image_id'] and image['Config']['Labels']['org.opencontainers.image.revision'] == SOURCE,
            'The prepared test image identity changed.')
    selected = path.parent / 'candidate.compose.json'
    require(R.digest(selected.read_bytes()) == state['candidate_sha256'], 'The prepared private configuration changed.')
    config_dir = Path(state['compose_directory'])
    ensure_private_config(Path(state['repo']), config_dir)
    target = config_dir / 'docker-compose.yml'
    require(not target.exists() or target.read_bytes() == selected.read_bytes(), 'Another test configuration occupies the target path.')
    if not state.get('handoff'):
        require(rows['backend']['Id'] == state['before']['backend']['Id'] and rows['backend']['State']['Running'],
                'Keep the original v9 backend running until the cursor handoff.')
        state['handoff'] = rpc(rows['backend'], cursor_expression(), path.parent / 'bridge-handoff.private.log')
        require({c['tracker'] for c in state['handoff']['cursors']} == {'deposits', 'withdrawals'}, 'Both test cursors are required.')
        save(path, state, 'cursors_saved')
        emit('test_bridge_cursors_saved', **state['handoff'])
    if not state['schema_complete']:
        require(rows['backend']['Id'] == state['before']['backend']['Id'], 'The original backend changed before schema activation.')
        save(path, state, 'stopping_backend')
        change(['docker', 'update', '--restart=no', rows['backend']['Id']], path, 'hold-old-backend-restart')
        change(['docker', 'stop', '-t', '300', rows['backend']['Id']], path, 'stop-old-backend', timeout=330)
        require(not current(project)['backend']['State']['Running'], 'The original backend did not stop.')
        save(path, state, 'backend_stopped')
        inspected = client(state, path, 'inspect', state['schema_before'])
        require(inspected['database_identity'] == state['schema_before']['database_identity']
                and inspected['heap_identity'] == state['schema_before']['heap_identity'], 'The original database/heap changed.')
        filesystem = os.statvfs(state['data_host_path'])
        state['schema_start_free_bytes'] = filesystem.f_bavail * filesystem.f_frsize
        require(state['schema_start_free_bytes'] >= 120 * 1024**3, 'Schema working-space reserve is unavailable.')
        save(path, state, 'schema_started')
        emit('test_schema_migrating', state=str(path), background_migrators_autostart=False)
        migrated = client(state, path, 'migrate', state['schema_before'])
        require(migrated['heap_identity'] == state['schema_before']['heap_identity'], 'The historical internal-transaction heap changed.')
        require(migrated['pending'] == {'repo': [20260721000000, 20260812000000], 'account': [], 'arbitrum': []},
                'The completed schema selection differs from the reviewed cutoff.')
        state['schema_complete'] = True
        save(path, state, 'schema_complete')
        emit('test_schema_complete', initial_schema_versions=29, historical_heap_rewritten=False,
             deferred_index_versions=[20260721000000, 20260812000000])
    if not target.exists():
        R.write_new(target, selected.read_bytes())
    save(path, state, 'configuration_selected')
    stable(state, current(project))
    emit('test_backend_recreating', image=state['image_tag'])
    change(['docker', 'compose', '-p', project, '--project-directory', str(config_dir), '--env-file', os.devnull,
            '-f', str(target), 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', 'backend'],
           path, 'recreate-test-backend', timeout=360)
    save(path, state, 'backend_started')
    deadline = time.monotonic() + 240
    runtime = None
    while time.monotonic() < deadline:
        try:
            live = current(project)
            stable(state, live)
            require(live['backend']['Image'] == state['image_id'] and live['backend']['State']['Running'],
                    'The selected backend image is not running.')
            ready = P.run(['docker', 'exec', live['backend']['Id'], 'curl', '-fsS', '--max-time', '5',
                           'http://127.0.0.1:4000/api/health/readiness'], timeout=10)
            require(ready.strip() == 'OK', 'Backend readiness has not passed.')
            runtime = rpc(live['backend'], runtime_expression(state), path.parent / ('runtime-' + uuid.uuid4().hex[:8] + '.private.log'))
            metrics = P.run(['docker', 'exec', live['backend']['Id'], 'curl', '-fsS', '--max-time', '10',
                             'http://127.0.0.1:4000/metrics'], timeout=15)
            P.private_write(path.parent / ('metrics-' + uuid.uuid4().hex[:8] + '.private.txt'), metrics)
            require('deriw_bridge_exporter_up 1' in metrics and all(
                'deriw_bridge_query_success{section="' + section + '"} 1' in metrics
                for section in ('deposits', 'withdrawals', 'rollup', 'published', 'confirmed')),
                'The native bridge database metrics are not ready.')
            verification = {section: ('deriw_bridge_verification_success{section="' + section + '"} 1') in metrics
                            for section in ('batch_head', 'batches', 'deposits', 'parent_finalized', 'parent_latest', 'parent_safe', 'withdrawals')}
            runtime['rpc_verification_success'] = verification
            require(all(verification.values()), 'Native bridge on-chain verification is not ready; metrics remain in the private journal.')
            break
        except P.PreflightError:
            emit('waiting_for_test_backend')
            time.sleep(5)
    else:
        raise P.PreflightError('Test backend verification did not complete; review the journal/private logs.')
    save(path, state, 'active')
    emit('test_v10_deployed', source_revision=SOURCE, image=state['image_tag'], state=str(path),
         private_compose=str(target), only_recreated_service='backend', source_checkout_changed=False,
         background_migrators_autostart=False, **runtime)


def check_host():
    require(os.geteuid() == 0 and sys.platform.startswith('linux'), 'Run this test release on its Linux server as root.')
    require(BASE.is_dir() and not BASE.is_symlink() and BASE.stat().st_uid == 0 and BASE.stat().st_mode & 0o077 == 0,
            'Use the existing private test preflight directory.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path('/data/blockscout'))
    parser.add_argument('--preflight', type=Path)
    parser.add_argument('--existing-backup', action='store_true')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--resume', type=Path)
    args = parser.parse_args(argv)
    os.umask(0o077)
    path = None
    state = None
    journal = {}
    try:
        check_host()
        with (BASE / 'upgrade.lock').open('a+') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise P.PreflightError('Another test upgrade holds the deployment lock.') from None
            if args.resume:
                path = args.resume.resolve(strict=True)
                require(path.name == 'state.json' and path.parent.parent == BASE.resolve()
                        and path.stat().st_mode & 0o077 == 0 and not args.resume.is_symlink(), 'Select this tool\'s private test journal.')
                state = json.loads(path.read_text())
                require(state.get('format') == FORMAT and state.get('source') == SOURCE and state.get('existing_backup_recorded'),
                        'The saved test release identity does not match this tool.')
                require(state.get('preparation_complete') is True, 'This journal did not complete preparation; create a new plan.')
            else:
                require(args.preflight is not None, 'Select the successful test preflight with --preflight.')
                path, state = prepare(args.repo.resolve(), args.preflight.resolve(), args.existing_backup, journal)
            if not args.prepare_only:
                apply(path, state)
        return 0
    except Exception as error:
        path, state = journal.get('path', path), journal.get('state', state)
        if path is not None and state is not None:
            P.private_write(path.parent / ('failure-' + uuid.uuid4().hex[:8] + '.private.log'), traceback.format_exc())
            state['failed_phase'] = state.get('phase')
            save(path, state, 'needs_attention')
            emit('test_release_needs_attention', state=str(path), failed_phase=state['failed_phase'])
        print('STOP:', str(error) if isinstance(error, P.PreflightError) else 'Release failed; raw diagnostics withheld.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
