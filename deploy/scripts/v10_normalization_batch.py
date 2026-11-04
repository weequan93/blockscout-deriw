#!/usr/bin/env python3
"""Read a bounded set of complete stored blocks below a verified pilot cursor."""
from datetime import datetime, timezone
import argparse
import json
import os
import stat
from pathlib import Path
import sys
import uuid

import candidate_build as B
import containerd_maintenance as M
import dev_config as D
import release as R
import v10_normalization_plan as N
import v10_normalization_pilot as P

ROOT = Path(__file__).resolve().parents[2]
BASE = Path('/data/blockscout-dev-private/v10-normalization-samples')
FORMAT = 'blockscout-dev-v10-normalization-sample-v1'
STAGE = 'completed_pilot'


def project(data, free):
    shared, blockers = N.common(data, free)
    bound = data.get('before_block')
    raw = data.get('raw_sample_rows')
    groups = data.get('groups')
    boundary, excluded = data.get('excluded_boundary_block'), data.get('excluded_boundary_rows')
    exhausted = data.get('older_non_null_range_exhausted')
    D.require(type(bound) is int and 0 <= bound < 2**63 and type(raw) is int and 0 <= raw <= 1001
              and isinstance(groups, list) and len(groups) <= 1000 and type(excluded) is int and 0 <= excluded <= raw
              and type(exhausted) is bool and exhausted == (raw < 1001), 'Invalid batch sample bounds')
    blocks = []
    previous = bound
    for row in groups:
        D.require(isinstance(row, dict) and set(row) == {'block', 'rows', 'needs', 'row_bytes'}
                  and all(type(v) is int for v in row.values()) and 0 <= row['block'] < previous
                  and 0 < row['rows'] <= 1000 and 0 <= row['needs'] <= row['rows'] and row['row_bytes'] >= 0,
                  'Invalid complete-block grouping')
        previous = row['block']
        blocks.append(dict(row))
    count = sum(row['rows'] for row in blocks)
    needed = sum(row['needs'] for row in blocks)
    size = sum(row['row_bytes'] for row in blocks)
    D.require(count <= 1000 and count + excluded == raw, 'Batch row accounting mismatch')
    if raw == 1001:
        D.require(type(boundary) is int and 0 <= boundary < bound and excluded > 0
                  and all(row['block'] > boundary for row in blocks), 'Incomplete boundary block was not excluded')
    else:
        D.require(boundary is None and excluded == 0, 'Unexpected excluded boundary')
    if not blocks:
        blockers.append('leading_block_exceeds_sample_cap' if raw else 'no_stored_rows_below_cursor')
    if blocks and needed == 0:
        blockers.append('selected_blocks_have_no_normalization_work')
    if size > 16 * 1024**2:
        blockers.append('selected_rows_exceed_16_mib_budget')
    return {'status': 'normalization_batch_sample_ready' if not blockers else 'normalization_batch_sample_needs_review',
            'read_only': True, 'apply_available': False, 'blockers': blockers, 'before_block': bound,
            'selected_rows': count, 'rows_needing_normalization': needed, 'selected_row_bytes': size,
            'selected_block_count': len(blocks), 'newest_selected_block': blocks[0]['block'] if blocks else None,
            'oldest_selected_block': blocks[-1]['block'] if blocks else None,
            'raw_sample_rows': raw, 'excluded_boundary_block': boundary, 'excluded_boundary_rows': excluded,
            'older_non_null_range_exhausted': exhausted, 'selected_blocks': blocks, **shared,
            'scope': 'Read-only indexed seek and at most 1001 older trace rows. An ambiguous last block is excluded completely. No row update, cursor advance, completion record, worker start, index build or application restart. This is a workload sample, not a full normalization budget.'}


def sample(api, root, pilot_path):
    global STAGE
    STAGE = 'completed_pilot'
    manifest, _, _ = api.configuration(root, 'dev')
    pilot = P.load(api, pilot_path, manifest)
    api.require(pilot['phase'] == 'applied' and pilot['writes_may_have_started'], 'The previous pilot must be recorded applied')
    verified = P.status(api, root, pilot_path, manifest)
    api.require(verified['status'] == 'verified' and verified['rows_needing_normalization'] == 0,
                'Verify the previous pilot before selecting older work')
    STAGE = 'bounded_older_sample'
    report = N.collect(api, root, Path(pilot['upgrade_state']), before_block=pilot['block'], batch_sample=True, projector=project)
    # Keep the complete block manifest on the server; stdout has a small preview.
    B.validate_state_directory(api, BASE, root)
    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    api.require(BASE.stat().st_uid == os.geteuid() and stat.S_IMODE(BASE.stat().st_mode) == 0o700, 'Sample directory ownership/permissions changed')
    receipt = BASE / ('sample-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8] + '.json')
    record = {'format': FORMAT, 'previous_pilot_state': str(pilot_path), **report}
    R.write_new(receipt, R.encoded(record))
    blocks = report.pop('selected_blocks')
    preview = blocks if len(blocks) <= 6 else blocks[:3] + blocks[-3:]
    return {**report, 'sample_file': str(receipt), 'sample_sha256': R.digest(R.encoded(record)),
            'block_preview': preview, 'preview_truncated': len(preview) < len(blocks),
            'note': 'Complete block/count manifest saved privately on this server. No normalization was performed by sampling.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pilot-state', type=Path, required=True)
    args = parser.parse_args()
    try:
        D.require(sys.platform.startswith('linux') and os.geteuid() == 0, 'Run on the Linux dev server as root')
        with M.global_lock(False), B.build_lock(D, D.MANAGED_RELEASES, ROOT):
            result = sample(D, ROOT, args.pilot_state)
        print(json.dumps(result, indent=2))
        return 0
    except Exception:
        print(json.dumps({'status': 'stopped', 'stage': STAGE, 'read_only': True, 'apply_available': False,
                          'raw_values_suppressed': True, 'reason': 'Bounded batch sampling failed; no normalization was performed.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
