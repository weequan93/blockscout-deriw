import contextlib
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import v10_review as T

ORIGINAL = 'a' * 40
CURRENT = 'b' * 40
BASELINE = 'sha256:' + 'c' * 64
IMAGE = 'sha256:' + 'd' * 64


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.private = self.directory / 'private.json'
        self.receipt_path = self.directory / 'candidate.json'
        self.plan = T.D.read_json(ROOT / 'deploy/upgrades/v10.2.6.json')
        self.before = {'backend': {'Image': BASELINE, 'Config': {'Env': ['RELEASE_VERSION=9.0.1']}}}
        self.manifest = {'baseline_image_ids': {'backend': BASELINE}}
        inventory_hash = hashlib.sha256(json.dumps(self.plan['target_migration_sha256'], sort_keys=True).encode()).hexdigest()
        self.saved = {'format': T.V.FORMAT, 'environment': 'dev', 'release_version': '10.2.6', 'platform': 'linux/amd64',
                      'source_commit': ORIGINAL, 'activation_performed': False, 'automatic_background_migrations_held': True,
                      'private_input_file': str(self.private), 'baseline_image_ids': {'backend': BASELINE},
                      'candidate_tag': 'deriw-blockscout:upgrade-v10-' + ORIGINAL[:12] + '-arbitrum-amd64',
                      'image_id': IMAGE, 'migration_inventory_sha256': inventory_hash,
                      'storage_state': '/fixture/storage/state.json',
                      'smoke_checks': [{'mode': mode, 'passed': True} for mode in ('unset', 'false', 'true')],
                      'build_arguments': {'CHAIN_TYPE': 'arbitrum', 'RELEASE_VERSION': '10.2.6',
                                          'BLOCKSCOUT_VERSION': ORIGINAL, T.V.HOLD: 'true'}}
        identity = {key: self.saved[key] for key in ('format', 'source_commit', 'release_version', 'platform',
                                                    'build_arguments', 'migration_inventory_sha256')}
        self.saved['build_fingerprint'] = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        self.baseline = {'Id': BASELINE, 'Config': {'User': 'blockscout:blockscout'}}
        self.image = {'Id': IMAGE, 'Os': 'linux', 'Architecture': 'amd64', 'Config': {
            'User': 'blockscout:blockscout', 'Env': [T.V.HOLD + '=true', 'RELEASE_VERSION=10.2.6'],
            'Labels': {T.B.LABEL: self.saved['build_fingerprint'], 'org.opencontainers.image.revision': ORIGINAL}}}
        self.api = types.SimpleNamespace(
            require=T.D.require, ConfigError=T.D.ConfigError, read_json=T.D.read_json, env_map=T.D.env_map,
            run=Mock(side_effect=['', 'deploy/scripts/v10_review.py\ndocs/upgrades/v10.2.6-candidate.md\n']),
            configuration=Mock(return_value=(self.manifest, {}, {})), load_private=Mock(return_value={'PRIVATE': 'fixture'}),
            plan=Mock(return_value={'changed_runtime_fields': [], 'live_declared_environment_drift': [], 'live_images_differing_from_capture': []}),
            inspect_project=Mock(return_value=self.before), managed_model=Mock(return_value={'services': {'backend': {}}}),
            ensure_stable=T.D.ensure_stable)
        self.tables = [{'table': name, 'exists': True, 'has_rows': False} for name in T.TABLES]
        self.pending = []
        for directory, repo in [('repo', 'Explorer.Repo'), ('account', 'Explorer.Repo.Account'), ('arbitrum', 'Explorer.Repo.Arbitrum')]:
            versions = [int(Path(name).name.split('_', 1)[0]) for name in self.plan['migration_changes']['added']
                        if name.startswith(directory + '/migrations/')]
            if directory == 'repo':
                versions += [20260721000000, 20260812000000]
            self.pending.append({'repository': repo, 'migration_directory': directory, 'status': 'ok',
                                 'candidate_pending_versions': versions})
        self.write()

    def write(self):
        self.receipt_path.write_text(json.dumps(self.saved))
        self.receipt_path.chmod(0o600)

    @contextlib.contextmanager
    def image_context(self, extra=False):
        with patch.object(T.B, 'clean_source', return_value=(CURRENT, '10.2.6')), \
                patch.object(T.B, 'image_info', side_effect=[self.baseline, self.image] + ([self.image] if extra else [])):
            yield

    def candidate(self):
        return T.candidate(self.api, ROOT, self.receipt_path, self.private, self.before, self.manifest)

    def test_helper_only_changes_can_review_original_candidate_without_rebuilding(self):
        with self.image_context():
            saved, image, plan, current = self.candidate()
        self.assertEqual(saved['source_commit'], ORIGINAL)
        self.assertEqual(current, CURRENT)
        self.assertEqual(image['Id'], IMAGE)
        self.assertEqual(plan['target_version'], '10.2.6')

    def test_application_changes_after_build_are_rejected(self):
        self.api.run.side_effect = ['', 'apps/explorer/lib/explorer/application.ex\n']
        with self.image_context():
            with self.assertRaises(T.D.ConfigError):
                self.candidate()

    def test_receipt_tampering_missing_checks_or_changed_live_images_are_rejected(self):
        for mutate in (
            lambda saved: saved.update(build_fingerprint='wrong'),
            lambda saved: saved.update(smoke_checks=[]),
            lambda saved: saved.update(private_input_file='/different/private.json'),
            lambda saved: saved.update(baseline_image_ids={}),
            lambda saved: saved.update(migration_inventory_sha256='wrong'),
            lambda saved: saved.update(format=T.B.FORMAT),
        ):
            original = deepcopy(self.saved)
            mutate(self.saved)
            self.write()
            self.api.run.side_effect = ['', 'deploy/scripts/v10_review.py\n']
            with self.image_context():
                with self.assertRaises(T.D.ConfigError):
                    self.candidate()
            self.saved = original

    def test_wrong_image_identity_or_unheld_image_is_rejected(self):
        for mutate in (lambda image: image.update(Id=BASELINE), lambda image: image['Config'].update(Env=['RELEASE_VERSION=10.2.6'])):
            original = deepcopy(self.image)
            mutate(self.image)
            self.api.run.side_effect = ['', 'deploy/scripts/v10_review.py\n']
            with self.image_context():
                with self.assertRaises(T.D.ConfigError):
                    self.candidate()
            self.image = original

    def test_classification_keeps_29_upstream_versions_separate_from_two_custom_versions(self):
        rows = T.classify(self.pending, self.plan)
        self.assertEqual([len(row['upstream_pending_versions']) for row in rows], [26, 2, 1])
        self.assertEqual(rows[0]['deferred_custom_versions'], [20260721000000, 20260812000000])
        self.assertTrue(all(not row['unexpected_pending_versions'] for row in rows))

    def test_unexpected_pending_version_is_reported_and_missing_repo_is_rejected(self):
        self.pending[0]['candidate_pending_versions'].append(20180117221923)
        rows = T.classify(self.pending, self.plan)
        self.assertEqual(rows[0]['unexpected_pending_versions'], [20180117221923])
        with self.assertRaises(T.D.ConfigError):
            T.classify(self.pending[:2], self.plan)

    def test_presence_reads_existing_repo_with_bounded_read_only_exists_queries(self):
        data = {'status': 'ok', 'tables': [{**row, 'private': 'never export'} for row in self.tables]}
        self.api.run.side_effect = None
        self.api.run.return_value = T.C.MARKER + json.dumps(data)
        rows = T.presence(self.api)
        command = self.api.run.call_args.args[0]
        self.assertEqual(command[:5], ['docker', 'exec', 'backend', '/app/bin/blockscout', 'rpc'])
        self.assertNotIn('never export', json.dumps(rows))
        expression = command[5]
        for token in ('SET TRANSACTION READ ONLY', "statement_timeout = '1500ms'", "lock_timeout = '500ms'", 'timeout: 5000', 'SELECT EXISTS', 'LIMIT 1'):
            self.assertIn(token, expression)
        for token in ('COUNT(', 'Ecto.Migrator', 'CREATE ', 'UPDATE ', 'DELETE ', 'INSERT '):
            self.assertNotIn(token, expression)

    def test_presence_failure_or_non_boolean_flags_are_rejected(self):
        self.api.run.side_effect = None
        for data in ({'status': 'unavailable'}, {'status': 'ok', 'tables': [{**row, 'has_rows': 'private'} for row in self.tables]}):
            self.api.run.return_value = T.C.MARKER + json.dumps(data)
            with self.assertRaises(T.D.ConfigError):
                T.presence(self.api)

    def complete_review(self):
        with self.image_context(extra=True), \
                patch.object(T.C, 'image_inventory', return_value={'migrations': self.plan['target_migration_sha256'], 'identity': (10001, 10001, ())}), \
                patch.object(T.C, 'pending_migrations', return_value=self.pending), \
                patch.object(T, 'presence', return_value=self.tables), \
                patch.object(T.U, 'history_report', return_value={'read_only': True}), \
                patch.object(T.C, 'mount_access', return_value={'numeric_user_and_groups_match': True}), \
                patch.object(T.C, 'live_health', return_value={'backend_readiness_ok': True, 'nft_connection_and_queue_ok': True}), \
                patch.object(T.V, 'storage_ready', return_value={'migration_space_verified': False}):
            return T.review(self.api, ROOT, self.receipt_path, self.private)

    def test_review_reports_actual_artifact_and_keeps_recovery_and_space_unapproved(self):
        result = self.complete_review()
        self.assertEqual(result['upstream_pending_count'], 29)
        self.assertEqual(result['deferred_custom_count'], 2)
        self.assertEqual(result['candidate_image'], IMAGE)
        self.assertFalse(result['deployment_permitted'])
        self.assertEqual(result['blockers'], ['snapshot_recovery_unverified', 'migration_working_space_unverified'])

    def test_nonempty_dropped_table_and_unexpected_versions_are_flagged(self):
        self.tables[0]['has_rows'] = True
        self.pending[0]['candidate_pending_versions'].append(20180117221923)
        result = self.complete_review()
        self.assertIn('account_public_tags_drop_would_remove_rows', result['blockers'])
        self.assertIn('pending_versions_outside_reviewed_sequence', result['blockers'])

    def test_compiled_migration_tampering_stops_before_live_version_queries(self):
        with self.image_context(), \
                patch.object(T.C, 'image_inventory', return_value={'migrations': {}, 'identity': (10001, 10001, ())}), \
                patch.object(T.C, 'pending_migrations') as pending:
            with self.assertRaises(T.D.ConfigError):
                T.review(self.api, ROOT, self.receipt_path, self.private)
        pending.assert_not_called()


if __name__ == '__main__':
    unittest.main()
