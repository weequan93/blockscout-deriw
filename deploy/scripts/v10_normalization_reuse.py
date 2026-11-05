"""Qualify one finite normalization reuse session from a completed vacuum."""
from datetime import datetime
import json
import os
from pathlib import Path
import re

import deploy_check as C
import dev_config as D
import release as R
import v10_normalization_pilot as P
import v10_upgrade as U

PROFILE = 'reuse-v1'
FORMAT = 'blockscout-dev-v10-normalization-reuse-review-v1'
TOTAL_SPACE_BYTES = 8 * 1024**3
HOLD_PROBE = '''
held = fn parent, child ->
  Enum.any?(Supervisor.which_children(parent), fn
    {^child, :undefined, :worker, _} -> true
    _ -> false
  end) and is_nil(Process.whereis(child))
end
true = Explorer.Migrator.StartupControl.disabled?()
true = held.(BlockScoutWeb.Supervisor, Indexer.Prometheus.Metrics)
true = held.(Indexer.Fetcher.EmptyBlocksSanitizer.Supervisor, Indexer.Fetcher.EmptyBlocksSanitizer)
IO.puts("BLOCKSCOUT_DEPLOY_CHECK=" <> Jason.encode!(%{runtime_holds_verified: true}))
'''


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from strings(child)


def cleanup_metrics(messages, database):
    """Select the main relation's notice, excluding its separate TOAST notice."""
    prefix = 'finished vacuuming "' + database + '.public.internal_transactions":'
    notices = [text for text in strings(messages) if text.startswith(prefix)]
    P.require(len(notices) == 1, 'Require one main-table vacuum completion notice; raw messages suppressed')
    text = notices[0]

    def numbers(pattern):
        found = re.findall(pattern, text)
        P.require(len(found) == 1, 'Vacuum cleanup metrics are incomplete or ambiguous; raw messages suppressed')
        row = found[0]
        return [int(v) for v in (row if isinstance(row, tuple) else (row,))]

    indexes = re.findall(r'index "[^"\n]+": pages: (\d+) in total, (\d+) newly deleted, (\d+) currently deleted, (\d+) reusable', text)
    P.require(indexes and 'index scan bypassed' not in text and 'tuples missed:' not in text,
              'Vacuum index cleanup was bypassed or heap cleanup was incomplete')
    return {'index_scans': numbers(r'index scans: (\d+)')[0],
            'dead_item_identifiers_removed': numbers(r'had (\d+) dead item identifiers removed')[0],
            'heap_pages_with_dead_items': numbers(r'index scan needed: (\d+) pages')[0],
            'heap_pages': numbers(r'pages: (\d+) removed, (\d+) remain, (\d+) scanned'),
            'tuples': numbers(r'tuples: (\d+) removed, (\d+) remain, (\d+) are dead but not yet removable'),
            'index_pages': [[int(v) for v in row] for row in indexes],
            'buffers': numbers(r'buffer usage: (\d+) hits, (\d+) misses, (\d+) dirtied'),
            'wal': numbers(r'WAL usage: (\d+) records, (\d+) full page images, (\d+) bytes')}


def validate_review(review):
    P.require(review['format'] == FORMAT and review['vacuum_sql_completed'] is True
              and review['completion_verified'] is True
              and re.fullmatch(r'[a-f0-9]{64}', review['vacuum_state_sha256'])
              and re.fullmatch(r'[a-f0-9]{64}', review['vacuum_inputs_sha256'])
              and re.fullmatch(r'[a-f0-9]{64}', review['normalization_state_sha256']),
              'Invalid completed vacuum review')
    before, after = review['resources_before'], review['resources_after']
    P.require(after['table']['vacuum_count'] > before['table']['vacuum_count']
              and after['table']['dead_rows'] == 0
              and before['table']['stats_reset'] == after['table']['stats_reset']
              and before['wal']['stats_reset'] == after['wal']['stats_reset']
              and before['table']['options'] == after['table']['options']
              and before['heap_identity'] == after['heap_identity']
              and after['heap_identity'] == review['database_anchor']['heap_identity']
              and after['runtime_holds_verified'] is True and not after['target_sessions'] and not after['vacuum']
              and type(after['data_free_bytes']) is int,
              'Vacuum review lacks stable completed cleanup and worker-hold evidence')
    metrics = review['cleanup']
    P.require(metrics['index_scans'] >= 1 and metrics['dead_item_identifiers_removed'] > 0
              and metrics['heap_pages_with_dead_items'] > 0 and metrics['tuples'][2] == 0
              and metrics['index_pages'] and type(review['receipt_elapsed_ms']) is int
              and 0 < review['receipt_elapsed_ms'] <= review['vacuum_duration_seconds'] * 1000,
              'Vacuum review lacks completed main-table index cleanup')
    P.require(type(review['maintenance_selected_rows']) is int and review['maintenance_selected_rows'] >= 0
              and datetime.fromisoformat(after['observed_at']) >= datetime.fromisoformat(before['observed_at']),
              'Invalid vacuum review accounting or timestamps')
    return review


