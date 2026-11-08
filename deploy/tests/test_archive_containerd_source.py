from copy import deepcopy
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import archive_containerd_source as A


class Host:
    def __init__(self, directory):
        self.directory = directory
        self.plan_hash = 'fixture-plan'
        self.plan = {'source_identity': A.identity(A.M.S.SOURCE)}
        self.events = []
        self.corrupt = False
        self.status_value = {'status': 'verified', 'recorded_status': 'active', 'observed_config_side': 'target'}

    def status(self):
        return deepcopy(self.status_value)

    def rows(self):
        return {}

    def read(self, arguments):
        self.events.append(arguments)
        if arguments == ['rsync', '--version']:
            return 'rsync version 3.2.7\nCapabilities: hardlinks ACLs xattrs'
        raise AssertionError('Unexpected command')

    def long(self, arguments, **options):
        self.events.append(arguments)
        log = self.directory / ('log-' + str(len(self.events)))
        log.write_bytes(b'')
        if arguments[0] == 'rsync':
            source, destination = map(Path, arguments[-2:])
            if '--dry-run' in arguments:
                if (source / 'payload').read_bytes() != (destination / 'payload').read_bytes():
                    log.write_bytes(b'differences')
                if (destination / 'extra').exists():
                    log.write_bytes(b'extra file')
            else:
                shutil.copytree(source, destination, dirs_exist_ok=True)
                if self.corrupt:
                    (destination / 'payload').write_bytes(b'corrupt archive')
        else:
            assert arguments[:2] == ['sync', '-f']
        return log


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name).resolve()
        self.source, self.target, self.directory = root / 'source', root / 'active', root / 'private'
        for path in (self.source, self.target, self.directory):
            path.mkdir(mode=0o700)
        (self.source / 'payload').write_bytes(b'retired store')
        (self.target / 'payload').write_bytes(b'newer active writes')
        self.patches = [patch.object(A.M.S, 'SOURCE', self.source), patch.object(A.M.S, 'TARGET', self.target),
                        patch.object(A.M.S, 'capacity', side_effect=self.capacity),
                        patch.object(A.M, 'process_ids', return_value=[100]),
                        patch.object(A.M, 'active_store_copy', return_value=False),
                        patch.object(A.Path, 'read_text', return_value='mounts on active data store'),
                        patch('sys.stdout', io.StringIO())]
        for item in self.patches:
            item.start()
        self.host = Host(self.directory)

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def capacity(self, path):
        return {'filesystem_device': -1 if path == self.source else self.target.stat().st_dev,
                'available_bytes': 928 * 1024**3, 'total_bytes': 50 * 1024**3}

    def test_check_is_read_only_and_reports_no_source_removal(self):
        result = A.archive(self.host)
        self.assertEqual(result['status'], 'archive_copy_ready')
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertFalse(result['source_removed'])

    def test_copy_verifies_and_syncs_archive_without_touching_original_or_active_store(self):
        result = A.archive(self.host, copy=True)
        destination = self.directory / 'original-store-archive'
        self.assertEqual(result['status'], 'original_store_archive_verified')
        self.assertEqual((destination / 'payload').read_bytes(), b'retired store')
        self.assertEqual((self.source / 'payload').read_bytes(), b'retired store')
        self.assertEqual((self.target / 'payload').read_bytes(), b'newer active writes')
        receipt = json.loads((self.directory / 'original-store-archive.json').read_bytes())
        self.assertTrue(receipt['copy_verified'])
        self.assertEqual(receipt['source_identity'], A.identity(self.source))
        self.assertTrue(any(row[:2] == ['sync', '-f'] for row in self.host.events))
        self.assertEqual((self.directory / 'original-store-archive.json').stat().st_mode & 0o777, 0o600)

    def test_corruption_does_not_mark_archive_verified_and_retry_can_complete(self):
        self.host.corrupt = True
        with self.assertRaisesRegex(A.M.S.PlanError, 'archive_checksum_or_metadata_differences'):
            A.archive(self.host, copy=True)
        receipt = self.directory / 'original-store-archive.json'
        self.assertFalse(json.loads(receipt.read_bytes())['copy_verified'])
        self.assertEqual((self.source / 'payload').read_bytes(), b'retired store')
        self.host.corrupt = False
        A.archive(self.host, copy=True)
        self.assertTrue(json.loads(receipt.read_bytes())['copy_verified'])

    def test_unexpected_extra_archive_file_is_preserved_and_rejected(self):
        A.archive(self.host, copy=True)
        extra = self.directory / 'original-store-archive' / 'extra'
        extra.write_bytes(b'preserve')
        with self.assertRaises(A.M.S.PlanError):
            A.archive(self.host, copy=True)
        self.assertEqual(extra.read_bytes(), b'preserve')

    def test_unowned_or_replaced_archive_directory_is_rejected(self):
        destination = self.directory / 'original-store-archive'
        destination.mkdir()
        with self.assertRaisesRegex(A.M.S.PlanError, 'unowned_archive_directory_exists'):
            A.archive(self.host, copy=True)
        destination.rmdir()
        A.archive(self.host, copy=True)
        destination.rename(self.directory / 'preserved-archive')
        destination.mkdir()
        with self.assertRaisesRegex(A.M.S.PlanError, 'archive_receipt_does_not_match'):
            A.archive(self.host, copy=True)

    def test_failed_readiness_old_configuration_or_original_mount_prevents_copy(self):
        for updates in ({'status': 'needs_attention'}, {'observed_config_side': 'source'}):
            original = deepcopy(self.host.status_value)
            self.host.status_value.update(updates)
            with self.assertRaises(A.M.S.PlanError):
                A.archive(self.host, copy=True)
            self.host.status_value = original
        with patch.object(A.Path, 'read_text', return_value='upperdir=' + str(self.source) + '/snapshots/1'):
            with self.assertRaisesRegex(A.M.S.PlanError, 'original_store_still_referenced'):
                A.archive(self.host, copy=True)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_low_free_space_stops_before_creating_archive(self):
        def capacity(path):
            value = self.capacity(path)
            value['available_bytes'] = 120 * 1024**3
            return value
        with patch.object(A.M.S, 'capacity', side_effect=capacity):
            with self.assertRaisesRegex(A.M.S.PlanError, 'archive_space_reserve_unavailable'):
                A.archive(self.host, copy=True)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_commands_copy_only_original_store_and_never_delete_files(self):
        destination = self.directory / 'original-store-archive'
        for verify in (False, True):
            arguments = A.commands(destination, verify)
            self.assertEqual(arguments[-2:], [str(self.source) + '/', str(destination) + '/'])
            self.assertNotIn(str(self.target) + '/', arguments)
            self.assertNotIn('--remove-source-files', arguments)
            if '--delete' in arguments:
                self.assertIn('--dry-run', arguments)
        self.assertIn('--fsync', A.commands(destination))


if __name__ == '__main__':
    unittest.main()
