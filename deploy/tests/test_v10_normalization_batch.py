from copy import deepcopy
import json
import tempfile
import types
from unittest.mock import Mock, patch
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import v10_normalization_batch as B
import test_v10_normalization_plan as F


class BatchTests(unittest.TestCase):
    def setUp(self):
        fixture = F.PlanTests()
        fixture.setUp()
        self.data = {**fixture.data, 'before_block': 21, 'raw_sample_rows': 5, 'excluded_boundary_block': None,
                     'excluded_boundary_rows': 0, 'older_non_null_range_exhausted': True,
                     'groups': [{'block': 20, 'rows': 2, 'needs': 2, 'row_bytes': 800},
                                {'block': 12, 'rows': 1, 'needs': 1, 'row_bytes': 400},
                                {'block': 11, 'rows': 1, 'needs': 1, 'row_bytes': 400},
                                {'block': 10, 'rows': 1, 'needs': 1, 'row_bytes': 400}]}
        self.free = fixture.free

    def test_complete_block_counts_are_kept_for_future_row_limits(self):
        result = B.project(self.data, self.free)
        self.assertEqual(result['status'], 'normalization_batch_sample_ready')
        self.assertEqual(result['selected_rows'], 5)
        self.assertEqual(result['selected_block_count'], 4)
        self.assertEqual(result['oldest_selected_block'], 10)
        self.assertTrue(result['read_only'])
        self.assertFalse(result['apply_available'])

    def test_full_probe_rejects_a_partial_boundary_in_the_selected_manifest(self):
        data = {**self.data, 'raw_sample_rows': 1001, 'older_non_null_range_exhausted': False,
                'excluded_boundary_block': 9, 'excluded_boundary_rows': 996}
        self.assertEqual(B.project(data, self.free)['selected_rows'], 5)
        data['excluded_boundary_block'] = 12
        with self.assertRaises(B.D.ConfigError):
            B.project(data, self.free)

    def test_oversized_leading_block_is_reported_without_claiming_it_is_complete(self):
        data = {**self.data, 'raw_sample_rows': 1001, 'groups': [], 'older_non_null_range_exhausted': False,
                'excluded_boundary_block': 20, 'excluded_boundary_rows': 1001}
        result = B.project(data, self.free)
        self.assertEqual(result['selected_rows'], 0)
        self.assertIn('leading_block_exceeds_sample_cap', result['blockers'])

    def test_empty_older_range_is_separate_from_whole_table_completion(self):
        data = {**self.data, 'raw_sample_rows': 0, 'groups': []}
        result = B.project(data, self.free)
        self.assertIn('no_stored_rows_below_cursor', result['blockers'])
        self.assertFalse(result['apply_available'])

    def test_duplicate_unsorted_out_of_range_or_wrong_counts_are_rejected(self):
        for mutate in (lambda d: d['groups'][1].update(block=20),
                       lambda d: d['groups'][0].update(block=21),
                       lambda d: d['groups'][0].update(rows=1001),
                       lambda d: d.update(excluded_boundary_rows=1),
                       lambda d: d['groups'][0].update(needs=3)):
            data = deepcopy(self.data)
            mutate(data)
            with self.assertRaises(B.D.ConfigError):
                B.project(data, self.free)

    def test_private_receipt_keeps_the_full_manifest_while_stdout_has_a_short_preview(self):
        data = {**self.data, 'raw_sample_rows': 8,
                'groups': [{'block': b, 'rows': 1, 'needs': 1, 'row_bytes': 400} for b in range(20, 12, -1)]}
        report = B.project(data, self.free)
        api = types.SimpleNamespace(require=B.D.require, configuration=Mock(return_value=({'chain_id': '18417507517'}, {}, {})))
        pilot = {'phase': 'applied', 'writes_may_have_started': True, 'block': 21, 'upgrade_state': '/fixture/upgrade/state.json'}
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve() / 'samples'
            with patch.object(B, 'BASE', base), patch.object(B.P, 'load', return_value=pilot), \
                    patch.object(B.P, 'status', return_value={'status': 'verified', 'rows_needing_normalization': 0}), \
                    patch.object(B.N, 'collect', return_value=report) as collect:
                result = B.sample(api, ROOT, Path('/fixture/pilot/state.json'))
            path = Path(result['sample_file'])
            saved = json.loads(path.read_text())
            self.assertEqual(len(saved['selected_blocks']), 8)
            self.assertNotIn('selected_blocks', result)
            self.assertEqual(len(result['block_preview']), 6)
            self.assertTrue(result['preview_truncated'])
            self.assertEqual(result['sample_sha256'], B.R.digest(path.read_bytes()))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(collect.call_args.kwargs['before_block'], 21)
            self.assertTrue(collect.call_args.kwargs['batch_sample'])

    def test_cursor_is_validated_before_embedding_public_probe_options(self):
        source = B.N.expression(ROOT, before_block=171635973, batch_sample=True)
        self.assertIn('options = %{batch_sample: true, before_block: 171635973}', source)
        for value in ('171635973; private', -1, True, 2**63):
            with self.assertRaises(B.D.ConfigError):
                B.N.expression(ROOT, before_block=value, batch_sample=True)
        with self.assertRaises(B.D.ConfigError):
            B.N.expression(ROOT, batch_sample=True)


if __name__ == '__main__':
    unittest.main()
