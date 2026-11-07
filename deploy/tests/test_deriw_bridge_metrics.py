"""Exporter semantics, unavailable data, endpoint caching and service isolation."""
import copy
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import deriw_bridge_metrics as M


def snapshot():
    message = {'complete': True, 'size': 3, 'highest_message_id': 12,
               'highest_relayed_message_id': 10, 'oldest_pending_timestamp': 900,
               'pending_without_timestamp': 1, 'missing_origination': 1,
               'completion_recorded': 1, 'states': {'initiated': 2, 'relayed': 1}}
    return {'observed_at_unix': 1000, 'sections': {
        'deposits': {'ok': True, 'data': copy.deepcopy(message)},
        'withdrawals': {'ok': True, 'data': copy.deepcopy(message)},
        'rollup': {'ok': True, 'data': {'size': 3, 'lowest_block': 100, 'highest_block': 102,
            'latest_block_timestamp': 999, 'states': [
                {'stage': 'processed', 'parent_finality': 'not_applicable', 'count': 1, 'highest_block': 102},
                {'stage': 'sent', 'parent_finality': 'unfinalized', 'count': 1, 'highest_block': 101},
                {'stage': 'confirmed', 'parent_finality': 'finalized', 'count': 1, 'highest_block': 100}]}},
        'published': {'ok': True, 'data': {'block_number': 101, 'batch_number': 20, 'parent_timestamp': 980, 'parent_finality': 'unfinalized'}},
        'confirmed': {'ok': True, 'data': {'block_number': 100, 'parent_timestamp': 990, 'parent_finality': 'finalized'}}},
        'trackers': {'deposits': {'ok': True, 'running': True, 'next_parent_block': 200},
                     'settlement': {'ok': True, 'running': False, 'next_parent_block': None}}}


