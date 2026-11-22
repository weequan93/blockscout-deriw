from copy import deepcopy
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import test_v10_native_normalization as F
import v10_native_normalization as N
import v10_native_oversized_block_repair as R
import v10_native_rebind as B


class RepairTests(unittest.TestCase):
    def setUp(self):
        F.NativeTests.setUp(self)
        self.original = (ROOT / 'deploy/scripts/v10/native_normalization.exs').read_bytes()
        self.inputs['payload']['start_before_block'] = 20000000
        self.inputs['payload'].update(database_identity={}, heap_identity=1, applied_versions=[1])
        self.inputs['script_sha256'] = N.R.digest(self.original)
        self.state.update(phase='needs_attention', reason='native_batch_requires_smaller_range', batch_blocks=1,
                          worker={'phase': 'needs_attention', 'reason': 'native_batch_requires_smaller_range'},
                          database={'native_meta': {'max_block_number': 9179901,
                                    'v10_native_job': self.inputs['payload']['token']}, 'native_status': 'started'})
        (self.directory / 'native.exs').write_bytes(self.original)
        self.state['inputs_sha256'] = N.R.digest(N.R.encoded(self.inputs))
        N.R.update_state(self.directory / 'inputs.json', self.inputs)
        token = self.inputs['payload']['token']
        self.anchor = {'system_identifier': 'fixture', 'database_identity': {}, 'heap_identity': 1,
                       'versions': [1], 'native_status': {'status': 'started',
                       'meta': {'v10_native_job': token, 'max_block_number': 9179901}}}
        self.binding = {'format': N.CURRENT_RUNTIME_FORMAT, 'source_root': str(ROOT),
                        'tool_source_commit': 'a' * 40, 'runtime_before': {},
                        'native_inputs_sha256': self.state['inputs_sha256'],
                        'native_worker_script_sha256': self.inputs['script_sha256'],
                        'database_anchor': deepcopy(self.anchor)}
        self.write_binding()
        self.report = {'status': 'started', 'token': token, 'cursor': 9179901,
                       'all_rows': 10147, 'target_rows': 10147,
                       'all_bytes': 3316668, 'target_bytes': 3316668}

    def write_binding(self):
        N.R.update_state(self.directory / 'current-runtime-binding.json', self.binding)
        self.state.update(runtime_binding_file='current-runtime-binding.json',
                          runtime_binding_sha256=N.R.digest(N.R.encoded(self.binding)))
        N.R.update_state(self.path, self.state)

    def prepare_context(self, report=None):
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(N, 'owned', return_value=None))
        stack.enter_context(patch.object(N.B, 'clean_source', return_value=('a' * 40, '10.2.6')))
        stack.enter_context(patch.object(N.K, 'stable_database_anchor', return_value=self.anchor))
        stack.enter_context(patch.object(N.Q, 'verify_holds'))
        stack.enter_context(patch.object(N, 'current_runtime_health', return_value={'status': 'verified'}))
        stack.enter_context(patch.object(N.J.P, 'free_bytes', return_value=800 * 1024**3))
        stack.enter_context(patch.object(R, 'rpc', return_value=self.report if report is None else report))
        return stack

    def legacy_repair(self):
        source = b'legacy frozen repair file'
        path = self.directory / 'native-oversized-block.exs'
        N.R.write_new(path, source)
        record = {'format': 'blockscout-dev-v10-oversized-block-repair-v1', 'block': 13519337,
                  'max_rows': 12000, 'script_file': path.name, 'script_sha256': N.R.digest(source)}
        self.state['worker_repair'] = record
        N.R.update_state(self.path, self.state)
        return record, source

    def test_second_block_preserves_original_script_previous_repair_and_cursor(self):
        old, raw = self.legacy_repair()
        self.state['phase'] = 'paused'  # Rebind occurs before preparation.
        self.state['reason'] = None
        N.R.update_state(self.path, self.state)
        with self.prepare_context():
            result = R.prepare(self.api, ROOT, self.path, 9179901)
        saved = N.load(self.path)
        self.assertEqual(result['target_rows'], 10147)
        self.assertEqual(saved['database'], self.state['database'])
        self.assertEqual(saved['worker_repair_history'], [old])
        self.assertEqual((self.directory / old['script_file']).read_bytes(), raw)
        self.assertEqual((self.directory / 'native.exs').read_bytes(), self.original)
        self.assertEqual(saved['inputs'], self.inputs)
        self.assertEqual(saved['worker_repair']['block'], 9179901)
        self.assertNotEqual(saved['worker_repair']['script_file'], old['script_file'])

    def test_repeating_preparation_is_idempotent(self):
        old, _ = self.legacy_repair()
        with self.prepare_context():
            R.prepare(self.api, ROOT, self.path, 9179901)
            first = sorted(p.name for p in self.directory.iterdir())
            R.prepare(self.api, ROOT, self.path, 9179901)
        self.assertEqual(first, sorted(p.name for p in self.directory.iterdir()))
        self.assertEqual(N.load(self.path)['worker_repair_history'], [old])

    def test_guard_rejects_excess_rows_bytes_wrong_token_or_checkpoint_without_preparing(self):
        mutations = [{'target_rows': 12001, 'all_rows': 12001},
                     {'target_rows': 10000, 'all_rows': 10000},
                     {'target_bytes': 16777217, 'all_bytes': 16777217},
                     {'target_rows': 10146}, {'token': 'b' * 16}, {'cursor': 9179900},
                     {'status': 'completed'}]
        before = self.path.read_bytes()
        files = sorted(p.name for p in self.directory.iterdir())
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.prepare_context({**self.report, **mutation}):
                with self.assertRaises(N.D.ConfigError):
                    R.prepare(self.api, ROOT, self.path, 9179901)
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(sorted(p.name for p in self.directory.iterdir()), files)

    def test_wrong_block_low_space_running_worker_and_unguarded_pause_are_rejected(self):
        with self.prepare_context():
            with self.assertRaisesRegex(N.D.ConfigError, 'exact stopped'):
                R.prepare(self.api, ROOT, self.path, 9179900)
            with patch.object(N.J.P, 'free_bytes', return_value=200 * 1024**3):
                with self.assertRaisesRegex(N.D.ConfigError, 'free space'):
                    R.prepare(self.api, ROOT, self.path, 9179901)
            with patch.object(N, 'owned', return_value={'State': {'Running': True}}):
                with self.assertRaisesRegex(N.D.ConfigError, 'must be stopped'):
                    R.prepare(self.api, ROOT, self.path, 9179901)
            self.state.update(phase='paused', worker=None)
            N.R.update_state(self.path, self.state)
            with self.assertRaisesRegex(N.D.ConfigError, 'expected oversized'):
                R.prepare(self.api, ROOT, self.path, 9179901)

    def test_generated_worker_keeps_normal_and_wal_byte_limits(self):
        source = R.repair_source(self.original.decode(), 9179901)
        self.assertIn('if blocks == [9179901], do: 12000, else: 10000', source)
        self.assertIn('LIMIT $2) it', source)
        self.assertIn('[blocks, limit + 1]', source)
        self.assertNotIn('LIMIT 10001) it', source)
        self.assertIn('bytes <= 16777216', source)
        self.assertIn('unless updated <= limit', source)
        self.assertIn('wal <= 134217728', source)
        self.assertIn("statement_timeout='30s'", source)
        self.assertIn('EmptyInternalTransactionsData.update_batch(blocks)', source)
        with self.assertRaises(N.D.ConfigError):
            R.repair_source(source, 9179900)
        with self.assertRaises(N.D.ConfigError):
            R.repair_source(self.original.decode(), True)

    def test_load_rejects_a_rehashed_worker_that_widens_the_cap(self):
        with self.prepare_context():
            R.prepare(self.api, ROOT, self.path, 9179901)
        saved = N.load(self.path)
        record = saved['worker_repair']
        raw = (self.directory / record['script_file']).read_bytes().replace(b'do: 12000', b'do: 999999')
        record['script_sha256'] = N.R.digest(raw)
        record['script_file'] = f"native-oversized-block-9179901-{record['script_sha256'][:12]}.exs"
        N.R.write_new(self.directory / record['script_file'], raw)
        N.R.update_state(self.path, saved)
        with self.assertRaisesRegex(N.U.UpgradeError, 'bounded source template'):
            N.load(self.path)

    def test_service_can_be_installed_without_starting(self):
        self.api.require = N.J.P.require
        with patch.object(N.sys, 'platform', 'linux'), patch.object(N.os, 'geteuid', return_value=0), \
             patch.object(N, 'load', return_value=self.state), \
             patch.object(N.sys, 'argv', ['native', '--install-service', '--no-start', '--state', str(self.path)]), \
             patch.object(N, 'install_service', return_value={'status': 'installed'}) as install, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(N.main(), 0)
        install.assert_called_once_with(N.D, self.path, start=False)

    def test_rebind_retains_previous_service_root_even_on_retry(self):
        previous_root = '/data/blockscout-v10-rebind-fb284e5e8'
        self.binding['source_root'] = previous_root
        self.binding['database_anchor']['native_status']['meta']['max_block_number'] = 13519337
        self.write_binding()
        self.api.require = N.J.P.require
        self.api.run = lambda command, **kwargs: 'a' * 40 if command[0] == 'git' else ''
        environment = ['BLOCKSCOUT_VERSION=f0f40cd352d26db5c1f4743f18ad110e85a3c4af',
                       'MIGRATION_BACKGROUND_AUTOSTART_DISABLED=true']
        runtime = {'backend': {'Config': {'Env': environment}, 'Image': 'sha256:' + 'b' * 64}}
        manifest = {'compose_project': 'docker-compose'}
        with patch.object(B, 'ROOT', ROOT), patch.object(B, 'D', self.api), \
             patch.object(self.api, 'configuration', return_value=(manifest, {}, {})), \
             patch.object(self.api, 'inspect_project', return_value=runtime), \
             patch.object(self.api, 'env_map', create=True, side_effect=N.D.env_map), \
             patch.object(N.K, 'stable_database_anchor', return_value=self.anchor), \
             patch.object(N, 'owned', return_value=None), \
             patch.object(B, 'os', F.types.SimpleNamespace(geteuid=lambda: 0, replace=F.os.replace)), \
             patch.object(B.sys, 'platform', 'linux'), contextlib.redirect_stdout(io.StringIO()):
            self.binding['runtime_before'] = runtime
            self.write_binding()
            for _ in range(2):
                B.main(['--state', str(self.path)])
                binding = N.runtime_binding(self.path, N.load(self.path))
                self.assertEqual(binding['previous_source_root'], previous_root)
            unit_dir = self.directory / 'systemd'
            unit_dir.mkdir()
            render = lambda path, root, executable: f'{root}\n{path}\n{executable}\n'
            unit = unit_dir / N.UNIT
            N.R.write_new(unit, render(self.path, previous_root, self.inputs['python_executable']).encode())
            with patch.object(N, 'SYSTEMD_DIRECTORY', unit_dir), patch.object(N, 'render_service', side_effect=render), \
                 patch.object(N.B, 'clean_source', return_value=('a' * 40, '10.2.6')):
                result = N.install_service(self.api, self.path, start=False)
            self.assertFalse(result['service_start_requested'])
            self.assertEqual(unit.read_text(), render(self.path, ROOT, self.inputs['python_executable']))


if __name__ == '__main__':
    unittest.main()
