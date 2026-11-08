import json
from pathlib import Path
import stat
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import dev_config as D
import v10_redis_rdb as RDB


class RedisRdbTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.data = self.root / 'redis-data'
        self.data.mkdir(mode=0o700)
        self.rdb = self.data / 'dump.rdb'
        self.rdb.write_bytes(b'synthetic RDB; data must not be printed')
        self.rdb.chmod(0o600)
        self.path = self.root / 'state.json'
        self.inputs = {'component': 'redis', 'component_before': {'dbfilename': 'dump.rdb'},
                       'image_id': 'sha256:' + 'b' * 64, 'before': {'redis-db': {
                           'Image': 'sha256:' + 'a' * 64, 'Mounts': [{'Type': 'bind',
                           'Source': str(self.data), 'Destination': '/data', 'RW': True}]}},
                       'source_root': '/data/original', 'tool_source_commit': 'a' * 40}
        self.commands = []
        self.api = types.SimpleNamespace(run=lambda command, **kwargs: self.commands.append(command) or '')

    def test_owner_read_only_check_preserves_data_permissions_and_writes_only_evidence(self):
        original, metadata = self.rdb.read_bytes(), self.rdb.stat()
        result = RDB.check(self.api, self.path, self.inputs, 'forward')
        command = self.commands[0]
        self.assertEqual(command[command.index('--user') + 1], f'{metadata.st_uid}:{metadata.st_gid}')
        self.assertEqual(command[command.index('--cap-drop') + 1], 'ALL')
        self.assertIn('--read-only', command)
        self.assertEqual(command[command.index('--network') + 1], 'none')
        self.assertTrue(command[command.index('--mount') + 1].endswith(',readonly'))
        self.assertEqual(command[-2], self.inputs['image_id'])
        self.assertEqual(self.rdb.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(self.rdb.stat().st_mode), 0o600)
        self.assertEqual((self.rdb.stat().st_uid, self.rdb.stat().st_gid), (metadata.st_uid, metadata.st_gid))
        self.assertTrue(result['verified'])
        self.assertEqual(len(list(self.root.glob('redis-rdb-owner-forward-*.json'))), 1)
        self.assertNotIn('data must not be printed', json.dumps(result))

    def test_rollback_checks_the_original_binary(self):
        RDB.check(self.api, self.path, self.inputs, 'rollback')
        self.assertEqual(self.commands[0][-2], self.inputs['before']['redis-db']['Image'])

    def test_failed_rdb_is_not_qualified_and_raw_output_is_suppressed(self):
        def fail(*args, **kwargs):
            raise D.ConfigError('PRIVATE-RDB-KEY-CANARY')
        self.api.run = fail
        with self.assertRaises(D.ConfigError) as caught:
            RDB.check(self.api, self.path, self.inputs, 'forward')
        self.assertNotIn('PRIVATE-RDB-KEY-CANARY', str(caught.exception))
        self.assertEqual(list(self.root.glob('redis-rdb-owner-*.json')), [])

    def test_symlink_or_changed_rdb_is_not_qualified(self):
        self.rdb.rename(self.data / 'original.rdb')
        self.rdb.symlink_to('original.rdb')
        with self.assertRaises(D.ConfigError):
            RDB.check(self.api, self.path, self.inputs, 'forward')
        self.assertEqual(self.commands, [])
        self.rdb.unlink()
        self.rdb.write_bytes(b'original')
        self.api.run = lambda *args, **kwargs: self.rdb.write_bytes(b'changed')
        with self.assertRaisesRegex(D.ConfigError, 'metadata changed'):
            RDB.check(self.api, self.path, self.inputs, 'forward')
        self.assertEqual(list(self.root.glob('redis-rdb-owner-*.json')), [])

    def deployment(self):
        original = lambda *args: None
        deployment = types.SimpleNamespace(
            load=lambda path: ({'phase': 'activation_requested', 'inputs_sha256': 'c' * 64}, self.inputs),
            ROOT=Path('/data/original'), D=types.SimpleNamespace(run=lambda command: 'deploy/scripts/v10_redis_rdb.py\n'),
            F=types.SimpleNamespace(source_review=lambda api: 'a' * 40),
            B=types.SimpleNamespace(clean_source=lambda api, root: ('b' * 40, '10.2.6')),
            stopped_redis_check=original)
        return deployment, original

    def test_recovery_keeps_original_source_and_replaces_only_checker_during_resume(self):
        deployment, original = self.deployment()
        argv = sys.argv
        def resume():
            self.assertIs(deployment.stopped_redis_check, RDB.check)
            self.assertEqual(sys.argv[1:], ['--resume', '--state', str(self.path)])
            self.assertEqual(deployment.ROOT, Path('/data/original'))
            return 0
        deployment.main = resume
        self.assertEqual(RDB.resume_existing(deployment, self.path), 0)
        self.assertIs(deployment.stopped_redis_check, original)
        self.assertIs(sys.argv, argv)
        evidence = json.loads(next(self.root.glob('redis-rdb-owner-recovery-*.json')).read_text())
        self.assertFalse(evidence['original_inputs_and_snapshots_edited'])
        self.assertEqual(evidence['original_source_commit'], 'a' * 40)

    def test_recovery_refuses_changed_creator_or_backend_source(self):
        deployment, original = self.deployment()
        deployment.F.source_review = lambda api: 'd' * 40
        with self.assertRaises(D.ConfigError):
            RDB.resume_existing(deployment, self.path)
        deployment.F.source_review = lambda api: 'a' * 40
        deployment.D.run = lambda command: 'apps/explorer/lib/changed.ex\n'
        with self.assertRaises(D.ConfigError):
            RDB.resume_existing(deployment, self.path)
        self.assertIs(deployment.stopped_redis_check, original)
        self.assertEqual(list(self.root.glob('redis-rdb-owner-recovery-*.json')), [])

    def test_recovery_exception_restores_creator_function_and_arguments(self):
        deployment, original = self.deployment()
        argv = sys.argv
        def fail():
            raise D.ConfigError('synthetic interruption')
        deployment.main = fail
        with self.assertRaises(D.ConfigError):
            RDB.resume_existing(deployment, self.path, rollback=True)
        self.assertIs(deployment.stopped_redis_check, original)
        self.assertIs(sys.argv, argv)
