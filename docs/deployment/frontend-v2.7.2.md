# Deriw frontend v2.7.2 deployment

This procedure updates only the frontend container for `explorer.dev.deriw.com`.
The backend remains v10.2.6 at `875082f41d79ef72cd9dd9a4e771953a181c6cf2`.
The UI image must identify source `27a4139d09ac57d975cc8c2bd4f1d804c8589f20`
and version `2.7.2-deriw.1`, and must be Linux AMD64. Select the published image
by its registry digest, never the moving `dev` tag.

The helper reads the current generated browser settings and Docker environment
on the server, keeps their values private, and prepares two immutable Compose
snapshots. It retains the Deriw API/token configuration, branding, chain/RPC,
ports, mounts, network and parent explorer details. It removes the retired
`NEXT_PUBLIC_API_SPEC_URL` and `NEXT_PUBLIC_ROLLUP_L1_BASE_URL`, fills the parent
chain's `baseUrl` if needed, and sets `NEXT_PUBLIC_ROLLUP_LAYER_NUMBER=3`.
It excludes the old image's generated `NEXT_PUBLIC_ICON_SPRITE_HASH`; the new
image supplies the hash for its own sprite file during startup. Copying that
build value as a runtime setting causes strict validation to reject it.
`ENVS_PRESET=none` prevents a newer upstream preset from replacing the captured
settings. Strict startup environment validation remains enabled.

## Activate on the dev server

Use a separate clean worktree at the published deployment-tool commit. Keep
`/data/blockscout-v10-bridge-rehearsal` and its original private release records.
Do not repoint that checkout to the squashed release branch.

```bash
python3 deploy/scripts/v10_frontend_deploy.py --run \
  --normalization-state /data/blockscout-dev-private/v10-native-normalization/job-20261106T103901Z-59bb4dc6/state.json \
  --image quanquanah/blockscout-frontend@sha256:76a170d12b74183e3df77dbfa4d047aa0937ad66ed43615ca0d278b4c67429a9
```

The helper pulls and checks the image, then runs a temporary container with the
migrated private environment and the same network, with no published ports. It
requires the complete entrypoint, asset preparation, validator and health
endpoint to succeed. This happens while the native normalization job continues.

After staging, it stops that job's owned controller/writer, verifies its database
connections are gone, and captures the database migration/cursor/heap identity.
It runs Compose with `--no-deps --no-build --pull never frontend`; other services
must retain their exact container identities. It checks frontend startup,
environment, image, mounts/ports/network and the existing backend health checks.
It gracefully reloads the existing Nginx process so its service-name lookups
follow the replacement frontend. The proxy container retains its identity.
The same database identity and checkpoint must still match before activation
can receive an immutable qualification receipt.

A new immutable frontend runtime binding retains the original bridge binding,
all prior journals, native worker source, database cursor and job token. The
systemd controller moves to this deployment-tool checkout and resumes the same
job. No backend/container restart, schema migration, reset of progress, growth
cap, or additional migration is introduced. Existing free-space reserves and
pause/retry policy continue to apply.

Save the printed `/data/blockscout-dev-private/frontend-releases/frontend-.../state.json`.
The final result should show `frontend_active` and a requested normalization
service start (or the already completed migration). Confirm deposits,
withdrawals, parent-chain links, address account value, trade expansion,
absolute timestamps and custom date filtering in the browser.

## Status, interruption and rollback

Run these from this deployment-tool checkout, replacing the state path:

```bash
python3 deploy/scripts/v10_frontend_deploy.py --status --state /data/.../state.json
python3 deploy/scripts/v10_frontend_deploy.py --resume --state /data/.../state.json
python3 deploy/scripts/v10_frontend_deploy.py --rollback --state /data/.../state.json
```

If a plan failed during staging and remains `prepared`, use the corrected
deployment-tool checkout to replace that preparation:

```bash
python3 deploy/scripts/v10_frontend_deploy.py --retry-staging \
  --state /data/blockscout-dev-private/frontend-releases/frontend-20261107T034957Z-60cc04fd/state.json
```

This requires the original containers to remain unchanged and refuses an
activated or qualified preparation. It creates a new private plan linked to the
failed state, resamples the current settings, and preserves the original plan
and logs. Both plans refer to the same native normalization job and checkpoint.
Use a separate worktree for the corrected tool; keep the earlier controller and
failed deployment checkout pinned. Do not edit frozen JSON to remove a setting.
Exited startup containers now report their exit code, error category and setting
names immediately, without printing setting values or waiting five minutes.

Status reads saved files. Resume reconciles the same prepared image and
snapshots. Rollback selects the preserved frontend image/environment and makes
another verified binding for the same normalization job. It retains the
forward receipt and binding. A failed check leaves normalization paused or
waiting with its cursor intact; do not edit the frozen JSON or create a new job
to bypass it. Read private logs on the server without publishing resolved
settings or asset URLs.

Keep the rollback image tag and all private release files. Future component
rollouts must preserve the frontend selection from this release snapshot;
rendering the earlier backend snapshot alone selects the previous frontend.
The bridge-only helper remains specific to its original backend rollout.

## Validation scope

The source candidate has unit/browser and production-bundle validation in the
frontend repository. The image requires its own container build/startup check.
Deployment tooling tests cover settings migration, immutable snapshot/receipt
checks, exact image identity, checkpoint handoff, rollback evidence and systemd
ownership. The server staging step validates the actual private environment;
local mocked tests cannot replace it.
The [dated tooling record](../../deploy/audits/frontend-deployment-validation-2026-11-07.json)
records the unit, configuration and real Compose transport checks.
The [sprite-handoff correction](../../deploy/audits/frontend-sprite-handoff-validation-2026-11-07.json)
records the reproduced validator failure, corrected settings and image-owned
sprite check.
