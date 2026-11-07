# Dev infrastructure patch deployment

Prepared on **7 November 2026**. These scripts have been tested locally; the dev
server has **not** been upgraded by this work. This is the entry point for the
Nginx, Redis and PostgreSQL patch window. Backend and frontend releases keep
their existing images.

## Reviewed scope

| Operation | Compose service recreated | Captured version → target | Image variant |
| --- | --- | --- | --- |
| `nginx` | `proxy` | 1.31.1 → 1.31.8 | Debian 13 / trixie |
| `redis` | `redis-db` | 8.8.0 → 8.8.4 | Alpine 3.23 |
| `postgresql-stats` | `stats-db` | 15.18 → 15.19 | Debian 13 / trixie |
| `postgresql-main` | `db` | 15.18 → 15.19 | Debian 13 / trixie |

The [image catalog](../../deploy/infra/targets.json) pins official Linux AMD64
manifests by digest. The [operator inventory](../../deploy/audits/infrastructure-inventory-2026-11-07.json)
records the current OS families, extensions and Redis persistence policy.
The helper verifies the live inventory again before applying a patch.

Perform the operations in table order, **one at a time**. Prepare the next
operation only after the previous one returns `infrastructure_finished`.
Each preparation inherits the latest verified Compose snapshot and migration
binding. Four preparations made from the original baseline cannot be applied
independently later.

Each operation has a private journal and a rollback image. The helper pulls and
stages the candidate while normalization runs. It then pauses the native writer,
takes the existing operation/build locks, replaces only the selected service,
checks health and continuity, and resumes the same native service. Nginx has a
brief proxy interruption; Redis and PostgreSQL connections are interrupted
during their clean restart. Backend/frontend containers are not recreated.
Existing Ecto and Redix clients are expected to reconnect. A health failure
leaves the migration paused and the operation resumable.

## Install a pinned tools checkout

Run on the **dev Linux server as root**, using its existing Docker Compose
installation. Keep every earlier backend/frontend/native-controller worktree
and all private journals. The new checkout becomes the controller source after
the first successful handoff; keep it clean and pinned too.

The published tag identifies the tested tools commit:

```bash
bash <<'SELECT_INFRA_TOOLS'
set -euo pipefail
git -C /data/blockscout fetch origin tag deriw-infra-patches-2026-11-07
if ! test -e /data/blockscout-v10-infrastructure; then
  git -C /data/blockscout worktree add --detach /data/blockscout-v10-infrastructure deriw-infra-patches-2026-11-07
fi
cd /data/blockscout-v10-infrastructure
test "$(git rev-parse HEAD)" = "$(git rev-parse deriw-infra-patches-2026-11-07^{commit})"
test -z "$(git status --porcelain)"
python3 deploy/scripts/v10_infrastructure_deploy.py --help
SELECT_INFRA_TOOLS
```

## Apply one component

Start with Nginx. The final JSON must say `infrastructure_finished`; its
`normalization` section must report `running`, `waiting`, or `completed`.
`waiting_for_autovacuum` and `long_transaction` are normal automatic safety
pauses. `service_start_requested` alone does not qualify success.

```bash
cd /data/blockscout-v10-infrastructure
bash deploy/steps/dev-v10-infrastructure.sh --run --component nginx \
  --normalization-state /data/blockscout-dev-private/v10-native-normalization/job-20261106T103901Z-59bb4dc6/state.json
```

After it finishes, run the same command with `--component redis`, then
`--component postgresql-stats`, then `--component postgresql-main`. Retain the
different infrastructure `state` path printed by **each** operation.
Run inside an existing terminal multiplexer if the SSH connection is unreliable.
After interruption use the saved state, rather than creating another release.

To stage separately before starting the maintenance interruption:

```bash
bash deploy/steps/dev-v10-infrastructure.sh --prepare --component nginx \
  --normalization-state /data/blockscout-dev-private/v10-native-normalization/job-20261106T103901Z-59bb4dc6/state.json
```

Use the printed infrastructure journal path as `INFRA_STATE`:

```bash
INFRA_STATE=/data/blockscout-dev-private/infrastructure-releases/infra-REPLACE_WITH_PRINTED_ID/state.json
bash deploy/steps/dev-v10-infrastructure.sh --stage --state "$INFRA_STATE"
bash deploy/steps/dev-v10-infrastructure.sh --resume --state "$INFRA_STATE"
```

`--prepare` saves inputs without stopping services. `--stage` starts and removes
an isolated candidate; it does not replace a live service. Both `--resume` and
`--run` can perform the interruption and handoff. If the proxy's origin
certificate needs a private CA, supply `--tls-ca /path/to/ca-bundle.pem` to
`--prepare` or `--run`; it must contain certificates only. TLS verification is
never disabled.

## Status, resume and rollback

From the same pinned tools checkout:

```bash
bash deploy/steps/dev-v10-infrastructure.sh --status --state "$INFRA_STATE"
python3 deploy/scripts/v10_native_normalization.py --status \
  --state /data/blockscout-dev-private/v10-native-normalization/job-20261106T103901Z-59bb4dc6/state.json
systemctl is-active blockscout-v10-native-normalization.service
```

Infrastructure `--status` reads saved files. Native status is also a checkpoint
read; check its timestamp. The native service may be inactive when the job has
completed. Use infrastructure `--resume` to reconcile an interrupted patch or
verify an already finished operation. It never creates a new native job.

For a failed activation, use the same journal:

```bash
bash deploy/steps/dev-v10-infrastructure.sh --resume --state "$INFRA_STATE"
```

To select its original image instead:

```bash
bash deploy/steps/dev-v10-infrastructure.sh --rollback --state "$INFRA_STATE"
```

Rollback preserves the **current data**, including writes made after the patch.
It does not restore an older RDB or PostgreSQL backup. Roll back only the latest
operation. If later operations have finished, an older component rollback needs
a new reviewed recovery plan from the current runtime. This helper deliberately
refuses to apply its old snapshot across newer deployments, even after the latest
operation was rolled back. Rollback completion is `infrastructure_rollback_finished`.
A rolled-back journal cannot be reactivated with `--resume`.

If the main database is unavailable after an attempted replacement, rollback
can stop the owned native client and recreate the original image using the
saved physical database anchor, without first requiring backend database RPC.
After recovery it verifies the database and the latest saved native cursor.
Unknown configuration/container changes, lost data or changed database identity
require review and leave normalization paused. Do not delete volumes/journals,
run Compose `down -v`, run init services or bypass the stopped state.

The qualified full Compose snapshot is stored in the latest infrastructure
journal and authenticated by the native job's runtime binding. Use these updated
tools for subsequent frontend releases: they inherit that snapshot. A generic
Compose `up` or an older deployment helper can select old image declarations and
invalidate the handoff; it is not the continuation command for this rollout.

## What remains preserved

The immutable native `inputs.json`, `native.exs`, token, database endpoint,
schema versions and heap identity are retained. The native framework continues
to own its database cursor. A committed batch whose cursor was not saved can
be replayed idempotently. The host journal is refreshed from the actual database
record during handoff; a completed migration remains completed. Its unit is
updated without starting new processing.

The existing 200 GiB reserve and pause210/resume220 GiB policy remain. There is
no cumulative growth cap. Global automatic migration startup, the metrics hold,
the empty-block sanitizer hold and deferred index/primary-key work stay as
previously configured. No historical duplicate reindex is rerun.

PostgreSQL keeps its original bind mounts, custom UID/GID, tuning, shared
memory, network addresses, cluster identifier and database OID. An entrypoint
guard requires an existing PG15 control file and refuses empty data. Both init
**declarations** inherit their selected database image; init containers are never
executed. Extension versions and collation-library signatures must match.
The 15.19 float GiST/ltree and custom decoder cases stop for separate review;
the tool does not reindex or run extension/schema migrations. Same-major
15.x updates do not require dump/restore, subject to the release-note caveats.
[PostgreSQL 15.19 notes](https://www.postgresql.org/docs/release/15.19/)

Redis is qualified for the inventoried standalone RDB mode, with AOF disabled.
It saves and stops cleanly, archives the final data privately with a checksum,
and checks RDB compatibility before either binary starts. A changed persistence
policy stops preparation. [Redis 8.8.4 notes](https://github.com/redis/redis/releases/tag/8.8.4)

Nginx retains the loaded configuration and referenced certificates/auth files
as private immutable artifacts. Its entrypoint loads these directly. Future
certificate/configuration changes must update this pinned selection through a
reviewed deployment; changing the former template directory alone will not
replace frozen files. Staging verifies HTTP, trusted HTTPS, deposits, backend
and stats APIs, websocket101 and source-map restrictions.
[Nginx changes](https://nginx.org/en/CHANGES)

The original upgrade's recorded recovery policy remains **operator manual
recovery with an unverified backup**. This patch tooling does not create or
qualify a full PostgreSQL backup, restore terabytes automatically, or change
that prior policy. Original images, data directories and journals are retained.
The independent bridge metrics exporter may report temporary unavailability
during a database restart; its pinned checkout and service are preserved.

## Validation and limits

The [validation record](../../deploy/audits/infrastructure-validation-2026-11-07.json)
records the final tests and image digests. Disposable local fixtures exercise
real old/new binaries, upgrade and rollback, Redis persistence, Nginx route
checks, and the unchanged native worker across a PostgreSQL restart. The
PostgreSQL test interrupts execution after data commit but before cursor save,
then proves the same job completes without duplicate updates. Existing Ecto
and Redix clients reconnect without container restarts. An empty PG directory
is explicitly refused.

These are synthetic data tests, not a full-size database restore, production
load test, downtime forecast or guarantee of remote TLS/RPC availability. Live
qualification occurs before the migration is resumed after each operation.