class BridgeMetricsTests(unittest.TestCase):
    def test_live_semantics_and_separate_parent_finality(self):
        data = snapshot()
        M.validate(data)
        output = M.render(data, 1000, True, now=1001)
        self.assertIn('deriw_bridge_sample_messages{direction="l2_to_l3",status="initiated"} 2', output)
        self.assertIn('deriw_bridge_sample_messages{direction="l3_to_l2",status="confirmed"} 0', output)
        self.assertIn('deriw_bridge_sample_oldest_pending_age_seconds{direction="l2_to_l3"} 100', output)
        self.assertIn('deriw_rollup_stage_block_number{stage="processed"} 102', output)
        self.assertIn('deriw_rollup_stage_parent_transaction_finalized{stage="sent"} 0', output)
        self.assertIn('deriw_rollup_stage_parent_transaction_finalized{stage="confirmed"} 1', output)

    def test_expired_or_failed_poll_removes_old_data(self):
        for success, now in [(True, 1151), (False, 1001), (True, 999)]:
            output = M.render(snapshot(), 1000, success, now=now)
            self.assertIn('deriw_bridge_exporter_up 0', output)
            self.assertNotIn('deriw_bridge_sample_messages', output)
            self.assertNotIn('deriw_rollup_stage_block_number', output)

    def test_failed_section_is_not_zero_or_stale(self):
        data = snapshot()
        data['sections']['confirmed'] = {'ok': False}
        data['sections']['deposits'] = {'ok': False}
        output = M.render(data, 1000, True, now=1000)
        self.assertIn('deriw_bridge_query_success{section="confirmed"} 0', output)
        self.assertNotIn('deriw_rollup_stage_block_number{stage="confirmed"}', output)
        self.assertNotIn('deriw_bridge_sample_messages{direction="l2_to_l3"', output)
        self.assertIn('deriw_bridge_sample_messages{direction="l3_to_l2"', output)

    def test_unknown_and_empty_are_distinct(self):
        data = snapshot()
        for key in ('confirmed', 'published'):
            data['sections'][key]['data'] = {}
        output = M.render(data, 1000, True, now=1000)
        self.assertIn('deriw_rollup_stage_observed{stage="confirmed"} 0', output)
        self.assertNotIn('deriw_rollup_stage_block_number{stage="confirmed"}', output)

    def test_duplicate_and_unbounded_labels_are_rejected(self):
        for mutate in [
            lambda d: d['sections']['withdrawals']['data']['states'].update({'secret-hash': 1}),
            lambda d: d['sections']['rollup']['data']['states'].append(d['sections']['rollup']['data']['states'][0]),
            lambda d: d['sections']['rollup']['data']['states'][0].update({'parent_finality': 'unexpected'}),
            lambda d: d['sections']['deposits']['data'].update({'size': 10001}),
            lambda d: d['sections']['confirmed']['data'].update({'block_number': float('nan')})]:
            data = snapshot()
            mutate(data)
            with self.assertRaises(M.D.ConfigError):
                M.validate(data)

    def test_render_has_unique_series_and_family_headers(self):
        output = M.render(snapshot(), 1000, True, now=1000)
        samples = [s.rsplit(' ', 1)[0] for s in output.splitlines() if not s.startswith('#')]
        self.assertEqual(len(samples), len(set(samples)))
        helps = [s for s in output.splitlines() if s.startswith('# HELP')]
        self.assertEqual(len(helps), len(set(helps)))

    def test_sample_truncation_is_visible(self):
        data = snapshot()
        data['sections']['withdrawals']['data']['complete'] = False
        output = M.render(data, 1000, True, now=1000)
        self.assertIn('deriw_bridge_sample_complete{direction="l3_to_l2"} 0', output)
        self.assertIn('latest-10000', output)

    def test_reorg_regression_is_reported_without_high_water_cache(self):
        data = snapshot()
        first = M.render(data, 1000, True, now=1000)
        data['sections']['confirmed']['data']['block_number'] = 95
        second = M.render(data, 1000, True, now=1000)
        self.assertIn('deriw_rollup_stage_block_number{stage="confirmed"} 100', first)
        self.assertIn('deriw_rollup_stage_block_number{stage="confirmed"} 95', second)

    def test_cached_http_scrapes_do_not_poll_database(self):
        calls = []
        def collector():
            calls.append(1)
            data = snapshot()
            data['observed_at_unix'] = int(M.time.time())
            return data
        cache = M.Cache(collector)
        cache.poll()
        server = ThreadingHTTPServer(('127.0.0.1', 0), M.handler(cache))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for _ in range(3):
                with urlopen('http://127.0.0.1:' + str(server.server_port) + '/metrics') as response:
                    self.assertIn('deriw_bridge_exporter_up 1', response.read().decode())
            self.assertEqual(calls, [1])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_poll_errors_do_not_leak_exception_text(self):
        def collector():
            raise ValueError('private connection URL')
        cache = M.Cache(collector)
        cache.poll()
        self.assertIn('deriw_bridge_exporter_up 0', cache.metrics())
        self.assertNotIn('private', cache.metrics())

    def test_service_does_not_restart_backend_or_normalizer(self):
        text = M.service_text(Path('/data/pinned-release'), Path('/usr/bin/python3'), '127.0.0.1', 9160)
        self.assertIn('--serve', text)
        self.assertIn('ProtectSystem=strict', text)
        self.assertNotIn('normalization.service', text)
        self.assertNotIn('docker compose', text)
        self.assertNotIn('ExecStartPre', text)

    def test_runtime_change_discards_observation(self):
        class API:
            configuration = staticmethod(lambda *_: ({}, None, None))
            inspect_project = staticmethod(lambda *_: {})
            run = staticmethod(lambda *_args, **_kwargs: '')
            def ensure_stable(*_args):
                raise M.D.ConfigError('changed')
        with patch.object(M.C, 'marked_json', return_value=snapshot()):
            with self.assertRaises(M.D.ConfigError):
                M.collect(API())

    def test_failed_live_preflight_does_not_install_or_start_service(self):
        data = snapshot()
        data['sections']['confirmed'] = {'ok': False, 'reason': 'statement_timeout'}
        with tempfile.TemporaryDirectory() as directory:
            unit = Path(directory) / M.UNIT
            with patch.object(M.sys, 'platform', 'linux'), patch.object(M.os, 'geteuid', return_value=0), \
                 patch.object(M, 'UNIT_PATH', unit), patch.object(M.D, 'run', return_value='') as run, \
                 patch.object(M, 'collect', return_value=data), patch('sys.stdout', new_callable=io.StringIO):
                with self.assertRaises(M.D.ConfigError):
                    M.install('127.0.0.1', 9160)
                self.assertFalse(unit.exists())
                self.assertFalse(any(call.args[0][0] == 'systemctl' for call in run.call_args_list))

    def test_successful_preflight_installs_only_new_observer(self):
        with tempfile.TemporaryDirectory() as directory:
            unit = Path(directory) / M.UNIT
            with patch.object(M.sys, 'platform', 'linux'), patch.object(M.os, 'geteuid', return_value=0), \
                 patch.object(M, 'UNIT_PATH', unit), patch.object(M.D, 'run', return_value='') as run, \
                 patch.object(M, 'collect', return_value=snapshot()), patch('sys.stdout', new_callable=io.StringIO):
                M.install('127.0.0.1', 9160)
                commands = [call.args[0] for call in run.call_args_list]
                self.assertEqual([command for command in commands if command[0] == 'systemctl'], [
                    ['systemctl', 'daemon-reload'], ['systemctl', 'enable', '--now', M.UNIT]])
                self.assertIn('deriw_bridge_metrics.py', unit.read_text())


if __name__ == '__main__':
    unittest.main()
