#!/usr/bin/env python3
"""Private observations and bounded compatibility checks for dev patch releases."""
from copy import deepcopy
import json
from pathlib import Path
import re

import dev_config as D
import release as R
import v10_native_normalization as N
import v10_postgres_collations as COLLATIONS

ROOT = Path(__file__).resolve().parents[2]
COMPONENTS = {
    'nginx': ('proxy', 'nginx', None),
    'redis': ('redis-db', 'redis', None),
    'postgresql-main': ('db', 'postgresql', 'db-init'),
    'postgresql-stats': ('stats-db', 'postgresql', 'stats-db-init'),
}
LABEL = 'org.deriw.infrastructure-release'
SIDE_LABEL = 'org.deriw.infrastructure-side'
PG_GUARD = ['sh', '-ec',
    'test "$(cat "$PGDATA/PG_VERSION")" = 15 && test -s "$PGDATA/global/pg_control" '
    '|| { echo "Existing PG15 data is required" >&2; exit 73; }; '
    'exec /usr/local/bin/docker-entrypoint.sh "$@"', 'pg-patch']

PG_QUERY = COLLATIONS.CTES + r"""
SELECT jsonb_build_object(
 'version',current_setting('server_version_num')::integer,
 'system_identifier',(SELECT system_identifier::text FROM pg_control_system()),
 'database_oid',(SELECT oid::bigint FROM pg_database WHERE datname=current_database()),
 'extensions',(SELECT jsonb_object_agg(extname,extversion) FROM pg_extension),
 'prepared_transactions',(SELECT count(*) FROM pg_prepared_xacts),
 'slot_plugins',(SELECT COALESCE(jsonb_agg(DISTINCT plugin) FILTER(WHERE plugin IS NOT NULL),'[]'::jsonb) FROM pg_replication_slots),
 'replicas',(SELECT count(*) FROM pg_stat_replication),
 'in_recovery',pg_is_in_recovery(),
 'tablespaces',(SELECT count(*) FROM pg_tablespace WHERE spcname NOT IN ('pg_default','pg_global')),
 'collation_mismatches',(SELECT count(*) FROM collation_bad),
 'database_collation_mismatch',(SELECT datcollversion IS DISTINCT FROM pg_database_collation_actual_version(oid) FROM pg_database WHERE datname=current_database()),
 'float_gist_indexes',(SELECT count(DISTINCT i.indexrelid) FROM pg_index i
   CROSS JOIN LATERAL unnest(i.indclass) op(oid) JOIN pg_opclass c ON c.oid=op.oid
   JOIN pg_am am ON am.oid=c.opcmethod WHERE am.amname='gist' AND c.opcintype IN ('float4'::regtype,'float8'::regtype)),
 'ltree_installed',EXISTS(SELECT 1 FROM pg_extension WHERE extname='ltree'),
 'data_directory',current_setting('data_directory'),
 'database_encoding',getdatabaseencoding(),
 'collation_review',""" + COLLATIONS.REVIEW_JSON + r""",
 'library_versions',(SELECT COALESCE(jsonb_agg(signature ORDER BY signature::text),'[]'::jsonb)
   FROM (SELECT DISTINCT jsonb_build_array(collprovider::text,pg_collation_actual_version(oid)) AS signature
     FROM pg_collation WHERE collversion IS NOT NULL) signatures))
"""

PSQL = ['sh', '-ec', 'export PGPASSWORD="$POSTGRES_PASSWORD"; '
        'exec psql -X -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" '
        '-v ON_ERROR_STOP=1 -At -c "$1"', '--']


def catalog(root=ROOT):
    data = json.loads((root / 'deploy/infra/targets.json').read_text())
    D.require(data['format'] == 'deriw-v10-infrastructure-targets-v1' and data['platform'] == 'linux/amd64',
              'Unexpected infrastructure image catalog')
    for name in ('nginx', 'redis', 'postgresql'):
        D.require(re.fullmatch({'nginx': 'nginx', 'redis': 'redis', 'postgresql': 'postgres'}[name]
                  + r'@sha256:[a-f0-9]{64}', data[name]['reference']), 'Use a reviewed immutable official image')
    return data


def os_release(text):
    fields = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
    return {key: fields.get(key, '').strip('"') for key in ('ID', 'VERSION_ID')}


def version_command(kind):
    return {'nginx': ['sh', '-c', 'exec nginx -v 2>&1'],
            'redis': ['redis-server', '--version'], 'postgresql': ['postgres', '--version']}[kind]


def version(text):
    match = re.search(r'\b\d+\.\d+(?:\.\d+)?\b', text)
    D.require(match is not None, 'Component version could not be identified')
    return match.group(0)


def live_fingerprint(api, name, kind):
    return {'os': os_release(api.run(['docker', 'exec', name, 'cat', '/etc/os-release'])),
            'version': version(api.run(['docker', 'exec', name, *version_command(kind)]))}


