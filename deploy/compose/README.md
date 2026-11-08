# Separate Compose directories for DeriW networks

Each network has its own working directory. The original `docker-compose/`
configuration, environment defaults, proxy files and historical dev profile
are unchanged and are not included by the new directories.

| Directory | Selection |
| --- | --- |
| `docker-compose/` | Original defaults and existing deployment tooling |
| `docker-compose.dev/` | Devnet |
| `docker-compose.test/` | Testnet |
| `docker-compose.prod/` | Production/mainnet |

The three new directories include the shared service definitions here, with
their own project directory. Build context resolves to the backend repository;
runtime env files resolve to the selected network's `envs/`. Docker Compose
2.20 or newer is required for `include` (Compose v5 also supports it).
These are configurable startup definitions, not completed migrations of the
three existing servers or an automated v9-to-v10 release runner.

The Compose files, `.env.sample` files, shared runtime templates and start scripts
belong in Git. Only actual server values (`.env`, `envs/*.env` and
`docker-compose.local.yml`) and generated data remain ignored. Each server creates
its own value files after checking out the source.

## One-time setup on each server

For testnet, from the backend repository:

```bash
cd docker-compose.test
bash ../deploy/compose/setup.sh
```

Use `docker-compose.dev` or `docker-compose.prod` for the other networks.
Setup creates a private `.env` and `envs/*.env` with mode 0600. It preserves
every existing file and never reads or evaluates the contents as shell code.
These files and local Compose overrides are ignored by Git. The environment
directories are excluded from the backend Docker build context.

Edit `.env` for the selected network's chain ID, source revision, images,
existing data directories and Nginx configuration directory. The devnet chain
ID is recorded; testnet and production IDs are intentionally blank. The image
and path fields with no known server selection are also blank and required.
Put the existing runtime settings in the corresponding `envs/*.env` files.
They use ordinary `KEY=value` syntax; no JSON conversion is needed.
Keep each server's own database credentials, RPC/L1/bridge configuration,
domains, history ranges, worker cookie and service settings.

`BACKEND_IMAGE` selects the application image, and `NFT_MEDIA_HANDLER_IMAGE`
selects a compatible worker (normally the same image). `BLOCKSCOUT_VERSION`
must identify that image's source commit. For a source build, get the SHA with
`git rev-parse HEAD`. The build recipe selects Arbitrum and release 10.2.6.
The runtime chain ID and frontend network ID both come from this directory's
`CHAIN_ID`. `FRONTEND_IMAGE` defaults to the digest recorded in the release
manifest; to select the tag discussed in chat, set:

```dotenv
FRONTEND_IMAGE=quanquanah/blockscout-frontend:deriw-v2.7.2
```

That tag's availability must be checked with the registry. The frontend remains
a pulled image; `--build` builds the backend, not the separate frontend source.

## Preserve existing server configuration before adoption

Use the installation's existing Compose project/network identity, data paths,
database image major version, service aliases, mount targets and ownership.
The sample project/network names are for separate stacks; leaving a new project
name on an existing installation creates a different stack rather than updating
the original one. Review container names used by monitors and automation too;
the shared definitions use Compose-generated names to avoid network collisions.

Store host-specific adjustments in this directory's ignored
`docker-compose.local.yml`, created by setup. The directory's Compose file merges
it with the shared definitions inside `include`. Use it for existing PostgreSQL commands/shared memory,
Redis settings, resource limits, additional queue/worker storage, certificate
binds, custom container names, ports and other server-specific topology. The
shared stack does not publish database/Redis ports, run ownership-rewriting init
containers or create missing data bind directories. Mount sources must already
exist. Keep the existing settings in the override if they differ from these
startup definitions. No database or Redis version upgrade is implied.

For the currently deployed devnet native normalization job, retain its pinned
controller, journals and authenticated private Compose snapshots. A raw Compose
recreation does not update the controller's runtime binding. Use the
[devnet deployment guide](../../docs/deployment/dev-v10.md) for that handoff;
the new devnet directory does not replace the running selection by itself.

## Validate and start

Once the selected directory has the correct settings and the database schema
is compatible with v10.2.6:

```bash
./start.sh --build
docker compose ps
```

From the repository root, the equivalent commands are:

```bash
./docker-compose.dev/start.sh --build
./docker-compose.test/start.sh --build
./docker-compose.prod/start.sh --no-build
```

Choose one environment's command on its server. Each start script explicitly
selects that directory's Compose file and `.env`, validates with `config --quiet`,
then runs `up -d` with the supplied options. It works from any current directory,
does not create/rewrite local values, and supports either the `docker compose`
plugin or the `docker-compose` v2 standalone command. `--no-build` uses the
selected release images; `--build` builds the backend from source.
Direct `docker compose up -d --build` from the selected directory still works.

`config --quiet` validates Compose resolution without printing credentials.
It does not prove that the RPC reports the intended chain, that the image
accepts every runtime variable, or that the database schema is compatible.
Validate those against the existing server before activation. The backend and
worker use `bin/blockscout start`; startup does not run `create_and_migrate`.
Automatic background migrations remain held in the image/runtime defaults.
There is deliberately no broad schema-migration command here. An existing v9
database needs its own reviewed v10 schema upgrade first; a fresh database also
needs schema initialization before the start-only applications can become ready.

## Publish once and promote a release

Publish a versioned Arbitrum backend image from a clean committed source revision.
Use the same image digest on devnet, testnet and production when architecture and
compile-time options match, while retaining each network's runtime settings.
For the normal validated v10 image update, change `BACKEND_IMAGE`,
`NFT_MEDIA_HANDLER_IMAGE` if needed, and `BLOCKSCOUT_VERSION` in the chosen
directory's `.env`. After its configuration/schema checks and any devnet native
handoff, a backend-only image update uses:

```bash
docker compose config --quiet
docker compose pull backend nft_media_handler
docker compose up -d --no-deps --no-build --pull never backend nft_media_handler
```

Keep the prior image selection and runtime files for recovery. This command
updates only the application pair and does not run database migrations or
replace the frontend, proxy, PostgreSQL, Redis or the other services.
After deployment, verify indexing progress, APIs and the worker connection for
that network before promoting the digest to the next network. A binary rollback
does not reverse a database schema migration.
