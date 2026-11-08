#!/usr/bin/env python3
"""Render infrastructure snapshots with synthetic values; never start services."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import dev_config as D
import release as R
import v10_infrastructure_checks as K
import v10_infrastructure_stage as S


def main():
    os.umask(0o077)
    literal = 'synthetic $LITERAL $$PAIR ${DO_NOT_EXPAND} "quotes"\nsecond line'
    manifest = {'compose_project': 'infra-snapshot-fixture'}
    before = {}
    model = {'services': {}, 'networks': {'default': {'name': 'infra-snapshot-network', 'external': True}}}
    with tempfile.TemporaryDirectory(prefix='infra-snapshot-', dir='/private/tmp') as temporary:
        root = Path(temporary)
        for number, name in enumerate(('proxy', 'redis-db', 'db', 'db-init', 'stats-db',
                                       'stats-db-init', 'backend', 'frontend'), 1):
            image = 'sha256:' + format(number, '064x')
            before[name] = {'Image': image, 'Config': {'Env': ['SPECIAL=' + literal],
                'Entrypoint': ['docker-entrypoint.sh']}, 'NetworkSettings': {'Networks': {
                    'infra-snapshot-network': {'IPAddress': '172.31.0.' + str(10 + number),
                                               'GlobalIPv6Address': ''}}}}
            model['services'][name] = {'image': image, 'environment': {'SPECIAL': literal},
                                       'networks': {'default': {}}}
        for name in ('db', 'stats-db'):
            model['services'][name].update(user='2000:2000', shm_size=6442450944,
                command=['postgres', '-c', 'shared_buffers=4GB'], volumes=[{'type': 'bind',
                'source': str(root / (name + '-data')), 'target': '/var/lib/postgresql/data'}])
        initial = root / 'initial.compose.json'
        initial.write_bytes(R.snapshot_bytes(model))
        model = D.compose_model(root, manifest, {}, [initial], True)
        artifacts = [{'source': str(root / 'nginx.conf'), 'target': '/etc/nginx/nginx.conf',
                      'type': 'bind', 'read_only': True}]
        results = []
        for component, (name, kind, _) in K.COMPONENTS.items():
            for side in ('forward', 'rollback'):
                selected = K.snapshot(model, before, component, 'sha256:' + 'f' * 64,
                                      root / 'state.json', side)
                if kind == 'nginx':
                    selected = S.nginx_snapshot(selected, artifacts)
                snapshot = root / (component + '-' + side + '.compose.json')
                snapshot.write_bytes(R.snapshot_bytes(selected))
                actual = R.check_snapshot(D, snapshot, selected, manifest, root)
                assert actual['services']['backend']['environment']['SPECIAL'] == literal
                assert actual['services'][name]['networks']['default']['ipv4_address'] == \
                    before[name]['NetworkSettings']['Networks']['infra-snapshot-network']['IPAddress']
                if kind == 'postgresql':
                    assert actual['services'][name]['entrypoint'] == K.PG_GUARD
                    assert actual['services'][name]['user'] == '2000:2000'
                    assert int(actual['services'][name]['shm_size']) == 6442450944
                changed = deepcopy(actual)
                changed['networks']['default']['ipam'] = {'config': [{'subnet': '172.30.0.0/24'}]}
                assert D.model_differences(selected, changed) == ['networks']
                changed = deepcopy(actual)
                changed['services'][name]['networks']['default']['ipv4_address'] = '172.31.0.254'
                assert D.model_differences(selected, changed) == ['services.' + name + '.networks']
                results.append({'component': component, 'side': side,
                    'empty_ipam_added_by_compose': actual['networks'] != selected['networks']})
        print(json.dumps({'status': 'passed', 'compose_version': D.run(['docker', 'compose', 'version']).strip(),
            'snapshot_round_trips': results, 'literal_dollars_quotes_newlines_preserved': True,
            'static_ips_and_pg_guard_preserved': True, 'explicit_ipam_and_ip_changes_rejected': True,
            'service_changes': False, 'image_pulls': False, 'database_calls': False,
            'network_changes': False, 'temporary_files_removed_on_exit': True}, indent=2))


if __name__ == '__main__':
    main()
