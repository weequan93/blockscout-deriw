from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import v10_normalization_session as J
import v10_normalization_reuse as Q
import v10_vacuum as V


NOTICE = '''finished vacuuming "fixture.public.internal_transactions": index scans: 1
pages: 0 removed, 100 remain, 10 scanned (10.00% of total)
tuples: 0 removed, 1000 remain, 0 are dead but not yet removable
index scan needed: 5 pages from table (5.00% of total) had 42 dead item identifiers removed
index "private-index-canary": pages: 90 in total, 0 newly deleted, 2 currently deleted, 2 reusable
buffer usage: 11 hits, 77 misses, 8 dirtied
WAL usage: 5 records, 3 full page images, 4096 bytes
'''


def review(parent_path, previous, free=883 * 1024**3):
    before = deepcopy(previous['inputs']['maintenance_origin']['resources'])
    before.update(observed_at='2026-11-05 00:43:30+00', heap_identity=1)
    before['table'].update(vacuum_count=0, dead_rows=25000, options=[])
    after = deepcopy(before)
    after.update(observed_at='2026-11-05 03:17:57+00', runtime_holds_verified=True,
                 target_sessions=[], vacuum=[], data_free_bytes=free)
    after['table'].update(vacuum_count=1, dead_rows=0)
    return {
        'format': Q.FORMAT, 'vacuum_state': '/private/vacuum/state.json', 'vacuum_state_sha256': 'a' * 64,
        'vacuum_inputs_sha256': 'b' * 64,
        'normalization_state': str(parent_path), 'normalization_state_sha256': J.R.digest(J.U.private(parent_path)),
        'normalization_cursor': previous['cursor'],
        'maintenance_selected_rows': previous['inputs']['maintenance_rows_before'] + previous['rows'],
        'maintenance_origin_sha256': J.R.digest(J.R.encoded(previous['inputs']['maintenance_origin'])),
        'candidate_image': previous['inputs']['candidate_image'], 'upgrade_state': previous['inputs']['upgrade_state'],
        'runtime_before_sha256': J.R.digest(J.R.encoded(J.D.stable(previous['inputs']['runtime_before']))),
        'database_anchor': {'database_identity': {}, 'heap_identity': 1},
        'vacuum_sql_completed': True, 'completion_verified': True,
        'vacuum_duration_seconds': 14400, 'receipt_elapsed_ms': 9247446,
        'resources_before': before, 'resources_after': after,
        'cleanup': Q.cleanup_metrics([{'message': NOTICE}], 'fixture')}


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        os.chmod(self.directory, 0o700)
        self.path = self.directory / 'state.json'
        self.parent_path = self.directory / 'parent.json'
        J.R.write_new(self.parent_path, b'{"checkpoint":"original"}')
        self.previous = {'cursor': 12, 'rows': 2, 'inputs': {
            'maintenance_rows_before': 249506, 'candidate_image': 'sha256:' + 'a' * 64,
            'upgrade_state': '/private/upgrade/state.json', 'runtime_before': {},
            'maintenance_origin': {'space_before': 888 * 1024**3, 'resources': {
                'table': {'vacuum_count': 0, 'dead_rows': 25000, 'stats_reset': None, 'options': []},
                'wal': {'stats_reset': 'fixture'}}}}}
        self.review = review(self.parent_path, self.previous)

    def vacuum(self):
        data = self.review
        payload = {'database_identity': {'database': 'fixture'}, 'heap_identity': 1}
        inputs = {'normalization_state': str(self.parent_path),
                  'normalization_state_sha256': data['normalization_state_sha256'],
                  'normalization_cursor': data['normalization_cursor'],
                  'maintenance_selected_rows': data['maintenance_selected_rows'],
                  'maintenance_origin': self.previous['inputs']['maintenance_origin'],
                  'candidate_image': data['candidate_image'], 'runtime_before': {},
                  'upgrade_state': data['upgrade_state'], 'chain_id': '1',
                  'limits': {'duration_seconds': 14400}, 'payload': payload}
        target = {'pid': 42}
        result = {'identity': target, 'token': 'a' * 16, 'mode': 'completed', 'command': 'vacuum',
                  'index_cleanup_requested': True, 'elapsed_ms': 9247446}
        inputs['payload']['token'] = result['token']
        state = {'phase': 'finished', 'stop_reason': 'vacuum_completed_needs_review',
                 'vacuum_sql_completed': True, 'completion_verified': True, 'inputs': inputs, 'target': target,
                 'inputs_sha256': 'b' * 64,
                 'result': result, 'latest': data['resources_after'],
                 'resources_before_sha256': J.R.digest(J.R.encoded(data['resources_before']))}
        for name, value in [('state.json', state), ('resources-before.json', data['resources_before']),
                            ('resources-after.json', data['resources_after'])]:
            J.R.write_new(self.directory / name, J.R.encoded(value))
        messages = [{'message': NOTICE}, {'message': NOTICE.replace('.public.internal_transactions', '.pg_toast.pg_toast_1')}]
        log = 'BLOCKSCOUT_VACUUM_RESULT=' + json.dumps(result) + '\nBLOCKSCOUT_VACUUM_VERBOSE=' + json.dumps(messages) + '\n'
        J.R.write_new(self.directory / 'client.log.private', log.encode())
        base = self.directory / 'sessions'
        base.mkdir(mode=0o700)
        api = types.SimpleNamespace(inspect_project=lambda *a: {}, ensure_stable=lambda *a: None)
        return state, base, api

    def test_main_table_notice_excludes_toast_and_suppresses_names(self):
        messages = [{'message': NOTICE}, {'message': NOTICE.replace('.public.internal_transactions', '.pg_toast.pg_toast_1')}]
        metrics = Q.cleanup_metrics(messages, 'fixture')
        self.assertEqual(metrics['tuples'][0], 0)
        self.assertEqual(metrics['dead_item_identifiers_removed'], 42)
        self.assertEqual(len(metrics['index_pages']), 1)
        self.assertNotIn('private-index-canary', json.dumps(metrics))

    def test_incomplete_or_ambiguous_cleanup_is_rejected(self):
        for messages in ([], [NOTICE, NOTICE], [NOTICE.replace('index scan needed:', 'index scan bypassed:')],
                         [NOTICE + '\ntuples missed: 1 dead from 1 pages not removed due to cleanup lock contention']):
            with self.subTest(messages=len(messages)), self.assertRaises(J.U.UpgradeError):
                Q.cleanup_metrics(messages, 'fixture')

    def test_counter_reset_or_unqualified_cleanup_is_rejected(self):
        mutations = [lambda x: x.update(completion_verified=False),
                     lambda x: x['resources_after']['table'].update(vacuum_count=0),
                     lambda x: x['resources_after']['table'].update(dead_rows=1),
                     lambda x: x['resources_after']['wal'].update(stats_reset='changed'),
                     lambda x: x['resources_after'].update(runtime_holds_verified=False),
                     lambda x: x['cleanup'].update(index_scans=0),
                     lambda x: x['cleanup'].update(tuples=[0, 1000, 1])]
        for mutate in mutations:
            data = deepcopy(self.review)
            mutate(data)
            with self.assertRaises(J.U.UpgradeError):
                Q.validate_review(data)

    def test_exact_parent_receipt_and_single_use_are_checked_before_dispatch(self):
        state, base, api = self.vacuum()
        with patch.object(J, 'BASE', base), patch.object(V, 'load', return_value=state), \
             patch.object(V, 'no_normalizer'), patch.object(V, 'owned_client', return_value=False), \
             patch.object(Q, 'verify_holds') as holds:
            proof = Q.prepare_review(api, self.path, self.parent_path, self.previous, {'chain_id': '1'})
            self.assertEqual(proof['cleanup']['dead_item_identifiers_removed'], 42)
            holds.assert_called_once()
            state['inputs']['normalization_cursor'] += 1
            with self.assertRaisesRegex(J.U.UpgradeError, 'another normalization checkpoint'):
                Q.prepare_review(api, self.path, self.parent_path, self.previous, {'chain_id': '1'})
            state['inputs']['normalization_cursor'] -= 1
            duplicate = base / 'reserved'
            duplicate.mkdir(mode=0o700)
            with patch.object(J, 'load', return_value={'inputs': {'reuse_review': proof}}):
                with self.assertRaisesRegex(J.U.UpgradeError, 'already reserved'):
                    Q.prepare_review(api, self.path, self.parent_path, self.previous, {'chain_id': '1'})
            state['result'] = {**state['result'], 'elapsed_ms': 2}
            with self.assertRaisesRegex(J.U.UpgradeError, 'receipt changed'):
                Q.prepare_review(api, self.path, self.parent_path, self.previous, {'chain_id': '1'})

    def test_grant_reservation_survives_session_archival_and_cannot_be_replaced(self):
        state, _, _ = self.vacuum()
        proof = {**self.review, 'vacuum_state': str(self.path),
                 'vacuum_state_sha256': J.R.digest(J.U.private(self.path))}
        session = self.directory / 'sessions' / 'session'
        session.mkdir(mode=0o700)
        session_path = session / 'state.json'
        Q.reserve_review(proof, session_path, 'c' * 64)
        reservation = self.directory / 'normalization-reuse.json'
        saved = J.U.private(reservation)
        self.assertEqual(json.loads(saved)['state'], str(session_path))
        with self.assertRaises(J.U.UpgradeError):
            Q.reserve_review(proof, Path('/private/another/state.json'), 'd' * 64)
        self.assertEqual(J.U.private(reservation), saved)

    def test_legacy_final_free_sample_skew_accepts_only_one_block_within_frozen_budgets(self):
        state, base, api = self.vacuum()
        state['latest'] = deepcopy(state['latest'])
        free = state['latest']['data_free_bytes']
        state['inputs'].update(space_before=free + 1024**3)
        state['inputs']['limits'].update(free_bytes=200 * 1024**3, space_bytes=4 * 1024**3)
        with patch.object(J, 'BASE', base), patch.object(V, 'load', return_value=state), \
             patch.object(V, 'no_normalizer'), patch.object(V, 'owned_client', return_value=False), \
             patch.object(Q, 'verify_holds'):
            state['latest']['data_free_bytes'] = free + 4096
            Q.prepare_review(api, self.path, self.parent_path, self.previous, {'chain_id': '1'})
            state['latest']['data_free_bytes'] = free + 4097
            with self.assertRaisesRegex(J.U.UpgradeError, '4 KiB'):
                Q.prepare_review(api, self.path, self.parent_path, self.previous, {'chain_id': '1'})
            state['latest']['data_free_bytes'] = free + 4096
            state['inputs']['limits']['free_bytes'] = free + 1
            with self.assertRaisesRegex(J.U.UpgradeError, 'frozen budget'):
                Q.prepare_review(api, self.path, self.parent_path, self.previous, {'chain_id': '1'})

    def test_cycle_reserves_parent_and_vacuum_without_overwriting_either_grant(self):
        self.vacuum()
        proof = {**self.review, 'vacuum_state': str(self.path),
                 'vacuum_state_sha256': J.R.digest(J.U.private(self.path))}
        cycle = {'state': str(self.parent_path), 'state_sha256': J.R.digest(J.U.private(self.parent_path)),
                 'inputs_sha256': 'd' * 64}
        # Use separate source directories, as on the server.
        parent = self.directory / 'parent'
        parent.mkdir(mode=0o700)
        J.R.write_new(parent / 'state.json', J.U.private(self.parent_path))
        cycle['state'] = str(parent / 'state.json')
        session = self.directory / 'sessions' / 'child'
        session.mkdir(mode=0o700)
        Q.reserve_cycle(cycle, proof, session / 'state.json', 'e' * 64)
        parent_grant = J.U.private(parent / 'normalization-cycle.json')
        vacuum_grant = J.U.private(self.directory / 'normalization-reuse.json')
        with self.assertRaises(J.U.UpgradeError):
            Q.reserve_cycle(cycle, proof, session / 'other.json', 'f' * 64)
        self.assertEqual(J.U.private(parent / 'normalization-cycle.json'), parent_grant)
        self.assertEqual(J.U.private(self.directory / 'normalization-reuse.json'), vacuum_grant)

    def test_resume_cannot_select_a_new_vacuum_or_profile(self):
        for flags in (['--after-vacuum', '/other'], ['--profile', Q.PROFILE]):
            with patch.object(J.sys, 'platform', 'linux'), patch.object(J.os, 'geteuid', return_value=0), \
                 patch.object(J.sys, 'argv', ['session', '--resume', '--state', '/saved', *flags]), \
                 patch.object(J.D, 'configuration') as configure, patch('builtins.print'):
                self.assertEqual(J.main(), 1)
                configure.assert_not_called()

    def test_reuse_hold_checks_share_the_original_health_window(self):
        inputs = {'profile': Q.PROFILE, 'limits': J.REUSE_LIMITS, 'upgrade_state': '/private/state.json'}
        with patch.object(Q, 'verify_holds') as holds, patch.object(J.P, 'context') as context, \
             patch.object(J.time, 'monotonic', side_effect=[100, 129]):
            self.assertEqual(J.session_context(None, ROOT, inputs), 100)
            holds.assert_called_once()
            with patch.object(J.U, 'verify') as verify:
                context.call_args.kwargs['verifier'](None, 'path', {}, {})
                self.assertEqual(verify.call_args.kwargs['deadline'], 130)
        with patch.object(Q, 'verify_holds'), patch.object(J.P, 'context'), \
             patch.object(J.time, 'monotonic', side_effect=[100, 130]):
            with self.assertRaisesRegex(J.U.UpgradeError, 'health evidence window'):
                J.session_context(None, ROOT, inputs)

    def test_hold_probe_only_reads_existing_supervisor_state(self):
        api = types.SimpleNamespace(require=J.P.require,
                                    run=lambda *a, **k: 'BLOCKSCOUT_DEPLOY_CHECK={"runtime_holds_verified":true}')
        Q.verify_holds(api)
        api.run = lambda *a, **k: 'BLOCKSCOUT_DEPLOY_CHECK={"runtime_holds_verified":false}'
        with self.assertRaises(J.U.UpgradeError):
            Q.verify_holds(api)
        for forbidden in ('terminate_child', 'restart_child', 'start_link', 'Repo.query', 'System.put_env'):
            self.assertNotIn(forbidden, Q.HOLD_PROBE)


if __name__ == '__main__':
    unittest.main()
