# Durable L2 to L3 deposit scan progress

**Deployed successfully on dev at 12:14 SGT on 8 November 2026.** The recovered
operator receipt reported `bridge_checkpoint_finished` and matching live/durable
deposit cursors of `278342997`. A later native checkpoint at 12:48 SGT showed
continued normalization progress to `13686814`, automatically waiting for a long
transaction. The [live rollout record](../../deploy/audits/bridge-checkpoint-live-2026-11-08.json)
retains these separate observation times.

Keep the active source checkout `/data/blockscout-v10-bridge-checkpoint` pinned
at `e7a16ead2bcf53da05b6e059da616ebacddbf22d`. No further deployment is needed
for this fix. Its journal is
`/data/blockscout-dev-private/releases/release-20261108T040539Z-a2d3d927/state.json`.
Use the existing native status command from this checkout for current progress;
the older infrastructure checkout does not understand the new backend binding.

The operator's 8 November 2026 observations showed the deposit scanner moving
forward over 31 seconds, but below its 7 November cursor. Source inspection
identified a restart weakness: initialization used the last stored deposit's
parent block, while progress through empty ranges existed only in worker memory.
A worker restart could therefore repeat previously scanned empty history. The
observations alone do not establish which event restarted that worker.

The fixed backend records the next parent block in the existing
`last_fetched_counters` table after each successful RPC/import chunk, including
empty chunks. Keys are scoped to parent chain ID and rollup/bridge contracts,
and do not contain RPC endpoints or credentials. Startup uses the greater of
that cursor and the bound from imported deposit origins. Failed chunks never
advance the checkpoint. A crash after import but before the checkpoint can
replay that chunk through the existing idempotent import.

The configured safe-block overlap still applies on each iteration for reorgs
and inconsistent RPC heads. The durable cursor records completed scanning; it
is not proof that every deposit relayed or that the scanner reached the head.
The historical/missing-origin tasks and their boundaries are preserved. No
schema migration, new database or manual counter reset is needed.

## Dev deployment

This release builds a new v10.2.6 backend image on the dev server and recreates
only `backend`, with start-only startup and automatic migrators still disabled.
It inherits the latest qualified Compose snapshot, including frontend `.4`,
Nginx `1.31.8` with its duplicate cleanup, Redis `8.8.4` and both PostgreSQL
`15.19` selections. Their containers and pinned worktrees remain.

The existing normalization job is paused before the build. Immediately before
backend replacement, the helper reads the old bridge worker's completed-callback
cursor and writes its matching durable checkpoint. This avoids discarding the
live scan position when installing the fix. After readiness, RPC, runtime,
worker-hold and database-continuity checks, it verifies the new tracker resumed
that checkpoint and resumes the same normalization job. Native inputs, worker
script, token, database/heap identity and cursor are preserved. The no-growth-cap
policy and existing 200 GiB reserve/pause210/resume220 policy remain.

The application sequence below is retained for reference for the completed dev
window. Run new deployments in the **dev server terminal as root**, preferably inside the existing
terminal multiplexer. The build can take several minutes; progress messages and
private logs are retained. The release tag pins the tested source profile.

```bash
bash <<'DEPLOY_BRIDGE_CHECKPOINT'
set -euo pipefail
git -C /data/blockscout fetch origin tag deriw-bridge-checkpoint-2026-11-08
if ! test -e /data/blockscout-v10-bridge-checkpoint; then
  git -C /data/blockscout worktree add --detach /data/blockscout-v10-bridge-checkpoint deriw-bridge-checkpoint-2026-11-08
fi
cd /data/blockscout-v10-bridge-checkpoint
test "$(git rev-parse HEAD)" = "$(git rev-parse deriw-bridge-checkpoint-2026-11-08^{commit})"
test -z "$(git status --porcelain)"
bash deploy/steps/dev-v10-bridge-checkpoint.sh --run \
  --normalization-state /data/blockscout-dev-private/v10-native-normalization/job-20261106T103901Z-59bb4dc6/state.json
DEPLOY_BRIDGE_CHECKPOINT
```

Expect `bridge_checkpoint_finished`, `bridge.checkpoint_key_matches: true`,
a durable/live deposit cursor at least equal to the handoff value, and a native
result of running, waiting or completed. The bridge fields are parent L2 block
numbers; the normalization cursor is a separate internal-transaction L3 history
cursor. A native autovacuum or long-transaction wait retains automatic resume.
The background bridge metrics service continues collecting through its own
pinned checkout and retries interrupted polls automatically.

## Recovery and rollback

Keep the printed `/data/blockscout-dev-private/releases/release-.../state.json`,
all earlier journals and worktrees. Recover through this exact source checkout:

```bash
cd /data/blockscout-v10-bridge-checkpoint
CHECKPOINT_RELEASE=/data/blockscout-dev-private/releases/release-REPLACE_WITH_PRINTED_ID/state.json
bash deploy/steps/dev-v10-bridge-checkpoint.sh --status --state "$CHECKPOINT_RELEASE"
bash deploy/steps/dev-v10-bridge-checkpoint.sh --resume --state "$CHECKPOINT_RELEASE"
```

An owned running target is qualified before considering another recreation.
If health has not recovered, normalization remains paused with its checkpoint.
Resume keeps the original source, snapshots, seed and database anchor. It does
not seed from an old log or reset budgets. A failure before a state was created
can be retried with `--run`; if abandoning that unchanged preparation, use the
original `r9` checkout `/data/blockscout-v10-nginx-cleanup` and its native
`--start --state` command to restart the previous controller after confirming
runtime stayed unchanged.

To restore this operation's previous backend binary and settings:

```bash
bash deploy/steps/dev-v10-bridge-checkpoint.sh --rollback --state "$CHECKPOINT_RELEASE"
```

Rollback retains current database rows, bridge checkpoints and normalization
progress. The old backend does not use the new durable bridge counter, so its
original restart/rescan behavior can return. A completed native migration is
never reopened. The journal's side cannot be reactivated with forward resume
after rollback. No automatic PostgreSQL/RDB restore or index rebuild occurs.

After successful handoff, keep this checkout pinned: it hosts the native
controller. For current progress, use its native `--status` and the existing
bounded `deriw_bridge_metrics.py --once` command. The deployment's `--status`
reads saved release metadata; its timestamp is not a fresh bridge observation.
Later frontend/infrastructure operations must use tools that understand this
authenticated backend handoff and inherit its full Compose snapshot.

## Validation

The [validation record](../../deploy/audits/bridge-checkpoint-validation-2026-11-08.json)
records the source hashes, real PostgreSQL/Ecto tests and deployment unit checks.
The fixture covers empty ranges, cross-process resume, partial RPC failures,
monotonic writes, identity isolation, invalid saved values and existing reorg
overlap. It also evaluates the exact live-handoff expression, then starts a fresh
Elixir process against the saved counter. Deployment tests retain the prior
Nginx/infrastructure proof, frozen native inputs and cursor, reject changed
receipts/ownership, and keep failed activations from resuming normalization.
The subsequent operator-recovered deployment receipt, recorded separately
above, confirms the live deployment completed. The pre-deployment validation
record remains historical evidence and has not been rewritten.
