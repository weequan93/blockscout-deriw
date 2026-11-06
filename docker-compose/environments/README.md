# Versioned environment configuration

**For the deployed dev v10 stack, use the [current deployment and operations
guide](../../docs/deployment/dev-v10.md).** The v9 capture/adoption workflow below
preserves the original baseline. Its image pins are not a new v10 deployment
selection; do not overwrite the active release or pinned native controller.

The dev profile now records the public effective settings that previously lived
in eight server repair layers. It is an adoption candidate, not an already
verified replacement for the running stack. The old stack continues running.

`dev/compose.json` is combined with `docker-compose/docker-compose.yml`; the
result uses two layers. Application code fixes are in the source commit, while
the profile records the dev chain, RPC, tuning, history policy, NFT aliases,
current image selections, storage paths, service topology and verifier list pin.
It supplies the verifier service missing from the legacy repository base file.

`dev/requirements.json` maps 133 required server variable names to their original
service/env key. They include secrets and ordinary values withheld by the public
capture. No values or credential hashes are included. `dev/private.example.json`
is a blank template, and cannot satisfy required inputs unchanged.

`dev/manifest.json` records chain `18417507517`, project `docker-compose`, source
capture metadata, baseline component image IDs and the existing port bindings.
Ports are inherited from the shared service files and checked against that
manifest, avoiding duplicate port declarations during Compose merging. The dev profile deliberately
preserves absolute data paths and the existing project/network identity. Do not
reuse it for a second stack or a different server environment.

The dev DB explicitly retains the confirmed server startup tuning, rather than
inheriting different defaults from the generic service definition:

| PostgreSQL setting | Dev value |
| --- | --- |
| `max_connections` | `80` |
| `shared_buffers` | `4GB` |
| `effective_cache_size` | `18GB` |
| `work_mem` | `16MB` |
| `maintenance_work_mem` | `512MB` |
| `max_worker_processes` | `4` |
| `max_parallel_workers` | `0` |
| `max_parallel_workers_per_gather` | `0` |
| Docker `shm_size` | `6442450944` bytes (6 GiB) |

The manifest also checks these settings during managed resolution. The selected
DB image remains `postgres:15`; no database service has been restarted by adding
this profile, and this does not perform a PostgreSQL version upgrade.

## First server comparison

After these commits are available in the server's source checkout, run there:

```bash
cd /data/blockscout

# Collect actual existing values into a new server-only file outside the checkout.
./deploy.sh dev capture-private \
  --output /data/blockscout-dev-private/private.json

# Compare the versioned profile against the current eight-file deployment.
./deploy.sh dev plan \
  --private-file /data/blockscout-dev-private/private.json
```

The source checkout must contain `deploy.sh`, `deploy/scripts/dev_config.py` and
the environment profile together. No standalone analysis helper is required.
Keep the existing eight Compose files in place for this comparison.

`capture-private` uses Docker metadata to copy the required live values into a
new private JSON file. It verifies the dev chain/service identities and matching
Erlang cookies, and refuses a moving deployment, missing values, symlinked output,
output inside the checkout or an existing output file. The private parent is
`0700` and the file is `0600`. Keep this file on the server; never paste/upload it.
Choose a new filename if repeating the capture rather than overwriting a previous
snapshot. This is an env-value snapshot, not a full config or database backup.

`plan` resolves the current eight files and the proposed two files internally.
It checks the backend/frontend network identity, user-ops RPC, private-value
consistency and the L2 RPC's reported chain ID. It reports changed service/field
names, declared/live env drift and image changes from the public capture. It
never prints raw env values, commands, Compose output or credential hashes.

The report also includes `runtime_difference_details`: it separates equivalent
empty network attachments (`null` versus `{}`), compares bind-mount policies
without exposing source paths, and shows only allowlisted numeric/boolean
PostgreSQL tuning options plus shared-memory sizes. Unknown command arguments
and sensitive-value overlaps are withheld. The integer rate-limit setting
`API_RATE_LIMIT_UI_V2_WITH_TOKEN` is not treated as a credential just because of
its name. Actual credential values remain protected, including numeric ones.
Nonempty aliases, static addresses and other real network changes remain
differences. Omitted `read_only` versus explicit false, and omitted versus empty
bind option objects, compare as defaults; explicit bind policies, changed paths,
read-only mounts and mount ordering remain differences.

