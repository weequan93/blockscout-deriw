# Versioned environment configuration

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

Commands are read-only for running services. The only persistent write is the
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
committed application code. A newer application image still needs a separate
build, candidate validation and explicit release selection. This tool does not
provide `upgrade`, `build`, `deploy`, `up` or `rollback` commands yet, and this
profile must not be activated directly before parity and release checks pass.
Do not remove the eight server layers until that adoption succeeds.

Test and prod profiles remain unconfigured because their actual inventories
have not been supplied. The CLI refuses those environments instead of selecting
dev or assuming the legacy base's chain ID is the production chain.

The complete server rollout design is in
[server-deployment-design.md](../../docs/server-deployment-design.md).

## Checks

```bash
python3 deploy/tests/test_dev_config.py --compose
```

The tests use synthetic private values, mocked daemon metadata and actual Compose
config resolution. They cover required inputs, wrong-chain rejection, private
file permissions/scope/no-overwrite, cookie equality, output suppression, startup
drift, literal-value handling and a real eight-to-two-layer comparison fixture.
No production RPC/database is used and no service is started.
