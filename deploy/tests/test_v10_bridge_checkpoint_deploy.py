from contextlib import nullcontext, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import test_v10_infrastructure as INFRA

import v10_bridge_checkpoint_deploy as X
import v10_native_normalization as N


class CheckpointDeploymentTests(unittest.TestCase):
    def setUp(self):
        fixture = INFRA.InfrastructureTests('test_native_handoff_preserves_frozen_inputs_and_cursor')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.addCleanup(patch.stopall)
        fixture.bind()
        self.native_path = fixture.native_path
        self.native = N.load(self.native_path)
        N.save(self.native_path, self.native, 'paused')
        self.directory = fixture.base / 'release-20261108T120000Z-1234abcd'
        self.directory.mkdir(mode=0o700)
        self.path = self.directory / 'state.json'
        patch.object(X.D, 'MANAGED_RELEASES', fixture.base).start()
        self.before = deepcopy(fixture.after)
        self.after = deepcopy(self.before)
        self.after['backend'] = INFRA.row('checkpoint-backend', 'c')
        self.inputs = {'format': X.FORMAT, 'source': {'source_commit': 'c' * 40}, 'source_root': str(X.ROOT),
            'normalization_state': str(self.native_path), 'native_inputs_sha256': self.native['inputs_sha256'],
            'prior_binding': {key: self.native[key] for key in ('runtime_binding_file', 'runtime_binding_sha256')},
            'before': self.before, 'backend_environment': {'BLOCKSCOUT_VERSION': X.BASE}}
        N.R.write_new(self.directory / 'inputs.json', N.R.encoded(self.inputs))
        seed = {'counter_key': 'arbitrum_to_l2_scan_v1:' + 'a' * 64, 'saved_next_parent_block': 278_500_000}
        N.R.write_new(self.directory / 'bridge-seed.json', N.R.encoded(seed))
        N.R.write_new(self.directory / 'database-before-forward.json', N.R.encoded(INFRA.anchor()))
        self.state = {'format': N.R.FORMAT, 'bridge_checkpoint_patch': True, 'bridge_patch': True,
            'bridge_stage': 'active', 'status': 'active', 'candidate_source_commit': 'c' * 40,
            'source_root': str(X.ROOT), 'normalization_state': str(self.native_path),
            'seed_sha256': N.R.digest(N.U.private(self.directory / 'bridge-seed.json')),
            'bridge_inputs_sha256': N.R.digest(N.R.encoded(self.inputs)), 'anchor_sha256': {
                'forward': N.R.digest(N.R.encoded(INFRA.anchor()))},
            'image_ids': {'forward': {name: r['Image'] for name, r in self.after.items()},
                          'rollback': {name: r['Image'] for name, r in self.before.items()}}}
        N.R.write_new(self.path, N.R.encoded(self.state))
        self.bridge = {'durable_next_parent_block': 278_500_050, 'next_parent_block': 278_500_050,
                       'checkpoint_key_matches': True, 'tracker_running': True}
        self.receipt()
        self.api = fixture.api
        self.api.require = N.D.require
        self.api.inspect_project = lambda *a, **k: deepcopy(self.before)
        patch.object(N, 'owned', return_value=None).start()

    def receipt(self, database=None):
        N.R.update_state(self.directory / 'receipt-forward.json', {'format': X.FORMAT, 'side': 'forward',
            'inputs_sha256': self.state['bridge_inputs_sha256'], 'runtime_before': self.after,
            'seed_sha256': N.R.digest(N.U.private(self.directory / 'bridge-seed.json')),
            'database_after': database or INFRA.anchor(), 'bridge': self.bridge})

    def bind(self, database=None):
        with patch.object(X, 'verify', return_value=({'status': 'verified'}, self.bridge)), \
             patch.object(X.K, 'stable_database_anchor', return_value=database or INFRA.anchor()):
            X.bind_native(self.api, self.path, 'forward')

    def test_binding_preserves_nginx_infrastructure_and_frozen_native_cursor(self):
        frozen = N.U.private(self.native_path.parent / 'inputs.json')
        script = N.U.private(self.native_path.parent / 'native.exs')
        self.bind()
        native = N.load(self.native_path)
        context = N.runtime_binding(self.native_path, native)
        self.assertEqual(context['backend_checkpoint_release_state'], str(self.path))
        self.assertEqual(context['runtime_before']['proxy'], self.before['proxy'])
        self.assertEqual(native['database']['native_meta']['max_block_number'], 20)
        self.assertEqual(frozen, N.U.private(self.native_path.parent / 'inputs.json'))
        self.assertEqual(script, N.U.private(self.native_path.parent / 'native.exs'))

    def test_backend_snapshot_keeps_the_patched_database_init_declarations(self):
        model = {'services': {name: {'image': row['Image'], 'environment': {}} for name, row in self.before.items()}}
        model['services']['db-init']['image'] = 'postgres@sha256:' + 'd' * 64
        model['services']['stats-db-init']['image'] = 'postgres@sha256:' + 'd' * 64
        selected = X.snapshot(model, self.before, 'sha256:' + 'c' * 64, 'c' * 40, self.path, 'forward')
        for name in ('db-init', 'stats-db-init'):
            self.assertEqual(selected['services'][name]['image'], model['services'][name]['image'])
        for name in ('proxy', 'frontend', 'redis-db', 'db', 'stats-db'):
            self.assertEqual(selected['services'][name]['image'], self.before[name]['Image'])
        self.assertEqual(selected['services']['backend']['command'], N.R.START)

    def test_completed_normalization_is_not_reopened_or_started(self):
        database = INFRA.anchor(-1)
        database['native_status']['status'] = 'completed'
        self.receipt(database)
        self.bind(database)
        native = N.load(self.native_path)
        self.assertEqual(native['phase'], 'completed')
        with patch.object(N, 'install_service', return_value={}) as install:
            result = X.resume_native(self.api, self.path, 'forward')
        install.assert_called_once_with(self.api, self.native_path, start=False)
        self.assertTrue(result['normalization_complete'])

    def test_changed_receipt_or_seed_is_rejected(self):
        self.bind()
        N.R.update_state(self.directory / 'bridge-seed.json', {'saved_next_parent_block': 999})
        with self.assertRaises(N.D.ConfigError):
            N.load(self.native_path)

    def test_a_newer_owner_cannot_be_overwritten(self):
        native = deepcopy(self.native)
        native.update(runtime_binding_file='frontend-runtime-binding-new-forward.json', runtime_binding_sha256='f' * 64)
        with self.assertRaises(N.D.ConfigError):
            X.own_binding(native, self.inputs, self.path)

    def test_unrelated_service_change_cannot_enter_the_backend_handoff(self):
        self.after['proxy']['Id'] = 'unreviewed-proxy'
        self.receipt()
        with self.assertRaises(N.D.ConfigError):
            self.bind()

    def test_inputs_tampering_is_rejected(self):
        N.R.update_state(self.directory / 'inputs.json', dict(self.inputs, source_root='/other'))
        with self.assertRaises(N.D.ConfigError):
            X.load(self.api, self.path)

    def test_requested_activation_requires_its_recorded_database_anchor(self):
        self.state.update(status='activation_requested', anchor_sha256={})
        N.R.update_state(self.path, self.state)
        with self.assertRaisesRegex(N.D.ConfigError, 'authenticated database anchor'):
            X.load(self.api, self.path)

    def test_receipt_resume_verifies_without_restarting_backend(self):
        with patch.object(X, 'source_review', return_value='c' * 40), \
             patch.object(X, 'verify', return_value=({'status': 'verified'}, self.bridge)) as verify, \
             patch.object(X.R, 'recreate') as recreate:
            self.assertEqual(X.activate(self.api, self.path, 'forward'), self.bridge)
        verify.assert_called_once()
        recreate.assert_not_called()

    def test_live_checkpoint_regression_stops_qualification(self):
        with patch.object(X.R, 'verify', return_value={'status': 'verified'}), \
             patch.object(X.P.Q, 'verify_holds'), \
             patch.object(X.K, 'stable_database_anchor', return_value=INFRA.anchor()), \
             patch.object(X, 'observe', return_value=dict(self.bridge, durable_next_parent_block=1)):
            with self.assertRaisesRegex(N.D.ConfigError, 'regressed'):
                X.verify(self.api, self.path, self.state, self.inputs, 'forward')

    def test_activation_is_journaled_and_holds_are_restored_before_qualification(self):
        (self.directory / 'receipt-forward.json').unlink()
        self.state.update(status='prepared', bridge_stage='candidate_verified')
        N.R.update_state(self.path, self.state)
        events = []
        def recreate(*args):
            saved = json.loads(N.U.private(self.path))
            self.assertEqual(saved['status'], 'activation_requested')
            self.assertIn('forward', saved['anchor_sha256'])
            self.assertTrue(saved['seed_sha256'])
            events.append('recreate')
            self.api.inspect_project = lambda *a, **kw: self.after
        def qualify(*args, **kwargs):
            self.assertEqual(events, ['recreate', 'holds'])
            return {'status': 'verified'}, self.bridge
        with patch.object(X, 'source_review', return_value='c' * 40), \
             patch.object(X.K, 'stable_database_anchor', return_value=INFRA.anchor()), \
             patch.object(X.N.J.P, 'free_bytes', return_value=N.RESUME_FREE), \
             patch.object(X.R, 'recreate', side_effect=recreate), \
             patch.object(X.P, 'restore_holds', side_effect=lambda *a: events.append('holds')), \
             patch.object(X, 'verify', side_effect=qualify):
            self.assertEqual(X.activate(self.api, self.path, 'forward'), self.bridge)
        self.assertEqual(X.load(self.api, self.path)[0]['status'], 'active')
        self.assertTrue((self.directory / 'receipt-forward.json').exists())

    def test_owned_running_target_reconciles_without_another_restart(self):
        (self.directory / 'receipt-forward.json').unlink()
        self.state.update(status='activation_requested', bridge_stage='activation_requested')
        N.R.update_state(self.path, self.state)
        self.after['backend']['Config'].update(Cmd=N.R.START, Labels={
            N.R.STATE_LABEL: str(self.path), N.R.SIDE_LABEL: 'forward'})
        self.api.inspect_project = lambda *a, **kw: self.after
        self.api.compose_model = lambda *a: {'services': {'backend': {}}}
        self.api.live_environment_differences = lambda *a: []
        with patch.object(X, 'source_review', return_value='c' * 40), \
             patch.object(X.K, 'stable_database_anchor', return_value=INFRA.anchor()), \
             patch.object(X.R, 'recreate') as recreate, patch.object(X.P, 'restore_holds'), \
             patch.object(X, 'verify', return_value=({'status': 'verified'}, self.bridge)):
            self.assertEqual(X.activate(self.api, self.path, 'forward'), self.bridge)
        recreate.assert_not_called()

    def test_owned_but_changed_image_is_not_overwritten(self):
        (self.directory / 'receipt-forward.json').unlink()
        self.after['backend'].update(Image='sha256:' + 'f' * 64)
        self.after['backend']['Config'].update(Cmd=N.R.START, Labels={
            N.R.STATE_LABEL: str(self.path), N.R.SIDE_LABEL: 'forward'})
        self.api.inspect_project = lambda *a, **kw: self.after
        with patch.object(X, 'source_review', return_value='c' * 40), patch.object(X.R, 'recreate') as recreate:
            with self.assertRaisesRegex(N.D.ConfigError, 'image or startup'):
                X.activate(self.api, self.path, 'forward')
        recreate.assert_not_called()

    def test_activation_failure_never_binds_or_starts_normalization(self):
        with patch.object(sys, 'argv', ['checkpoint', '--resume', '--state', str(self.path)]), \
             patch.object(X.sys, 'platform', 'linux'), patch.object(X.os, 'geteuid', return_value=0), \
             patch.object(X, 'source_review', return_value='c' * 40), patch.object(X, 'load', return_value=(self.state, self.inputs)), \
             patch.object(X.P, 'pause_native'), patch.object(X.M, 'global_lock', return_value=nullcontext()), \
             patch.object(X.B, 'build_lock', return_value=nullcontext()), \
             patch.object(X, 'activate', side_effect=N.D.ConfigError('Health failed')), \
             patch.object(X, 'bind_native') as bind, patch.object(X, 'resume_native') as resume, redirect_stdout(io.StringIO()):
            self.assertEqual(X.main(), 2)
        bind.assert_not_called()
        resume.assert_not_called()

    def test_newer_runtime_binding_is_rejected_before_pausing_the_native_job(self):
        native = deepcopy(self.native)
        native.update(runtime_binding_file='frontend-runtime-binding-new-forward.json', runtime_binding_sha256='f' * 64)
        with patch.object(sys, 'argv', ['checkpoint', '--resume', '--state', str(self.path)]), \
             patch.object(X.sys, 'platform', 'linux'), patch.object(X.os, 'geteuid', return_value=0), \
             patch.object(X, 'source_review', return_value='c' * 40), patch.object(X, 'load', return_value=(self.state, self.inputs)), \
             patch.object(N, 'load', return_value=native), patch.object(X.P, 'pause_native') as pause, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(X.main(), 2)
        pause.assert_not_called()

    def test_mismatched_creation_commit_is_rejected_before_pausing(self):
        with patch.object(sys, 'argv', ['checkpoint', '--resume', '--state', str(self.path)]), \
             patch.object(X.sys, 'platform', 'linux'), patch.object(X.os, 'geteuid', return_value=0), \
             patch.object(X, 'source_review', return_value='f' * 40), patch.object(X, 'load', return_value=(self.state, self.inputs)), \
             patch.object(X.P, 'pause_native') as pause, redirect_stdout(io.StringIO()):
            self.assertEqual(X.main(), 2)
        pause.assert_not_called()

    def test_source_profile_rejects_unrelated_application_changes(self):
        api = deepcopy(self.api)
        def run(command):
            if 'diff' in command:
                return '\n'.join(sorted(X.APP_FILES))
            if 'show' in command:
                return (X.ROOT / command[-1].split(':', 1)[1]).read_text()
            return ''
        api.run = run
        X.review_application(api, X.ROOT, 'c' * 40)
        api.run = lambda command: 'apps/explorer/lib/unrelated.ex' if 'diff' in command else ''
        with self.assertRaisesRegex(N.D.ConfigError, 'another application'):
            X.review_application(api, X.ROOT, 'c' * 40)


if __name__ == '__main__':
    unittest.main()
