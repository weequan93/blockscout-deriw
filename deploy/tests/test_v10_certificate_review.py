from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import v10_certificate_review as C


class CertificateReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.base = Path(cls.temp.name).resolve()
        def run(*arguments):
            result = subprocess.run(['openssl', *arguments], capture_output=True, timeout=15)
            if result.returncode:
                raise AssertionError('Synthetic certificate generation failed')
        cls.root = cls.base / 'root.pem'
        cls.root_key = cls.base / 'root.key'
        run('req', '-new', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '30',
            '-subj', '/CN=Fixture CA', '-addext', 'basicConstraints=critical,CA:TRUE',
            '-addext', 'keyUsage=critical,keyCertSign,cRLSign', '-out', str(cls.root), '-keyout', str(cls.root_key))
        cls.key = cls.base / 'leaf.key'
        csr = cls.base / 'leaf.csr'
        run('req', '-new', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=explorer.fixture.test',
            '-out', str(csr), '-keyout', str(cls.key))
        extension = cls.base / 'extension.cnf'
        extension.write_text('[leaf]\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:explorer.fixture.test,DNS:stats.fixture.test\n')
        cls.leaf = cls.base / 'leaf.pem'
        run('x509', '-req', '-in', str(csr), '-CA', str(cls.root), '-CAkey', str(cls.root_key),
            '-set_serial', '2', '-days', '7', '-extfile', str(extension), '-extensions', 'leaf', '-out', str(cls.leaf))
        cls.bundle = cls.base / 'fullchain.pem'
        cls.bundle.write_bytes(cls.leaf.read_bytes() + cls.root.read_bytes())
        cls.other_key = cls.base / 'other.key'
        run('genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048', '-out', str(cls.other_key))
        (cls.base / 'newcerts').mkdir()
        (cls.base / 'index.txt').write_text('')
        (cls.base / 'serial').write_text('10\n')
        ca = cls.base / 'ca.cnf'
        ca.write_text('[ca]\ndefault_ca=fixture\n[fixture]\n' +
            'database=' + str(cls.base / 'index.txt') + '\nserial=' + str(cls.base / 'serial') +
            '\nnew_certs_dir=' + str(cls.base / 'newcerts') + '\ncertificate=' + str(cls.root) +
            '\nprivate_key=' + str(cls.root_key) + '\ndefault_md=sha256\npolicy=policy\nx509_extensions=leaf\n' +
            '[policy]\ncommonName=supplied\n' + extension.read_text())
        cls.expired = cls.base / 'expired.pem'
        run('ca', '-batch', '-config', str(ca), '-in', str(csr), '-out', str(cls.expired),
            '-startdate', '20200101000000Z', '-enddate', '20200102000000Z', '-notext')

    def setUp(self):
        self.key.chmod(0o600)
        self.other_key.chmod(0o600)

    def test_valid_bundle_checks_key_chain_and_all_hostnames_without_changing_files(self):
        original = {p: p.read_bytes() for p in (self.key, self.bundle, self.root)}
        result = C.review(self.bundle, self.key, ['explorer.fixture.test', 'stats.fixture.test'], ca_bundle=self.root)
        self.assertEqual(result['status'], 'certificate_bundle_reviewed')
        self.assertEqual(result['checked_name_count'], 2)
        self.assertFalse(result['services_changed'])
        self.assertFalse(result['certificate_files_changed'])
        self.assertEqual(original, {p: p.read_bytes() for p in original})

    def test_expired_certificate_is_rejected(self):
        with self.assertRaisesRegex(C.ReviewError, 'expired'):
            C.review(self.expired, self.key, ['explorer.fixture.test'], chain=self.root, ca_bundle=self.root)

    def test_mismatched_private_key_is_rejected(self):
        with self.assertRaisesRegex(C.ReviewError, 'does not match'):
            C.review(self.bundle, self.other_key, ['explorer.fixture.test'], ca_bundle=self.root)

    def test_wrong_hostname_and_untrusted_chain_are_rejected(self):
        with self.assertRaises(C.ReviewError):
            C.review(self.bundle, self.key, ['other.fixture.test'], ca_bundle=self.root)
        with self.assertRaises(C.ReviewError):
            C.review(self.bundle, self.key, ['explorer.fixture.test'])

    def test_private_key_permissions_are_checked(self):
        self.key.chmod(0o644)
        self.addCleanup(self.key.chmod, 0o600)
        with self.assertRaisesRegex(C.ReviewError, 'only by its owner'):
            C.review(self.bundle, self.key, ['explorer.fixture.test'], ca_bundle=self.root)

    def test_private_material_cannot_be_supplied_as_a_certificate(self):
        private_marker = self.base / 'private-marker.pem'
        private_marker.write_bytes(b'-----BEGIN PRIVATE KEY-----\nDO-NOT-PRINT-THIS\n-----END PRIVATE KEY-----\n')
        with self.assertRaises(C.ReviewError) as error:
            C.review(private_marker, self.key, ['explorer.fixture.test'], ca_bundle=self.root)
        self.assertNotIn('DO-NOT-PRINT-THIS', str(error.exception))

    def test_saved_configuration_collects_every_tls_name(self):
        loaded = '''http {
        server { listen 443 ssl; server_name explorer.fixture.test;
          ssl_certificate /shared/fullchain.pem; }
        server { listen 443 ssl; server_name stats.fixture.test;
          ssl_certificate /shared/fullchain.pem; }
        }'''
        self.assertEqual(C.expected_names(loaded, 'explorer.fixture.test'),
                         ['explorer.fixture.test', 'stats.fixture.test'])
        with self.assertRaises(C.ReviewError):
            C.expected_names(loaded.replace('/shared/fullchain.pem;', '/shared/other.pem;', 1), 'explorer.fixture.test')

