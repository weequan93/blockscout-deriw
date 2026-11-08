from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import dev_config as D
import release as R
import v10_nginx_duplicates as Q
import v10_infrastructure_stage as S
import v10_infrastructure_deploy as I


MASTER = '/etc/nginx/nginx.conf'
A = '/etc/nginx/conf.d/a.conf'
B = '/etc/nginx/conf.d/b.conf'


def dump(files):
    return '\n'.join('# configuration file ' + name + ':\n' + text for name, text in files.items())


def server(names='localhost', port='80', body='location / { return 200 "first"; }'):
    return 'server { listen ' + port + '; server_name ' + names + '; ' + body + ' }\n'


def files(a, b):
    # Dump order is deliberately different from include execution order.
    return {MASTER: 'events {} http { include /etc/nginx/conf.d/*.conf; }', B: b, A: a}


class DuplicateServerTests(unittest.TestCase):
    def test_removes_only_later_ignored_servers_even_when_their_bodies_differ(self):
        first = server(body='location / { return 200 "PRIVATE_CANARY"; }')
        original = files(first, server(body='location / { return 403; }'))
        changed, review = Q.plan(dump(original))
        self.assertNotIn(A, changed)
        self.assertNotIn('return 403', changed[B])
        self.assertEqual(review['removed_server_blocks'], 1)
        self.assertNotIn('PRIVATE_CANARY', json.dumps(review))
        repaired = dict(original, **changed)
        self.assertEqual(Q.plan(dump(repaired))[0], {})

    def test_explicit_include_order_takes_priority_over_file_names_and_dump_order(self):
        original = files(server(), server(body='return 204;'))
        original[MASTER] = 'http { include conf.d/b.conf; include conf.d/a.conf; }'
        changed, _ = Q.plan(dump(original))
        self.assertEqual(set(changed), {A})

    def test_operator_thirteen_server_layout_keeps_its_five_effective_servers(self):
        default = '/etc/nginx/conf.d/default.conf'
        explorer = '/etc/nginx/conf.d/explorer.conf'
        microservices = '/etc/nginx/conf.d/microservices.conf'
        group = (server('explorer.dev.deriw.com', '443 ssl')
                 + server('explorer_dev.deriw.com', '443 ssl')
                 + server() + server(port='8080') + server(port='8081'))
        original = {MASTER: 'events {} http { include /etc/nginx/conf.d/*.conf; }',
            default: group + group, explorer: server(),
            microservices: server(port='8080', body='add_header Origin different;')
                           + server(port='8081', body='add_header Origin different;')}
        changed, review = Q.plan(dump(original), original)
        self.assertEqual(review['removed_server_blocks'], 8)
        self.assertEqual(review['removed_server_names'], 8)
        _, remaining = Q.http_servers(dump(dict(original, **changed)))
        self.assertEqual(len(remaining), 5)
        self.assertTrue(all(filename == default for filename, _ in remaining))

    def test_retains_unique_alias_and_its_routes(self):
        original = files(server('same.test'), server('SAME.test unique.test', body='return 201;'))
        changed, review = Q.plan(dump(original))
        self.assertIn('server_name unique.test;', changed[B])
        self.assertIn('return 201;', changed[B])
        self.assertEqual(review['removed_server_blocks'], 0)
        self.assertEqual(review['removed_server_names'], 1)

    def test_distinct_ports_and_ipv4_addresses_and_ipv6_are_preserved(self):
        for port in ('8080', '127.0.0.1:80', '[::]:80'):
            with self.subTest(port=port):
                self.assertEqual(Q.plan(dump(files(server(), server(port=port))))[0], {})

    def test_equivalent_wildcard_socket_forms_are_deduplicated(self):
        for port in ('0.0.0.0:80', '*:80'):
            changed, _ = Q.plan(dump(files(server(), server(port=port))))
            self.assertEqual(set(changed), {B})

    def test_first_server_and_other_configuration_bytes_are_preserved(self):
        leading = '# keep this comment\nmap $host $fixture { default private; }\n'
        first = server()
        second = server(body='return 404;')
        original = files(leading + first + '\n' + second + '# keep tail\n', '')
        changed, _ = Q.plan(dump(original), original)
        self.assertEqual(changed[A], leading + first + '\n\n' + '# keep tail\n')

    def test_duplicate_tokens_in_one_server_are_removed_without_changing_its_body(self):
        original = files(server('local.test LOCAL.test another.test'), '')
        changed, review = Q.plan(dump(original))
        self.assertIn('server_name local.test another.test;', changed[A])
        self.assertIn('return 200', changed[A])
        self.assertEqual(review['removed_server_blocks'], 0)

    def test_primary_alias_is_preserved_when_referenced_by_variables_or_redirects(self):
        for setting in ('proxy_set_header Host $server_name;', 'server_name_in_redirect on;'):
            original = files(server('same.test'), server('same.test unique.test', body=setting))
            with self.assertRaisesRegex(D.ConfigError, 'primary alias'):
                Q.plan(dump(original))

    def test_partial_address_conflict_is_refused(self):
        later = server().replace('listen 80;', 'listen 80; listen 8080;')
        with self.assertRaisesRegex(D.ConfigError, 'some listener addresses'):
            Q.plan(dump(files(server(), later)))

    def test_later_explicit_default_and_protocol_changes_are_refused(self):
        for port in ('80 default_server', '80 ssl', '80 reuseport', '[::]:80 ipv6only=off'):
            with self.subTest(port=port):
                with self.assertRaises(D.ConfigError):
                    Q.plan(dump(files(server(), server(port=port))))

    def test_keeps_an_explicit_default_with_a_unique_name(self):
        original = files(server('same.test'), server('same.test unique.test', port='80 default_server'))
        changed, _ = Q.plan(dump(original))
        self.assertIn('listen 80 default_server;', changed[B])
        self.assertIn('server_name unique.test;', changed[B])

    def test_dynamic_names_and_server_includes_and_repeated_server_includes_are_refused(self):
        for later in (server('*.test'), server('~^test'), server('$hostname'),
                      server(body='include /etc/nginx/locations.conf;')):
            with self.subTest(later=later):
                with self.assertRaises(D.ConfigError):
                    Q.plan(dump(files(server(), later)))
        original = files(server(), '')
        original[MASTER] = 'http { include conf.d/a.conf; include conf.d/a.conf; }'
        with self.assertRaisesRegex(D.ConfigError, 'included more than once'):
            Q.plan(dump(original))

    def test_missing_include_and_include_cycle_are_refused(self):
        for master in ('http { include missing.conf; }', 'http { include nginx.conf; }'):
            with self.assertRaises(D.ConfigError):
                Q.plan(dump({MASTER: master}))

    def test_same_server_names_in_stream_do_not_count_as_http_duplicates(self):
        original = files(server(), '')
        original[MASTER] = 'stream { server { listen 80; proxy_pass private:99; } } http { include conf.d/*.conf; }'
        self.assertEqual(Q.plan(dump(original))[0], {})

    def test_forward_artifacts_and_rollback_bytes_are_independent_and_authenticated(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            original = files(server(), server(body='return 403;'))
            artifacts = []
            for number, (name, text) in enumerate(original.items()):
                source = directory / ('original-' + str(number))
                R.write_new(source, text.encode())
                artifacts.append({'source': str(source), 'target': name, 'sha256': R.digest(text.encode()),
                                  'runtime_overlay': True, 'type': 'bind', 'read_only': True})
            nginx = {'files': artifacts, 'loaded': dump(original)}
            repaired = Q.freeze(nginx, directory)
            inputs = {'nginx': repaired}
            original_bytes = deepcopy(artifacts)
            S.validate_artifacts(inputs)
            S.validate_artifacts(inputs)
            self.assertEqual(repaired['files'], original_bytes)
            forward = {row['target']: row for row in S.nginx_files(inputs, 'forward')}
            rollback = {row['target']: row for row in S.nginx_files(inputs, 'rollback')}
            self.assertNotEqual(forward[B]['source'], rollback[B]['source'])
            self.assertNotIn('return 403', Path(forward[B]['source']).read_text())
            self.assertIn('return 403', Path(rollback[B]['source']).read_text())
            Path(forward[B]['source']).write_text('changed')
            with self.assertRaises(D.ConfigError):
                S.validate_artifacts(inputs)

    def test_warning_check_reads_stderr_and_refuses_conflicts(self):
        commands = []
        api = types.SimpleNamespace(run=lambda command: commands.append(command) or 'nginx: [warn] conflicting server name')
        with self.assertRaises(D.ConfigError):
            S.nginx_test(api, 'candidate', True)
        self.assertEqual(commands[-1][-1], 'nginx -t 2>&1')

    def test_configuration_repair_can_use_only_the_reviewed_already_installed_image(self):
        target = I.K.catalog()['nginx']
        original = {'version': '1.31.8', 'os': {'ID': 'debian', 'VERSION_ID': '13'}}
        candidate = dict(original, image_id='reviewed')
        before = {'proxy': {'Image': 'reviewed'}}
        I.validate_image_selection('nginx', before, original, candidate, target, repair=True)
        for options in ({'repair': False}, {'repair': True, 'before': {'proxy': {'Image': 'unknown'}}}):
            with self.assertRaises(D.ConfigError):
                I.validate_image_selection('nginx', options.get('before', before), original, candidate,
                                           target, repair=options['repair'])
        with self.assertRaises(D.ConfigError):
            I.validate_image_selection('nginx', before, dict(original, version='1.31.9'), candidate, target, repair=True)


if __name__ == '__main__':
    unittest.main()
