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

## Deploy with the same runtime layout as dev

The operator selected the dev layout for test too: **source on `deriw-dev`,
configuration and data together under `docker-compose.test`**. This replaces
the earlier backend-only deployment design in `21c452495`. The operator
confirmed that earlier command was not executed. The public entry point
`deploy/scripts/deploy_testnet_v10.py` now delegates to `testnet_v10_layout.py`.
The v9 preflight and the qualified f0 application revision remain applicable.

The supplied preflight identified a 13.37 TB main database, a 6.20 TB historical
internal-transaction relation and approximately 1.27 TB filesystem free space.
The initial migration relations totalled 490,905,600 bytes; the internal types
matched the path that avoids a heap rewrite; selected drop/export tables were
empty. This authorizes the reviewed initial schema path, not historical
normalization or large index/primary-key conversion.

Fetch the deployment tools without first switching the server checkout:

```bash
bash <<'UPGRADE_TEST_V10'
set -euo pipefail
umask 077
cd /data/blockscout
git fetch origin deriw-dev
test_tools_revision=$(git rev-parse FETCH_HEAD)
test_tools_directory=$(mktemp -d /data/blockscout-test-private/tools-XXXXXXXX)
git archive "$test_tools_revision" deploy/scripts deploy/upgrades |
  tar -xf - -C "$test_tools_directory"
python3 -I "$test_tools_directory/deploy/scripts/deploy_testnet_v10.py" \
  --repo /data/blockscout \
  --preflight /data/blockscout-test-private/preflight-20261116T012142Z-2y46fkhi \
  --existing-backup
UPGRADE_TEST_V10
```

`--existing-backup` records the already supplied backup/snapshot confirmation.
It does not create or verify a recovery copy. `--prepare-only` optionally builds
and validates the plan without stopping services, moving directories, changing
branches, saving live cursors or executing migrations.

The initial preparation requires the original `docker-compose` directory and
an absent `docker-compose.test`. It compares the captured containers/settings,
all service mounts, ports and networks, and confirms that DB/Redis writable
binds and the inspected main data path are inside the original runtime tree.
Symlinked bind sources, nested host filesystem mounts, another container using this tree, tracked database
files, source edits outside the runtime tree, a diverged local `deriw-dev` or
another worktree owning that branch stop preparation. Unrelated services are
not adopted automatically. Existing private configuration stays on the server.

Existing internal Compose links such as `db:database` are retained. Their peer
containers, shared networks and aliases are checked against Docker metadata
(both legacy host links and endpoint links). Missing aliases, unexpected peers
and undeclared links stop preparation. Schema clients receive the same database
alias, resolved against the current `db` container each time, so a database URL
using `database` works before and after container recreation. This corrects the
overly broad link rejection in `1e653dc11`, which stopped before any service or
schema change on the supplied test deployment.

The release performs these stages:

1. Build `deriw-blockscout:test-v10.2.6-f0f40cd352d2` from a Git archive of the
   exact qualified application commit while the old backend runs. Retain its
   runtime environment and numeric UID/GID; hold automatic migrators, the
   empty-blocks sanitizer and uncataloged-token scans; enable native bridge
   metrics while keeping the heavy indexer collector disabled. Preserve the
   test RPCs/contracts, chain 2885 and current internal-fetcher policy.
2. Probe the actual test parent RPC against stored event anchors: at most 68
   sequential log requests, at most four per second, checking wide/narrow
   completeness. A passing probe permits 10,000-block deposit/withdrawal reads;
   otherwise keep original ranges. Preserve polling intervals.
3. Save completed v9 deposit and withdrawal callbacks into monotonic,
   parent-chain/contract-scoped counters. A failed handoff prevents service
   stops and schema writes.
4. Stop the old backend, recheck the original database/heap and apply the 26
   main, two account and one Arbitrum migration versions. Each SQL statement
   has a 120-second deadline and two-second lock deadline. Keep 100 GiB free
   and a 20 GiB schema-phase growth budget. Preserve the legacy historical
   heap/primary key and defer versions `20260721000000` and `20260812000000`.
5. Disable old containers' restart policies and stop the whole captured Compose
   project. Require both PostgreSQL containers to exit cleanly and all project
   containers to stop. Recheck foreign attachments and bind identities.
6. Rename `docker-compose` to `docker-compose.test` on the **same filesystem**.
   This is a directory rename, with no 13 TB copy. Compare device/inode values
   for the root and every moved bind source. Retain ownership and permissions
   inside the data tree; restrict the private runtime root to mode 0700.
7. Restore only Git-tracked templates into the vacated `docker-compose` path,
   then switch/fast-forward the source checkout to `deriw-dev`. The original
   branch remains available. Never reset a diverged branch or discard edits
   outside the moved runtime directory. Require the chosen branch's application
   files to match qualified f0; newer operations-only commits are permitted.
8. Select a resolved private Compose file in `docker-compose.test`. Remap bind
   sources to their new paths, disallow automatic creation of missing source
   paths, preserve existing non-backend image IDs, commands/environments/ports
   and network definitions, and pin current container IPs. This avoids a dynamic
   service claiming an old static database address. Preserve original config
   files and a private backup of the moved main Compose file.
9. Remove only the captured, already stopped DB initializer containers, retaining
   their updated declarations. Recreate the databases/Redis first; check health
   and the original database identity, heap and migration state; then recreate
   remaining fixed-address services and the new backend. Start the proxy after
   API readiness. A service captured without an IP while restarting is held
   with automatic restarts disabled until every saved fixed address is occupied;
   start it last and explicitly restore its selected restart policy.
