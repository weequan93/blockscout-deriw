import contextlib
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import v10_candidate as V

COMMIT = 'a' * 40
IMAGE = 'sha256:' + 'b' * 64
SECRET = 'fixture-private-v10-value'


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.private = self.directory / 'inputs.json'
        self.private.write_text('{}')
        self.private.chmod(0o600)
        self.output = self.directory / 'candidates'
        self.values = {'PRIVATE': SECRET}
        self.before = {'backend': {'Image': IMAGE, 'Id': 'c' * 64,
                                   'Config': {'Env': ['RELEASE_VERSION=9.0.1']}, 'State': {'Running': True}}}
        self.model = {'services': {'backend': {
            'environment': {'CHAIN_ID': '18417507517', 'RELEASE_COOKIE': SECRET},
            'build': {'args': {'CHAIN_TYPE': 'arbitrum', 'RELEASE_VERSION': '9.0.1'}}}}}
        self.report = {'changed_runtime_fields': [], 'live_declared_environment_drift': [], 'live_images_differing_from_capture': []}
        self.api = types.SimpleNamespace(
            require=V.D.require, ConfigError=V.D.ConfigError, env_map=V.D.env_map, read_json=V.D.read_json,
            configuration=Mock(return_value=({}, {}, {})), load_private=Mock(return_value=self.values),
            plan=Mock(return_value=self.report), inspect_project=Mock(return_value=self.before),
            managed_model=Mock(return_value=self.model), ensure_stable=V.D.ensure_stable)
        self.source = {'source_commit': COMMIT, 'release_version': '10.2.6', 'platform': 'linux/amd64',
                       'tag': 'deriw-blockscout:upgrade-v10-fixture-arbitrum-amd64', 'fingerprint': 'd' * 64,
                       'build_arguments': {'CHAIN_TYPE': 'arbitrum', 'RELEASE_VERSION': '10.2.6', V.HOLD: 'true'},
                       'baseline_runtime_user': '10001:10001', 'migration_inventory_sha256': 'e' * 64}
        self.image = {'Id': IMAGE, 'Os': 'linux', 'Architecture': 'amd64', 'Config': {
            'User': '10001:10001', 'Env': [V.HOLD + '=true', 'RELEASE_VERSION=10.2.6'],
            'Labels': {V.B.LABEL: self.source['fingerprint'], 'org.opencontainers.image.revision': COMMIT}}}

    @contextlib.contextmanager
    def mocked(self, existing=None):
        with contextlib.ExitStack() as stack:
            mocks = {}
            for module, name, options in [
                (V, 'source_plan', {'return_value': self.source}),
                (V, 'storage_ready', {'return_value': {'migration_space_verified': False}}),
                (V.B, 'market_policy', {'return_value': types.SimpleNamespace(provider_check=Mock(return_value={'status': 'ok'}), FIELDS=set())}),
                (V.B, 'image_info', {'side_effect': [existing, self.image, self.image]}),
                (V.B, 'compile_image', {}), (V.B, 'clean_source', {'return_value': (COMMIT, '10.2.6')}),
                (V.B, 'smoke_image', {'return_value': [{'mode': value, 'passed': True} for value in ('unset', 'false', 'true')]})]:
                mocks[name] = stack.enter_context(patch.object(module, name, **options))
            yield mocks

    def build(self, **options):
        return V.build(self.api, ROOT, self.private, self.output, self.directory / 'storage-state', **options)

    def test_check_is_read_only_and_separate_from_activation(self):
        with self.mocked() as mocks:
            result = self.build(check=True)
        self.assertEqual(result['status'], 'v10_candidate_build_ready')
        self.assertFalse(self.output.exists())
        self.assertFalse(result['deployment_permitted'])
        mocks['compile_image'].assert_not_called()
        mocks['smoke_image'].assert_not_called()
        self.assertEqual(self.api.plan.call_args.args[-1], Path('/data/blockscout/docker-compose'))

    def test_build_records_upgrade_format_and_hold_without_private_values(self):
        stdout = io.StringIO()
        with self.mocked() as mocks, contextlib.redirect_stdout(stdout):
            result = self.build()
        receipt = Path(result['receipt'])
        data = json.loads(receipt.read_text())
        self.assertEqual(data['format'], V.FORMAT)
        self.assertNotEqual(data['format'], V.B.FORMAT)
        self.assertEqual(data['build_arguments'][V.HOLD], 'true')
        self.assertFalse(data['deployment_permitted'])
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(SECRET, receipt.read_text() + stdout.getvalue())
        self.assertIn('StartupControl.disabled?', mocks['smoke_image'].call_args.kwargs['extra_expression'])
        self.assertIn('SwitchPendingOperations', V.held_expression())
        mocks['storage_ready'].assert_called()

    def test_runtime_drift_blocks_build(self):
        self.report['changed_runtime_fields'] = ['services.db.command']
        with self.mocked() as mocks:
            with self.assertRaises(V.D.ConfigError):
                self.build()
        mocks['compile_image'].assert_not_called()

    def test_live_profile_cannot_override_image_hold(self):
        self.model['services']['backend']['environment'][V.HOLD] = 'false'
        with self.mocked() as mocks:
            with self.assertRaises(V.D.ConfigError):
                self.build()
        mocks['compile_image'].assert_not_called()

    def test_build_and_smoke_failures_never_write_success_receipt(self):
        for failing in ('compile_image', 'smoke_image'):
            with self.mocked() as mocks, contextlib.redirect_stdout(io.StringIO()):
                mocks[failing].side_effect = V.D.ConfigError('fixture failure')
                with self.assertRaises(V.D.ConfigError):
                    self.build()
            self.assertEqual(list(self.output.glob('*/candidate.json')), [])

    def test_existing_candidate_needs_explicit_reuse(self):
        with self.mocked(existing=self.image) as mocks:
            with self.assertRaises(V.D.ConfigError):
                self.build()
        mocks['compile_image'].assert_not_called()

    def test_reuse_verifies_image_and_runs_smoke_without_rebuilding(self):
        with self.mocked(existing=self.image) as mocks, contextlib.redirect_stdout(io.StringIO()):
            result = self.build(reuse=True)
        self.assertEqual(result['status'], 'v10_candidate_verified')
        mocks['compile_image'].assert_not_called()
        mocks['smoke_image'].assert_called_once()

    def test_wrong_image_hold_platform_or_user_are_rejected(self):
        for mutate in (
            lambda image: image.update(Architecture='arm64'),
            lambda image: image['Config'].update(User='0'),
            lambda image: image['Config'].update(Env=['RELEASE_VERSION=10.2.6']),
            lambda image: image['Config']['Labels'].update({V.B.LABEL: 'wrong'}),
        ):
            image = deepcopy(self.image)
            mutate(image)
            with self.assertRaises(V.D.ConfigError):
                V.validate_image(self.api, image, self.source)

    def test_source_or_private_changes_prevent_acceptance(self):
        for change in ('source', 'private'):
            with self.mocked() as mocks, contextlib.redirect_stdout(io.StringIO()):
                if change == 'source':
                    mocks['clean_source'].return_value = ('f' * 40, '10.2.6')
                else:
                    self.api.load_private.side_effect = [self.values, {'changed': SECRET}]
                with self.assertRaises(V.D.ConfigError):
                    self.build()
            self.api.load_private.side_effect = None
            self.assertEqual(list(self.output.glob('*/candidate.json')), [])

    def test_container_change_prevents_success_receipt(self):
        with self.mocked(), contextlib.redirect_stdout(io.StringIO()):
            changed = deepcopy(self.before)
            changed['backend']['Image'] = 'sha256:' + 'f' * 64
            self.api.inspect_project.side_effect = [self.before, changed]
            with self.assertRaises(V.D.StabilityError):
                self.build()
        self.assertEqual(list(self.output.glob('*/candidate.json')), [])

    def test_source_plan_versions_only_candidate_and_retains_profile(self):
        original = deepcopy(self.model)
        with patch.object(V.B, 'clean_source', return_value=(COMMIT, '10.2.6')), \
                patch.object(V.B, 'source_plan', return_value=deepcopy(self.source)) as base:
            result = V.source_plan(self.api, ROOT, {}, self.model, self.before)
        self.assertEqual(self.model, original)
        self.assertEqual(base.call_args.args[3]['services']['backend']['build']['args']['RELEASE_VERSION'], '10.2.6')
        self.assertEqual(result['format'], V.FORMAT)
        self.assertEqual(result['build_arguments'][V.HOLD], 'true')
        self.assertIn('upgrade-v10-', result['tag'])

    def test_source_version_baseline_and_platform_are_strict(self):
        with patch.object(V.B, 'clean_source', return_value=(COMMIT, '9.0.1')):
            with self.assertRaises(V.D.ConfigError):
                V.source_plan(self.api, ROOT, {}, self.model, self.before)
        with patch.object(V.B, 'clean_source', return_value=(COMMIT, '10.2.6')), \
                patch.object(V.B, 'source_plan', return_value={**self.source, 'platform': 'linux/arm64'}):
            with self.assertRaises(V.D.ConfigError):
                V.source_plan(self.api, ROOT, {}, self.model, self.before)
        changed = deepcopy(self.before)
        changed['backend']['Config']['Env'] = ['RELEASE_VERSION=10.2.6']
        with patch.object(V.B, 'clean_source', return_value=(COMMIT, '10.2.6')):
            with self.assertRaises(V.D.ConfigError):
                V.source_plan(self.api, ROOT, {}, self.model, changed)

    def test_storage_checks_live_mounts_without_rebasing_the_old_image_inventory(self):
        maintenance = types.SimpleNamespace(
            identities=Mock(), source_retirement=Mock(return_value={'phase': 'complete'}), target_configuration=Mock(),
            rows=Mock(return_value={}), verify_backend_path=Mock(),
            plan={'before': {}, 'public_plan': {'running_services': []}},
            status=Mock(side_effect=AssertionError('Do not compare pre-build global image inventory')))
        with patch.object(V.M, 'Maintenance', return_value=maintenance), \
                patch.object(V.M.S, 'capacity', side_effect=[{'available_bytes': 31 * 1024**3}, {'available_bytes': 898 * 1024**3}]):
            result = V.storage_ready(Path('/fixture/state.json'))
        self.assertFalse(result['migration_space_verified'])
        maintenance.target_configuration.assert_called_once()
        maintenance.verify_backend_path.assert_called_once()
        maintenance.status.assert_not_called()

    def test_unfinished_storage_retirement_blocks_build(self):
        maintenance = types.SimpleNamespace(identities=Mock(), source_retirement=Mock(return_value=None))
        with patch.object(V.M, 'Maintenance', return_value=maintenance):
            with self.assertRaises(V.D.ConfigError):
                V.storage_ready(Path('/fixture/state.json'))


if __name__ == '__main__':
    unittest.main()
