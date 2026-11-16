# Upgrade the existing DeriW test explorer

Target: **https://explorerv2.test.deriw.com/**, Arbitrum chain **2885**.
The 16 November 2026 inventory identified Blockscout **9.0.1**, PostgreSQL **15**,
Compose project `docker-compose` and the source checkout `/data/blockscout`.
The operator confirmed an existing recent database backup or snapshot.

The target application is the dev-qualified **10.2.6** release at
`f0f40cd352d26db5c1f4743f18ad110e85a3c4af`, including native bridge metrics and
durable deposit/withdrawal scan progress. This is a database-schema upgrade,
so the dev metrics-only or catchup deployment scripts cannot perform it.
Those helpers also bind to a different chain ID and dev-only normalization state.

## Inspect the existing database and configuration

Run this on the test server. Fetching the tool does not switch branches, pull
into the checkout or replace server configuration:

```bash
bash <<'INSPECT_TEST_V10'
set -euo pipefail
umask 077
cd /data/blockscout
git fetch origin deriw-dev
test_tools_revision=$(git rev-parse FETCH_HEAD)
test_preflight_script=$(mktemp /tmp/deriw-test-v10-preflight.XXXXXX)
git show "${test_tools_revision}:deploy/scripts/testnet_v10_preflight.py" > "$test_preflight_script"
python3 -I "$test_preflight_script" --repo /data/blockscout
INSPECT_TEST_V10
```

The tool checks chain 2885 before reading database metadata or creating private
snapshots. It uses existing Repo connections with read-only transactions,
1.5-second statement deadlines and bounded catalog/empty-table queries. It
does not count or scan all historical transactions. It reports:

- Actual PostgreSQL version, database size and estimated relation row counts.
- Pending versions for the main, account and Arbitrum repositories when they
  share the main database configuration; separate routes remain explicitly unknown.
- Internal-transaction types, key columns/indexes and selected migration states.
- Sizes of tables affected by the initial schema phase and whether the selected
  drop/export tables have rows.
- Data, WAL and tablespace filesystem capacity when the backend's database
  connection matches the inspected `db` container. Shared device free space
  must not be added together.
- Current images, container states, platform and Compose configuration availability.
- Readable live bridge cursors/ranges and selected backend feature flags; parent
  RPC configuration is reported only as present/absent, without its URL.

Container metadata, resolved Compose/environment values, original Compose files,
tracked local edits and database observations are saved under a new mode-0700
directory beneath `/data/blockscout-test-private/`, with mode-0600 files.
Only the sanitized JSON report should be shared. Passwords, RPC URLs, database
addresses, raw command/error output and data/WAL paths stay private. The tracked
diff and source filename inventory do not back up untracked source file contents.

The tool leaves the checkout, database schema, workers and containers unchanged.
Configuration snapshots do not create or verify a database backup; the report's
`database_backup_created=false` means this inspection created none. The existing
operator-confirmed backup remains the recovery provision. `deployment_ready=false`
identifies this as an inspection report, rather than an activation command.

## Use the report for the migration stage

The initial schema phase must be separated from historical filling, heavy index
creation and primary-key conversion. The report distinguishes the reviewed
upstream migration cutoffs from the two later DeriW covering-index migrations.
An initial schema resource comparison does not establish enough working space
for the historical conversion. Preserve PostgreSQL 15, all current test RPC and
rollup addresses, data mounts, domain configuration and a private copy of the
original images/configuration when preparing the actual release.

The existing frontend is tagged `quanquanah/blockscout-frontend:prod`; the public
runtime settings identify the test network, so the tag itself is not a network
identity. `user-ops-indexer` was already restarting in the supplied inventory;
record that condition separately when assessing the backend release.

The checked-in preflight was verified with synthetic data through the cached
Blockscout **9.0.1** release and disposable PostgreSQL **15**, including rejected
write attempts and enforced statement deadlines. Python fixtures check wrong-chain
rejection, separate repository routing, private-value preservation and a complete
read-only container/configuration capture.

## Deploy the qualified backend

The supplied preflight selected a 13.37 TB main database, a 6.20 TB historical
internal-transaction relation and approximately 1.27 TB filesystem free space.
Its initial migration relations totalled 490,905,600 bytes, the internal types
matched the path that avoids a heap rewrite, and all selected drop/export
tables were empty. The backend's test RPCs, contracts and data mounts are the
configuration authority; the application revision is shared with dev.

`deploy/scripts/deploy_testnet_v10.py` implements this specific initial upgrade.
Fetch the reviewed deployment-tool revision and extract its `deploy/scripts`
and `deploy/upgrades` directories into a new mode-0700 directory under
`/data/blockscout-test-private/`. Run its Python entry point with `-I` and:

```text
--repo /data/blockscout
--preflight /data/blockscout-test-private/preflight-20261116T012142Z-2y46fkhi
--existing-backup
```

`--existing-backup` records the operator's previously supplied backup/snapshot
confirmation. It does not create or verify a recovery copy. `--prepare-only`
optionally stops after the image and configuration checks, before the cursor
handoff or any service stop. The deployment does the following:

