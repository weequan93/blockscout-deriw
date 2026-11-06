#!/usr/bin/env python3
"""Reversibly hold the empty-block sanitizer while normalizing dev stored traces."""
import argparse
import base64
from datetime import datetime, timezone
import json
import ipaddress
import os
from pathlib import Path
import re
import sys
import uuid

import candidate_build as B
import containerd_maintenance as M
import deploy_check as C
import dev_config as D
import release as R
import v10_normalization_pilot as P
import v10_upgrade as U

ROOT = Path(__file__).resolve().parents[2]
BASE = Path('/data/blockscout-dev-private/v10-empty-blocks-holds')
FORMAT = 'blockscout-dev-v10-empty-blocks-hold-v1'
QUERY_MD5 = 'f7d49ed68ea1ac040431775d295eaae5'


def rpc(api, source, payload, mode):
    encoded = base64.b64encode(R.encoded({**payload, 'mode': mode})).decode()
    code = source.decode() + '\nresult = BlockscoutV10EmptyBlocksHold.run(Jason.decode!(Base.decode64!("' + encoded + '")))\n'
    code += 'IO.puts("BLOCKSCOUT_DEPLOY_CHECK=" <> Jason.encode!(result))\n'
    return C.marked_json(api, api.run(['docker', 'exec', 'backend', '/app/bin/blockscout', 'rpc', code], timeout=30))


def load(path):
    P.require(path.is_absolute() and path.resolve() == path and path.parent.parent == BASE and path.name == 'state.json'
              and re.fullmatch(r'hold-[0-9TZ-]+-[a-f0-9]{8}', path.parent.name), 'Unexpected sanitizer hold path')
    P.require(path.parent.stat().st_uid == os.geteuid() and path.parent.stat().st_mode & 0o777 == 0o700,
              'Private hold directory permissions changed')
    state = json.loads(U.private(path))
    frozen = U.private(path.parent / 'inputs.json')
    P.require(state['format'] == FORMAT and state['inputs'] == json.loads(frozen) and state['inputs_sha256'] == R.digest(frozen)
              and state['phase'] in ('prepared', 'hold_requested', 'held', 'restore_requested', 'restored'), 'Hold inputs/state changed')
    P.require(R.digest(U.private(path.parent / 'empty-blocks.exs')) == state['inputs']['script_sha256']
              and state['inputs']['payload']['query_md5'] == QUERY_MD5, 'Frozen sanitizer client changed')
    addresses = state['inputs']['payload']['backend_client_addresses']
    P.require(isinstance(addresses, list) and 1 <= len(addresses) <= 8
              and all(str(ipaddress.ip_address(value)) == value for value in addresses), 'Invalid saved backend address identity')
    return state


def public(path, state, observed=None):
    return {'status': 'empty_blocks_hold_' + state['phase'], 'state': str(path), 'environment': 'dev',
            'tool_source_commit': state['inputs']['tool_source_commit'],
            'effect': 'Pause empty-block repair scans and their block/refetch bookkeeping. Optional query cancellation requires the exact saved identity; stale or absent targets are left alone. Holding does not restart the backend or normalize stored traces; prior sanitizer bookkeeping is retained.',
            'query_target_provided': state['inputs']['payload']['pid'] is not None,
            'target_selection': state['inputs'].get('target_selection', 'explicit'),
            'restore': 'Use --restore --state with this journal after maintenance; restoring immediately starts empty-block sanitization again.',
            'persistent_environment_changed': False, 'automatic_background_migrations_held': True,
            **({'observed': observed} if observed else {})}