Docker inspection is different from Compose declaration ordering: some Docker
versions return the same final mount records in a different array order on each
read. The snapshot stability check compares those full records as a multiset,
retaining duplicates and every property. `inspection_order_only_changes` reports
such harmless reorderings. Real source/target/mode/driver/policy changes, process
restarts, recreation, environment or other configuration changes still stop the
check. A stopped stability check includes `changed_containers`, with service and
field names and public lifecycle metadata; it never includes raw env, commands,
labels or host paths. Compose volume declaration ordering remains compared.

The comparison uses the backend's recorded file list and defaults to live Compose
directory `/data/blockscout/docker-compose`. A different live path requires an
explicit `--live-compose-dir` argument and matching recorded paths. Managed
resolution disables implicit `.env` discovery and ignores ambient `DEV_*` and
`COMPOSE_*` selections. Runtime private values are injected from JSON as data,
without shell evaluation. A public fixture detects the installed Compose CLI's
dollar escaping so literal `$`, `$$`, quotes and newlines compare correctly.

These two commands are read-only for running services. Their only persistent write is the
explicit private snapshot from `capture-private`. `plan` uses metadata reads,
Compose configuration resolution and one bounded `eth_chainId` request through
the existing backend. It performs no build, container recreation, migration or
deployment.

Only the output of these commands is suitable for sharing. `plan` can exit zero
with `status: differences_require_review`; this means the comparison completed,
not that deployment is approved. Review every reported difference.

## Before adoption or upgrade

The profile explicitly sets `INDEXER_UNCATALOGED_TOKEN_TRANSFERS_ENABLED=false`,
the committed source's new startup policy. The last verified backend image
predates that option. This can be an intentional env difference and must be
deployed with the reviewed repaired image; it does not stop an already running
historical scan.

The verifier now uses the tested native execution selection and the compatible
52-entry Vyper index at commit `5de16fa32acc6b55707188922d5366915aec21af`, matching
the confirmed server repair after the original public capture.

Build definitions remain inherited from repository source, with explicit Arbitrum
build arguments. They are not a reproduction of unreviewed server Dockerfile
customizations. The comparison excludes build fields and does not inspect
mounted file contents or prove image/tag binding. Commands, entrypoints and
other resolved runtime fields are compared against the recorded live model;
remaining differences must be reviewed before adoption. The real Nginx templates
and TLS assets still require their own source/config backup and comparison.

Current backend/NFT image pins reproduce the observed deployment, not the newly
committed application code. The candidate build below prepares a replacement
backend image; it preserves the existing NFT worker. Targeted dev `deploy`,
`status` and `rollback` are described below. Fetch/ref selection and a combined
`upgrade` command remain unimplemented. Keep the eight server layers and their
private inputs for comparison and recovery.

Test and prod profiles remain unconfigured because their actual inventories
have not been supplied. The CLI refuses those environments instead of selecting
dev or assuming the legacy base's chain ID is the production chain.

The complete server rollout design is in
[server-deployment-design.md](../../docs/server-deployment-design.md).

## Build a dev candidate

### Enable ETH price data on dev

The dev profile now selects the user's requested Ethereum USD price from
CoinGecko. These public settings are committed in `dev/compose.json`; no provider
key is committed or added to the 133 server-only inputs. The native symbol/name
are `ETH`/`Ether` for backend market lookups. Existing frontend branding is preserved.

`MARKET_NATIVE_COIN_PRICE_ONLY=true` retains prices and price history while
withholding the provider's Ethereum supply, market cap and global trading volume
from native-coin API results. Existing cached/database rates also pass through
this policy. Foreign market-cap history is not fetched and existing market-cap
chart points are masked when served. Top-account native supply percentages stay
hidden until a separate dev-chain supply source is implemented. Other environments
retain the previous behavior unless they explicitly enable this option.