def image_fingerprint(api, reference, kind):
    image = json.loads(api.run(['docker', 'image', 'inspect', reference]))[0]
    D.require(image['Os'] == 'linux' and image['Architecture'] == 'amd64', 'Expected a Linux AMD64 patch image')
    command = ['docker', 'run', '--rm', '--pull', 'never', '--network', 'none', '--read-only',
               '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--memory', '128m',
               '--entrypoint', 'sh', image['Id'], '-ec',
               'cat /etc/os-release; echo INFRA_VERSION_START; exec "$@"', '--', *version_command(kind)]
    raw = api.run(command, timeout=30)
    system, separator, binary = raw.partition('INFRA_VERSION_START\n')
    D.require(bool(separator), 'Patch image metadata unavailable')
    return {'image_id': image['Id'], 'os': os_release(system), 'version': version(binary)}


def validate_target(kind, before, candidate, target):
    D.require(before['version'] == target['from_version'],
              'Live version differs from the inventoried patch baseline; review this newer runtime separately')
    D.require(candidate['version'] == target['version'] and candidate['os']['ID'] == target['os_id'],
              'Candidate binary or operating system differs from the reviewed target')
    D.require(before['os']['ID'] == candidate['os']['ID'], 'Operating system family change needs a separate rollout')
    if kind in ('postgresql', 'nginx'):
        D.require(before['os']['VERSION_ID'] == candidate['os']['VERSION_ID'] == target['os_version'],
                  'Keep the reviewed Debian major release')
    if kind == 'postgresql':
        D.require(before['version'].startswith('15.'), 'This is a PostgreSQL 15 minor patch only')
    elif kind == 'redis':
        D.require(before['version'].startswith('8.8.'), 'This is a Redis 8.8 patch only')
        D.require(before['os']['VERSION_ID'].split('.')[:2] == candidate['os']['VERSION_ID'].split('.')[:2],
                  'Keep the captured Alpine major/minor branch')
    else:
        D.require(before['version'].startswith('1.31.'), 'This is an Nginx 1.31 patch only')


def postgres(api, name):
    # Metadata only. The SQL executes in a short read-only transaction.
    query = "BEGIN READ ONLY; SET LOCAL statement_timeout='3000ms'; " + PG_QUERY + '; COMMIT;'
    raw = api.run(['docker', 'exec', name, *PSQL, query], timeout=10)
    values = [line for line in raw.splitlines() if line.startswith('{')]
    D.require(len(values) == 1, 'PostgreSQL metadata unavailable')
    return json.loads(values[0])


def validate_postgres(data, expected=None, allow_minor=False):
    D.require(150000 <= data['version'] < 160000 and not data['in_recovery'], 'Expected the existing PostgreSQL 15 primary')
    D.require(not data['prepared_transactions'] and not data['replicas'] and not data['tablespaces'],
              'Prepared transactions, replicas or external tablespaces require a separate patch review')
    D.require(set(data['slot_plugins']) <= {'pgoutput', 'test_decoding'}, 'Review custom logical decoding plugins before 15.19')
    COLLATIONS.validate(data)
    D.require(not data['float_gist_indexes'] and not data['ltree_installed'],
              'Review PostgreSQL 15.19 float GiST or ltree reindex requirements separately')
    if expected:
        fields = ('system_identifier', 'database_oid', 'extensions', 'data_directory', 'database_encoding', 'library_versions')
        D.require(all(data[key] == expected[key] for key in fields), 'PostgreSQL cluster, database, extensions or data path changed')
        if not allow_minor:
            D.require(data['version'] == expected['version'], 'PostgreSQL version changed before the patch')


def redis(api, name):
    raw = json.loads(api.run(['docker', 'exec', name, 'redis-cli', '--json', 'CONFIG', 'GET',
                             'appendonly', 'save', 'cluster-enabled', 'dir', 'dbfilename']))
    config = raw if isinstance(raw, dict) else dict(zip(raw[::2], raw[1::2]))
    D.require(config.get('appendonly') == 'no' and config.get('cluster-enabled') == 'no' and config.get('save'),
              'Only the captured standalone Redis RDB configuration is qualified')
    D.require(config.get('dir') == '/data' and re.fullmatch(r'[A-Za-z0-9_.-]+', config.get('dbfilename', '')),
              'Redis persistence path differs from the captured data mount')
    return config


def data_mount(row, destination):
    mounts = [m for m in row.get('Mounts', []) if m['Destination'] == destination]
    D.require(len(mounts) == 1 and mounts[0]['Type'] == 'bind' and mounts[0].get('RW'),
              'Expected the existing writable data bind mount')
    path = Path(mounts[0]['Source'])
    D.require(path.is_absolute() and path.resolve() == path and path.is_dir(), 'Data bind path changed or is not a directory')
    D.require(not re.search(r'[,\r\n\0]', str(path)), 'Data bind path cannot contain mount-option separators')
    return path


