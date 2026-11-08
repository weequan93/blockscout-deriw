# Resume the interrupted Redis patch

Current window update: this Redis recovery and both PostgreSQL patches have
finished, and normalization has resumed. The
[main health recovery guide](postgresql-health-recovery.md) records finalization
and restoration of the sanitizer hold. Keep the
original Redis recovery commands and creator checkout below as historical
recovery instructions for that journal.

The 7 November 2026 Redis patch stopped at `stopped_redis_check`, after the clean
Redis shutdown and final backup, before the new image started. The native writer
was paused by the patch window. Keep the existing journal:

`/data/blockscout-dev-private/infrastructure-releases/infra-20261107T135833Z-0fdb1c52/state.json`

The operator's comparison established that the RDB is owned by `999:1000`, mode
`0600`. The original verifier exited `1`; the same image running as that owner
exited `0` and reported both `Checksum OK` and `RDB looks OK`. The default root
process had all capabilities dropped and could not bypass another user's file
permissions. This was a verifier identity error. It does not require a restore,
file permission change or migration cursor reset.

## Recover this same journal

Run this complete block on the **dev server as root**. It installs the tested
recovery module into a separate pinned checkout. It then runs the **original
creation checkout's** deployment code, replacing only its RDB verifier for this
process. All original pause, backup, image, runtime, source, health, handoff and
checkpoint checks still run. Recovery evidence is appended privately; original
inputs, snapshots, backups and controller checkouts stay unchanged.

```bash
bash <<'RESUME_REDIS_OWNER_CHECK'
set -euo pipefail
git -C /data/blockscout fetch origin tag deriw-infra-patches-2026-11-07-r5
if ! test -e /data/blockscout-v10-redis-rdb-owner; then
  git -C /data/blockscout worktree add --detach /data/blockscout-v10-redis-rdb-owner deriw-infra-patches-2026-11-07-r5
fi
test "$(git -C /data/blockscout-v10-redis-rdb-owner rev-parse HEAD)" = "$(git -C /data/blockscout rev-parse deriw-infra-patches-2026-11-07-r5^{commit})"
test -z "$(git -C /data/blockscout-v10-redis-rdb-owner status --porcelain)"
cd /data/blockscout-v10-infrastructure-fix
python3 - <<'PY'
import importlib.util, sys
from pathlib import Path
sys.path.insert(0, 'deploy/scripts')
import v10_infrastructure_deploy as I
module = Path('/data/blockscout-v10-redis-rdb-owner/deploy/scripts/v10_redis_rdb.py')
spec = importlib.util.spec_from_file_location('redis_owner_recovery', module)
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)
path = Path('/data/blockscout-dev-private/infrastructure-releases/infra-20261107T135833Z-0fdb1c52/state.json')
raise SystemExit(recovery.resume_existing(I, path))
PY
RESUME_REDIS_OWNER_CHECK
```

The corrected verifier keeps `--network none`, `--read-only`, the read-only data
bind and `--cap-drop ALL`. It adds the existing RDB file's numeric owner UID/GID.
It still requires successful `redis-check-rdb`, rejects symlinks, and checks that
file identity, ownership, mode, size and modification metadata stayed unchanged.
It records checker source/image and successful verification in separate private
evidence files. There is no automatic data restore or checksum bypass.

Expect `infrastructure_finished`, target Redis `8.8.4`, with normalization
`running`, `waiting` or `completed`. `service_start_requested` alone is not the
completion criterion. If a later check fails, retain this same journal and its
output; do not create a second Redis release or start an old Compose model.

If the original image must be selected instead, use the same recovery block with
`recovery.resume_existing(I, path, rollback=True)`. The corrected read-only
verifier then uses the original image. Existing rollback ownership and continuity
checks still apply; it preserves current data rather than restoring the backup.

## Continue with PostgreSQL

Redis has returned `infrastructure_finished` and its native controller resumed.
The subsequent PostgreSQL preparation stopped before changes because of unused
named-collation versions. Follow the [r6 PostgreSQL continuation guide](postgresql-collation-review.md)
for fresh preparations from the verified Redis binding. It retains the corrected
default RDB checker for any future Redis operation.
Do not repoint or delete the original `-infrastructure-fix` checkout: the recovered
Redis journal and resumed controller still reference it.

```bash
cd /data/blockscout-v10-collation-review
bash deploy/steps/dev-v10-infrastructure.sh --run --component postgresql-stats \
  --normalization-state /data/blockscout-dev-private/v10-native-normalization/job-20261106T103901Z-59bb4dc6/state.json
```

Only after that finishes, run the same command with `--component postgresql-main`.
Retain each new journal and wait for its verified handoff before continuing.
Nginx still requires the [origin certificate replacement](origin-certificate.md)
and a fresh preparation. No certificate change is part of Redis recovery.

## Validation

The [validation record](../../deploy/audits/redis-rdb-owner-validation-2026-11-07.json)
records the operator's comparison, focused tests and the real Redis fixture.
The fixture reproduces failure with a protected RDB and dropped capabilities,
uses the production checker with both new and original binaries, rejects a
damaged copy, and verifies data/permissions/backup preservation. The existing
Redix client reconnects through upgrade and binary rollback without a container
restart. Synthetic fixtures do not qualify the remote rollout; its own checks
must still finish.
