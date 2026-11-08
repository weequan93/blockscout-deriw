# Consolidated DeriW release source

This release presents the finished changes as **one commit in each repository**:

| Repository | New branch | Parent baseline | Final reviewed tree before documentation/ops cleanup |
| --- | --- | --- | --- |
| Backend | `codex/consolidated-v10-release` | `origin/deriw-prod` = `ddfedcadd25e07367c6133d10a44e7fde4d3898e` | `a766697c35152219ee569108e68ced1e7638427d` |
| Frontend | `codex/consolidated-frontend-v272` | `origin/deriw-dev` = `25ccfdbb744365d9005e0e7b7adb16c171a4f7fd` | `8dbd8245479df03dd0ac6478bd891846059027d7` |

Backend source includes v10.2.6, retained DeriW adaptations, the bridge tracker
and durable cursor, native resumable normalization, metrics, targeted frontend
and infrastructure deployment, and their tested recovery tools. Frontend source
includes v2.7.2, DeriW account/trade features, the compatible Node/Next/React
selection and the final three-column footer. The historical optional 32 GiB
profile is superseded by the uncapped native policy and is not imported.

Existing worktrees and stashes were inspected before consolidation: the checked
backend development/upgrade/release/native worktrees and both frontend worktrees
were clean, with no stash entries. Historical intermediate layouts and fixes
are represented by their final tree, not replayed as separate commits. Original
branches, tags, deployed Git objects and private evidence remain available.
No deployed branch was rebased, force-pushed or deleted.

## Source identity and images

The application's files equal the reviewed source before this cleanup. New
changes only update documentation, release metadata and read-only operations.
The release manifest records source trees and the qualified image identity.
A squash produces a new Git commit; it does **not** change an already built
image's source label. The frontend image remains version `2.7.2-deriw.4`, source
`d30a3ebe40d6fb6a061a84afed74afe32c8a6abe`, digest
`sha256:44bd15d39c839baf1e32e28ce99c63c9eee99db8e08043cc0df8ceb1d84faf28`.
The deployed backend remains built from `e7a16ead2bcf53da05b6e059da616ebacddbf22d`.

The new branches are review/integration candidates. Choose them as the one-commit
release branches rather than merging their many historical parents as well.
An eventual change to a protected production/development branch must follow its
normal merge process. Installing the operations checkout changes no containers.
Do not rebuild or relabel the existing images merely to match the new SHA.

## Configuration and module reconciliation

The public Compose adoption baseline is intentionally preserved byte for byte.
Replacing its older image declarations would break the original capture and
would not reproduce the live server's private settings. Actual selections are
in authenticated release snapshots; the infrastructure digest catalog and the
release manifest identify the tested newer modules. Init service declarations
retain the patched PostgreSQL image while historical stopped init containers
are not restarted. The NFT worker and compatible Rust services retain their
qualified images rather than being replaced without a reason.

All existing private snapshots, source hashes, runtime proof chains, leases,
worker holds and database cursors retain their original identities. The new
operations helper understands the latest bridge binding and reads the backend
revision from that authenticated runtime, not from an older hard-coded SHA.
`config-check` checks the saved selection and running configuration without
printing values or writing files. The [main guide](dev-v10.md) is the single
entry point; older runbooks retain their pinned recovery procedures.

## Verification

The consolidation validation record in `deploy/audits` records source equality,
source guard checks, test results and parent/count checks. Existing real
PostgreSQL/Elixir checkpoint and frontend browser/build/image qualification
records remain dated evidence. They are not rewritten to claim a fresh server
deployment or a new build of the squashed commit.
