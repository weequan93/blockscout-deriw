from copy import deepcopy
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import retire_containerd_source as R


class Host:
    source_retirement = R.M.Maintenance.source_retirement
    summary = R.M.Maintenance.summary

    def __init__(self, directory):
        self.directory = directory
        self.path = directory / 'state.json'
        self.plan_hash = 'fixture-plan'
        self.plan = {'source_identity': R.A.identity(R.M.S.SOURCE)}
        self.state = {'status': 'active', 'services_started_on_target': True, 'copy_verified': True}
        self.events = []
        self.healthy = True
        self.fail_removal = False
        self.config_side = 'target'

    def status(self):
        R.M.require(R.A.identity(R.M.S.SOURCE) == self.plan['source_identity'], 'source_identity_changed')
        return {**self.summary(), 'status': 'verified' if self.healthy else 'needs_attention',
                'recorded_status': 'active', 'observed_config_side': self.config_side,
                'backend_readiness_ok': self.healthy, 'nft_connection_and_queue_ok': self.healthy}

    def rows(self):
        return {}

    def long(self, arguments, **options):
        self.events.append(arguments)
        log = self.directory / ('log-' + str(len(self.events)))
        log.write_bytes(b'')
        if arguments[0] == 'rsync':
            assert '--dry-run' in arguments
            source, destination = map(Path, arguments[-2:])
            def contents(path):
                return {str(item.relative_to(path)): ('link', os.readlink(item)) if item.is_symlink()
                        else ('dir',) if item.is_dir() else ('file', item.read_bytes())
                        for item in path.rglob('*')}
            if contents(source) != contents(destination):
                log.write_bytes(b'archive differences')
        elif arguments[0] == 'find':
            if self.fail_removal:
                (R.M.S.SOURCE / 'payload').unlink()
                raise R.M.S.PlanError('fixture_interrupted_removal')
            subprocess.run(arguments, check=True, capture_output=True, timeout=10)
        else:
            assert arguments[:2] == ['sync', '-f']
        return log


class RetirementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name).resolve()
        self.source, self.target, self.directory = root / 'source', root / 'active', root / 'private'
        self.archive = self.directory / 'original-store-archive'
        for path in (self.source, self.target, self.directory, self.archive):
            path.mkdir(mode=0o700)
        for path in (self.source, self.archive):
            (path / 'payload').write_bytes(b'old store')
            (path / 'nested').mkdir()
            (path / 'nested' / 'second').write_bytes(b'second payload')
            (path / 'active-link').symlink_to(self.target, target_is_directory=True)
        (self.target / 'new-writes').write_bytes(b'keep active data')
        original_stat = Path.stat
        def storage_stat(path, *args, **kwargs):
            value = original_stat(path, *args, **kwargs)
            if path == self.source:
                fields = list(value)
                fields[stat.ST_DEV] = -1
                return os.stat_result(fields)
            return value
        self.patches = [patch.object(R.M.S, 'SOURCE', self.source), patch.object(R.M.S, 'TARGET', self.target),
                        patch.object(Path, 'stat', storage_stat),
                        patch.object(Path, 'read_text', return_value='mounts on active data store'),
                        patch.object(R.M, 'process_ids', return_value=[100]),
                        patch.object(R, 'active_removal', return_value=False),
                        patch.object(R.M, 'active_store_copy', return_value=False),
                        patch.object(R.M.S, 'capacity', return_value={'available_bytes': 898 * 1024**3}),
                        patch('sys.stdout', io.StringIO())]
        for item in self.patches:
            item.start()
        self.host = Host(self.directory)
        self.copy_receipt_path = self.directory / 'original-store-archive.json'
        self.copy_receipt = {'format': R.A.FORMAT, 'plan_sha256': self.host.plan_hash,
                             'source_identity': self.host.plan['source_identity'],
                             'archive_identity': R.A.identity(self.archive), 'copy_verified': True}
        self.write_receipt()

    def write_receipt(self):
        R.M.write_atomic(self.copy_receipt_path, R.M.encoded(self.copy_receipt))

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_check_reads_only_without_creating_retirement_journal(self):
        before = sorted(item.name for item in self.directory.iterdir())
        result = R.retire(self.host)
        self.assertEqual(result['status'], 'source_retirement_ready')
        self.assertEqual(sorted(item.name for item in self.directory.iterdir()), before)
        self.assertEqual(self.host.events, [])

    def test_removal_preserves_archive_active_data_and_original_directory_identity(self):
        before = R.A.identity(self.source)
        result = R.retire(self.host, apply=True)
        self.assertEqual(result['status'], 'source_retirement_verified')
        self.assertEqual(R.A.identity(self.source), before)
        self.assertEqual(list(self.source.iterdir()), [])
        self.assertEqual((self.archive / 'payload').read_bytes(), b'old store')
        self.assertTrue((self.archive / 'active-link').is_symlink())
        self.assertEqual((self.target / 'new-writes').read_bytes(), b'keep active data')
        summary = self.host.status()
        self.assertTrue(summary['original_store_cleanup_performed'])
        self.assertTrue(summary['source_archive_retained'])
        self.assertFalse(summary['source_retained'])
        events = len(self.host.events)
        R.retire(self.host, apply=True)
        self.assertEqual(len(self.host.events), events)

    def test_corrupt_archive_prevents_journal_creation_or_source_removal(self):
        (self.archive / 'payload').write_bytes(b'tampered')
        with self.assertRaisesRegex(R.M.S.PlanError, 'archive_changed_source_removal_refused'):
            R.retire(self.host, apply=True)
        self.assertFalse((self.directory / 'original-store-retirement.json').exists())
        self.assertEqual((self.source / 'payload').read_bytes(), b'old store')

    def test_unverified_or_wrong_source_receipt_prevents_removal(self):
        for changes in ({'copy_verified': False}, {'plan_sha256': 'different'}, {'source_identity': {}}, {'archive_identity': {}}):
            original = deepcopy(self.copy_receipt)
            self.copy_receipt.update(changes)
            self.write_receipt()
            with self.assertRaises(R.M.S.PlanError):
                R.retire(self.host, apply=True)
            self.copy_receipt = original
        self.assertEqual((self.source / 'payload').read_bytes(), b'old store')

    def test_partial_removal_resumes_without_recopied_or_incomplete_archive_comparison(self):
        self.host.fail_removal = True
        with self.assertRaisesRegex(R.M.S.PlanError, 'fixture_interrupted_removal'):
            R.retire(self.host, apply=True)
        self.assertEqual(self.host.source_retirement()['phase'], 'removing')
        self.assertFalse(self.host.summary()['source_retained'])
        self.assertEqual((self.archive / 'payload').read_bytes(), b'old store')
        self.host.fail_removal = False
        self.assertTrue(R.retire(self.host)['resume_partial_removal'])
        R.retire(self.host, apply=True)
        self.assertEqual(list(self.source.iterdir()), [])
        self.assertEqual(len([event for event in self.host.events if event[0] == 'rsync']), 1)

    def test_receipt_change_after_partial_removal_and_refilled_source_are_rejected(self):
        R.retire(self.host, apply=True)
        (self.source / 'unexpected').write_bytes(b'outside write')
        with self.assertRaisesRegex(R.M.S.PlanError, 'no_longer_empty'):
            self.host.status()
        (self.source / 'unexpected').unlink()
        self.copy_receipt['copy_verified'] = False
        self.write_receipt()
        with self.assertRaisesRegex(R.M.S.PlanError, 'archive_receipt_changed'):
            self.host.status()

    def test_source_identity_config_health_and_mount_drift_block_removal(self):
        self.host.healthy = False
        with self.assertRaises(R.M.S.PlanError):
            R.retire(self.host, apply=True)
        self.host.healthy = True
        self.host.config_side = 'source'
        with self.assertRaises(R.M.S.PlanError):
            R.retire(self.host, apply=True)
        self.host.config_side = 'target'
        with patch.object(Path, 'read_text', return_value='upperdir=' + str(self.source) + '/snapshot'):
            with self.assertRaises(R.M.S.PlanError):
                R.retire(self.host, apply=True)
        self.source.rename(self.source.with_name('preserved-source'))
        self.source.mkdir()
        with self.assertRaisesRegex(R.M.S.PlanError, 'source_identity_changed'):
            R.retire(self.host, apply=True)

    def test_archive_copy_is_refused_after_retirement_is_recorded(self):
        R.retire(self.host, apply=True)
        with self.assertRaisesRegex(R.M.S.PlanError, 'source_retirement_recorded'):
            R.A.archive(self.host, copy=True)

    def test_surviving_removal_process_blocks_retry(self):
        with patch.object(R, 'active_removal', return_value=True):
            with self.assertRaisesRegex(R.M.S.PlanError, 'previous_source_operation_is_running'):
                R.retire(self.host, apply=True)

    def test_replaced_archive_directory_blocks_source_removal(self):
        self.archive.rename(self.directory / 'preserved-archive')
        self.archive.mkdir()
        with self.assertRaisesRegex(R.M.S.PlanError, 'verified_archive_required'):
            R.retire(self.host, apply=True)
        self.assertEqual((self.source / 'payload').read_bytes(), b'old store')
        self.assertFalse(any(event[0] == 'find' for event in self.host.events))

    def test_retirement_record_survives_later_application_attention_state(self):
        R.retire(self.host, apply=True)
        self.host.state['status'] = 'needs_attention'
        self.assertEqual(self.host.source_retirement()['phase'], 'complete')
        self.assertTrue(self.host.summary()['source_archive_retained'])


if __name__ == '__main__':
    unittest.main()