10. Verify the new mount paths, image selections, environments, runtime version,
    held migrators, resumed cursors/ranges/intervals, five native DB metric
    sections and seven on-chain verification sections.

The source checkout and deployed image can show different commit hashes when
`deriw-dev` includes newer deployment-tool/documentation commits. Their
application trees must match; the image retains its honest f0 source label.

Successful layout:

```text
/data/blockscout/                         # Git branch: deriw-dev
├── docker-compose/                      # tracked public defaults
└── docker-compose.test/                 # ignored private runtime tree
    ├── docker-compose.yml               # selected full runtime configuration
    ├── envs/                            # preserved test environment files
    ├── proxy/                           # preserved test proxy/cert configuration
    └── services/
        ├── blockscout-db-data/
        ├── stats-db-data/
        ├── redis-data/
        ├── logs/
        └── dets/
```

This deployment includes a maintenance window for the entire Compose project.
The pre-existing restarting user-ops service is recorded separately; this
release does not fix its unrelated error. A separately managed exporter with no
bind into the moved tree stays outside the project and may report DB/API
unavailability during maintenance. No metric host port is newly exposed.
Final success is `phase="test_v10_deployed"`, including `source_branch`,
`checkout_revision`, `runtime_and_data_directory` and the private journal path.
Use `docker-compose.test/docker-compose.yml` for subsequent test operations.

## Resume and recovery

If the moved runtime stops at `recreate-services` because a restarting dynamic
service claimed a saved IP, use the updated tool with the **same journal**.
The startup recovery also supports the observed verifier failure parsing the
upstream Vyper version `0.5.0b1+commit.577d2534`:

```bash
python3 -I /PATH/TO/UPDATED/deploy/scripts/deploy_testnet_v10.py \
  --resume /data/blockscout-test-private/upgrade-REPLACE/state.json \
  --repair-startup
```

This requires completed schema migration and runtime relocation. It preserves
the original `relocated.compose.json` and its hash, creates a separate private
`startup-repair.compose.json`, and atomically selects it in `.test`. The only
configuration amendments are the existing verifier's Vyper list URL, pinned to
dev's reviewed `5de16fa32acc6b55707188922d5366915aec21af` list, and exact preservation
of its captured null/list entrypoint representation. A custom Vyper URL or a
later runtime Compose edit stops recovery for review. Verifier images, Vyper
enablement, Solidity settings and all other environments remain preserved.
The original database/schema inspection and final service/API/metric checks
still run. The original checkout target is retained; fetch the updated tools
without pulling the checked-out branch forward during recovery.

The address fix pauses only journal-owned dynamic services. It preserves all
saved static IPs and does not disconnect foreign containers or rebuild the
Docker network. A retry skips schema writes and the completed directory rename;
it can reuse the saved backend image without another build. The pre-existing
user-ops restart loop may continue after its restart policy is restored.

The private journal records stages atomically. On failure, share only the
sanitized `STOP:`/phase report; command, schema and metrics details stay private.
Resolve the stated condition and resume the same completed **v2 relocation**
preparation:

```bash
test_v10_state=/data/blockscout-test-private/upgrade-REPLACE/state.json
test_v10_tool=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["tool_script"])' "$test_v10_state")
python3 -I "$test_v10_tool" --resume "$test_v10_state"
```

An incomplete preparation or earlier backend-only journal cannot activate this
layout. The schema client accepts only the reviewed applied-version subset and
resumes remaining versions. After schema completion, resume skips that stage.
If a process exits between the atomic directory rename and journal update,
resume identifies the original directory inode at its new path instead of
moving/copying it again. Git switches use tracked-template restoration and
fast-forward merges; data and private configuration stay in the moved tree.
Compose reuses already selected containers on retry instead of forcing another
recreation. No automatic rollback to v9 is performed after schema writes.

The operator-confirmed database backup/snapshot plus captured images and
configuration remain the recovery provision. Restoring the old v9 application
requires the matching old database state. A post-start verification failure may
leave new containers running; a failure during relocation may leave services
stopped with their old restart policies disabled. The journal distinguishes
those stages. Do not manually start old containers whose bind sources still
refer to the original path after it has been repopulated with public templates.

## Validation and scope

```bash
python3 -m unittest discover -s deploy/tests -p 'test_deploy_testnet_v10.py' -v
python3 -m unittest discover -s deploy/tests -p 'test_testnet_v10_layout.py' -v
python3 deploy/tests/check_testnet_v10_upgrade.py
python3 deploy/tests/check_testnet_v10_layout.py
```

The first integration exercises real v9 callback extraction, monotonic scoped
cursors, the v9-compatible bounded RPC probe, wrong-chain rejection, deliberately
blocked/partial/repeated 29-version migrations, retained heap and held indexes,
and actual chain-2885 backend/native metric/API startup with disposable PG15.
Its Docker network is internal and its data uses tmpfs.

The layout integration uses only its own labelled containers, private temporary
Git clone and disposable synthetic bind-mounted PG15/Redis data. It performs a
real directory rename, switches the temporary checkout from `deriw-pre` to
`deriw-dev`, restores public defaults, recreates containers with the same IPs and
non-backend images, and checks preserved PostgreSQL identity/heap, Redis data,
private settings, native DB metrics and proxy readiness. The backend and schema
clients use the actual `db:database` link, with the database URL resolving through
that alias across container replacement. Stopped initializer
containers are removed and DB dependencies are satisfied without rerunning them.
No live service or
host port is used. Parent RPC verification is synthetic/omitted in this fixture;
production readiness still requires all seven actual RPC metric sections.
Neither fixture is a full-size database rehearsal or proof of snapshot restore.
