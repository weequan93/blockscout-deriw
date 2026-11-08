# Finalize the installed PostgreSQL patch

**Completed on 8 November 2026 at 00:42 SGT.** This journal now reports
`infrastructure_finished`, PostgreSQL `15.19`, and `native_normalization_running`.
The cursor advanced from `14264419` to `14263999`. Both runtime holds were
verified and the finalization requested no further database restart. Keep the
completed journal and creator checkout; the commands below document recovery,
not a routine operation to repeat on this finished window.

See the [completed rollout record](../../deploy/audits/infrastructure-rollout-status-2026-11-08.json).

The main PostgreSQL operation installed the `15.19` image but its application
health window expired before a completion receipt was saved. The journal is:

`/data/blockscout-dev-private/infrastructure-releases/infra-20261107T155504Z-9210c7d2/state.json`

The subsequent read-only review found the main database running, outside recovery,
with the expected cluster, database, extensions and library versions. Backend
readiness, NFT connectivity/queue, runtime settings, market-price readiness and
chain RPC all passed. The backend, NFT worker and database had zero restarts and
were not OOM-killed. The old combined health result did not retain the initial
failing subprobe, so it does not establish which condition recovered first.

During recovery, normalization was deliberately paused at cursor `14264419`. Keep all saved inputs,
anchors, earlier Redis/stats receipts and controller checkouts. The existing
healthy database needs qualification and journal finalization.

## Recover the same journal

Run the complete block on the **dev server as root**. It selects the tested `r7`
helper in a separate checkout, then loads the unchanged `r6` creation checkout
for this operation. Only the activation recovery function is overlaid in memory.
The original pause/lock, source, health, identity, checkpoint, binding and resume
checks remain authoritative. Recovery evidence is appended privately; frozen
inputs and snapshots are not edited.

```bash
bash <<'FINALIZE_MAIN_POSTGRESQL'
set -euo pipefail
git -C /data/blockscout fetch origin tag deriw-infra-patches-2026-11-07-r7
if ! test -e /data/blockscout-v10-health-recovery; then
  git -C /data/blockscout worktree add --detach /data/blockscout-v10-health-recovery deriw-infra-patches-2026-11-07-r7
fi
test "$(git -C /data/blockscout-v10-health-recovery rev-parse HEAD)" = "$(git -C /data/blockscout rev-parse deriw-infra-patches-2026-11-07-r7^{commit})"
test -z "$(git -C /data/blockscout-v10-health-recovery status --porcelain)"
cd /data/blockscout-v10-collation-review
python3 - <<'PY'
import importlib.util, sys
from pathlib import Path
sys.path.insert(0, 'deploy/scripts')
import v10_infrastructure_deploy as I
module = Path('/data/blockscout-v10-health-recovery/deploy/scripts/v10_infrastructure_recovery.py')
spec = importlib.util.spec_from_file_location('patch_health_recovery', module)
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)
path = Path('/data/blockscout-dev-private/infrastructure-releases/infra-20261107T155504Z-9210c7d2/state.json')
raise SystemExit(recovery.resume_existing(I, path))
PY
FINALIZE_MAIN_POSTGRESQL
```

For the matching owned running target, expect `reconciling_running_patch` with
`service_restart_requested: false`, then `infrastructure_finished` with target
`15.19` and normalization `running`, `waiting` or `completed`.

The helper checks image, ownership labels, service configuration, stage receipt,
paused database anchor, stopped native writer, preserved inputs and free-space
reserve. It runs the original full verification before writing a receipt. A
failed health check leaves the operation pending and the writer paused. It does
not stop the matching running target as a retry mechanism. When the selected
service is missing, stopped or on the opposite side, original activation/rollback
rules remain responsible for any required recreation.

After successful finalization, retain the `r6` creation checkout: the resumed
controller still references it. Retain the separate `r7` recovery checkout and
evidence too. Do not rerun `--run --component postgresql-main` to recover this
operation, restore older data or reset the cursor.

If rollback is actually required, the same helper accepts
`recovery.resume_existing(I, path, rollback=True)`. Selecting the opposite image
uses the original rollback procedure and can require a service restart. It does
not perform an automatic data restore.

## Sanitizer hold lost during the database restart

After the running-target fix was installed, a traced verification failure was
located in `v10_normalization_reuse.verify_holds`, with an Elixir `MatchError`.
A read-only supervisor check showed metrics still held, automatic migration
startup disabled, and the empty-block sanitizer running again. This is a
supervised worker state change; the backend container itself had not restarted.
The initial expired health window did not retain enough evidence to establish
that it had the same cause.

The operator reapplied only the existing reviewed sanitizer hold under the
operation/build locks, then reran the r7 reconciliation. The helper checked the
original source, selected database, stopped native writer and unchanged peer
containers. It supplied no query-cancellation target, preserved the other indexer
children and required both strict hold checks to pass. A separate private
`sanitizer-hold-recovery-*.json` record captures the source/input hashes and
successful hold verification. Finalization then succeeded and the cursor advanced.

