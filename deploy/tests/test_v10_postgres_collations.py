from copy import deepcopy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import dev_config as D
import v10_postgres_collations as C


class CollationReviewTests(unittest.TestCase):
    def good(self):
        return {'database_collation_mismatch': False, 'collation_mismatches': 808,
                'collation_review': {'format': C.FORMAT, 'default_collation_mismatch': False,
                    'mismatched_collations': 808, 'dependency_catalogs': [], **dict.fromkeys(C.COUNTS, 0)}}

    def test_unchanged_default_and_unused_named_mismatches_are_admitted_without_editing_input(self):
        data = self.good()
        original = deepcopy(data)
        C.validate(data)
        self.assertEqual(data, original)

    def test_clean_database_keeps_existing_contract(self):
        C.validate({'database_collation_mismatch': False, 'collation_mismatches': 0})

    def test_any_dependency_or_unresolved_function_requires_review(self):
        for field in C.COUNTS:
            with self.subTest(field=field):
                data = self.good()
                data['collation_review'][field] = 1
                with self.assertRaises(D.ConfigError):
                    C.validate(data)

    def test_missing_partial_or_inconsistent_evidence_is_refused(self):
        for field in ('format', 'default_collation_mismatch', 'mismatched_collations',
                      'dependency_catalogs', *C.COUNTS):
            with self.subTest(missing=field):
                data = self.good()
                data['collation_review'].pop(field)
                with self.assertRaises(D.ConfigError):
                    C.validate(data)
        for field, value in (('format','unknown'), ('mismatched_collations',807),
                             ('dependency_catalogs',[{'catalog':'pg_class','count':1}]),
                             ('direct_dependencies',False), ('direct_dependencies',None)):
            data = self.good()
            data['collation_review'][field] = value
            with self.assertRaises(D.ConfigError):
                C.validate(data)

    def test_default_mismatch_is_never_admitted_even_without_named_dependencies(self):
        data = self.good()
        data['database_collation_mismatch'] = True
        with self.assertRaises(D.ConfigError):
            C.validate(data)
        data = self.good()
        data['collation_review']['default_collation_mismatch'] = True
        with self.assertRaises(D.ConfigError):
            C.validate(data)

    def test_unavailable_or_invalid_counts_are_refused(self):
        for value in (None,False,-1,'0'):
            with self.assertRaises(D.ConfigError):
                C.validate({'database_collation_mismatch': False,'collation_mismatches': value})