def validate_inputs(inputs):
    review = inputs.get('reuse_review')
    if inputs.get('profile') != PROFILE:
        P.require(review is None, 'A vacuum reuse review requires the reuse profile')
        return
    P.require(review is not None, 'The reuse profile requires a completed vacuum review')
    validate_review(review)
    P.require(inputs['execution_profile'] == inputs['payload'].get('execution_profile') == 'lean-v1'
              and inputs['start_cursor'] == review['normalization_cursor']
              and inputs['maintenance_rows_before'] == review['maintenance_selected_rows']
              and R.digest(R.encoded(inputs['maintenance_origin'])) == review['maintenance_origin_sha256']
              and inputs['space_before'] == inputs['maintenance_origin']['space_before']
              and inputs['candidate_image'] == review['candidate_image']
              and inputs['upgrade_state'] == review['upgrade_state']
              and R.digest(R.encoded(D.stable(inputs['runtime_before']))) == review['runtime_before_sha256']
              and all(inputs['payload'][k] == v for k, v in review['database_anchor'].items())
              and inputs['start_parent'] == review['normalization_state']
              and inputs['start_parent_sha256'] == review['normalization_state_sha256'],
              'Reuse review differs from the frozen normalization checkpoint or runtime')


def verify_holds(api):
    # This reads two supervisor trees in the existing backend. It changes no
    # worker, configuration or database state and starts no application.
    import v10_vacuum as V
    report = C.marked_json(api, api.run(['docker', 'exec', '-i', 'backend', 'sh', '-c', V.RPC_SCRIPT],
                                      input_text=HOLD_PROBE, timeout=15))
    P.require(report == {'runtime_holds_verified': True}, 'Keep both reviewed runtime holds active')