The forward-recovery command below records that completed intervention. Its
phase guard deliberately refuses the already finished journal. Use it only for
a reviewed matching pending forward operation; do not replay it as routine
maintenance or change a saved phase to make it run.

```bash
cd /data/blockscout-v10-collation-review
python3 - <<'PY'
import importlib.util, json, sys
from pathlib import Path
sys.path.insert(0, 'deploy/scripts')
import v10_infrastructure_deploy as I
path = Path('/data/blockscout-dev-private/infrastructure-releases/infra-20261107T155504Z-9210c7d2/state.json')
with I.M.global_lock(True), I.B.build_lock(I.D, I.D.MANAGED_RELEASES, I.ROOT):
    state, inputs = I.load(path)
    I.D.require(state['phase'] == 'activation_requested' and inputs['component'] == 'postgresql-main', 'Use the interrupted main database journal')
    I.D.require(I.F.source_review(I.D) == inputs['tool_source_commit'], 'Keep the original creation checkout')
    manifest, _, _ = I.D.configuration(I.ROOT, 'dev')
    rows = I.D.inspect_project(manifest)
    I.K.unchanged_others(I.D, inputs['before'], rows, inputs['component'])
    I.component_contract(I.D, path, inputs, 'forward', rows)
    job = Path(inputs['normalization_state'])
    native = I.N.load(job)
    I.D.require(native['inputs_sha256'] == inputs['native_inputs_sha256'] and native['phase'] in ('paused', 'completed') and I.N.owned(I.D, job, native['inputs']) is None, 'Keep the same native writer stopped during hold recovery')
    addresses = sorted({n[k] for n in rows['backend']['NetworkSettings']['Networks'].values() for k in ('IPAddress', 'GlobalIPv6Address') if n.get(k)})
    script = (I.ROOT / 'deploy/scripts/v10/empty_blocks_hold.exs').read_bytes()
    payload = {'pid': None, 'query_start': None, 'backend_start': None, 'query_md5': I.P.E.QUERY_MD5, 'backend_client_addresses': addresses}
    result = I.P.E.rpc(I.D, script, payload, 'hold')
    I.D.require(result['sanitizer_worker_held'] and result['other_indexer_children_preserved'] and not result['cancellation_requested'], 'Sanitizer hold requires review')
    I.P.Q.verify_holds(I.D)
    I.D.ensure_stable(rows, I.D.inspect_project(manifest), 'Runtime changed during hold recovery')
    I.R.write_new(path.parent / ('sanitizer-hold-recovery-' + I.uuid.uuid4().hex[:8] + '.json'), I.R.encoded({'inputs_sha256': state['inputs_sha256'], 'script_sha256': I.R.digest(script), 'sanitizer_held': True, 'runtime_holds_verified': True, 'query_cancellation_requested': False}))
    print(json.dumps({'runtime_holds_verified': True, 'database_restart_requested': False}), flush=True)
module = Path('/data/blockscout-v10-health-recovery/deploy/scripts/v10_infrastructure_recovery.py')
spec = importlib.util.spec_from_file_location('patch_health_recovery', module)
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)
raise SystemExit(recovery.resume_existing(I, path))
PY
```

A database restart can leave container identities unchanged while supervised
workers start again. For future PostgreSQL cutovers, observe and reapply the
required holds before qualifying the window. Do not bypass `verify_holds` to
resume the native writer. The r7 helper reconciles installed containers; it does
not automatically repair worker holds, and the separate intervention above
remains necessary if that condition recurs.

## Later operations

New `r7` preparations include this reconciliation behavior by default. They also
retain a bounded latest health report as `health-latest.json` in the private
infrastructure journal, including the existing readiness/runtime/RPC result.
This file is diagnostic evidence, not a completion receipt or a health waiver.

After the main journal finishes, the remaining infrastructure component is
Nginx. Its [origin certificate](origin-certificate.md) still needs replacement
before a fresh Nginx preparation from the latest verified runtime. Use the
[infrastructure guide](infrastructure-patches.md) for subsequent operations.

## Validation

The [validation record](../../deploy/audits/postgresql-health-recovery-validation-2026-11-07.json)
includes the operator's healthy `15.19` observation, unit rejection cases, and
a real PG15/Ecto fixture. Both failed and successful health reconciliation
preserve container identities/start times; only successful health creates the
receipt. The same native migration then advances through the patch and rollback
without duplicate updates or loss of its token/checkpoint.

The fixture uses synthetic local data and a controlled application-health failure.
The actual server's original full health checks still have to pass during this
recovery. No backend restart, data restore, reindex, schema migration or collation
version refresh is part of finalizing this installed patch.