The initial price-history window is two days, rather than a full-year backfill,
to obtain yesterday's closing price for the daily change. ERC-20 price fetching
stays disabled. Market price history can write a few ordinary market-history
rows; no schema migration or chain-history repair is requested.

From a clean committed review checkout containing this update, run:

```bash
./deploy.sh dev eth-price \
  --private-file /data/blockscout-dev-private/private-20261103.json --check

./deploy.sh dev eth-price \
  --private-file /data/blockscout-dev-private/private-20261103.json --apply
```

`--check` performs the existing deployment comparison and a single unauthenticated
HTTPS quote read through the running backend. It performs no build or restart.
The quote read has a 10-second curl limit, 2 MiB body cap, TLS validation and no
redirect following; only a positive Ethereum/USD quote is accepted. Provider
failure stops the command before a build or recreation. Diagnostics never print
keys, request headers, raw errors or the response body.

`--apply` builds a candidate from the selected committed source, runs isolated
configuration and price-policy checks, records the candidate receipt, and invokes
the existing backend-only release. The new price-only code requires a rebuilt
image for this rollout. Startup migrations remain disabled, both reviewed index
migrations remain deferred, and NFT/DB/frontend/proxy containers are preserved.
Only the exact reviewed ETH setting values are allowed as additional deployment
differences; other runtime, image or private-input drift still stops preflight.

The command prints private release-state and rollback paths. Keep them on the
server. Runtime success additionally requires an actual positive rate in the
backend's market cache (`market_price_ready=true`); configuration alone does not
prove the provider fetch succeeded. Daily percentage change can appear later as
the two-day price history arrives. A failed provider fetch after restart leaves
the release available for status/rollback; it does not trigger an automatic
rollback or touch unrelated services. Follow the printed state path with the
existing `status` or `rollback` commands. If a previous attempt built the same
candidate, use `eth-price --apply --reuse` to revalidate it before deployment.

### Build separately

Use the clean review checkout on the explorer server. The source revision must
contain these commands and be committed; dirty checkouts are refused. Keep the
existing eight-layer stack running and retain its configuration backups.

```bash
cd /data/blockscout-config-review-20261103
./deploy.sh dev build \
  --private-file /data/blockscout-dev-private/private-20261103.json --check

./deploy.sh dev build \
  --private-file /data/blockscout-dev-private/private-20261103.json
```

`--check` reads metadata, resolves configuration and checks the L2 chain ID without
building or writing state. Build preflight allows only the reviewed historical
token-repair flag difference and rejects live env/image drift. A successful
preflight does not approve activation.

Build mode streams `git archive` of the selected commit to Docker, using the
committed `docker/Dockerfile`, explicit Arbitrum/API options and the baseline
backend architecture. Ignored/untracked server files and `.git` are excluded;
runtime private inputs are not passed as build arguments or added to the archive.
The candidate tag includes the commit and architecture. An existing tag is never
intentionally overwritten; use `--reuse` to revalidate a matching candidate
without rebuilding it. Docker base images and dependency downloads still require
network access, and a rebuild is not guaranteed to reproduce identical bytes.

Builds can consume substantial host CPU, memory, disk and I/O. They have a
one-hour limit and print periodic progress. The default server-only state root is
`/data/blockscout-dev-private/releases` (`0700`); each run has private build/check
logs and, on success, a `candidate.json` receipt (`0600`). Keep these files on the
server: logs can contain private diagnostics. Share only the CLI JSON summary.
All checkouts for this dev stack must use the same state root so its build lock
serializes candidate builds. `--state-dir` changes that root explicitly.

