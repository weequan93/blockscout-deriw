from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import current_ops as O
import v10_native_normalization as N
import v10_bridge_deploy as P


class CurrentOpsTests(unittest.TestCase):
    def test_current_production_is_contained_and_classified(self):
        report = O.source_check()
        self.assertEqual(report['status'], 'source_consolidated')
        self.assertEqual(report['production_commits_missing_from_release'], 0)
        self.assertEqual(report['unclassified_custom_commits'], [])

    def test_new_missing_unclassified_production_commit_requires_review(self):
        calls = []
        def git(*args, **kwargs):
            calls.append(args)
            if args[0] == 'rev-parse':
                return 'a' * 40 if args[2].startswith('origin') else 'b' * 40
            if args[0] == 'merge-base':
                return 'c' * 40
            return 'a' * 40
        with patch.object(O, 'git', side_effect=git):
            report = O.source_check()
        self.assertEqual(report['status'], 'source_review_required')
        self.assertEqual(report['production_commits_missing_from_release'], 1)
        self.assertEqual(report['unclassified_custom_commits'], ['a' * 40])
        self.assertTrue(all(call[0] in ('rev-parse', 'rev-list', 'merge-base') for call in calls))

    def test_image_release_mapping_requires_digest_and_revision(self):
        snapshot = json.loads(O.SNAPSHOT.read_text())
        known = snapshot['images']['stats']
        row = {'Image': known['image_id']}
        image = {'Id': row['Image'], 'Config': {'Labels': {
            'org.opencontainers.image.revision': known['revision']}}}
        self.assertEqual(O.image_record('stats', row, image, snapshot)['mapped_release_as_of_2026_11_06'], '2.19.1')
        image['Config']['Labels']['org.opencontainers.image.revision'] = 'a' * 40
        self.assertIsNone(O.image_record('stats', row, image, snapshot)['mapped_release_as_of_2026_11_06'])
        image['Config']['Labels']['org.opencontainers.image.revision'] = known['revision']
        row['Image'] = image['Id'] = 'sha256:' + 'a' * 64
        self.assertIsNone(O.image_record('stats', row, image, snapshot)['mapped_release_as_of_2026_11_06'])

    def test_unlabeled_database_maps_only_its_observed_digest(self):
        snapshot = json.loads(O.SNAPSHOT.read_text())
        identity = snapshot['images']['db']['image_id']
        report = O.image_record('db', {'Image': identity}, {'Id': identity}, snapshot)
        self.assertEqual(report['mapped_release_as_of_2026_11_06'], '15.18')

    def test_inventory_exports_allowlist_and_uses_read_only_commands(self):
        identity = 'sha256:' + 'a' * 64
        secret = 'private-value-DO-NOT-PRINT'
        runtime = {'frontend': {'Image': identity, 'Config': {'Env': ['PASSWORD=' + secret]}}}
        images = [{'Id': identity, 'Created': '2026-11-06T00:00:00Z', 'RepoTags': [secret],
                   'Config': {'Env': ['DATABASE_URL=' + secret], 'Labels': {
                       'secret': secret, 'org.opencontainers.image.version': secret,
                       'org.opencontainers.image.revision': secret}}}]
        commands = []
        inspections = []
        def run(command, **kwargs):
            commands.append(command)
            if command[:3] == ['docker', 'image', 'inspect']:
                return json.dumps(images)
            if command[:4] == ['docker', 'exec', 'frontend', 'node']:
                return json.dumps({'node': 'v22.15.0', 'next': '16.2.10', 'react': '19.1.0',
                                   'react-server-dom-webpack': secret, 'environment': secret})
            return 'example 15.18'
        api = types.SimpleNamespace(configuration=lambda *a: ({}, {}, {}),
            inspect_project=lambda *a: inspections.append(True) or deepcopy(runtime), run=run,
            ensure_stable=O.D.ensure_stable)
        report = O.components(api)
        self.assertNotIn(secret, json.dumps(report))
        self.assertIsNone(report['images'][0]['oci_version'])
        self.assertIsNone(report['versions']['frontend']['react-server-dom-webpack'])
        self.assertEqual(report['versions']['frontend']['next'], '16.2.10')
        self.assertEqual(len(inspections), 2)
        self.assertTrue(all(c[:3] == ['docker', 'image', 'inspect'] or
                            (c[:2] == ['docker', 'exec'] and c[3] in
                             ('postgres', 'redis-server', 'sh', 'node')) for c in commands))

    def test_inventory_refuses_runtime_change(self):
        identity = 'sha256:' + 'a' * 64
        before = {'frontend': {'Image': identity}}
        after = {'frontend': {'Image': 'sha256:' + 'b' * 64}}
        api = types.SimpleNamespace(configuration=lambda *a: ({}, {}, {}),
            inspect_project=unittest.mock.Mock(side_effect=[before, after]),
            run=lambda *a: json.dumps([{'Id': identity}]), ensure_stable=O.D.ensure_stable)
        with patch.object(O, 'version_probes', return_value={}):
            with self.assertRaises(O.D.ConfigError):
                O.components(api)

    def test_unavailable_version_probe_is_unknown_and_suppresses_error(self):
        def run(*args, **kwargs):
            raise O.D.ConfigError('sensitive transport details')
        result = O.version_probes(types.SimpleNamespace(run=run))
        self.assertTrue(all(value is None for value in result.values()))

    def test_status_only_reads_checkpoint_and_service(self):
        with patch.object(N, 'load', return_value={'phase': 'waiting'}), \
             patch.object(N, 'public', return_value={'status': 'waiting'}), \
             patch.object(O, 'service_status', return_value='active'), \
             patch.object(N, 'rpc', side_effect=AssertionError('no DB call for saved status')):
            self.assertEqual(O.observe('status', O.NATIVE_STATE),
                             {'read_only': True, 'service': 'active', 'normalization': {'status': 'waiting'}})

    def test_bridge_observation_requires_unchanged_verified_runtime(self):
        api = types.SimpleNamespace(configuration=lambda *a: ({}, {}, {}),
            inspect_project=lambda *a: {'backend': {'Image': 'new'}}, ensure_stable=O.D.ensure_stable)
        state = {'inputs': {'runtime_before': {'backend': {'Image': 'old'}}}}
        with patch.object(N, 'load', return_value=state), \
             patch.object(N, 'runtime_binding', return_value=None), \
             patch.object(P, 'bridge_status') as bridge:
            with self.assertRaises(O.D.ConfigError):
                O.observe('bridge', O.NATIVE_STATE, api)
            bridge.assert_not_called()

    def test_main_suppresses_private_error_details(self):
        output = io.StringIO()
        with patch.object(sys, 'argv', ['ops', 'components']), \
             patch.object(O, 'components', side_effect=O.D.ConfigError('password=PRIVATE')), \
             patch('sys.stdout', output):
            self.assertEqual(O.main(), 2)
        self.assertNotIn('PRIVATE', output.getvalue())
        self.assertTrue(json.loads(output.getvalue())['read_only'])


if __name__ == '__main__':
    unittest.main()
