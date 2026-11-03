# Server deployments for dev, test and prod

Status: partially implemented, 3 November 2026. The versioned dev adoption
profile and `./deploy.sh dev capture-private`, `plan`, candidate `build` and
read-only `deploy-check` now exist;
see [environment configuration](../docker-compose/environments/README.md).
Candidate build uses the current clean committed checkout and requires
`--private-file`; it has no `--ref` option and performs no activation. Upgrade,
deployment and rollback commands below remain an implementation contract, not
available operations. Test/prod profiles are not configured.
No running deployment has been changed by this repository work.

The application repairs were committed first as
`0d10d900419de5e2199f09a63e831d9d0ed74842` on `dev`.

## Operator workflow

Give every server the same entry point, with an explicit environment:

```bash
# Available now, from a clean committed checkout on the dev explorer server.
./deploy.sh dev plan --private-file /server/private.json
./deploy.sh dev build --private-file /server/private.json --check
./deploy.sh dev build --private-file /server/private.json
./deploy.sh dev deploy-check --private-file /server/private.json --receipt /server/candidate.json

# Proposed commands, available after further implementation.
./deploy.sh dev plan --ref origin/dev
./deploy.sh dev upgrade --ref origin/dev

# Proposed ref-selection support.
./deploy.sh dev build --ref <commit>

# Deploy an already built, tested release.
./deploy.sh test deploy --release <release-id>
./deploy.sh prod deploy --release <release-id>

./deploy.sh dev status
./deploy.sh dev rollback --release <previous-release-id>
```

`upgrade` performs fetch, revision selection, preflight, build, candidate checks,
deployment and verification in order. It stops on failure and prints the last
completed phase and a safe recovery command. A build failure leaves the running
services intact. It never falls back to another environment or an older image.

Production selects an explicit previously tested release. It does not deploy
whatever a moving branch or a `latest` image tag happens to contain. Servers may
build candidates with the same interface, but a production candidate must pass
test validation before selection for production.

Bare `docker compose up -d --build` is not the managed server entry point. It
cannot enforce environment selection, chain identity, source revision, release
selection or migration/rollback handling. The repository's legacy Compose
examples can remain available for local development during migration.

## Configuration ownership

Use one source tree and one generic service topology. Environment selections
are separate from application logic; host-specific paths and private values
live outside the source checkout.

Proposed repository layout:

```text
deploy.sh
deploy/
  compose.yml                       # neutral service definitions
  common/                           # reviewed public defaults per service
  environments/
    dev/compose.yml
    dev/public.env
    dev/manifest.json
    test/compose.yml
    test/public.env
    test/manifest.json
    prod/compose.yml
    prod/public.env
    prod/manifest.json
  templates/                        # blank host/private templates
  scripts/                          # implementation and validators
  tests/
```

| Location | Owns |
| --- | --- |
| Shared topology and public defaults | Service relationships, service DNS, application defaults and compatibility requirements |
| Environment files and manifest | Confirmed chain ID/type, public RPC endpoints and domains, worker tuning, history policy, enabled components and expected parent chain where applicable |
| Server host settings | Existing Compose project identity, absolute storage/config paths, host port bindings and permitted machine limits |
| Server private files | DB credentials/URLs, release cookie, secret key base and authenticated endpoint URLs |
| Generated release manifest | Exact source commit, nonsecret build options, application/component image identities, validation results and selected configuration revision |

The neutral topology must not inherit literal credentials or chain selections
from today's legacy `common-*.env` files. Extract reviewed public defaults and
map private values explicitly. Do not commit raw resolved Compose output or copy
the current server env files into Git.

Environment manifests must identify their environment and required chain. There
is no default environment and no fallback chain ID. Incomplete test/prod
manifests fail validation instead of inheriting dev or legacy values.

For the current dev deployment, retain these verified selections:

| Setting | Dev selection |
| --- | --- |
| L2 chain ID/type | `18417507517`, `arbitrum` |
| HTTP/trace RPC | `http://10.0.2.11:1100/` |
| Approximate transaction count | `true` |
| Contract-code concurrency/retry | `1`, `60s` |
| Historical enrichment range | `176114083..latest` |
| Backend DB/API pools | `40`, `20` |
| NFT producer/worker | `producer@backend.blockscout.internal`, `worker@nft-media.blockscout.internal` |
| Verifier Vyper list revision | `5de16fa32acc6b55707188922d5366915aec21af`, 52 versions |

The historical token-transfer repair opt-in defaults to false in the repaired
source. Its first activation is an explicit upgrade change: the observed live
backend is still the older image. Do not describe the source default as already
deployed. Normal block/log/token-transfer indexing remains independently enabled.