Three temporary release checks load runtime configuration with the historical
repair option unset, false and true. They use no network, a read-only root
filesystem, `/tmp` tmpfs and CPU/memory limits. Runtime private values enter only
through stdin. Checks verify the dev chain, count/backoff/range settings, bundled
receipt fallback, repair startup policy and old-block revert guard. They do not
start the application or migrations, connect to the DB or make RPC requests.
Temporary check containers are removed. Source, private inputs, live deployment
and candidate tag are checked again before a success receipt is written.

`candidate_verified` confirms these limited checks. It does not prove full app
startup, migration compatibility, live mount permissions or NFT connectivity.
`runtime_user_matches_baseline` reports only the declared image-user comparison.
The CLI creates a candidate image without restarting any running service. Actual
activation and rollback use the separate commands below.

## Collect pre-activation evidence

After `candidate_verified`, retain its image and receipt. Run from the clean
review checkout, using the receipt path printed by that build:

```bash
./deploy.sh dev deploy-check \
  --private-file /data/blockscout-dev-private/private-20261103.json \
  --receipt /data/blockscout-dev-private/releases/candidate-<id>/candidate.json
```

This command collects evidence before activation. It
compares the migration-file contents in the candidate and current backend images,
checks candidate-inherited image defaults, tests existing log/DETS directory
access, compares numeric users/groups and probes current readiness/NFT
connectivity. Temporary inventory containers are isolated from the network and
host storage, use read-only root filesystems and are removed. Image default
differences are reported as field/env names; private values are never printed.
Migration differences now include the added/removed/changed relative filenames
(up to 32 of each). The command also reads presence, validity, readiness and size
metadata for the six known performance indexes in the two fork migrations; it
does not build an index or execute a table scan.

Migration status uses the existing backend Repo connections in short read-only
transactions with statement/lock timeouts. It directly reads
`public.schema_migrations`; it does not use Ecto migration-status APIs that can
create that table. A missing/unavailable schema table, unsupported migration
source/prefix or unavailable DB route requires review. No Repo or application is
started, and no schema, persistent setting or data is changed. Mount checks run
`test` under the existing backend user without creating files; they do not prove
ongoing write success under the new image.

The `migration_files` and `pending_count` fields describe the live image and are
explicitly labeled `pending_count_basis: live_image`. The candidate's file
versions are checked against the same database route separately, with
`candidate_migration_files`, `candidate_pending_count` and
`candidate_pending_versions`. Zero pending live-image migrations does not show
that additional candidate migrations have been applied. The candidate comparison
must also pass. Inactive chain/feature directories are inventoried, but only the
repositories selected by the existing release are queried.

`preactivation_evidence_passed` means these limited checks passed, not that the
candidate is deployed or that DB backups/restore have been proven. Otherwise,
review `blockers`. The existing startup command includes `create_and_migrate`;
new/changed or pending migrations require a separate recovery/compatibility plan.
The targeted start-only deployment below can defer only the two exact reviewed
index migrations; it does not turn a pending schema migration into an applied one.

A newer tooling commit may check the already-built candidate if it descends
from the candidate's source commit and changes only deploy tools or their
documentation. Changes to application code, Docker recipes or environment
profiles require a new candidate build. The command rechecks source, receipt,
inputs, image identity and live stability before accepting its evidence.

## Deploy the existing dev backend without startup migrations

The clean candidate contains two additional index-only migrations:
`20260721000000` (five block/transaction indexes) and `20260812000000` (the logs
covering index). The observed live DB has the five former index names valid and
ready, but no logs covering index. Neither migration version is recorded as
applied. Index presence alone does not establish that its definition matches the
migration. A normal migration-enabled startup could therefore start a large logs
index build while the API is waiting for startup.

The managed dev release policy uses `bin/blockscout start` for both deployment
and rollback, separating schema/index work from application restart. This policy
is in `deploy/scripts/release.py`. It accepts only the reviewed migration files
with their exact hashes and pending version IDs. Removed/modified migrations,
additional pending structural migrations, unknown source changes and unrelated
pre-activation blockers stop deployment. Existing DBs only: this command cannot
bootstrap a fresh database. No migration version is marked as applied, no index
is built, and no migration file is removed from the candidate.