def prepare_review(api, path, parent_path, previous, manifest):
    import v10_normalization_session as J
    import v10_vacuum as V
    state = V.load(path)
    inputs = state['inputs']
    P.require(not (path.parent / 'normalization-reuse.json').exists(),
              'This vacuum already reserved a reuse session; inspect its saved state instead')
    P.require(state['phase'] == 'finished' and state.get('stop_reason') == 'vacuum_completed_needs_review'
              and state.get('vacuum_sql_completed') is True and state.get('completion_verified') is True,
              'Reuse requires a completed and verified vacuum window')
    P.require(inputs['normalization_state'] == str(parent_path)
              and inputs['normalization_state_sha256'] == R.digest(U.private(parent_path))
              and inputs['normalization_cursor'] == previous['cursor']
              and inputs['maintenance_selected_rows'] == previous['inputs']['maintenance_rows_before'] + previous['rows']
              and inputs['maintenance_origin'] == previous['inputs']['maintenance_origin']
              and inputs['candidate_image'] == previous['inputs']['candidate_image']
              and inputs['upgrade_state'] == previous['inputs']['upgrade_state']
              and inputs['chain_id'] == manifest['chain_id'], 'Vacuum belongs to another normalization checkpoint')
    before = json.loads(U.private(path.parent / 'resources-before.json'))
    after = json.loads(U.private(path.parent / 'resources-after.json'))
    P.require(R.digest(R.encoded(before)) == state['resources_before_sha256']
              and all(state['latest'][key] == after[key] for key in ('observed_at', 'table', 'wal', 'data_free_bytes')),
              'Saved vacuum resource evidence changed')
    result = V.marker(path, 'BLOCKSCOUT_VACUUM_RESULT=')
    P.require(isinstance(result, dict), 'Reuse requires the saved successful SQL completion receipt')
    V.validate_result(result, state)
    P.require(result == state['result'] and result['mode'] == 'completed', 'Vacuum completion receipt changed')
    review = validate_review({
        'format': FORMAT, 'vacuum_state': str(path), 'vacuum_state_sha256': R.digest(U.private(path)),
        'vacuum_inputs_sha256': state['inputs_sha256'],
        'normalization_state': str(parent_path), 'normalization_state_sha256': inputs['normalization_state_sha256'],
        'normalization_cursor': inputs['normalization_cursor'],
        'maintenance_selected_rows': inputs['maintenance_selected_rows'],
        'maintenance_origin_sha256': R.digest(R.encoded(inputs['maintenance_origin'])),
        'candidate_image': inputs['candidate_image'], 'upgrade_state': inputs['upgrade_state'],
        'runtime_before_sha256': R.digest(R.encoded(D.stable(inputs['runtime_before']))),
        'database_anchor': {k: inputs['payload'][k] for k in ('database_identity', 'heap_identity')},
        'vacuum_sql_completed': True, 'completion_verified': True,
        'vacuum_duration_seconds': inputs['limits']['duration_seconds'], 'receipt_elapsed_ms': result['elapsed_ms'],
        'resources_before': before, 'resources_after': after,
        'cleanup': cleanup_metrics(V.marker(path, 'BLOCKSCOUT_VACUUM_VERBOSE='), inputs['payload']['database_identity']['database'])})
    # A vacuum grants one new finite experiment. Reserving inputs consumes that
    # grant even if its preparation fails; retain that journal for review.
    for directory in J.BASE.iterdir():
        other = J.load(directory / 'state.json', manifest, check_resources=False)
        saved = other['inputs'].get('reuse_review')
        P.require(not saved or (saved['vacuum_inputs_sha256'] != review['vacuum_inputs_sha256']
                               and saved['normalization_state_sha256'] != review['normalization_state_sha256']),
                  'This vacuum already reserved a reuse session; inspect its saved state instead')
    V.no_normalizer(api, manifest)
    P.require(not V.owned_client(api, path, inputs), 'The old vacuum client must be absent before reuse')
    api.ensure_stable(inputs['runtime_before'], api.inspect_project(manifest), 'Runtime changed after qualified vacuum')
    verify_holds(api)
    return review


def reserve_review(review, session_path, inputs_sha256):
    # Keep the consumed grant beside its immutable vacuum journal, including
    # after a finished session is manually archived. Never replace a receipt.
    vacuum_path = Path(review['vacuum_state'])
    P.require(R.digest(U.private(vacuum_path)) == review['vacuum_state_sha256'],
              'Vacuum checkpoint changed before reuse reservation')
    reservation = vacuum_path.parent / 'normalization-reuse.json'
    P.require(not reservation.exists(), 'This vacuum already reserved a reuse session')
    for directory in (session_path.parent, session_path.parent.parent):
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    R.write_new(reservation, R.encoded({'format': FORMAT, 'vacuum_inputs_sha256': review['vacuum_inputs_sha256'],
                                      'state': str(session_path), 'inputs_sha256': inputs_sha256}))
    fd = os.open(vacuum_path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def public(inputs):
    review = inputs.get('reuse_review')
    if review is None:
        return None
    return {'vacuum_state': review['vacuum_state'], 'receipt_elapsed_ms': review['receipt_elapsed_ms'],
            'cleanup': review['cleanup'], 'maintenance_rows_before_reuse': review['maintenance_selected_rows'],
            'original_data_free_bytes': inputs['space_before'],
            'post_vacuum_data_free_bytes': review['resources_after']['data_free_bytes'],
            'cumulative_space_limit_bytes': TOTAL_SPACE_BYTES,
            'scope': 'One finite reuse measurement; another successor requires review. No sustained run or migration completion.'}