def pin_addresses(model, service, row):
    """Keep the original DB endpoint so the frozen native payload remains valid."""
    result = deepcopy(model)
    declared = result['services'][service].get('networks') or {}
    D.require(isinstance(declared, dict), 'Explicit Compose networks are required')
    actual = row['NetworkSettings']['Networks']
    for key in declared:
        network = result.get('networks', {}).get(key) or {}
        name = network.get('name', key)
        D.require(name in actual and actual[name].get('IPAddress'), 'Captured service network address is missing')
        # Existing networks are retained, never created/reconfigured by a patch.
        result['networks'][key] = {'name': name, 'external': True}
        options = declared[key] or {}
        options['ipv4_address'] = actual[name]['IPAddress']
        if actual[name].get('GlobalIPv6Address'):
            options['ipv6_address'] = actual[name]['GlobalIPv6Address']
        declared[key] = options
    return result


def snapshot(model, before, component, image_id, path, side):
    name, kind, initializer = COMPONENTS[component]
    result = deepcopy(model)
    for service_name, service in result['services'].items():
        R.freeze_null_environment(service, before[service_name])
        service.pop('build', None)
        if service_name not in ('db-init', 'stats-db-init'):
            service['image'] = before[service_name]['Image']
        service['pull_policy'] = 'never'
    selected = result['services'][name]
    selected['image'] = image_id if side == 'forward' else before[name]['Image']
    selected.setdefault('labels', {}).update({LABEL: str(path), SIDE_LABEL: side})
    if kind == 'postgresql':
        D.require(before[name]['Config'].get('Entrypoint') in (['docker-entrypoint.sh'], ['/usr/local/bin/docker-entrypoint.sh'], PG_GUARD),
                  'Review the custom PostgreSQL entrypoint first')
        selected['entrypoint'] = PG_GUARD
        if side == 'forward':
            result['services'][initializer]['image'] = image_id
    return pin_addresses(result, name, before[name])


def unchanged_others(api, before, after, component):
    selected = COMPONENTS[component][0]
    api.ensure_stable({k: v for k, v in before.items() if k != selected},
                      {k: v for k, v in after.items() if k != selected}, 'An unrelated container changed during the infrastructure patch')
    D.require(all(before[k]['State']['Running'] == after[k]['State']['Running'] for k in before if k != selected),
              'An unrelated service running state changed')


def stable_database_anchor(api):
    return N.rpc(api, '''
true = Explorer.Migrator.StartupControl.disabled?()
{:ok, anchor} = Explorer.Repo.transaction(fn ->
  q = fn sql -> Explorer.Repo.query!(sql, [], log: false, timeout: 2000).rows end
  q.("SET TRANSACTION READ ONLY")
  q.("SET LOCAL statement_timeout='1500ms'")
  [[data]] = q.(~S"""
  SELECT jsonb_build_object('system_identifier',(SELECT system_identifier::text FROM pg_control_system()),
    'database_identity',jsonb_build_object('database',current_database(),
      'oid',(SELECT oid::bigint FROM pg_database WHERE datname=current_database()),
      'address',host(inet_server_addr()),'port',inet_server_port(),'postgres_major',current_setting('server_version_num')::integer/10000),
    'heap_identity',pg_relation_filenode('public.internal_transactions'::regclass)::bigint,
    'versions',(SELECT jsonb_agg(version ORDER BY version) FROM schema_migrations),
    'native_status',(SELECT to_jsonb(s) FROM migrations_status s WHERE migration_name='empty_internal_transactions_data'))
  """)
  data
end, timeout: 5000)
IO.puts("BLOCKSCOUT_DEPLOY_CHECK=" <> Jason.encode!(anchor))
''')


def check_anchor(expected, actual, progress=False):
    D.require(all(expected[k] == actual[k] for k in ('system_identifier', 'database_identity', 'heap_identity', 'versions')),
              'Cluster, frozen DB endpoint, heap identity or migration versions changed')
    if not progress:
        D.require(expected['native_status'] == actual['native_status'], 'Native checkpoint changed while paused')
    else:
        previous, current = expected['native_status'], actual['native_status']
        D.require(previous is not None and current is not None
                  and previous['meta']['v10_native_job'] == current['meta']['v10_native_job']
                  and -1 <= current['meta']['max_block_number'] <= previous['meta']['max_block_number']
                  and current['status'] in ('started', 'completed'), 'Native token or cursor continuity changed')
        D.require(previous['status'] != 'completed' or
                  (current['status'] == 'completed' and current['meta']['max_block_number'] == -1),
                  'A completed native migration cannot be reopened')