From the clean review checkout on the dev explorer server:

```bash
./deploy.sh dev deploy \
  --private-file /data/blockscout-dev-private/private-20261103.json \
  --receipt /data/blockscout-dev-private/releases/candidate-<id>/candidate.json \
  --check

./deploy.sh dev deploy \
  --private-file /data/blockscout-dev-private/private-20261103.json \
  --receipt /data/blockscout-dev-private/releases/candidate-<id>/candidate.json \
  --apply
```

`--check` performs read-only preflight and reports the exact start-only policy and
deferred versions without writing release state or changing services. `--apply`
repeats preflight, acquires the same default state-root lock used by builds, saves
private forward/rollback Compose snapshots, and recreates only `backend` with
`--no-deps --no-build --pull never`. API/indexing pauses during replacement,
including the existing stop grace period. Runtime private values are frozen as
data, with dollar escaping tested through Compose and actual fixture containers.
All service images are pinned in the snapshots; only backend is selected for
recreation. DB, worker, frontend, proxy and other containers are preserved.

Each release lives under
`/data/blockscout-dev-private/releases/release-<timestamp>-<id>/`. Keep the entire
directory server-local: it contains secrets in the `0600` snapshots/state and
private logs, inside a `0700` directory. A rollback tag preserves the previous
backend image. Never prune images or remove these snapshots while needed for
live operation or recovery. Snapshot hashes remain private; edits stop replay.
The source checkout can move to a newer reviewed commit without changing saved
release inputs. Existing absolute data mounts and the project/network identity
stay in use; Nginx/TLS mount contents are not replaced by this backend deployment.

The CLI prints the state path and rollback command before service recreation.
It checks readiness, RPC chain identity, runtime count/backoff/range settings,
disabled historical scanner, NFT connectivity, mounts/ports/networks and the
preservation of other containers. `verified` is scoped to those checks; chain
indexing progress and representative API requests still need observation.
Normal indexing continues to write application data. There is no automatic
rollback or database restore on a failed readiness check.

Use the printed state path to inspect or roll back:

```bash
./deploy.sh dev status --state /data/blockscout-dev-private/releases/release-<id>/state.json
./deploy.sh dev rollback --state /data/blockscout-dev-private/releases/release-<id>/state.json --check
./deploy.sh dev rollback --state /data/blockscout-dev-private/releases/release-<id>/state.json --apply
```

Rollback restores the previous image/environment while retaining start-only
startup. It can resume the previous image's historical scanner policy. It performs
no schema downgrade or DB restore. State is saved before recreation so an
interrupted deployment can be inspected and recovered, including when backend
removal completed but replacement did not. A newer release's backend or changed
unrelated services stop rollback rather than being overwritten.

The managed backend records its private release snapshot in Compose labels.
Subsequent `plan` and `build` commands recognize this context and compare against
the selected images and start-only policy. The public source still consists of
the generic base plus dev profile; the runtime snapshot adds the private inputs,
release image selection and reviewed startup policy. Do not use a bare `compose
up` against the old files to manage this adopted backend: it could restore the
old image and migration-enabled startup. Use the release commands instead.
Test/prod remain unconfigured. Index migration/backup/recovery operations are
separate work and are not automated by this dev adoption command.

## Repair the dev HTTP stats route

The public homepage requests `/statsapi/api/v1/pages/main`. A CDN can use HTTP
for its origin connection even when the browser uses HTTPS. The legacy default
template routed this prefix only in the HTTPS server. Direct stats-service HTTP
200 combined with an HTTP-proxy 404 identifies a proxy-path problem, without
requiring a stats image upgrade or a backend/database restart.

The default template now includes the prefix on port 80. Use the targeted helper
to update matching HTTP frontend blocks in the existing mounted templates and
their rendered counterparts:

```bash
./deploy.sh dev proxy-stats --check
./deploy.sh dev proxy-stats --apply
```