The Vyper list pin is a verifier-version compatibility selection. Keep it with
the tested verifier image in the release configuration. Advance it only after
testing that image's parser against the replacement list; do not track mutable
`main` implicitly. Solidity/Vyper service enablement remains explicit.

Test and prod require their own sanitized inventories. The legacy base's chain
`2886` is not proof of the actual production chain. Never copy dev's historical
cutoff, RPC endpoint, DB capacity or frontend network ID into another environment.

## Server layout and private inputs

Proposed host-owned paths, configurable during adoption:

```text
/etc/blockscout/<env>/host.env
/etc/blockscout/<env>/secrets/<service>.env
/var/lib/blockscout-deploy/<env>/state/
/data/blockscout-deploy/source.git/       # fetched source cache
/data/blockscout-deploy/releases/<sha>/   # clean, immutable source checkouts
```

Existing database and storage directories stay at their current absolute paths.
They are not moved into the release checkout or a new project automatically.

Secrets/state directories use mode `0700`; private files use `0600`. The CLI must
not print secret values, private file contents, raw container environment, raw
Compose configuration or credential hashes. Backups containing private values
remain server-local and are excluded from shareable release reports.

Use a strict allowlist of host setting names. Parse input files as data, without
sourcing arbitrary shell code. Ignore unapproved ambient deployment variables
and automatic `.env`/override discovery. All Compose files, project identity and
input paths are selected explicitly. Confirm private values survive interpolation
unchanged, including dollar signs, quotes and URL-encoded characters.

Compose interpolation input and container environment input are distinct. The
implementation must explicitly map each required setting to its service and test
effective precedence. Private files override only the intended private fields;
they cannot silently change `CHAIN_ID`, project identity or release selection.

Use file-based secret input where the application supports it; otherwise inject
the required environment settings from the server's private files. Environment
injection is still visible to privileged Docker inspection. Do not pass runtime
secrets as build arguments or include them in a build context/image layer.

## Source selection and image builds

1. Acquire an environment deployment lock, shared by all checkouts on that host.
2. Fetch the configured remote. Resolve the requested ref once to a full commit.
3. Create/reuse a clean detached checkout for that commit; refuse dirty source.
4. Record the source commit and the deployment-config revision selected from it.
5. Build the application candidate with explicit compile-time chain type,
   architecture and reviewed public build options.
6. Label and record the image's source commit and build inputs. Use a unique
   release name; do not overwrite a previous release tag.
7. Run candidate configuration checks before touching the live application.

Fetching into an isolated source cache avoids `git pull` merges or overwrites in
an operator-modified active checkout. A second upgrade cannot enter while the
first holds the environment lock. Never use `git reset --hard`, `git clean`, or
automatic stashing to force an upgrade through a dirty operator checkout.

Backend and NFT worker images are a tested pair. Normally build both from the
same source revision, but preserve the currently verified different image pair
during initial adoption until a replacement pair is tested. Frontend, verifier,
database and other microservices also have explicit immutable image selections.
Do not pull/recreate all 14 services just because the backend source changed.

Use the same built application artifact in test/prod when compile-time chain
type and architecture match. A different compatibility group requires its own
build and tests. Chain ID, domains and runtime RPC selection remain external.

A local Docker image ID and a registry digest are different identifiers. Record
both when available. Transfer an approved artifact through the configured
registry or a checked archive; a local `sha256:...` image ID alone does not make
the image available on another server. Registry credentials stay server-side.
If another server rebuilds instead, record its resulting image identity and
rerun validation; do not claim it is the identical promoted artifact.

## Preflight, plan and apply

`plan` produces a redacted, concrete service/config/image diff. `upgrade` runs the
same validation before applying, and rechecks state after the build:

- Require a complete environment manifest, host settings and private inputs.
- Check Docker/Compose capabilities needed by the implementation; report precise
  missing features instead of silently using different syntax or semantics.
- Query configured L2 RPC endpoints with bounded calls and verify their chain ID.
  Verify L1/parent endpoints against a separately configured parent ID.
- Check consistent backend/frontend/user-ops chain selection and RPC roles.
- Check matching backend/worker cookies without revealing values, node names,
  network aliases, storage paths and required file ownership.
- Check current project/container/image identities and database destination.
- List exactly which services, images and public settings will change, whether
  startup executes migrations, and the expected interruption.
- Refuse unreviewed environment/model differences or changes during the build.