def prepare(api, root, upgrade_path, pid=None, query_start=None):
    P.require((pid is None and query_start is None) or
              (type(pid) is int and 0 < pid < 2**31 and isinstance(query_start, str)
               and re.fullmatch(r'[0-9:.+ TZ-]{10,64}', query_start)),
              'Provide both observed query fields or omit both for a sanitizer-only hold')
    manifest, upgrade = P.context(api, root, upgrade_path)
    source = B.clean_source(api, root)
    P.require(source[1] == '10.2.6', 'Tool source release changed')
    before = api.inspect_project(manifest)
    addresses = sorted({str(ipaddress.ip_address(network[key]))
                        for network in (before['backend'].get('NetworkSettings', {}).get('Networks') or {}).values()
                        for key in ('IPAddress', 'GlobalIPv6Address') if network.get(key)})
    P.require(1 <= len(addresses) <= 8, 'Backend connection address identity unavailable; no runtime change requested')
    script = (root / 'deploy/scripts/v10/empty_blocks_hold.exs').read_bytes()
    payload = {'pid': pid, 'query_start': query_start, 'query_md5': QUERY_MD5, 'backend_start': None,
               'backend_client_addresses': addresses}
    result = rpc(api, script, payload, 'observe')
    P.require(result.get('sanitizer_worker_running') is True and result.get('other_sanitizer_children_preserved') is True
              and result.get('other_indexer_children_preserved') is True and result.get('source_query_match_verified') is True,
              'Expected running empty-block sanitizer child is absent; inspect its owner before creating another sanitizer hold')
    targets = result['target']
    P.require(len(targets) <= 1, 'Unexpected target observation')
    selection = 'explicit' if pid is not None else 'worker_only'
    if pid is None:
        candidates = result.get('current_sanitizer_queries') or []
        P.require(len(candidates) <= 1, 'Multiple matching sanitizer queries need review; no runtime change was requested')
        if candidates:
            selected = candidates[0]
            P.require(selected['query_md5'] == QUERY_MD5 and type(selected['pid']) is int and selected['pid'] > 0
                      and isinstance(selected['query_start'], str) and isinstance(selected['backend_start'], str),
                      'Invalid current sanitizer-query identity')
            payload.update({k: selected[k] for k in ('pid', 'query_start', 'backend_start')})
            selection = 'current_query_fingerprint'
    if targets and targets[0]['expected_query'] and targets[0]['same_role'] and targets[0].get('same_backend_address') is True and not targets[0]['assigned_xid_present']:
        payload['backend_start'] = targets[0]['backend_start']
    api.ensure_stable(before, api.inspect_project(manifest), 'Deployment changed while observing the sanitizer')
    B.validate_state_directory(api, BASE, root)
    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = BASE / ('hold-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8])
    directory.mkdir(mode=0o700)
    path = directory / 'state.json'
    inputs = {'upgrade_state': str(upgrade_path), 'candidate_image': upgrade['candidate_image'], 'runtime_before': before,
              'tool_source_commit': source[0], 'script_sha256': R.digest(script), 'payload': payload, 'target_selection': selection}
    R.write_new(directory / 'inputs.json', R.encoded(inputs))
    R.write_new(directory / 'empty-blocks.exs', script)
    state = {'format': FORMAT, 'phase': 'prepared', 'inputs': inputs, 'inputs_sha256': R.digest(R.encoded(inputs))}
    R.write_new(path, R.encoded(state))
    print(json.dumps({'phase': 'empty_blocks_hold_prepared', 'state': str(path), 'runtime_changed': False}), flush=True)
    return public(path, state, result)


def execute(api, root, path, restore=False, observe=False):
    state = load(path)
    inputs = state['inputs']
    manifest, _ = P.context(api, root, Path(inputs['upgrade_state']))
    api.ensure_stable(inputs['runtime_before'], api.inspect_project(manifest), 'Deployment changed since hold preparation')
    if not observe:
        P.require(B.clean_source(api, root) == (inputs['tool_source_commit'], '10.2.6'), 'Use the hold creation commit for runtime changes')
        P.require(restore or state['phase'] not in ('restore_requested', 'restored'), 'A restored hold cannot be applied again')
        state['phase'] = 'restore_requested' if restore else 'hold_requested'
        R.update_state(path, state)
        print(json.dumps({'phase': state['phase'], 'state': str(path)}), flush=True)
    result = rpc(api, U.private(path.parent / 'empty-blocks.exs'), inputs['payload'], 'observe' if observe else 'restore' if restore else 'hold')
    api.ensure_stable(inputs['runtime_before'], api.inspect_project(manifest), 'Deployment changed during sanitizer operation')
    P.context(api, root, Path(inputs['upgrade_state']))
    if not observe:
        P.require(result['sanitizer_worker_running'] if restore else result['sanitizer_worker_held'] and not result['original_query_active'],
                  'Sanitizer operation needs reconciliation; keep the requested journal and use status/resume')
        state['phase'] = 'restored' if restore else 'held'
        state['result'] = result
        R.update_state(path, state)
    return public(path, state, result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    for name in ('prepare', 'hold', 'resume', 'status', 'restore'):
        modes.add_argument('--' + name, action='store_true')
    parser.add_argument('--upgrade-state', type=Path)
    parser.add_argument('--pid', type=int)
    parser.add_argument('--query-start')
    parser.add_argument('--state', type=Path)
    args = parser.parse_args()
    try:
        P.require(sys.platform.startswith('linux') and os.geteuid() == 0, 'Run on the Linux dev server as root')
        with M.global_lock(not args.status), B.build_lock(D, D.MANAGED_RELEASES, ROOT):
            if args.prepare or args.hold:
                P.require(args.state is None and args.upgrade_state is not None, 'Prepare needs the active upgrade and observed query identity')
                result = prepare(D, ROOT, args.upgrade_state, args.pid, args.query_start)
                args.state = Path(result['state'])
                if args.hold:
                    print(json.dumps(result, indent=2), flush=True)
                    result = execute(D, ROOT, args.state)
            else:
                P.require(args.state is not None and args.upgrade_state is None and args.pid is None and args.query_start is None,
                          'Use only the saved hold state')
                result = execute(D, ROOT, args.state, restore=args.restore, observe=args.status)
        print(json.dumps(result, indent=2))
        return 0
    except Exception as error:
        print(json.dumps({'status': 'stopped', 'state': str(args.state) if args.state else None, 'raw_values_suppressed': True,
                          'reason': str(error) if isinstance(error, U.UpgradeError) else 'Sanitizer operation failed; retain the private journal',
                          'recovery': 'Status observes the child. Resume reconciles a requested hold; restore restarts only the empty-block sanitizer worker on the same backend deployment.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
