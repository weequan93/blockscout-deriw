from copy import deepcopy
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import dev_config as D
import v10_nft as N


class NFTPlanTests(unittest.TestCase):
    def setUp(self):
        self.model = D.read_json(ROOT / 'docker-compose/environments/dev/compose.json')

    def test_both_roles_use_matching_image_and_preserve_existing_folder(self):
        result = N.plan(D, self.model)
        self.assertEqual(result['replace_services'], ['backend', 'nft_media_handler'])
        self.assertEqual(result['backend_environment']['NFT_MEDIA_HANDLER_BUCKET_FOLDER'], '/folder_1')
        self.assertTrue(result['folder_preserved_from_legacy_map'])

    def test_peer_or_folder_drift_requires_review(self):
        mutations = [
            lambda m: m['services']['backend']['environment'].update(RELEASE_NODE='other@host'),
            lambda m: m['services']['nft_media_handler']['environment'].update(NFT_MEDIA_HANDLER_NODES_MAP='{}'),
            lambda m: m['services']['nft_media_handler']['environment'].update(NFT_MEDIA_HANDLER_NODES_MAP=json.dumps({N.PRODUCER: '/../other'})),
            lambda m: m['services']['backend']['environment'].update(NFT_MEDIA_HANDLER_BUCKET_FOLDER='/other'),
        ]
        for mutate in mutations:
            model = deepcopy(self.model)
            mutate(model)
            with self.assertRaises(D.ConfigError):
                N.plan(D, model)

    def test_worker_smoke_uses_private_stdin_and_no_network_or_application_start(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            environment = {'RELEASE_COOKIE': 'private-worker-cookie', 'NFT_MEDIA_HANDLER_NODES_MAP': json.dumps({N.PRODUCER: '/folder_1'})}
            completed = subprocess.CompletedProcess([], 0, N.MARKER + '\n', '')
            with patch.object(N.subprocess, 'run', return_value=completed) as run, contextlib.redirect_stdout(io.StringIO()):
                result = N.smoke_worker(D, 'sha256:' + 'a' * 64, '10.2.6', environment, directory)
            command = run.call_args_list[0].args[0]
            self.assertNotIn('private-worker-cookie', str(command))
            self.assertEqual(command[command.index('--network') + 1], 'none')
            self.assertIn('--read-only', command)
            self.assertIn('blockscout eval', command[-1])
            values = json.loads(run.call_args_list[0].kwargs['input'])
            self.assertEqual(values['RELEASE_VERSION'], '10.2.6')
            self.assertEqual(values['RELEASE_COOKIE'], 'private-worker-cookie')
            self.assertTrue(result['passed'])
            self.assertEqual((directory / 'nft-worker-smoke.log.private').stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
