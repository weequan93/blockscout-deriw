from copy import deepcopy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import v10_normalization_plan as N


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.data = {'block': 176000000, 'seek_plan': {'access': 'Index Only Scan', 'index': 'internal_transactions_block_number_idx', 'estimated_total_cost': 1.5},
                     'batch_plan': {'access': 'Index Scan', 'index': 'internal_transactions_block_number_idx', 'estimated_total_cost': 4.5},
                     'sample_rows': 20, 'row_limit_exceeded': False, 'rows_needing_normalization_in_sample': 20,
                     'sampled_row_bytes': 8192, 'maximum_sampled_row_bytes': 1024,
                     'statuses': [['reindex_duplicated_internal_transactions', 'completed']],
                     'table_update_stats': [[0, 0, 0, 0]], 'wal_bytes': '1024000', 'wal_stats_reset': '2026-11-04 00:00:00+00'}
        self.free = 888 * 1024**3

    def test_candidate_is_read_only_and_does_not_claim_full_migration_or_wal_budget(self):
        result = N.project(self.data, self.free)
        self.assertEqual(result['status'], 'normalization_pilot_candidate_selected')
        self.assertTrue(result['read_only'])
        self.assertFalse(result['apply_available'])
        self.assertTrue(result['sample_is_complete_block'])
        self.assertEqual(result['worker_states'][0]['status'], 'not_recorded')

    def test_large_block_large_tuple_workload_low_space_and_empty_work_are_reported(self):
        for kind in ('rows', 'bytes', 'free', 'none'):
            data = deepcopy(self.data)
            free = self.free
            if kind == 'rows':
                data.update(sample_rows=1001, row_limit_exceeded=True)
            elif kind == 'bytes':
                data['sampled_row_bytes'] = 17 * 1024**2
            elif kind == 'free':
                free = 199 * 1024**3
            else:
                data['rows_needing_normalization_in_sample'] = 0
            result = N.project(data, free)
            self.assertEqual(result['status'], 'normalization_pilot_needs_review')
            self.assertTrue(result['blockers'])

    def test_unfinished_reindex_existing_heavy_job_or_completed_normalization_blocks_pilot(self):
        for value in ([], [['empty_internal_transactions_data', 'completed']],
                      [['reindex_duplicated_internal_transactions', 'completed'], [N.H.HISTORY_WORKERS[2], 'started']]):
            data = deepcopy(self.data)
            data['statuses'] = value
            self.assertTrue(N.project(data, self.free)['blockers'])

    def test_scan_plan_bad_counts_private_labels_and_malformed_stats_are_rejected(self):
        for mutate in (lambda d: d['seek_plan'].update(access='Seq Scan'),
                       lambda d: d['batch_plan'].update(index='private value with spaces'),
                       lambda d: d.update(row_limit_exceeded=True),
                       lambda d: d.update(sample_rows=1002),
                       lambda d: d.update(wal_stats_reset='private-value'),
                       lambda d: d.update(statuses=[['unknown-private-name', 'started']]),
                       lambda d: d.update(table_update_stats=[[0, 0, 'secret', 0]])):
            data = deepcopy(self.data)
            mutate(data)
            with self.assertRaises(N.D.ConfigError):
                N.project(data, self.free)

    def test_source_never_starts_worker_or_queries_unbounded_predicate_maximum(self):
        source = '\n'.join(line for line in (ROOT / 'deploy/scripts/v10/normalization_plan.exs').read_text().splitlines()
                           if not line.lstrip().startswith('#'))
        for value in ('SET TRANSACTION READ ONLY', "statement_timeout = '1500ms'", "lock_timeout = '500ms'", 'timeout: 5000',
                      'EXPLAIN (FORMAT JSON)', 'LIMIT 1001', 'ORDER BY block_number DESC LIMIT 1'):
            self.assertIn(value, source)
        for value in ('EXPLAIN ANALYZE', '.start_link(', '.update_batch(', 'UPDATE ', 'DELETE ', 'INSERT ', 'COUNT(', 'max(it.block_number)'):
            self.assertNotIn(value, source)


if __name__ == '__main__':
    unittest.main()
