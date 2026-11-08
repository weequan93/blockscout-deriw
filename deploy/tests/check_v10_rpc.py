#!/usr/bin/env python3
"""Exercise the exact RPC curl protocol against a disposable local HTTP server."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
from threading import Thread
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import dev_config as D

CHAIN = 18417507517
PRIVATE = 'fixture-private-rpc-canary'
REQUEST = {'jsonrpc': '2.0', 'id': 1, 'method': 'eth_chainId', 'params': []}


def main():
    observed = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            assert json.loads(self.rfile.read(int(self.headers['Content-Length']))) == REQUEST
            route = urlsplit(self.path).path
            observed.append(route)
            status, body = 200, json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': hex(CHAIN)}).encode()
            if route == '/wrong':
                body = b'{"result":"0x1"}'
            elif route in ('/unavailable', '/limited'):
                status, body = (503 if route == '/unavailable' else 429), PRIVATE.encode()
            elif route == '/invalid':
                body = PRIVATE.encode()
            elif route == '/rpc-error':
                body = json.dumps({'error': {'code': -32603, 'message': PRIVATE}}).encode()
            elif route == '/large':
                body = b'x' * 8192
            elif route == '/slow':
                time.sleep(4.25)
            try:
                self.send_response(status)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = 'http://127.0.0.1:' + str(server.server_address[1])
    def runner(command, **kwargs):
        assert command == ['docker', 'exec', '-i', 'backend', 'sh', '-c', D.RPC_CHAIN_SCRIPT]
        assert PRIVATE not in ' '.join(command)
        # Replace only the Docker wrapper. This executes the exact production
        # POSIX shell, real curl, stdin URL transport and metadata protocol.
        result = subprocess.run(['sh', '-c', command[-1]], input=kwargs['input_text'],
            capture_output=True, text=True, timeout=kwargs['timeout'])
        if result.returncode:
            raise D.ConfigError('Fixture shell failed; raw output suppressed')
        return result.stdout
    results = []
    try:
        cases = [('ok', None), ('wrong', 'identity_mismatch'), ('unavailable', 'http_error'),
                 ('limited', 'http_error'), ('invalid', 'invalid_rpc_response'),
                 ('rpc-error', 'invalid_rpc_response'), ('large', 'response_size_limit'), ('slow', 'timeout')]
        for route, expected in cases:
            manifest = {'chain_id': str(CHAIN), 'http_rpc': base + '/' + route + '?private=' + PRIVATE}
            try:
                number = D.rpc_chain(manifest, runner=runner)
                assert expected is None and number == CHAIN
                results.append({'case': route, 'status': 'verified'})
            except D.RpcChainError as error:
                assert expected is not None and error.kind == expected
                assert PRIVATE not in str(error) + json.dumps(error.report)
                if route == 'unavailable':
                    assert error.report['http_status'] == 503 and error.report['curl_exit'] == 22
                if route == 'limited':
                    assert error.report['http_status'] == 429 and error.report['curl_exit'] == 22
                if route == 'rpc-error':
                    assert error.report['rpc_error_code'] == -32603
                if route == 'slow':
                    assert error.report['curl_exit'] == 28 and error.report['http_status'] == 0
                results.append({'case': route, 'status': 'rejected', 'kind': error.kind})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert len(observed) == len(cases)
    print(json.dumps({'status': 'passed', 'cases': results,
        'actual_curl_and_shell_protocol_verified': True, 'read_only_eth_chain_id_only': True,
        'endpoint_and_response_values_redacted': True, 'live_services_accessed': False}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
