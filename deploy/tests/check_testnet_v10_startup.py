#!/usr/bin/env python3
"""Reproduce and recover an IP collision using owned disposable containers."""
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import testnet_v10_layout as L
T = L.T
LABEL = 'org.deriw.test-startup-fixture'


def main():
    os.umask(0o077)
    token = uuid.uuid4().hex[:16]
    project = 'test-startup-' + token
    network = project + '_default'
    directory = Path(tempfile.mkdtemp(prefix='.test-v10-startup-', dir=str(ROOT.parent)))
    path = directory / 'state.json'
    target = directory / 'compose.private.json'
    image = json.loads(T.P.run(['docker', 'image', 'inspect', 'redis:7-alpine']))[0]['Id']
    created = False
    try:
        T.P.run(['docker', 'network', 'create', '--internal', '--label', LABEL + '=' + token, network])
        created = True
        meta = json.loads(T.P.run(['docker', 'network', 'inspect', network]))[0]
        subnet = ipaddress.ip_network(meta['IPAM']['Config'][0]['Subnet'])
        address = str(subnet.network_address + 2)
        model = {'name': project, 'services': {}, 'networks': {'default': {'name': network, 'external': True}}}
        for name in ('sig-provider', 'user-ops-indexer'):
            model['services'][name] = {
                'image': image, 'pull_policy': 'never', 'restart': 'always',
                'labels': {LABEL: token, L.LABEL: str(path)}, 'entrypoint': ['sh', '-c'],
                'command': ['sleep 3600'], 'networks': {'default': {}}}
        model['services']['sig-provider']['networks']['default']['ipv4_address'] = address
        # Exercise the actual Compose round trip for the incident's null
        # entrypoint representation and dollar-containing private values.
        verifier_model = json.loads(json.dumps(model))
        verifier_model['services']['sc-verifier'] = {
            'image': image, 'entrypoint': [], 'environment': {
                L.VYPER_LIST_KEY: 'https://raw.githubusercontent.com/blockscout/solc-bin/main/vyper.list.json',
                'PRIVATE': 'literal$with$$pair\nand newline'}, 'networks': {'default': {}}}
        before = {'sc-verifier': {'Image': image, 'Config': {'Entrypoint': None}}}
        repair_state = {'format': L.FORMAT, 'schema_complete': True, 'before': before, 'project': project,
                        'layout': {'directory_moved': True, 'new': str(directory),
                                   'root_identity': L.identity(directory), 'original_main_sha256': 'unused'}}
        original = directory / 'relocated.compose.json'
        original.write_bytes(T.R.snapshot_bytes(verifier_model))
        repair_state['relocated_sha256'] = T.R.digest(original.read_bytes())
        runtime = directory / 'docker-compose.yml'
        runtime.write_bytes(original.read_bytes())
        L.prepare_startup_repair(path, repair_state)
        L.write_config(path, repair_state)
        L.prepare_startup_repair(path, repair_state)
        resolved = T.D.compose_model(directory, {'compose_project': project}, {}, [runtime], True)
        assert resolved['services']['sc-verifier']['environment'][L.VYPER_LIST_KEY] == L.VYPER_LIST
        assert resolved['services']['sc-verifier']['environment']['PRIVATE'] == 'literal$with$$pair\nand newline'
        T.R.write_new(target, T.R.snapshot_bytes(model))
        state = {'project': project, 'layout': {'running_services': sorted(model['services'])}}
        L.up(path, state, target, ['user-ops-indexer'])
        row = T.current(project)['user-ops-indexer']
        assert row['NetworkSettings']['Networks'][network]['IPAddress'] == address
        # A restart captured with no current address receives the first free
        # address, which belongs to sig-provider in the saved selection.
        command = ['docker', 'compose', '-p', project, '--project-directory', str(directory),
                   '--env-file', os.devnull, '-f', str(target), 'up', '-d', '--no-deps',
                   '--no-build', '--pull', 'never', 'sig-provider']
        failure = subprocess.run(command, capture_output=True, text=True, timeout=40)
        T.P.private_write(directory / 'expected-collision.private.log', failure.stdout + failure.stderr)
        assert failure.returncode != 0 and 'address already in use' in (failure.stdout + failure.stderr).lower()
        state['before'] = T.current(project)
        dynamic = L.hold_dynamic_services(path, state, model)
        assert dynamic == {'user-ops-indexer'}
        L.up(path, state, target, ['sig-provider'])
        L.restore_dynamic_services(path, state, target, dynamic, model)
        rows = T.current(project)
        assert rows['sig-provider']['NetworkSettings']['Networks'][network]['IPAddress'] == address
        assert rows['user-ops-indexer']['NetworkSettings']['Networks'][network]['IPAddress'] != address
        assert rows['user-ops-indexer']['HostConfig']['RestartPolicy']['Name'] == 'always'
        assert all(c['State']['Running'] for c in rows.values())
        # Retry with the same config hashes: docker update is not visible in
        # Compose's hash, so the explicit restart-policy restoration matters.
        dynamic = L.hold_dynamic_services(path, state, model)
        L.restore_dynamic_services(path, state, target, dynamic, model)
        assert T.current(project)['user-ops-indexer']['HostConfig']['RestartPolicy']['Name'] == 'always'
        print(json.dumps({'status': 'passed', 'address_collision_reproduced': True,
                          'saved_address_recovered': True, 'retry_and_restart_policy_verified': True,
                          'verifier_repair_compose_roundtrip_verified': True,
                          'host_ports_published': False, 'live_services_accessed': False}))
    finally:
        ids = T.P.run(['docker', 'ps', '-aq', '--filter', 'label=' + LABEL + '=' + token]).split()
        for ident in ids:
            row = json.loads(T.P.run(['docker', 'inspect', ident]))[0]
            assert row['Config']['Labels'].get(LABEL) == token
            T.P.run(['docker', 'rm', '-f', ident])
        if created:
            meta = json.loads(T.P.run(['docker', 'network', 'inspect', network]))[0]
            assert meta['Labels'].get(LABEL) == token and not meta.get('Containers')
            T.P.run(['docker', 'network', 'rm', network])


if __name__ == '__main__':
    main()
