from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import v10_infrastructure_deploy as I
import v10_infrastructure_recovery as H
import test_v10_infrastructure as T


class InfrastructureRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = T.InfrastructureTests(methodName='runTest')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        f = self.f
        (f.directory / 'receipt-forward.json').unlink()
        f.inputs.update(component='postgresql-main', target=I.K.catalog()['postgresql'])
        f.inputs['source_root'] = str(I.ROOT)
        f.state['phase'] = 'activation_requested'
        f.write_inputs()
        I.R.write_new(f.directory / 'stage-receipt.json', I.R.encoded({
            'inputs_sha256':f.state['inputs_sha256'],'image_id':f.inputs['image_id']}))
        self.runtime = deepcopy(f.before)
        self.runtime['db'] = T.row('patched-db','b')
        self.runtime['db']['Config']['Labels'] = {I.K.LABEL:str(f.path), I.K.SIDE_LABEL:'forward'}
        f.api.inspect_project = lambda *a, **kw: deepcopy(self.runtime)
        self.source = patch.object(I.F, 'source_review', return_value='c' * 40).start()
        self.contract = patch.object(I, 'component_contract').start()
        self.owned = patch.object(I.N, 'owned', return_value=None).start()
        patch.object(I.N.J.P, 'free_bytes', return_value=I.N.RESUME_FREE).start()
        patch.object(I.K, 'stable_database_anchor', return_value=T.anchor()).start()
        self.verify = patch.object(I, 'verify', return_value=deepcopy(self.runtime)).start()

    def test_running_patch_finishes_receipt_without_recreation_and_preserves_frozen_files(self):
        f = self.f
        paths = [f.path.parent/'inputs.json', f.path.parent/'forward.compose.json',
                 f.native_path.parent/'inputs.json', f.native_path.parent/'native.exs']
        before = {p:p.read_bytes() for p in paths}
        with redirect_stdout(io.StringIO()):
            receipt = H.reconcile(I, f.api, f.path, 'forward')
        self.assertEqual(receipt['runtime_before'], self.runtime)
        self.assertEqual(I.load(f.path)[0]['phase'], 'active')
        self.verify.assert_called_once_with(f.api, f.path, 'forward', wait=True)
        self.contract.assert_called_once()
        self.owned.assert_called_once()
        self.assertEqual(f.commands, [])
        self.assertEqual(before, {p:p.read_bytes() for p in paths})

    def test_health_failure_keeps_request_and_never_writes_a_receipt(self):
        self.verify.side_effect = I.D.ConfigError('Health not ready')
        with redirect_stdout(io.StringIO()), self.assertRaises(I.D.ConfigError):
            H.reconcile(I, self.f.api, self.f.path, 'forward')
        self.assertEqual(I.load(self.f.path)[0]['phase'], 'activation_requested')
        self.assertFalse((self.f.directory/'receipt-forward.json').exists())
        self.assertEqual(self.f.commands, [])

    def test_original_stopped_and_opposite_side_services_use_original_activation(self):
        for mode in ('original','stopped','opposite'):
            with self.subTest(mode=mode):
                current = deepcopy(self.runtime)
                if mode == 'original':
                    current['db'] = deepcopy(self.f.before['db'])
                elif mode == 'stopped':
                    current['db']['State']['Running'] = False
                else:
                    current['db']['Config']['Labels'][I.K.SIDE_LABEL] = 'rollback'
                self.f.api.inspect_project = lambda *a, **kw: current
                self.assertIsNone(H.reconcile(I, self.f.api, self.f.path, 'forward'))
        self.verify.assert_not_called()

    def test_wrong_owned_image_is_not_accepted(self):
        self.runtime['db']['Image'] = T.image('d')
        with self.assertRaises(I.D.ConfigError):
            H.reconcile(I, self.f.api, self.f.path, 'forward')
        self.verify.assert_not_called()

    def test_running_writer_and_changed_peer_are_not_accepted(self):
        self.owned.return_value = {'State':{'Running':True}}
        with self.assertRaises(I.D.ConfigError):
            H.reconcile(I, self.f.api, self.f.path, 'forward')
        self.owned.return_value = None
        self.runtime['backend']['Id'] = 'unexpected-backend'
        with self.assertRaises(I.D.ConfigError):
            H.reconcile(I, self.f.api, self.f.path, 'forward')
        self.verify.assert_not_called()

    def test_missing_stage_or_mismatched_request_is_not_accepted(self):
        (self.f.directory/'stage-receipt.json').unlink()
        with self.assertRaises((I.D.ConfigError,OSError)):
            H.reconcile(I, self.f.api, self.f.path, 'forward')
        self.f.state['phase'] = 'staged'
        self.f.write_inputs()
        with self.assertRaises(I.D.ConfigError):
            H.reconcile(I, self.f.api, self.f.path, 'forward')
        self.verify.assert_not_called()

    def test_receipt_already_present_is_left_to_original_verification(self):
        self.f.receipt('forward', self.runtime)
        self.assertIsNone(H.reconcile(I, self.f.api, self.f.path, 'forward'))
        self.verify.assert_not_called()

    def test_recovery_overlay_keeps_creator_and_restores_function_after_exception(self):
        f = self.f
        original, argv = I.activate, sys.argv
        with patch.object(I.B, 'clean_source', return_value=('d'*40,'10.2.6')), \
             patch.object(I.D, 'run', return_value='deploy/scripts/v10_infrastructure_recovery.py\n'), \
             patch.object(I, 'main', side_effect=I.D.ConfigError('synthetic interruption')):
            with self.assertRaises(I.D.ConfigError):
                H.resume_existing(I, f.path)
        self.assertIs(I.activate, original)
        self.assertIs(sys.argv, argv)
        evidence = json.loads(next(f.directory.glob('health-recovery-*.json')).read_text())
        self.assertFalse(evidence['health_checks_bypassed'])
        self.assertFalse(evidence['original_inputs_and_snapshots_edited'])

    def test_recovery_overlay_refuses_changed_creator_and_application_source(self):
        self.source.return_value = 'changed'
        with self.assertRaises(I.D.ConfigError):
            H.resume_existing(I, self.f.path)
        self.source.return_value = 'c'*40
        with patch.object(I.B, 'clean_source', return_value=('d'*40,'10.2.6')), \
             patch.object(I.D, 'run', return_value='apps/explorer/changed.ex\n'), \
             self.assertRaises(I.D.ConfigError):
            H.resume_existing(I, self.f.path)
        self.assertEqual(list(self.f.directory.glob('health-recovery-*.json')), [])
