# Deriw dev v10 deployment and operations

This is the current entry point for the dev explorer, updated **7 November 2026**.
The v10.2.6 backend and bridge fix are deployed. Historical internal-transaction
normalization continues as a resumable systemd job. Component patch updates and
the deferred index/primary-key work are separate follow-ups.

The [frontend v2.7.2 rollout](frontend-v2.7.2.md) has a separate targeted helper.
After that rollout, its new worktree hosts the same native normalization job's
controller. Preserve the earlier backend/controller checkouts and private journals.

## 1. Select the right checkout

| Checkout on the dev server | Purpose | Required revision |
| --- | --- | --- |
| `/data/blockscout-v10-bridge-rehearsal` | Active release and native controller | `875082f41d79ef72cd9dd9a4e771953a181c6cf2` |
| `/data/blockscout-v10-rehearsal` | Original native job source/evidence | `8362025e323403b846de375ffb8b527b38a78cb7` |
| `/data/blockscout-v10-ops` | New read-only command/guide checkout | The reviewed documentation/tooling release commit |

Keep the first two checkouts pinned. The native service verifies its source
commit and runtime binding. Checking out the newest branch tip in its controller
directory can stop further work. Source publication does not deploy an image.

Install these new read-only tools in a **third worktree**. On the server, use
`OPS_COMMIT` for the reviewed commit supplied with this documentation update:

```bash
OPS_COMMIT=REPLACE_WITH_REVIEWED_COMMIT
[[ "$OPS_COMMIT" =~ ^[0-9a-f]{40}$ ]]
git -C /data/blockscout fetch origin release/v10-bridge-deploy
git -C /data/blockscout worktree add --detach /data/blockscout-v10-ops "$OPS_COMMIT"
cd /data/blockscout-v10-ops
```

For an existing ops worktree, verify it is clean before explicitly checking out
another reviewed tooling commit. None of this requires a backend restart.

The shell files below run with `bash`, so they do not depend on executable-bit
preservation or interactive zsh comment settings.

## 2. Confirm production source consolidation

Every commit in the reviewed `origin/deriw-prod` is already contained in the
release history. There is no additional production merge to replay.

| Lineage evidence captured on 6 November | Commit/count |
| --- | --- |
| Production tip | `ddfedcadd25e07367c6133d10a44e7fde4d3898e` |
| Deployed release tip | `875082f41d79ef72cd9dd9a4e771953a181c6cf2` |
| Merge base | The production tip above |
| Production commits absent from release | **0** |
| Production custom commits after upstream v9.0.1 | 29, including two merges |

The [custom-change reconciliation](../upgrades/v10.2.6-custom-changes.md)
classifies all 29 commits. It preserves surviving Deriw settings/repairs, ports
changes that conflict with upstream v10, and retains the effect of historical
reverts. Reapplying reverted experiments would change the intended result.
The [saved source report](../../deploy/audits/source-consolidation-2026-11-06.json)
records this reviewed baseline.

Refresh local refs when you want a new comparison, then run the check from the
new tools checkout (on the Mac or server):

```bash
git fetch origin deriw-prod release/v10-bridge-deploy
bash deploy/ops.sh source-check
```

This reads local Git history and the reconciliation table. It returns exit 2 if
production commits are missing or new custom commits lack classification. It
does not merge, fetch, reset, or rewrite history itself. Production ancestry
does not mean a production environment is configured or deployed.

## 3. Read current progress

Run these on the **dev server**, from `/data/blockscout-v10-ops`:

```bash
bash deploy/ops.sh status
bash deploy/ops.sh bridge
bash deploy/ops.sh components
```

Or run the complete review step file once:

```bash
bash deploy/steps/dev-v10-review.sh
```

All these tools are read-only. They use the existing native job by default and
accept `--state /data/.../state.json` for an existing job. `status` reads its saved
checkpoint plus systemd state. `bridge` reads the active backend's tracker state;
`components` inspects images and bounded version probes without pulling images.
They can run while normalization owns the maintenance lock.

To watch bridge progress, run `watch -n 30 bash deploy/ops.sh bridge`. Pressing
Ctrl-C stops the display only; it does not stop indexing or normalization.
For native progress use `watch -n 30 bash deploy/ops.sh status`.

| Field | Meaning |
| --- | --- |
| `native_cursor` | Descends through historical block ranges. It is not a row count or completion percentage. |
| `native_normalization_running` | The host is authorizing batches. |
| `native_normalization_waiting` | Automatic pause; read `reason`. It resumes when the condition clears. |
| `native_worker.last_batch_updated_rows` | The latest batch only, not a cumulative total. A worker status can lag the host's gate. |
| `observed_at_utc` | Checkpoint freshness. An active service alone does not establish forward progress. |
| Bridge `next_parent_block` | Next parent-chain range to scan. Compare two observations to confirm movement. |
| Bridge `historical_boundary` | Separate historical boundary; it is not the current parent-chain head. |
| `normalization_complete=true` | Native data conversion and full residual check passed. Deferred index/PK work remains separate. |

The bridge cursor advanced from 278161947 to 278163347 between 14:03 and 14:07
UTC on 6 November. That proves scanning during that interval. It does not prove
all deposits have recovered their parent transaction/block/time. Confirm those
fields through `/api/v2/arbitrum/messages/to-rollup` and `/deposits` as the tracker
catches up. No full-table completion percentage or ETA has been established.