`--check` reads container metadata and Nginx configuration in memory, compares
bounded GET probes, reports only route classifications and certificate expiry
metadata, and writes nothing. It requires the expected dev template mount,
standard Nginx master arguments, a working direct stats endpoint and a working
main backend API route. It inspects the loaded `default.conf` and `explorer.conf`
and matches HTTP frontend blocks against the declared frontend destination or
the corresponding template variable. The observed dev layout has two such
blocks in `default.conf` plus another in `explorer.conf`; all three need the
prefix. Existing server/TLS definitions and unrelated upstreams are preserved.
The template/rendered match counts must agree, and matching frontends in other
loaded files require review. Unknown stats destinations or multiple stats
locations within one server also stop the check. It can add a missing prefix location or correct a missing URI
slash in an existing stats upstream. A route already configured correctly is
retained; graceful reload also refreshes Nginx's upstream DNS resolution.

`--apply` takes the shared release lock and saves private originals under
`/data/blockscout-dev-private/releases/proxy-stats-<timestamp>-<id>/`. It changes
only matching HTTP stats routes in the loaded `default.conf`/`explorer.conf`
files and their corresponding mounted `.template` files, runs `nginx -t`, and gracefully reloads
Nginx. It verifies HTTP stats JSON shape against the direct service and checks
the existing main API. Containers are not recreated. Backend, NFT, database,
Compose snapshots and TLS files are not modified. On a failed syntax/route test,
it restores the originals and reloads the old configuration if needed.

The helper prints a private backup path and restore command before file changes:

```bash
./deploy.sh dev proxy-stats --restore /data/blockscout-dev-private/releases/proxy-stats-<id>/state.json
```

Keep the entire backup directory on the server; configurations and metadata can
contain private values. Later unknown edits are never overwritten. A partial
interruption can be restored when each file still matches its saved original or
patched version. The private v2 backup captures every selected file pair, and
restore still accepts the earlier single-file v1 backup format. A failed write,
syntax check or route check restores the full selected set. This changes an existing mount's file contents, separately from
the backend release configuration comparison. Retain both backups.

Origin TLS is a separate check. The server probe reported an expired certificate
on the proxy HTTPS listener. This helper reports the certificate date where it
can read the public certificate file, retains TLS verification for HTTPS probes,
and does not renew or replace certificates or keys. A working public HTTP/CDN
route does not establish that origin HTTPS trust is valid. Renew the certificate
through the existing certificate-management process and verify origin TLS before
selecting HTTPS as the CDN origin protocol. Check the public browser URL after
repair; the helper's HTTP check is inside the Docker network.

## Checks

```bash
python3 deploy/tests/test_dev_config.py --compose
python3 deploy/tests/test_candidate_build.py
# Optional: builds a tiny disposable scratch image through the real archive pipe.
python3 deploy/tests/test_candidate_build.py --docker
# Requires Docker and the pinned Elixir fixture image, but no Mix deps or DB.
python3 deploy/tests/check_candidate_smoke.py
python3 deploy/tests/test_deploy_check.py
# Optional: exercise the exact migration expression in an isolated Elixir fixture.
python3 deploy/tests/test_deploy_check.py --docker
python3 deploy/tests/test_release.py --compose
# Optional: two disposable containers in a unique fixture project; no database.
python3 deploy/tests/test_release.py --compose --docker
python3 deploy/tests/test_proxy_stats.py
# Optional: isolated Nginx, generated test certificates and fake upstreams.
python3 deploy/tests/test_proxy_stats.py --docker
```

The tests use synthetic private values, mocked daemon metadata and actual Compose
config resolution. They cover required inputs, wrong-chain rejection, private
file permissions/scope/no-overwrite, cookie equality, output suppression, startup
drift, literal-value handling and a real eight-to-two-layer comparison fixture.
Candidate tests also exercise build/reuse/failure/race/lock handling, private
stdin/log handling and exact release expressions with source-derived Elixir
fixtures. The optional Docker test verifies archive exclusions and removes its
fixture image/container. No production RPC/database is used and no service is started.
