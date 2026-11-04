#!/usr/bin/env python3
"""Verify complete-block sampling, partial-tail exclusion and oversized heads."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import check_v10_normalization_plan as F
import v10_normalization_batch as B


def check(image, network, token, environment, sql, pg, directory):
    def sample(bound):
        return F.inspect(image, network, token, environment, before_block=bound, batch_sample=True, projector=B.project)
    small = sample(21)
    assert small['status'] == 'normalization_batch_sample_ready'
    assert small['selected_rows'] == 5 and small['selected_block_count'] == 4
    assert [r['block'] for r in small['selected_blocks']] == [20, 12, 11, 10]
    assert small['older_non_null_range_exhausted']
    empty = sample(0)
    assert empty['selected_rows'] == 0 and empty['older_non_null_range_exhausted']
    assert 'no_stored_rows_below_cursor' in empty['blockers']
    # Synthetic complete blocks above the original two-row block. The 1001st
    # result cuts through block20; neither of its two rows may be selected.
    sql("INSERT INTO internal_transactions SELECT (jsonb_populate_record(NULL::internal_transactions,to_jsonb(t)||jsonb_build_object('block_number',g,'block_index',g,'index',g))).* FROM (SELECT * FROM internal_transactions WHERE block_number=10 LIMIT 1) t CROSS JOIN generate_series(1000,1999) g")
    capped = sample(5000)
    assert capped['selected_rows'] == 1000 and capped['selected_block_count'] == 1000
    assert capped['raw_sample_rows'] == 1001 and capped['excluded_boundary_block'] == 20
    assert capped['excluded_boundary_rows'] == 1
    assert all(r['block'] > 20 and r['rows'] == 1 for r in capped['selected_blocks'])
    assert not capped['older_non_null_range_exhausted']
    # A very busy first block cannot be passed to a block-based callback under
    # the cap. Report it explicitly while keeping its partial sample unselected.
    sql("INSERT INTO internal_transactions SELECT (jsonb_populate_record(NULL::internal_transactions,to_jsonb(t)||jsonb_build_object('block_number',4000,'block_index',g,'index',g))).* FROM (SELECT * FROM internal_transactions WHERE block_number=10 LIMIT 1) t CROSS JOIN generate_series(3000,4100) g")
    large = sample(5000)
    assert large['selected_rows'] == 0 and large['excluded_boundary_block'] == 4000
    assert large['excluded_boundary_rows'] == 1001
    assert 'leading_block_exceeds_sample_cap' in large['blockers']
    assert sql("SELECT count(*) FROM migrations_status WHERE migration_name='empty_internal_transactions_data'").strip() == '0'
    assert sql("SELECT status FROM migrations_status WHERE migration_name='reindex_duplicated_internal_transactions'").strip() == 'completed'
    return {'status': 'passed', 'exact_read_only_probe': True, 'complete_small_groups_checked': True,
            'partial_tail_block_excluded': True, '1000_selected_row_cap_checked': True,
            'oversized_leading_block_reported': True, 'empty_non_null_range_checked': True,
            'migration_records_unchanged': True, 'application_or_worker_started': False, 'live_services_accessed': False}


if __name__ == '__main__':
    raise SystemExit(F.main(pilot_check=check))
