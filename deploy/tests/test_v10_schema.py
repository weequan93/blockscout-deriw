from copy import deepcopy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import v10_schema as S


class SchemaBudgetTests(unittest.TestCase):
    def setUp(self):
        self.data = {'types': list(map(list, S.EXPECTED_TYPES.items())),
                     'internal_transactions_heap_identity': 123,
                     'relations': [[name, 1024**2] for name in sorted(S.REQUIRED)]}
        self.capacity = {'data_available_bytes': 900 * S.GIB}
        self.presence = [{'table': name, 'exists': True, 'has_rows': False} for name in S.EMPTY_TABLES]

    def test_budget_only_covers_initial_schema_and_requires_recheck(self):
        result = S.budget(self.data, self.capacity, self.presence)
        self.assertEqual(result['status'], 'eligible_for_initial_schema_phase')
        self.assertFalse(result['historical_backfill_space_verified'])
        self.assertTrue(result['recheck_after_stopping_clients_required'])
        self.assertEqual(result['minimum_remaining_free_bytes'], 100 * S.GIB)

    def test_type_change_large_tables_low_space_or_nonempty_drop_block_eligibility(self):
        for condition in ('type', 'size', 'space', 'rows'):
            data, capacity, presence = deepcopy((self.data, self.capacity, self.presence))
            if condition == 'type':
                data['types'][1][1] = 'numeric(78,0)'
            elif condition == 'size':
                data['relations'][0][1] = 2 * S.GIB
            elif condition == 'space':
                capacity['data_available_bytes'] = 119 * S.GIB
            else:
                presence[0]['has_rows'] = True
            self.assertEqual(S.budget(data, capacity, presence)['status'], 'initial_schema_resources_require_review')

    def test_missing_duplicate_or_invalid_metadata_cannot_establish_budget(self):
        for mutate in (lambda d: d.update(relations=d['relations'][1:]),
                       lambda d: d['relations'].append(d['relations'][0]),
                       lambda d: d.update(internal_transactions_heap_identity=None),
                       lambda d: d['relations'][0].__setitem__(1, 'private')):
            data = deepcopy(self.data)
            mutate(data)
            with self.assertRaises(S.D.ConfigError):
                S.budget(data, self.capacity, self.presence)
        with self.assertRaises(S.D.ConfigError):
            S.budget(self.data, self.capacity, [])

    def test_probe_has_bounded_read_only_catalog_queries_without_trace_scan(self):
        body = S.body()
        for token in ('SET TRANSACTION READ ONLY', "statement_timeout = '1500ms'", "lock_timeout = '500ms'",
                      'timeout: 5000', 'pg_attribute', 'pg_total_relation_size', 'pg_relation_filenode'):
            self.assertIn(token, body)
        for token in ('FROM internal_transactions', 'COUNT(', 'Ecto.Migrator', 'UPDATE ', 'CREATE ', 'DELETE '):
            self.assertNotIn(token, body)


if __name__ == '__main__':
    unittest.main()
