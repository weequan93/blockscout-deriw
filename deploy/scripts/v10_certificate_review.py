#!/usr/bin/env python3
"""Read-only review of a replacement origin certificate against saved Nginx config."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import ssl
import stat
import subprocess
import tempfile
import time

import proxy_stats as W
import v10_infrastructure_deploy as I

CERTIFICATE = re.compile(br'-----BEGIN CERTIFICATE-----\s+.*?-----END CERTIFICATE-----', re.S)


class ReviewError(Exception):
    pass


def require(condition, reason):
    if not condition:
        raise ReviewError(reason)


def blocks(path):
    require(path.is_file(), 'Certificate or chain file is unavailable')
    raw = path.read_bytes()
    require(len(raw) <= 1024 * 1024 and b'PRIVATE KEY' not in raw, 'Provide only a PEM certificate chain')
    result = CERTIFICATE.findall(raw)
    require(0 < len(result) <= 16 and not CERTIFICATE.sub(b'', raw).strip(), 'Unrecognized PEM certificate bundle')
    return [part + b'\n' for part in result]


def command(arguments, data=None):
    try:
        result = subprocess.run(['openssl', *arguments], input=data, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        raise ReviewError('OpenSSL certificate review is unavailable; raw output suppressed') from None
    require(result.returncode == 0 and len(result.stdout) < 1024 * 1024,
            'OpenSSL rejected the key, chain, hostname or validity; raw output suppressed')
    return result.stdout


def expected_names(loaded, public_host):
    names, certificates = {public_host.lower()}, set()
    for server in W.servers(W.parse(I.D, loaded)):
        if not W.listens(server, 443):
            continue
        certificates.update(W.values(d)[1] for d in W.directives(server, 'ssl_certificate'))
        for directive in W.directives(server, 'server_name'):
            names.update(value.lower() for value in W.values(directive)[1:] if value != '_')
    require(len(certificates) == 1, 'Review expects the one shared origin certificate in this Nginx configuration')
    require(all(re.fullmatch(r'(?:\*\.)?[a-z0-9][a-z0-9.-]*', name) and '..' not in name for name in names),
            'Dynamic Nginx server names require a separate certificate review')
    return sorted(names)


def review(certificate, key, names, chain=None, ca_bundle=None):
    require(key.is_file() and not key.is_symlink(), 'Private key must be a regular staged file')
    metadata = key.stat()
    require(metadata.st_uid == os.geteuid() and stat.S_IMODE(metadata.st_mode) & 0o077 == 0,
            'Private key must be owned by the reviewing account and readable only by its owner')
    pem = blocks(certificate)
    intermediates = pem[1:] + (blocks(chain) if chain else [])
    public_from_cert = command(['x509', '-pubkey', '-noout'], pem[0])
    cert_key = command(['pkey', '-pubin', '-outform', 'DER'], public_from_cert)
    private_public_key = command(['pkey', '-in', str(key), '-passin', 'pass:', '-pubout', '-outform', 'DER'])
    require(cert_key == private_public_key, 'The staged private key does not match the certificate')
    with tempfile.TemporaryDirectory(prefix='deriw-certificate-review-') as temporary:
        directory = Path(temporary)
        leaf = directory / 'leaf.pem'
        leaf.write_bytes(pem[0])
        try:
            decoded = ssl._ssl._test_decode_cert(str(leaf))
            starts = ssl.cert_time_to_seconds(decoded['notBefore'])
            expires = ssl.cert_time_to_seconds(decoded['notAfter'])
        except (OSError, ValueError, KeyError, ssl.SSLError):
            raise ReviewError('Certificate validity could not be read') from None
        require(starts <= time.time() < expires, 'The replacement certificate is expired or not yet valid')
        dns = {value.lower() for kind, value in decoded.get('subjectAltName', ()) if kind == 'DNS'}
        args = ['verify', '-purpose', 'sslserver']
        if intermediates:
            chain_file = directory / 'chain.pem'
            chain_file.write_bytes(b''.join(intermediates))
            args += ['-untrusted', str(chain_file)]
        if ca_bundle:
            blocks(ca_bundle)
            args += ['-CAfile', str(ca_bundle)]
        for name in names:
            require(not name.startswith('*.') or name in dns,
                    'Replacement certificate does not preserve a configured wildcard name')
            hostname = 'certificate-review.' + name[2:] if name.startswith('*.') else name
            command([*args, '-verify_hostname', hostname, str(leaf)])
    return {'status': 'certificate_bundle_reviewed', 'checked_name_count': len(names),
            'key_matches_certificate': True, 'chain_and_hostnames_verified': True,
            'expires_at_utc': datetime.fromtimestamp(expires, timezone.utc).isoformat(),
            'trust_source': 'provided_CA_bundle' if ca_bundle else 'host_CA_store',
            'services_changed': False, 'certificate_files_changed': False, 'database_calls': False,
            'note': 'Review covers the captured Nginx names. Deployment still requires syntax, reload and strict origin HTTPS checks.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--nginx-state', required=True, type=Path)
    parser.add_argument('--certificate', required=True, type=Path)
    parser.add_argument('--private-key', required=True, type=Path)
    parser.add_argument('--chain', type=Path)
    parser.add_argument('--ca-bundle', type=Path)
    args = parser.parse_args()
    try:
        require(os.geteuid() == 0, 'Run on the dev server as root; staged key ownership is checked')
        _, inputs = I.load(args.nginx_state)
        require(inputs['component'] == 'nginx', 'Use the saved Nginx preparation state')
        names = expected_names(inputs['nginx']['loaded'], inputs['public_host'])
        print(json.dumps(review(args.certificate, args.private_key, names, args.chain, args.ca_bundle), indent=2))
        return 0
    except Exception as error:
        print(json.dumps({'status': 'stopped', 'read_only': True, 'raw_values_suppressed': True,
            'reason': str(error) if isinstance(error, (ReviewError, I.D.ConfigError)) else 'Certificate review failed; raw output suppressed'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
