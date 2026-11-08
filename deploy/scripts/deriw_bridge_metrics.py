#!/usr/bin/env python3
"""Read-only bridge/settlement Prometheus exporter for the captured DeriW dev chain.

Polls independently of scrapes. No DB migrations, persistent backend configuration,
normalization leases, worker holds, or application containers are changed.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
import os
from pathlib import Path
import sys
import threading
import time

import dev_config as D
import deploy_check as C
import v10_vacuum as V

ROOT = Path(__file__).resolve().parents[2]
UNIT = 'deriw-bridge-metrics.service'
UNIT_PATH = Path('/etc/systemd/system') / UNIT
STATES = ('initiated', 'sent', 'confirmed', 'relayed')
SECTIONS = ('deposits', 'withdrawals', 'rollup', 'published', 'confirmed')
INTERVAL = 60
MAX_AGE = 150


def expression(root=ROOT):
    queries = json.loads((root / 'deploy/scripts/v10/bridge_metrics_queries.json').read_text())
    D.require(set(queries) == set(SECTIONS), 'Unexpected bridge query inventory')
    # Encoding is Elixir-safe even if a future static SQL comment contains #{...}.
    import base64
    encoded = base64.b64encode(json.dumps(queries).encode()).decode()
    return '''
true = System.get_env("CHAIN_ID") == "18417507517"
:arbitrum = Application.get_env(:explorer, :chain_type)
"10.2.6" = Application.spec(:explorer, :vsn) |> to_string()
bridge_metric_queries = Jason.decode!(Base.decode64!("''' + encoded + '''"))
''' + (root / 'deploy/scripts/v10/bridge_metrics.exs').read_text()


def collect(api=D):
    manifest, _, _ = api.configuration(ROOT, 'dev')
    before = api.inspect_project(manifest)
    data = C.marked_json(api, api.run(
        ['docker', 'exec', '-i', 'backend', 'sh', '-c', V.RPC_SCRIPT],
        input_text=expression(), timeout=25))
    api.ensure_stable(before, api.inspect_project(manifest), 'Runtime changed during bridge metrics read')
    validate(data)
    return data


def number(value, optional=False):
    return (optional and value is None) or (type(value) in (int, float)
        and math.isfinite(value) and 0 <= value < 2**63)


def validate(data):
    D.require(isinstance(data, dict) and number(data.get('observed_at_unix'))
              and isinstance(data.get('sections'), dict) and set(data['sections']) == set(SECTIONS),
              'Invalid bridge metrics response')
    valid_pairs = {('processed', 'not_applicable'), ('sealed', 'not_applicable'),
                   ('sent', 'unfinalized'), ('sent', 'finalized'),
                   ('confirmed', 'unfinalized'), ('confirmed', 'finalized')}
    for name, section in data['sections'].items():
        D.require(isinstance(section, dict) and type(section.get('ok')) is bool,
                  'Invalid section result')
        if not section['ok']:
            continue
        row = section.get('data')
        D.require(isinstance(row, dict), 'Invalid aggregate result')
        if name in ('deposits', 'withdrawals'):
            D.require(type(row.get('complete')) is bool and number(row.get('size')) and row['size'] <= 10000
                      and isinstance(row.get('states'), dict) and set(row['states']) <= set(STATES)
                      and all(number(n) for n in row['states'].values())
                      and sum(row['states'].values()) == row['size'], 'Invalid bridge state counts')
            for key in ('highest_message_id', 'highest_relayed_message_id', 'oldest_pending_timestamp'):
                D.require(number(row.get(key), optional=True), 'Invalid bridge watermark')
            for key in ('pending_without_timestamp', 'missing_origination', 'completion_recorded'):
                D.require(number(row.get(key)) and row[key] <= row['size'], 'Invalid bridge coverage')
        elif name == 'rollup':
            D.require(number(row.get('size')) and row['size'] <= 1000 and isinstance(row.get('states'), list),
                      'Invalid rollup sample')
            seen = set()
            for state in row['states']:
                key = (state.get('stage'), state.get('parent_finality'))
                D.require(key in valid_pairs and key not in seen
                          and number(state.get('count')) and number(state.get('highest_block')),
                          'Invalid rollup state')
                seen.add(key)
            D.require(sum(s['count'] for s in row['states']) == row['size'], 'Rollup sample counts differ')
            for key in ('lowest_block', 'highest_block', 'latest_block_timestamp'):
                D.require(number(row.get(key), optional=True), 'Invalid rollup watermark')
        elif row:
            D.require(number(row.get('block_number')) and number(row.get('parent_timestamp'))
                      and row.get('parent_finality') in ('unfinalized', 'finalized'), 'Invalid settlement watermark')
            if name == 'published':
                D.require(number(row.get('batch_number')), 'Invalid batch watermark')
    D.require(isinstance(data.get('trackers'), dict) and set(data['trackers']) == {'deposits', 'settlement'},
              'Invalid tracker inventory')
    for row in data['trackers'].values():
        D.require(type(row.get('ok')) is bool and type(row.get('running')) is bool
                  and number(row.get('next_parent_block'), optional=True), 'Invalid tracker observation')


def render(data=None, last_success=0, success=False, now=None):
    now = time.time() if now is None else now
    fresh = bool(success and data and 0 <= now - data['observed_at_unix'] <= MAX_AGE)
    lines = []
    declared = set()

    def metric(name, help_text, value, **labels):
        if name not in declared:
            lines.extend(['# HELP ' + name + ' ' + help_text, '# TYPE ' + name + ' gauge'])
            declared.add(name)
        # All label values are validated enums/static names, never data identifiers.
        suffix = '{' + ','.join(k + '=' + json.dumps(str(v)) for k, v in sorted(labels.items())) + '}' if labels else ''
        lines.append(name + suffix + ' ' + str(int(value) if type(value) is bool else value))

    metric('deriw_bridge_exporter_up', '1 when the latest polling attempt succeeded and is fresh.', int(fresh))
    metric('deriw_bridge_exporter_last_success_timestamp_seconds', 'Unix time of the last successful polling attempt.', last_success)
    if not fresh:
        return '\n'.join(lines) + '\n'
    metric('deriw_bridge_observation_timestamp_seconds', 'Backend snapshot Unix timestamp.', data['observed_at_unix'])
    sections = data['sections']
    for name in SECTIONS:
        metric('deriw_bridge_query_success', '1 when this bounded read succeeded in the latest snapshot.',
               int(sections[name]['ok']), section=name)
    for name, direction in (('deposits', 'l2_to_l3'), ('withdrawals', 'l3_to_l2')):
        section = sections[name]
        if not section['ok']:
            continue
        row = section['data']
        label = {'direction': direction}
        metric('deriw_bridge_sample_complete', '1 when the latest-10000-message sample covers every indexed message in this direction.', row['complete'], **label)
        metric('deriw_bridge_sample_size', 'Number of indexed messages sampled, at most 10000 per direction.', row['size'], **label)
        for state in STATES:
            metric('deriw_bridge_sample_messages', 'Current message state counts within the latest-10000 indexed messages; gauges, not lifetime counters.', row['states'].get(state, 0), status=state, **label)
        for key in ('highest_message_id', 'highest_relayed_message_id'):
            if row[key] is not None:
                metric('deriw_bridge_sample_' + key, 'Highest observed ID within the sampled indexed messages; does not imply gap-free progress.', row[key], **label)
        for key in ('pending_without_timestamp', 'missing_origination', 'completion_recorded'):
            metric('deriw_bridge_sample_' + key, 'Number of sampled messages with ' + key.replace('_', ' ') + '.', row[key], **label)
        if row['oldest_pending_timestamp'] is not None:
            metric('deriw_bridge_sample_oldest_pending_age_seconds', 'Age of the oldest non-relayed sampled message with a known origination time.', max(0, data['observed_at_unix'] - row['oldest_pending_timestamp']), **label)
    if sections['rollup']['ok']:
        row = sections['rollup']['data']
        metric('deriw_rollup_sample_size', 'Number of latest indexed consensus blocks classified, at most 1000.', row['size'])
        for key in ('lowest_block', 'highest_block', 'latest_block_timestamp'):
            if row[key] is not None:
                metric('deriw_rollup_sample_' + key, 'Latest-1000 indexed consensus block sample ' + key.replace('_', ' ') + '.', row[key])
        values = {(s['stage'], s['parent_finality']): s for s in row['states']}
        for stage, finality in [('processed', 'not_applicable'), ('sealed', 'not_applicable'),
                                ('sent', 'unfinalized'), ('sent', 'finalized'),
                                ('confirmed', 'unfinalized'), ('confirmed', 'finalized')]:
            value = values.get((stage, finality))
            metric('deriw_rollup_sample_blocks', 'Exclusive settlement stages within the latest-1000 indexed blocks; sent/confirmed match the explorer UI.',
                   value['count'] if value else 0, stage=stage, parent_finality=finality)
        if row['highest_block'] is not None:
            metric('deriw_rollup_stage_block_number', 'L3 block number recorded for this cumulative stage: indexed head, published batch end, or confirmed block; not a contiguous coverage proof.', row['highest_block'], stage='processed')
    for section, stage in (('published', 'sent'), ('confirmed', 'confirmed')):
        if not sections[section]['ok']:
            continue
        row = sections[section]['data']
        metric('deriw_rollup_stage_observed', '1 when this settlement-stage watermark was found in the index.', int(bool(row)), stage=stage)
        if row:
            metric('deriw_rollup_stage_block_number', 'L3 block number recorded for this cumulative stage: indexed head, published batch end, or confirmed block; not a contiguous coverage proof.', row['block_number'], stage=stage)
            metric('deriw_rollup_stage_parent_transaction_finalized', 'Whether the transaction supplying this watermark is finalized on the parent; separate from rollup confirmation.', int(row['parent_finality'] == 'finalized'), stage=stage)
            metric('deriw_rollup_stage_parent_timestamp_seconds', 'Timestamp of the indexed parent-chain transaction for this stage.', row['parent_timestamp'], stage=stage)
            if section == 'published':
                metric('deriw_rollup_latest_published_batch_number', 'Latest batch number indexed from the parent chain; not batch poster health.', row['batch_number'])
    for name, row in data['trackers'].items():
        metric('deriw_bridge_tracker_observation_success', '1 when tracker process state could be read.', int(row['ok']), tracker=name)
        if row['ok']:
            metric('deriw_bridge_tracker_running', '1 when the tracker process exists; does not prove its cursor advances.', int(row['running']), tracker=name)
            if row.get('next_parent_block') is not None:
                metric('deriw_bridge_tracker_next_parent_block', 'Next parent L2 block requested by the live discovery task; historical/backfill work is separate.', row['next_parent_block'], tracker=name)
    return '\n'.join(lines) + '\n'


class Cache:
    def __init__(self, collector=collect):
        self.collector, self.lock = collector, threading.Lock()
        self.data, self.last_success, self.success = None, 0, False

    def poll(self):
        try:
            data = self.collector()
            with self.lock:
                self.data, self.last_success, self.success = data, time.time(), True
        except Exception:
            # Errors can contain private SQL/endpoints. Publish only health gauges.
            with self.lock:
                self.success = False

    def metrics(self):
        with self.lock:
            return render(self.data, self.last_success, self.success)


def handler(cache):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ('/metrics', '/healthz'):
                self.send_error(404)
                return
            body = cache.metrics().encode() if self.path == '/metrics' else b'exporter process alive\n'
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; version=0.0.4; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass
    return Handler


def serve(listen, port):
    cache, stop = Cache(), threading.Event()

    def poll():
        while not stop.is_set():
            cache.poll()
            stop.wait(INTERVAL)

    thread = threading.Thread(target=poll, daemon=True)
    thread.start()
    server = ThreadingHTTPServer((listen, port), handler(cache))
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()


def service_text(root, executable, listen, port):
    # systemd is not a shell: quote argv and escape specifier expansion.
    def quote(value):
        return json.dumps(str(value).replace('%', '%%'))
    command = ' '.join(quote(v) for v in [executable, root / 'deploy/scripts/deriw_bridge_metrics.py',
                                         '--serve', '--listen', listen, '--port', port])
    return f'''[Unit]
Description=DeriW read-only bridge and settlement metrics
After=docker.service
Requires=docker.service
RequiresMountsFor=/data

[Service]
Type=simple
User=root
ExecStart={command}
Restart=always
RestartSec=15
TimeoutStopSec=30
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
Environment=PYTHONDONTWRITEBYTECODE=1
StandardOutput=null
StandardError=null

[Install]
WantedBy=multi-user.target
'''


def install(listen, port):
    D.require(sys.platform == 'linux' and os.geteuid() == 0, 'Install on the dev server as root')
    D.require(not D.run(['git', '-C', str(ROOT), 'status', '--porcelain']).strip(), 'Use a clean pinned worktree')
    D.require(ROOT.resolve() == ROOT and not any(c in str(ROOT) for c in '\n\r'), 'Use an absolute pinned source root')
    text = service_text(ROOT, Path(sys.executable).resolve(), listen, port)
    D.require(not UNIT_PATH.is_symlink(), 'Exporter service must not be a symlink')
    if UNIT_PATH.exists():
        D.require(UNIT_PATH.read_text() == text, 'Existing exporter service definition differs')
    # Complete the live read before installing anything. A failed query may need
    # a plan/statistics review; silently shipping an unavailable stage is not useful.
    data = collect()
    ready = all(section['ok'] for section in data['sections'].values())
    print(json.dumps({'phase': 'bridge_metrics_preflight', 'ready': ready,
                      'sections': {k: {'ok': v['ok'], **({'reason': v['reason']} if 'reason' in v else {})}
                                   for k, v in data['sections'].items()},
                      'database_writes': False}), flush=True)
    D.require(ready, 'Bridge metrics preflight requires review')
    if not UNIT_PATH.exists():
        descriptor = os.open(UNIT_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(descriptor, 'w') as file:
            file.write(text)
    D.run(['systemctl', 'daemon-reload'])
    D.run(['systemctl', 'enable', '--now', UNIT])
    print(json.dumps({'status': 'bridge_metrics_service_start_requested', 'service': UNIT,
                      'listen': listen, 'port': port, 'backend_restarted': False,
                      'database_writes': False, 'normalization_changed': False}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--once', action='store_true', help='Print one bounded read as JSON; change no services')
    mode.add_argument('--serve', action='store_true')
    mode.add_argument('--install-service', action='store_true')
    parser.add_argument('--listen', default='127.0.0.1', help='IPv4 interface; default is host-only')
    parser.add_argument('--port', type=int, default=9160)
    args = parser.parse_args()
    try:
        D.require(ipaddress.ip_address(args.listen).version == 4 and 1024 <= args.port <= 65535,
                  'Provide an IPv4 address and an unprivileged TCP port')
        if args.once:
            print(json.dumps({'read_only': True, 'raw_values_suppressed': True, **collect()}, indent=2))
        elif args.install_service:
            install(args.listen, args.port)
        else:
            serve(args.listen, args.port)
        return 0
    except Exception:
        print(json.dumps({'status': 'bridge_metrics_unavailable', 'raw_values_suppressed': True,
                          'database_writes': False}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
