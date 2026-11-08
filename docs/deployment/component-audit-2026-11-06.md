# Dev Compose component audit — 6 November 2026

The operator supplied a read-only inventory of all 14 Compose service roles,
their running image IDs/OCI revisions, and PostgreSQL, Redis, Nginx and frontend
package versions. The sanitized [evidence snapshot](../../deploy/audits/dev-components-2026-11-06.json)
is versioned with this report. Image creation dates are not release identities.
Local Docker image IDs are also not necessarily pullable registry manifest
digests: obtain the target platform's `RepoDigest` when pinning a future rollout.

No component was pulled, restarted, upgraded or repinned for this audit. The
current [deployment guide](dev-v10.md) describes the required native-job handoff
before changing a bound runtime. These are dated decisions; recheck release
notes and image availability when scheduling deployment.

Update, 8 November: frontend **2.7.2-deriw.4**, Nginx **1.31.8**, Redis
**8.8.4** and both PostgreSQL **15.19** patches are recorded as deployed.
See the [current guide](dev-v10.md) and [release manifest](../../deploy/release-manifest.json)
for the current selection. The following table preserves the original audit;
its planned actions are historical and should not be replayed.

## Upgrade decisions

| Component | Confirmed running version | Decision |
| --- | --- | --- |
| Backend | Blockscout 10.2.6, source `875082f41` | Keep the verified bridge release while normalization runs. A newer backend major is a separate project. |
| NFT media worker | Same 10.2.6 queue protocol, source `f8119a0a2` | Keep. Queue connectivity was verified against the new backend; the bridge-only code difference does not require replacing this worker. |
| Main and stats PostgreSQL image | 15.18 | Plan a same-major **15.19** maintenance patch. No PG17/18/19 major upgrade is required by this v10 rollout. |
| Redis | 8.8.0 | Plan/test **8.8.4** on the existing branch. |
| Nginx | 1.31.1 mainline | Plan/test **1.31.8** on the same mainline branch. |
| Custom frontend | Package 1.0.0; Next 16.2.10, React 19.1.0, Node 22.15.0 | Locate its source/lockfile; rebuild with a supported patched Node 22.x (candidate **22.23.4**) and review framework dependencies. Do not replace with upstream frontend latest. |
| Stats | **2.19.1** | Matches newest official stats tag listed at review; keep current digest. |
| Smart-contract verifier | **1.11.1** | Matches newest official verifier tag listed at review; keep current digest/list compatibility. |
| Signature provider | **1.1.4** | Matches newest official sig-provider tag listed at review; keep current digest. |
| User-ops indexer | **1.4.3** | Matches newest official user-ops tag listed at review; keep current digest. |
| Visualizer | **0.2.1** | Matches newest official visualizer tag listed at review. Its 2024 image date alone is not an upgrade requirement. |

The four PostgreSQL service roles (`db`, `db-init`, `stats-db`, `stats-db-init`)
share one image ID. The operator probed the main DB binary; the shared immutable
image identifies the stats binary too. The reusable inventory also probes it
directly. Init roles are not independent long-running databases to restart.

## PostgreSQL, Redis and Nginx patch scope

