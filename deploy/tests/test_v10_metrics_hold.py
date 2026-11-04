import contextlib
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import v10_metrics_hold as H


class MetricsHoldTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        self.base = Path(self.temp.name).resolve()
        os.chmod(self.base, 0o700)
        patch.object(H, 'BASE', self.base).start()
        patch.object(H.B, 'clean_source', return_value=('a' * 40, '10.2.6')).start()
        patch.object(H.B, 'validate_state_directory').start()
        self.manifest = {'chain_id': '18417507517'}
        self.runtime = {'backend': {'Id': 'a' * 64, 'Image': 'sha256:' + 'b' * 64}}
        self.api = types.SimpleNamespace(inspect_project=lambda *a: deepcopy(self.runtime), ensure_stable=H.D.ensure_stable)
        patch.object(H.P, 'context', return_value=(self.manifest, {'candidate_image': self.runtime['backend']['Image']})).start()
        self.running, self.bad_target, self.lose_reply = True, False, False
        self.stops, self.starts = 0, 0
        patch.object(H, 'rpc', side_effect=self.rpc).start()
        self.path = None

    def rpc(self, api, source, payload, mode):
        if mode != 'observe':
            state = H.load(self.path)
            self.assertEqual(state['phase'], 'restore_requested' if mode == 'restore' else 'hold_requested')
        if mode == 'hold' and self.running:
            self.stops += 1
            self.running = False
            if self.lose_reply:
                self.lose_reply = False
                raise H.U.UpgradeError('simulated lost hold reply')
        if mode == 'restore' and not self.running:
            self.starts += 1
            self.running = True
        return {'mode': mode, 'metrics_worker_running': self.running, 'metrics_worker_held': not self.running,
                'other_web_children_preserved': True, 'original_query_active': self.running,
                'target': [{'expected_query': not self.bad_target, 'same_role': True, 'assigned_xid_present': False,
                            'backend_start': '2026-11-04 03:30:20+00'}] if self.running else []}

    def prepare(self):
        with contextlib.redirect_stdout(io.StringIO()):
            result = H.prepare(self.api, ROOT, Path('/private/upgrade/state.json'), 32950, '2026-11-04 08:30:20.496611+00')
        self.path = Path(result['state'])
        return self.path

    def execute(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return H.execute(self.api, ROOT, self.path, **kwargs)

    def test_preparation_is_observation_only_and_hold_restore_are_reversible(self):
        self.prepare()
        self.assertEqual(self.stops, 0)
        self.assertEqual(H.load(self.path)['phase'], 'prepared')
        result = self.execute()
        self.assertEqual(result['status'], 'metrics_hold_held')
        self.assertEqual(self.stops, 1)
        self.execute()
        self.assertEqual(self.stops, 1)
        result = self.execute(restore=True)
        self.assertEqual(result['status'], 'metrics_hold_restored')
        self.execute(restore=True)
        self.assertEqual(self.starts, 1)
        with self.assertRaises(H.U.UpgradeError):
            self.execute()

    def test_lost_reply_reconciles_an_already_stopped_child_without_another_stop(self):
        self.prepare()
        self.lose_reply = True
        with self.assertRaises(H.U.UpgradeError):
            self.execute()
        self.assertEqual(H.load(self.path)['phase'], 'hold_requested')
        status = self.execute(observe=True)
        self.assertTrue(status['observed']['metrics_worker_held'])
        self.assertEqual(H.load(self.path)['phase'], 'hold_requested')
        self.execute()
        self.assertEqual(self.stops, 1)
        self.assertEqual(H.load(self.path)['phase'], 'held')

    def test_wrong_target_cannot_prepare_a_mutation(self):
        self.bad_target = True
        with self.assertRaises(H.U.UpgradeError):
            self.prepare()
        self.assertEqual(self.stops, 0)
        self.assertEqual(list(self.base.iterdir()), [])

    def test_changed_backend_or_frozen_inputs_cannot_change_runtime(self):
        self.prepare()
        self.execute()
        self.runtime['backend']['Id'] = 'c' * 64
        with self.assertRaises(H.D.StabilityError):
            self.execute(restore=True)
        self.assertEqual(self.starts, 0)
        state = H.load(self.path)
        state['inputs']['payload']['query_md5'] = '0' * 32
        H.R.update_state(self.path, state)
        with self.assertRaises(H.U.UpgradeError):
            H.load(self.path)


if __name__ == '__main__':
    unittest.main()