Render resolved Compose configuration only in memory, with raw errors suppressed
or redacted. Source the final model from the checked release and explicit input
files. Generated image overrides select verified images and prevent a surprise
build or pull during activation.

Apply targeted services with explicit project/file arguments and
`--no-deps --no-build --pull never`. Recreate additional components only when the
release plan includes their changes and validates their compatibility. Never run
`down`, delete volumes, prune images, initialize a replacement DB or rename the
deployment project as an ordinary upgrade step.

Before applying a release with DB migrations, identify the pending migrations,
check their entrypoint behavior and rehearse them on a test DB copy. Require a
usable restore point and classify compatibility with the previous image. Database
backup state and migration completion are tracked separately from image state.

## Verification and rollback

Successful deployment requires:

- Expected application/component image identities and runtime chain settings.
- Backend readiness and stable restart counts.
- Indexing/RPC head relationship within the environment's reviewed tolerance;
  distinguish a stationary chain from an explorer that stopped processing.
- NFT worker/producer connectivity and queue availability when enabled.
- Verifier health and compiler-version availability when enabled.
- Preservation of unrelated containers and existing storage bindings.

Run representative application requests separately where needed. A health check
or compiler list alone does not prove complete historical indexing or successful
contract compilation. Record the validation scope in the release report.

Keep a previous release manifest, images, matching public configuration and
server-local private configuration snapshots. Rollback selects that release and
the same storage/project identity. Failed or interrupted activation must be
resumable; state changes are atomic and include the last completed phase.

Image rollback is automatic only for a release whose schema compatibility permits
it. A DB schema change is not undone by selecting the old image; use the rehearsed
DB recovery procedure when necessary. Never automatically restore a DB backup
over newer chain/indexing data as a generic health-check response.

Retain releases/images/config assets referenced by either live mounts or rollback
state. Cleanup is a separate explicit operation and never touches database data.

## Adopt the current dev stack first

1. Capture the full effective server configuration privately and classify it.
   The public inventory already covers 14 services and eight Compose layers,
   but withholds 135 private/unclassified values and opaque command/healthcheck/
   label/config content. It is evidence, not a complete deployable profile.
2. Incorporate the verified Vyper list repair applied after that public capture.
3. Create a dev profile and server-private inputs that reproduce the current
   effective behavior. Verify commands, entrypoints, mount contents and proxy
   configuration against reviewed source; do not guess omitted values.
4. Compare the new resolved model against the live eight-layer stack. Initial
   adoption permits no unexpected runtime, image or storage changes.
5. Retain project `docker-compose`, network `docker-compose_default` and the
   existing absolute DB/stats/Redis/log/DETS paths. Do not create a second dev
   stack accidentally through a new project name or relative path resolution.
6. Preserve current container names during adoption where dependencies require
   them. New installations use project-scoped service names and DNS, avoiding
   hard-coded names shared across environments on the same host.
7. Build the committed repair candidate. Plan the explicit new historical repair
   startup setting and deploy it only after the scanner-progress implications
   have been considered. Verify runtime behavior and store rollback state.
8. Exercise the entire lifecycle on dev, then obtain independent test/prod
   inventories and complete their profiles. Retire dev's numbered repair layers
   only after parity, successful activation and rollback validation.

The server-specific Dockerfile/entrypoint differences also need review before
building directly from the clean repository. Preserve necessary release config
packaging and migrations through reviewed source changes rather than depending
on untracked server modifications. Do not promise a clean-checkout release is
equivalent until this comparison and candidate checks pass.

## Implementation acceptance checks

The implementation is ready when disposable integration fixtures establish:

1. Every environment resolves independently; absent/unknown environments and
   wrong RPC chain IDs fail before activation.
2. Test/prod cannot inherit dev RPCs, chain ID, cutoffs or storage paths.
3. Required secrets never appear in output, public manifests, images or Git;
   secret values round-trip correctly through input/Compose handling.
4. Dirty source, missing inputs, changed tags, concurrent operations and
   unexpected configuration/container drift are rejected.
5. Build/candidate failures leave the live deployment intact; activation is
   targeted and preserves all unrelated containers and storage.
6. Interrupted fetch/build/apply/verification/rollback can resume without losing
   private state or applying an unintended release.
7. A release has source/config/image identity and validation evidence; image
   transfer preserves identity where promotion claims the same artifact.
8. Migration-compatible rollback and the separate migration recovery path are
   both exercised before relying on them for production.

Implement these in small steps: neutral profiles and validators, release
selection/build checks, then adoption/apply/verification/rollback. Keep each step
reviewable and avoid a production deployment until its required inputs and
validation evidence exist.
