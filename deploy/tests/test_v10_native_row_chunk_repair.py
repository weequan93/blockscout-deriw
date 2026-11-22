from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import test_v10_native_oversized_block_repair as F
import v10_native_normalization as N
import v10_native_oversized_block_repair as O
import v10_native_row_chunk_repair as C


class RowChunkTests(unittest.TestCase):
    setUp = F.RepairTests.setUp
    write_binding = F.RepairTests.write_binding
    prepare_context = F.RepairTests.prepare_context

    def test_preparation_accepts_capped_inspection_preserving_original_repair_and_cursor(self):
        with self.prepare_context():
            O.prepare(self.api, ROOT, self.path, 9179901, max_rows=20000)
        self.state = N.load(self.path)
        old = deepcopy(self.state['worker_repair'])
        old_source = (self.directory / old['script_file']).read_bytes()
        self.state.update(phase='needs_attention', reason='native_batch_requires_smaller_range')
        self.write_binding()
        report = {**self.report, 'all_rows': 20001, 'target_rows': 20001, 'all_bytes': 6536564}
        with self.prepare_context(report):
            result = C.prepare(self.api, ROOT, self.path, 9179901)
            files = sorted(p.name for p in self.directory.iterdir())
            C.prepare(self.api, ROOT, self.path, 9179901)
        saved = N.load(self.path)
        self.assertTrue(result['sample_limit_reached'])
        self.assertEqual(result['chunk_rows'], 5000)
        self.assertTrue(result['subsequent_single_blocks_use_row_chunks'])
        self.assertFalse(result['database_writes'])
        self.assertEqual(saved['worker_repair_history'], [old])
        self.assertEqual((self.directory / old['script_file']).read_bytes(), old_source)
        self.assertEqual((self.directory / 'native.exs').read_bytes(), self.original)
        self.assertEqual(saved['inputs'], self.inputs)
        self.assertEqual(saved['database'], self.state['database'])
        self.assertEqual(files, sorted(p.name for p in self.directory.iterdir()))

    def test_partially_or_fully_normalized_large_block_can_be_replayed(self):
        for remaining in (0, 1, 7000):
            with self.subTest(remaining=remaining), self.prepare_context(
                    {**self.report, 'all_rows': 20001, 'target_rows': remaining}):
                C.prepare(self.api, ROOT, self.path, 9179901)
            self.assertEqual(N.load(self.path)['worker_repair']['chunk_rows'], 5000)

    def test_wrong_checkpoint_token_or_inspection_is_rejected_before_preparation(self):
        before = self.path.read_bytes()
        files = sorted(p.name for p in self.directory.iterdir())
        mutations = ({'cursor': 9179900}, {'token': 'b' * 16}, {'status': 'completed'},
                     {'all_rows': 10000}, {'all_rows': 20002}, {'target_rows': -1},
                     {'target_rows': 30000}, {'all_bytes': -1})
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.prepare_context({**self.report, **mutation}):
                with self.assertRaises(N.D.ConfigError):
                    C.prepare(self.api, ROOT, self.path, 9179901)
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(files, sorted(p.name for p in self.directory.iterdir()))

    def test_running_worker_low_space_and_wrong_block_are_rejected(self):
        with self.prepare_context():
            with self.assertRaisesRegex(N.D.ConfigError, 'exact stopped'):
                C.prepare(self.api, ROOT, self.path, 9179900)
            with patch.object(N, 'owned', return_value={'State': {'Running': True}}):
                with self.assertRaisesRegex(N.D.ConfigError, 'must be stopped'):
                    C.prepare(self.api, ROOT, self.path, 9179901)
            with patch.object(N.J.P, 'free_bytes', return_value=200 * 1024**3):
                with self.assertRaisesRegex(N.D.ConfigError, 'free space'):
                    C.prepare(self.api, ROOT, self.path, 9179901)

    def test_load_rejects_changed_chunk_limit_metadata_and_rehashed_source(self):
        with self.prepare_context():
            C.prepare(self.api, ROOT, self.path, 9179901)
        saved = N.load(self.path)
        for mutation in ({'chunk_rows': 10000}, {'chunk_rows': True}, {'block': -1},
                         {'script_file': '../native.exs'}, {'max_rows': 50000}):
            changed = deepcopy(saved)
            changed['worker_repair'].update(mutation)
            N.R.update_state(self.path, changed)
            with self.subTest(mutation=mutation), self.assertRaises(N.U.UpgradeError):
                N.load(self.path)
        changed = deepcopy(saved)
        record = changed['worker_repair']
        data = (self.directory / record['script_file']).read_bytes().replace(b'LIMIT 5000 FOR UPDATE', b'LIMIT 50000 FOR UPDATE')
        record['script_sha256'] = N.R.digest(data)
        record['script_file'] = f"native-row-chunks-{record['script_sha256'][:12]}.exs"
        N.R.write_new(self.directory / record['script_file'], data)
        N.R.update_state(self.path, changed)
        with self.assertRaisesRegex(N.U.UpgradeError, 'bounded source template'):
            N.load(self.path)

    def test_source_keeps_native_transformation_and_guards_with_fixed_ctids(self):
        data = C.source(self.original.decode())
        self.assertIn('defmodule BlockscoutNativeNormalization.RowCallback do', data)
        self.assertNotIn('defmodule Explorer.Migrator.EmptyInternalTransactionsData do', data)
        self.assertIn('^row_ctids', data)
        self.assertIn('LIMIT 5000 FOR UPDATE', data)
        self.assertIn('ISOLATION LEVEL REPEATABLE READ', data)
        self.assertIn('bytes <= 16777216', data)
        self.assertIn('wal <= 134217728', data)
        self.assertIn('updated == rows and updated <= 5000', data)
        self.assertIn("statement_timeout='30s'", data)
        self.assertIn("lock_timeout='500ms'", data)
        self.assertIn('def update([block]), do:', data)
        self.assertIn('unless rows <= 10000 and bytes <= 16777216', data)
        self.assertIn('native_residual_rows_require_review', data)
        with patch.object(C, 'CALLBACK_SHA256', '0' * 64):
            with self.assertRaisesRegex(N.D.ConfigError, 'reviewed revision'):
                C.source(self.original.decode())

    def test_worker_reports_partial_block_progress_without_private_fields(self):
        report = {'token': self.inputs['payload']['token'], 'phase': 'running',
                  'row_chunk_block': 9179901, 'row_chunk_committed_rows': 5000,
                  'PRIVATE': 'do-not-expose'}
        with patch.object(self.api, 'run', return_value=json.dumps(report)):
            result = N.read_worker(self.api, self.inputs)
        self.assertEqual(result['row_chunk_committed_rows'], 5000)
        self.assertNotIn('PRIVATE', result)


if __name__ == '__main__':
    unittest.main()