[PostgreSQL 15.19 release notes](https://www.postgresql.org/docs/release/15.19/)
describe fixes from 15.18 and state that a dump/restore is not required for an
existing 15.x cluster. Review the configuration/data-cleanup notes and any
extension-specific reindex requirements before scheduling; do not interpret
that as permission to rerun the historical Blockscout duplicate reindex. Preserve
the confirmed dev DB tuning, both volume paths and recovery backups. The
generic Compose PG17 default is not the dev version selection.

[Redis 8.8.4](https://github.com/redis/redis/releases/tag/8.8.4) is a security
release. Its reported cluster-bus issue depends on cluster mode; this inventory
does not establish that the instance is exposed. A same-minor patch is a smaller
compatibility step than moving to a newer Redis feature branch. Validate the
configured persistence mode, data recovery and backend reconnect behavior.

[Nginx news](https://nginx.org/news.html) lists 1.31.8 as the current mainline
patch at review. The [security advisories](https://nginx.org/en/security_advisories.html)
include affected version ranges containing 1.31.1; exposure depends on enabled
modules and configuration. Keep the current branch unless a stable-branch move
is separately tested. Validate candidate configuration with `nginx -t`, then
exercise HTTPS, API/websocket routing, stats routes, deposits, source-map
restrictions and certificate mounts on the isolated candidate. Preserve the
actual server templates; repository examples alone do not reproduce them.

## Frontend: separate source and release lifecycle

The current UI is the custom image `quanquanah/blockscout-frontend:dev`. This
backend repository does not contain its Next.js application source or lockfile.
Its package version `1.0.0` and absent OCI revision do not identify an upstream
Blockscout frontend release. Rebuilding the backend does not rebuild this UI.

The [Node release index](https://nodejs.org/dist/index.json) lists 22.23.4 as the
newest Node 22.x release dated on/before 6 November. Prefer a patched supported
22.x runtime for this custom app before considering a Node major change. Pin
the rebuilt frontend source and image digest, and emit OCI version/revision
labels so the next audit can identify it.

[Upstream frontend 2.12.0 compatibility](https://github.com/blockscout/frontend/releases/tag/v2.12.0)
requires Blockscout API 12.0.0, Stats 2.19.1 and Interchain API 1.11.0. Backend
10.2.6 does not meet that minimum. Matching the stats version alone is not
sufficient; there is no interchain service in the captured 14-role dev stack.
Preserve the custom branding/settings and test the v10 APIs when patching its
dependencies rather than adopting that frontend release blindly.

React 19.1.0 is a reason to inspect the dependency tree, not proof of an exposed
React Server Components flaw. The [React advisory](https://react.dev/blog/2025/12/03/critical-security-vulnerability-in-react-server-components)
identifies specific `react-server-dom-*` packages and server-component support.
Confirm installed/transitive packages, router modes and the current advisory
fixes in the frontend source; a successful Pages Router response for `/deposits`
does not prove the entire app lacks server components. Missing results from the
bounded version probe mean unknown, not absent. There is no confirmed Next.js
upgrade requirement based solely on the supplied `16.2.10` version.

## Rust release identities

Each OCI source revision was matched exactly to a versioned tag in the official
`blockscout/blockscout-rs` repository. The package crates' `0.1.0` versions are
placeholders and were not used. The evidence is the running image ID plus its
OCI revision, not a mutable `latest` label.

| Service | OCI revision | Official tag |
| --- | --- | --- |
| stats | `cd413c58c3469bdd442d0e7a0cebdaba79e641d5` | [stats/v2.19.1](https://github.com/blockscout/blockscout-rs/tree/stats/v2.19.1) |
| sc-verifier | `9457af29b9f98a5f6ad0ee289f8a3255fa4cc7ed` | [smart-contract-verifier/v1.11.1](https://github.com/blockscout/blockscout-rs/tree/smart-contract-verifier/v1.11.1) |
| sig-provider | `1aff234b419265f8bdd1d3dd1056219f733592b5` | [sig-provider/v1.1.4](https://github.com/blockscout/blockscout-rs/tree/sig-provider/v1.1.4) |
| user-ops-indexer | `3c82877cd7ebc45360670a4fc0108923dfd059cc` | [user-ops-indexer/v1.4.3](https://github.com/blockscout/blockscout-rs/tree/user-ops-indexer/v1.4.3) |
| visualizer | `acf11c8757f5c4cba1caddcfe6ea0ee7914f30ff` | [visualizer/v0.2.1](https://github.com/blockscout/blockscout-rs/tree/visualizer/v0.2.1) |

Reproduce the mapping without pulling or running an image:

```bash
git ls-remote --tags https://github.com/blockscout/blockscout-rs.git \
  'refs/tags/stats/*' 'refs/tags/smart-contract-verifier/*' \
  'refs/tags/sig-provider/*' 'refs/tags/user-ops-indexer/*' 'refs/tags/visualizer/*'
```

Preserve the verifier's tested Vyper list at
`5de16fa32acc6b55707188922d5366915aec21af` unless testing a compatible parser/list
replacement. Any future verifier patch must exercise a representative Solidity
and Vyper request; stats must retain its database and API schema; user-ops must
retain its RPC/chain configuration; visualizer must keep its sol2uml endpoint
contract. No forced replacement is indicated by this release/tag audit.

## Reproducibility and next rollout

The dev declarations still include moving `latest`, `dev`, `nginx`, `redis:alpine`
and `postgres:15` tags. The live digest snapshot and frozen release context are
more precise than those names. Pin explicit platform-specific registry digests
in the **next reviewed deployment snapshot**, after testing; do not edit the
original captured baseline or pull moving tags into this running deployment.

Patch PostgreSQL, Redis and Nginx in planned service-specific windows with
recovery/handoff checks. Treat the custom frontend rebuild as separate work
requiring its source. A component restart without updating verified native-job
runtime evidence will pause normalization, even when the component is compatible.
This audit delivers the plan and evidence; the existing bridge-only deploy helper
does not provide a generic component upgrade/handoff command.
