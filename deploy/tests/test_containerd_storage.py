import importlib.util
import io
import json
from pathlib import Path
import stat
import tempfile
import tomllib
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('containerd_storage', ROOT / 'deploy/scripts/containerd_storage.py')
S = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(S)
BASE = '''version = 3
root = '/var/lib/containerd'
state = '/run/containerd'
imports = []

[plugins.'io.containerd.snapshotter.v1.overlayfs']
  root_path = ''
  sync_remove = false

[plugins.'private.registry']
  token = 'private-fixture-value'
'''


class ConfigTests(unittest.TestCase):
    def test_only_global_root_changes_in_parsed_configuration(self):
        result = S.replace_root(BASE, S.TARGET)
        actual, expected = tomllib.loads(result), tomllib.loads(BASE)
        expected['root'] = str(S.TARGET)
        self.assertEqual(actual, expected)
        self.assertEqual(actual['version'], 3)
        self.assertEqual(actual['state'], '/run/containerd')
        S.validate_candidate(BASE, result)

    def test_missing_global_root_is_added_without_changing_plugin_root(self):
        original = "version = 3\n[plugins.test]\nroot = '/plugin-root'\n"
        actual = tomllib.loads(S.replace_root(original, S.TARGET))
        self.assertEqual(actual['root'], str(S.TARGET))
        self.assertEqual(actual['plugins']['test']['root'], '/plugin-root')

    def test_source_format_version_is_preserved(self):
        original = BASE.replace('version = 3', 'version = 2')
        self.assertEqual(tomllib.loads(S.replace_root(original, S.TARGET))['version'], 2)

    def test_duplicate_global_roots_are_rejected(self):
        with self.assertRaisesRegex(S.PlanError, 'duplicate_top_level'):
            S.replace_root("root = '/one'\nroot = '/two'\n", S.TARGET)

    def test_effective_plugin_state_or_version_changes_are_rejected(self):
        after = S.replace_root(BASE, S.TARGET)
        for changed in (after.replace('false', 'true'), after.replace('/run/containerd', '/data/run'),
                        after.replace('version = 3', 'version = 2')):
            with self.subTest(candidate=changed), self.assertRaises(S.PlanError):
                S.validate_candidate(BASE, changed)

    def test_plugin_pointing_at_original_store_is_rejected(self):
        before = BASE.replace("root_path = ''", "root_path = '/var/lib/containerd/custom'")
        with self.assertRaisesRegex(S.PlanError, 'plugin_retains_old_storage_path'):
            S.validate_candidate(before, S.replace_root(before, S.TARGET))

    def test_default_unmatched_import_glob_is_allowed_but_real_imports_are_not(self):
        values = {'imports': ['/etc/containerd/conf.d/*.toml']}
        with patch.object(S.glob, 'glob', return_value=[]):
            S.validate_imports(values)
        with patch.object(S.glob, 'glob', return_value=['/etc/containerd/conf.d/private.toml']):
            with self.assertRaisesRegex(S.PlanError, 'imported_configuration'):
                S.validate_imports(values)
        with self.assertRaises(S.PlanError):
            S.validate_imports({'imports': ['relative.toml']})


class ProcessTests(unittest.TestCase):
    def test_selected_flags_handle_both_forms_and_withhold_other_values(self):
        values = S.storage_arguments(['containerd', '-c', '/etc/containerd/config.toml',
                                      '--root=/var/lib/containerd', '--state', '/run/containerd',
                                      '--unrelated=private-fixture-value'])
        self.assertEqual(values, {'config': '/etc/containerd/config.toml', 'root': '/var/lib/containerd',
                                  'state': '/run/containerd'})
        self.assertNotIn('private-fixture-value', json.dumps(values))

    def test_duplicate_and_incomplete_flags_are_rejected(self):
        for values in (['containerd', '--config'], ['containerd', '-c', '/one', '--config=/two']):
            with self.assertRaises(S.PlanError):
                S.storage_arguments(values)

    def test_running_backend_overlay_root_is_matched_by_mount_boundary(self):
        path = '/var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots/9/fs'
        mount = '40 20 0:1 / / rw - overlay overlay rw,lowerdir=/lower,upperdir=' + path + ',workdir=/work\n'
        self.assertEqual(S.root_upperdir(mount), path)
        for changed in (mount.replace('/var/lib/containerd/', '/var/lib/containerd-other/'),
                        mount.replace(' / / ', ' / /other ')):
            with self.assertRaises(S.PlanError):
                S.root_upperdir(changed)


class PrivatePlanTests(unittest.TestCase):
    def test_prepare_writes_private_snapshots_without_exporting_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder).resolve() / 'private'
            with patch.object(S, 'PRIVATE', directory):
                report = {'status': 'ready', 'services': 14, 'read_only_services': True}
                private = {'before': {'backend': {'password': 'private-fixture-value'}}}
                result = S.prepare(report, private, BASE.encode(), S.replace_root(BASE, S.TARGET).encode())
            state = Path(result['private_plan'])
            self.assertEqual(stat.S_IMODE(state.parent.stat().st_mode), 0o700)
            for path in state.parent.iterdir():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertIn('private-fixture-value', state.read_text())
            self.assertNotIn('private-fixture-value', json.dumps(result))
            self.assertEqual((state.parent / 'containerd.before.toml').read_bytes(), BASE.encode())

    def test_private_directory_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder).resolve()
            (directory / 'redirect').symlink_to(directory, target_is_directory=True)
            with patch.object(S, 'PRIVATE', directory / 'redirect'), self.assertRaises(S.PlanError):
                S.prepare({}, {}, b'before', b'after')

    def test_check_mode_does_not_write_or_return_private_inputs(self):
        output = io.StringIO()
        with patch.object(S, 'collect', return_value=({'status': 'ready'}, {'secret': 'private-fixture-value'}, b'', b'')), \
             patch.object(S, 'prepare') as prepare, patch('sys.argv', ['containerd_storage.py', '--check']), \
             patch('sys.stdout', output):
            self.assertEqual(S.main(), 0)
        prepare.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), {'status': 'ready'})

    def test_external_failure_never_exports_raw_exception(self):
        output = io.StringIO()
        with patch.object(S, 'collect', side_effect=RuntimeError('private-fixture-value')), \
             patch('sys.argv', ['containerd_storage.py', '--check']), patch('sys.stdout', output):
            self.assertEqual(S.main(), 1)
        self.assertNotIn('private-fixture-value', output.getvalue())
        self.assertFalse(json.loads(output.getvalue())['services_changed'])


if __name__ == '__main__':
    unittest.main()
