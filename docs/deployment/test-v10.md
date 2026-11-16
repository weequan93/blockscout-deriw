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
