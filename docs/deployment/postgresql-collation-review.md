# Continue PG15 patches after collation review

Current window update: stats and main PostgreSQL have both finished at `15.19`,
and normalization has resumed. The main journal's
[health/sanitizer recovery](postgresql-health-recovery.md) completed without
another database restart. The pinned commands below document the original `r6`
preparations; keep that creator checkout intact and retain both completed journals.

The stats PostgreSQL preparation stopped before creating a release journal or
changing services. The existing native normalization service was not paused.
Redis `8.8.4` is already verified by its finished journal; keep that journal and
every earlier controller checkout.

The operator's catalog review found the same state in `stats-db` and `db`:

- Database default: libc `en_US.utf8`, recorded and actual version both `2.41`.
- 808 named collations have older recorded library versions: 806 ICU and two
  libc entries.
- No direct dependencies, pinned collations, relation attributes, index keys,
  partition keys, types or ranges reference those mismatched entries.
- No non-system function source mentions `COLLATE`.

These are unused named collation records. This review does not identify indexes
that need rebuilding for this mismatch. It is not a general integrity check of
every stored value or index. PostgreSQL's
[collation guidance](https://www.postgresql.org/docs/15/sql-altercollation.html)
requires rebuilding dependent objects when a used collation changes; refreshing
the catalog version alone does not perform or verify that work.

## What r6 checks

Revision `r6` repeats the complete catalog review in the same short read-only
transaction as the PostgreSQL preflight. Unused named mismatches are admitted
only when the default matches, every required dependency/use count is present
and zero, the count matches the observer, and no unresolved function source
mentions `COLLATE`. Incomplete evidence still stops. A default mismatch or any
referenced named mismatch still requires separate review.

The staging check also requires the candidate's actual collation-library
signatures to equal the current runtime. Cluster identity, database OID,
extensions, data mount, image version, startup policy, native token/cursor and
runtime checks remain in place. The dependency review is repeated before
replacement, after restart and during the final handoff. Explicit later release
verification repeats it too; periodic native health checks retain their existing
application/checkpoint scope. Any reference found by the review stops the patch
rather than silently admitting an earlier saved review.

The patch does **not** run `ALTER COLLATION`, `ALTER DATABASE ... REFRESH COLLATION
VERSION`, `REINDEX`, a restore or schema migrations. Older unused catalog
versions are retained. The private PostgreSQL inputs and public release JSON
include the review counts; `collation_versions_refreshed` and `reindex_performed`
remain false. Future use of an old named collation requires its own review.

## Upgrade stats PostgreSQL first

Run this complete block on the **dev server as root**. The prior stats attempt
printed `state: null`, so this is a fresh preparation from the current verified
Redis binding. Do not alter an older checkout to install the new tools.

```bash
bash <<'UPGRADE_STATS_POSTGRESQL'
set -euo pipefail
git -C /data/blockscout fetch origin tag deriw-infra-patches-2026-11-07-r6
if ! test -e /data/blockscout-v10-collation-review; then
  git -C /data/blockscout worktree add --detach /data/blockscout-v10-collation-review deriw-infra-patches-2026-11-07-r6
fi
cd /data/blockscout-v10-collation-review
test "$(git rev-parse HEAD)" = "$(git rev-parse deriw-infra-patches-2026-11-07-r6^{commit})"
test -z "$(git status --porcelain)"
bash deploy/steps/dev-v10-infrastructure.sh --run --component postgresql-stats \
  --normalization-state /data/blockscout-dev-private/v10-native-normalization/job-20261106T103901Z-59bb4dc6/state.json
UPGRADE_STATS_POSTGRESQL
```

Expect `infrastructure_finished`, `target_version: 15.19`, the zero-use review,
and normalization `running`, `waiting` or `completed`. The helper pauses the
native writer for the service restart and resumes the same saved job after
health and checkpoint continuity are verified. Backend/frontend containers
remain unchanged. Retain the new PostgreSQL journal printed by this operation.

If the operation stops after printing a state path, use `--resume --state` with
that same path from the same pinned `r6` checkout. If recovery requires the old
image, use `--rollback --state` on that journal. Neither action restores older
data or resets native progress. See the [infrastructure guide](infrastructure-patches.md).

## Upgrade main PostgreSQL afterward

Only after the stats operation finishes and its native handoff is verified:

```bash
cd /data/blockscout-v10-collation-review
bash deploy/steps/dev-v10-infrastructure.sh --run --component postgresql-main \
  --normalization-state /data/blockscout-dev-private/v10-native-normalization/job-20261106T103901Z-59bb4dc6/state.json
```

This prepares from the newly verified stats binding. Keep its separate journal
and final JSON too. The existing 200 GiB reserve / pause210-resume220 policy
remains; there is no cumulative growth cap. All deferred index/primary-key work
and other migrator holds remain as before.

Nginx still needs the [origin certificate replacement](origin-certificate.md)
and a fresh preparation from the latest verified runtime.

## Validation and limits

The [validation record](../../deploy/audits/postgresql-collation-validation-2026-11-07.json)
includes the operator's zero-dependency evidence, unit coverage for missing or
positive evidence, and real PG15.18 → 15.19 → 15.18 tests preserving unused
collation metadata. Used indexes, columns, partition keys and SQL function
source, as well as default mismatches, are explicitly rejected in the fixture.
The original native migration fixture also exercises checkpoint replay and
continuation through the binary patch and rollback.

These tests use synthetic local data. The scripts still qualify the actual
server runtime and checkpoint after each patch. The catalog review cannot prove
the behavior of arbitrary dynamically assembled SQL; its source check rejects
explicit `COLLATE` in non-system functions, and the reviewed application/runtime
remain pinned. No broad application-level collation change is authorized here.