1. Compare the preflight container identities, environments and backend
   attachments with the current stack. Require the same test chain, original
   PostgreSQL route, reviewed schema path and resource reserves.
2. Build `deriw-blockscout:test-v10.2.6-f0f40cd352d2` from a Git archive of the
   exact qualified application commit. The server's `deriw-pre` checkout and
   local environment files do not enter the build or change branches.
3. Retain backend runtime configuration and numeric UID/GID. Enable native
   bridge metrics, hold automatic background migrations, the empty-blocks
   sanitizer and uncataloged token scans, and keep the heavy indexer metrics
   collector disabled. Preserve the current internal-transaction fetcher policy.
4. Probe the actual test parent RPC using stored event anchors: at most 68
   sequential `eth_getLogs` reads, at most four per second, with completeness
   checks. Only a passing probe enables 10,000-block deposit/withdrawal requests;
   otherwise retain the original ranges. The original polling intervals stay.
5. Read v9 deposits from `data.new_msg_to_l2_start_block` and withdrawals from
   the buffered task state. Save monotonic, parent-chain/contract-scoped cursors
   in `last_fetched_counters` before stopping the backend. A failed handoff
   prevents the stop and schema phase.
6. Disable automatic restart of the old backend and stop that container only.
   Recheck the original database/heap, then run the 26 main, two account and
   one Arbitrum migration versions. Each statement has a 120-second server
   deadline and a two-second lock deadline. Monitor a 100 GiB remaining-free
   reserve and a 20 GiB schema-phase consumption budget.
7. Retain the legacy internal-transaction heap and primary key. Leave the two
   custom covering indexes (`20260721000000`, `20260812000000`), historical
   normalization and heavy index/primary-key workers deferred.
8. Write a private, mode-0600 resolved Compose snapshot at
   `/data/blockscout/docker-compose.test/docker-compose.yml`. Its bind mounts
   still point at their original paths; no database directory is moved or
   copied. The original Compose files stay in place. If the old branch does
   not ignore this private directory, add a local `.git/info/exclude` rule.
9. Recreate only `backend` with the built image and a start-only command.
   Verify readiness, chain/version/source, held migrators, resumed scoped
   cursors/ranges/intervals, all five native database metric sections and all
   seven on-chain verification sections. Other service identities, images,
   configuration and mounts must remain unchanged.

The backend API is unavailable while its old container is stopped and the
bounded schema change/new startup run. PostgreSQL, proxy, frontend, stats and
the separately managed Prometheus exporter stay in place. This procedure does
not change host metric port bindings or the already restarting user-ops service.
The final successful report is `phase="test_v10_deployed"` and includes the
private state path, source revision and verified cursors.

## Resume and recovery

Every stage is recorded atomically in a mode-0600 `state.json` within the private
upgrade directory. Build, client, Compose and metric output remain in private
logs. On a failure, share only the sanitized `STOP:`/phase report. Resolve the
reported condition before resuming the **same** prepared journal:

```bash
test_v10_state=/data/blockscout-test-private/upgrade-REPLACE/state.json
test_v10_tool=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["tool_script"])' "$test_v10_state")
python3 -I "$test_v10_tool" --resume "$test_v10_state"
```

A journal that did not finish image/configuration preparation cannot activate.
An interrupted schema phase checks the same database, applied-version subset,
heap and selected migration statuses before applying only the remaining
versions. It removes only leftover schema clients belonging to that journal.
After a completed schema stage, retries verify/start the prepared backend
without repeating that stage.

After schema writes begin, do not start the old v9 backend or use its original
startup migration command as an automatic rollback. The script leaves the old
backend's restart policy disabled on failure. The existing database backup or
snapshot and the captured old image/configuration are the recovery provision;
restoring v9 requires restoring the corresponding database state too. The new
v10 backend may remain running if a post-start metrics verification fails; its
private logs distinguish this from a migration failure.

## Validation and scope

Run the Python control-flow fixtures and the integration fixture:

```bash
python3 -m unittest discover -s deploy/tests -p 'test_deploy_testnet_v10.py' -v
python3 deploy/tests/check_testnet_v10_upgrade.py
```

The integration uses the exact f0 release image, cached v9.0.1 and disposable
PostgreSQL 15 with synthetic rows, internal Docker networking, tmpfs storage,
no host mounts and no published ports. It exercises legacy cursor extraction,
scoped monotonic persistence, the test-adapted v9 RPC probe, wrong-chain
rejection, a deliberately blocked account migration after the main schema,
partial/repeated migration runs, unchanged heap and selected migration statuses,
deferred indexes, real test-chain backend startup and native database metrics.
Representative block/transaction/internal-transaction API routes run against
the retained legacy primary key. Compose round-tripping retains literal
dollar signs and newlines. Full-application fixture bridge/block fetchers are
paused and the parent transport is synthetic; the server's bounded RPC probe
and post-start verification cover the actual test provider. This is not a
full-size database rehearsal or proof of snapshot restoration.
