# DeriW dev deployment and operations

Start here for the existing dev explorer. Updated **8 November 2026**.
The backend, frontend, bridge checkpoint fix and infrastructure patches are
already deployed. Publishing the consolidated source does not redeploy them.

## Current selection

| Component | Qualified selection | Current action |
| --- | --- | --- |
| Backend / deposit tracker | Blockscout **10.2.6**, durable deposit scan checkpoints | Keep the deployed image |
| Frontend | **2.7.2-deriw.4**, DeriW footer: three columns, two links per column | Keep the qualified image |
| Nginx | **1.31.8**, duplicate servers removed | Keep the saved configuration and certificate |
| Redis | **8.8.4** | Keep persistence and the existing data mount |
| Main / stats PostgreSQL | **15.19** | Keep both clusters, extensions and tuning |
| NFT worker | **10.2.6** | Compatible worker retained |
| Stats / verifier / signature provider | **2.19.1 / 1.11.1 / 1.1.4** | Audited images retained |
| User-ops indexer / visualizer | **1.4.3 / 0.2.1** | Audited images retained |

These are recorded release selections, not a fresh claim about the running
server or newest upstream versions. The [release manifest](../../deploy/release-manifest.json)
records immutable image references and source provenance. Init containers remain
stopped; their historical image IDs can differ from the updated declarations.

## Daily checks: one step file

From a separate checkout of this consolidated release on the **dev server**:

```bash
bash deploy/steps/dev-v10-review.sh
```

It checks source consolidation, migration status, configuration, bridge scanning
and component versions. It is read-only: no pull, restart, migration or config
write. It stops on a failed check. Individual commands are:

```bash
bash deploy/ops.sh status
bash deploy/ops.sh bridge
bash deploy/ops.sh config-check
bash deploy/ops.sh components
bash deploy/ops.sh diagnose
python3 deploy/scripts/deriw_bridge_metrics.py --once
```

`config-check` authenticates the saved deployment selection and native binding,
compares declared environment values with running containers, checks bind-source
presence, and detects changes during observation. Values and private paths are
withheld. This is a consistency check, not a backup or database-health test.

For progress, compare two readings or use `watch -n 30 bash deploy/ops.sh status`.
The normalization cursor descends; the deposit scan cursor ascends. Neither is
a completed-row percentage. The metrics JSON reports deposits, withdrawals and
the processed/sent/confirmed rollup stages. Counts describe indexed records;
they do not prove lifetime on-chain completeness.

`waiting_for_autovacuum` and `long_transaction` are automatic waits. They retain
the checkpoint and resume when the condition clears. A worker's last batch can
still say running while its host has paused new batches. `completed` requires
both native conversion and the final residual check. Deferred index and primary
key migrations remain a separate task.

## Install this guide and checks without changing the running stack

Run once in the **dev server terminal**:

```bash
bash <<'INSTALL_DERIW_OPS'
set -euo pipefail
git -C /data/blockscout fetch origin deriw-prod
git -C /data/blockscout fetch origin tag deriw-v10-consolidated-2026-11-08
if ! test -e /data/blockscout-v10-ops-consolidated; then
  git -C /data/blockscout worktree add --detach /data/blockscout-v10-ops-consolidated deriw-v10-consolidated-2026-11-08
fi
cd /data/blockscout-v10-ops-consolidated
test "$(git rev-parse HEAD)" = "$(git rev-parse deriw-v10-consolidated-2026-11-08^{commit})"
test -z "$(git status --porcelain)"
bash deploy/steps/dev-v10-review.sh
INSTALL_DERIW_OPS
```

Keep `/data/blockscout-v10-bridge-checkpoint` pinned at
`e7a16ead2bcf53da05b6e059da616ebacddbf22d`: it hosts the native controller.
The metrics service uses `/data/blockscout-bridge-metrics`. Do not move either
service to this documentation checkout. The earlier checkouts are still needed
by authenticated recovery records; do not delete or repoint them.

## Configuration stays on the server

The authoritative deployment is the **latest authenticated private Compose
snapshot**, reached through the native job's runtime binding. It includes the
actual selected images, environment, commands, mounts, ports and networks.
The repository Compose files are the historical adoption baseline, not a
replacement for that selection. Do not copy example env files over live files,
run a generic `docker compose up`, or select mutable `latest` images.

Retain and back up `/data/blockscout-dev-private`, the original server Compose
files, externally mounted configuration/certificates, and every source checkout
referenced by a release or service. Private release directories use 0700 and
files use 0600. Credentials and resolved configurations stay out of Git.
Database/RDB recovery backups are separate; source consolidation does not create
or verify a recoverable data backup.

The targeted helpers save private inputs and rollback selections before service
changes, inherit the current qualified snapshot, and recreate only the selected
service. Nginx configuration and certificate artifacts are hashed and preserved.
Frontend startup regenerates its own sprite metadata and validates migrated
settings before activation. The helpers retain the native cursor/token and
restore the worker holds before resuming the same job. Changed ownership,
configuration or failed health stops the operation for review.

There is no cumulative disk-growth cap. New native batches pause below 210 GiB
free and resume at 220 GiB; the minimum reserve is 200 GiB. Automatic migration
startup remains disabled; only the selected normalizer is authorized. The two
heavy schema migrations, historical duplicate reindex and primary-key work are
not started by these scripts.

## Deployment or recovery

No rollout is needed merely to adopt this source squash. For a later change,
select and test a new immutable candidate, then use the matching targeted guide:

| Change | Guide / command entry |
| --- | --- |
| Frontend image | [Frontend](frontend-v2.7.2.md), `deploy/steps/dev-v10-frontend.sh` |
| Infrastructure patch | [Infrastructure](infrastructure-patches.md), `deploy/steps/dev-v10-infrastructure.sh` |
| Installed bridge checkpoint fix | [Bridge checkpoint](bridge-checkpoint.md), its pinned creation checkout |
| Metrics endpoint | [Bridge metrics](deriw-bridge-metrics.md), `deploy/steps/deriw-bridge-metrics.sh` |

Completed upgrades are not commands to rerun. The bridge checkpoint helper is a
one-time transition from the earlier backend; it is not a generic backend update
command. Use each saved journal's creation checkout for `--resume`, `--status`
or `--rollback`; the consolidated source does not rewrite its frozen source
identity. A rollback changes the selected binary/settings, never restores data
or resets a native checkpoint automatically.

For an intentional native pause or restart after resolving `needs_attention`:

```bash
cd /data/blockscout-v10-bridge-checkpoint
python3 deploy/scripts/v10_native_normalization.py --pause --state /data/blockscout-dev-private/v10-native-normalization/job-20261106T103901Z-59bb4dc6/state.json
```

Use `--start` with the same state to resume intentionally; automatic `waiting`
needs neither command. Keep all journals. See [source consolidation](source-consolidation.md)
for the two repository commits, provenance and future deployment boundaries.
A new server or production environment needs its own inventory/private settings
and recovery plan; the fixed dev paths and journals cannot be reused there.