## 4. Automatic migration, waiting and recovery

The job uses the native `Explorer.Migrator.FillingMigration` checkpoint framework
and `EmptyInternalTransactionsData.update_batch/1`. It runs one writer in a
separate container and persists its cursor in `migrations_status`. Replaying an
interrupted batch is idempotent. Systemd restarts a crashed controller; it also
starts on boot, subject to unchanged runtime identity and restored worker holds.

There is **no cumulative disk-growth cap**. New batches pause below 210 GiB free
and resume at 220 GiB. The 200 GiB reserve remains. PostgreSQL autovacuum handles
routine cleanup; conversion waits on estimated dead rows over 10 million,
unhealthy dependencies, disabled autovacuum, or client transactions over 300s.
No routine manual batch/vacuum commands are needed.

For `long_transaction`, inspect metadata without canceling anything:

```bash
bash deploy/ops.sh diagnose
```

At the last operator observation (13:57 UTC), normalization was waiting at
130156230 on two long backend reads. This is dated evidence, not a current live
assertion. Review the owner/caller before choosing any query cancellation or
application fix; repeatedly killing queries does not fix their source.

For an intentional pause or restart after resolving `needs_attention`, use the
**pinned active controller checkout**, not the newer ops checkout:

```bash
cd /data/blockscout-v10-bridge-rehearsal
python3 deploy/scripts/v10_native_normalization.py --pause --state /data/blockscout-dev-private/v10-native-normalization/job-20261106T103901Z-59bb4dc6/state.json
python3 deploy/scripts/v10_native_normalization.py --start --state /data/blockscout-dev-private/v10-native-normalization/job-20261106T103901Z-59bb4dc6/state.json
```

These are alternatives: use `--pause` to stop, `--start` to resume the same job.
Automatic `waiting` requires neither. Do not delete journals or reset migration
metadata. Preserve `/data/blockscout-dev-private`, database backups, and both
source worktrees. A backend/DB/container change requires a reviewed runtime
handoff, including restoring the metrics and empty-block sanitizer holds.

The [native job runbook](../upgrades/v10.2.6-native-normalization.md) documents
batch/lease limits and the original installation; it is not an instruction to
prepare another job on this server.

## 5. Deployment and rollback commands for this installed release

The bridge deployment is already active. Its exact state is:

`/data/blockscout-dev-private/releases/release-20261106T114924Z-daebdb61/state.json`

For full read-only release verification, use its pinned checkout:

```bash
cd /data/blockscout-v10-bridge-rehearsal
python3 deploy/scripts/v10_bridge_deploy.py --status --state /data/blockscout-dev-private/releases/release-20261106T114924Z-daebdb61/state.json
```

The original rollout procedure is in the
[bridge deployment runbook](../upgrades/v10.2.6-bridge-deployment.md). Its `--run`
and `--resume` are specific to that reviewed application change and creation
commit; they are not a general deployment command for a newer branch tip. Do not
rerun `--run` from the ops checkout: it can pause the job before rejecting the
changed source. The saved forward/rollback snapshots are recovery evidence;
any rollback needs the native checkpoint/holds/runtime binding reconciled too.
There is no documented one-command bridge-plus-native rollback shortcut.

For the **next application or component deployment**, follow this sequence:

1. Read status, fresh image/version inventory and long-transaction diagnostics.
   Save exact source and pullable registry digests plus private recovery inputs.
2. Build/test candidates on an isolated stack or database clone. Select explicit
   versions/digests and retain current working images/configuration for rollback.
3. Prepare a deployment-specific handoff that pauses this exact native job,
   waits for its owned writer to exit, and records its DB cursor/token/identity.
4. Recreate only reviewed services. Keep chain ID, volumes, PostgreSQL tuning,
   proxy templates/TLS assets and private inputs. Never replay duplicate reindex
   or unrelated schema migrations as part of a component patch.
5. Verify APIs, schema/heap/cursor identity, all worker holds and service-specific
   behavior; bind the same native job to the verified runtime before resuming.

That future handoff is not implemented by these read-only tools. Changing any
bound frontend/proxy/Redis/DB/etc container without one invalidates the native
controller's strict runtime evidence. Schedule the reviewed component patches
using the [component audit](component-audit-2026-11-06.md).

## 6. What is complete and what remains

| Work | State |
| --- | --- |
| Production history included in release | Complete for production tip `ddfedcadd` |
| v10.2.6 code and 29 reviewed schema migrations | Deployed and verified |
| Bridge historical-boundary fix | Deployed; forward scanning observed |
| Native normalization automation | Installed, resumable; completion still pending |
| Deposit origination metadata recovery | Still needs representative API/UI confirmation |
| Unique index, NOT NULL, PK and two custom index migrations | Deferred; separate review after conversion |
| Component patch deployment | Audited and planned; not applied by this documentation change |
| Test/prod configuration and rollout | Not configured/authorized by dev ancestry proof |

`./deploy.sh dev build/deploy` still enforces the earlier v9 profile contract;
do not use it as the v10 entry point. The captured dev manifest/image IDs are
historical baselines; active release journals supply the current image context.
Do not rewrite those baselines to look current. Generic Compose examples select
PostgreSQL 17 in places, while the actual dev overrides use PostgreSQL 15. Do
not copy generic service defaults over this installation or run a blanket
`docker compose pull/up --build` against moving tags.
