"""Reconcile an owned running patch after interrupted health qualification."""
import json
from pathlib import Path
import sys
import uuid

import dev_config as D
import release as R


def reconcile(deployment, api, path, side):
    state, inputs = deployment.load(path)
    receipt_path = path.parent / ('receipt-' + side + '.json')
    if receipt_path.exists():
        return None  # The original receipt reconciliation remains authoritative.
    manifest, _, _ = api.configuration(deployment.ROOT, 'dev')
    name, kind, _ = deployment.K.COMPONENTS[inputs['component']]
    runtime = api.inspect_project(manifest, require_running=False, allow_missing_services=(name,))
    selected = runtime.get(name) or {}
    labels = selected.get('Config', {}).get('Labels') or {}
    if not (labels.get(deployment.K.LABEL) == str(path)
            and labels.get(deployment.K.SIDE_LABEL) == side
            and selected.get('State', {}).get('Running') is True):
        return None
    D.require(deployment.F.source_review(api) == inputs['tool_source_commit'],
              'Use this infrastructure release creation commit')
    expected_phase = 'activation_requested' if side == 'forward' else 'rollback_requested'
    D.require(state['phase'] == expected_phase, 'Running patch has no matching requested activation checkpoint')
    expected_image = inputs['image_id'] if side == 'forward' else inputs['before'][name]['Image']
    D.require(selected['Image'] == expected_image, 'Owned running patch does not use the requested image')
    D.require((path.parent / ('database-before-' + side + '.json')).is_file()
              and side in state['anchor_sha256'], 'Running patch has no saved database continuity anchor')
    if side == 'forward':
        stage = json.loads(deployment.U.private(path.parent / 'stage-receipt.json'))
        D.require(stage.get('inputs_sha256') == state['inputs_sha256']
                  and stage.get('image_id') == inputs['image_id'], 'A successful matching stage receipt is required')
    deployment.K.unchanged_others(api, inputs['before'], runtime, inputs['component'])
    deployment.component_contract(api, path, inputs, side, runtime)
    native_path = Path(inputs['normalization_state'])
    native = deployment.N.load(native_path)
    D.require(native['inputs_sha256'] == inputs['native_inputs_sha256']
              and native['phase'] in ('paused','completed')
              and deployment.N.owned(api, native_path, native['inputs']) is None,
              'The same native writer must be stopped before patch reconciliation')
    if kind == 'redis':
        D.require((path.parent / ('redis-backup-' + side + '.json')).is_file(), 'Installed Redis has no final backup receipt')
        deployment.private_backup(api, path, inputs, side)  # Verify the retained backup; never take a live replacement.
    D.require(deployment.N.J.P.free_bytes() >= deployment.N.RESUME_FREE,
              'Keep the existing 220 GiB deployment/resume reserve')
    print(json.dumps({'phase':'reconciling_running_patch','state':str(path),
                      'service':name,'service_restart_requested':False}), flush=True)
    qualified = deployment.verify(api, path, side, wait=True)
    api.ensure_stable(runtime, qualified, 'Runtime changed while reconciling the installed patch')
    receipt = {'format': deployment.FORMAT, 'side': side, 'inputs_sha256': state['inputs_sha256'],
               'runtime_before': qualified, 'source_root': inputs['source_root'],
               'tool_source_commit': inputs['tool_source_commit'],
               'normalization_state': inputs['normalization_state'],
               'anchor_sha256': state['anchor_sha256'][side],
               'database_after': deployment.K.stable_database_anchor(api)}
    R.write_new(receipt_path, R.encoded(receipt))
    deployment.save(path, state, 'active' if side == 'forward' else 'rolled_back')
    return receipt


def resume_existing(deployment, path, rollback=False):
    state, inputs = deployment.load(path)
    D.require(state['phase'] in ('activation_requested','rollback_requested','active','rolled_back','finished','rollback_finished'),
              'Use an already requested infrastructure patch journal')
    D.require(deployment.ROOT == Path(inputs['source_root'])
              and deployment.F.source_review(deployment.D) == inputs['tool_source_commit'],
              'Recover through the unchanged original infrastructure creation checkout')
    recovery_root = Path(__file__).resolve().parents[2]
    commit, version = deployment.B.clean_source(deployment.D, recovery_root)
    D.require(version == '10.2.6', 'Use the reviewed v10 patch recovery tools')
    changes = deployment.D.run(['git','-C',str(recovery_root),'diff','--name-only',inputs['tool_source_commit'],commit]).splitlines()
    D.require(all(name.startswith(('deploy/','docs/')) for name in changes), 'Recovery changed backend application/configuration')
    evidence = {'format':'deriw-infrastructure-health-recovery-v1',
                'original_source_commit':inputs['tool_source_commit'],'recovery_source_commit':commit,
                'recovery_source_root':str(recovery_root),'source_sha256':R.digest(Path(__file__).read_bytes()),
                'inputs_sha256':state['inputs_sha256'],'original_inputs_and_snapshots_edited':False,
                'health_checks_bypassed':False,
                'policy':'Verify a matching owned running target before considering recreation'}
    R.write_new(path.parent / ('health-recovery-' + uuid.uuid4().hex[:8] + '.json'), R.encoded(evidence))
    original, argv = deployment.activate, sys.argv
    def activate(api, selected_path, side):
        receipt = reconcile(deployment, api, selected_path, side)
        return receipt if receipt is not None else original(api, selected_path, side)
    deployment.activate = activate
    sys.argv = ['v10_infrastructure_deploy.py','--rollback' if rollback else '--resume','--state',str(path)]
    try:
        return deployment.main()
    finally:
        deployment.activate, sys.argv = original, argv
