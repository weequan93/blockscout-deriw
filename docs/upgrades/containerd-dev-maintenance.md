# Execute the reviewed dev containerd storage move

The saved operator plan is
`/data/blockscout-dev-private/containerd-storage/plan-4xiv4wmt/state.json`.
It records 14 dev Docker containers, with 12 running, containerd 2.2.4, an empty
`moby_history` namespace and the active backend's image store on the root disk.
The move uses `/data/containerd` and retains `/run/containerd` for runtime state.
The current Blockscout v9 image and its start-only command are preserved.

The maintenance executor is separate from the read-only/preparation helper.
The saved private files may contain credentials; keep the entire directory on
the server. Run the commands from a host SSH/console session with access that
remains available when Docker is stopped. Avoid other Docker, containerd,
package-update or deployment operations during the move.

## Read-only check

```bash
cd /data/blockscout-v10-rehearsal
python3 deploy/scripts/containerd_maintenance.py --check \
  --state /data/blockscout-dev-private/containerd-storage/plan-4xiv4wmt/state.json
```

This reloads and validates the private plan, snapshot hashes, source directory
identity, current configuration, images, container settings, running set,
workload ownership and free space. It also checks the persistent `/data` mount
and destination filesystem. Ext4 is supported; XFS requires `xfs_info` and
`ftype=1`. The new systemd drop-in must not already exist. This check creates
no journal, target directory or service/configuration change.

## Maintenance: copy, validate, then resume

The following block interrupts the 12 recorded running services, including
both PostgreSQL containers. Copy duration has not been measured on this store.
Heartbeat output reports the current phase and elapsed time every 20 seconds
during long commands. The source PostgreSQL data directories are not copied.

```bash
cd /data/blockscout-v10-rehearsal
python3 deploy/scripts/containerd_maintenance.py --apply \
  --state /data/blockscout-dev-private/containerd-storage/plan-4xiv4wmt/state.json &&
python3 deploy/scripts/containerd_maintenance.py --resume \
  --state /data/blockscout-dev-private/containerd-storage/plan-4xiv4wmt/state.json
```

`--apply` checks again before creating a private execution journal. It holds a
host-wide lock across different plans. It saves the image/tag bindings and
temporarily disables all container restart policies. Applications stop before
the two databases. PostgreSQL receives `SIGINT` and Docker uses an indefinite
container shutdown timeout, so this procedure does not ask Docker to kill a
database after a grace period. The client command itself has a bounded wait;
failure leaves a recovery record rather than switching configuration.

After all containers/tasks stop, the executor stops the Docker socket, Docker
and containerd. It confirms no remaining daemon/shim or store mounts before
copying. A directory created inside the private plan is recorded by device/inode
before it is renamed to `/data/containerd`, so an interrupted run cannot silently
adopt an unrelated destination.

The cold copy uses `rsync -aHAXSx --numeric-ids`, preserving ownership, modes,
hard links, ACLs, xattrs and sparse files. The copy retains a 100 GiB destination
free-space reserve and writes logs only inside the private plan directory. A
second rsync performs checksum/metadata verification with **dry-run** deletion
reporting; it never actually deletes extra target files. Any difference prevents
configuration activation. No original store file is removed.

The root-only candidate configuration is atomically installed with the original
file ownership/mode. The executor adds
`/etc/systemd/system/containerd.service.d/90-blockscout-storage.conf` containing
`RequiresMountsFor=/data/containerd` so containerd waits for the data mount.
It reloads systemd and starts the same daemon versions, checks the original
image/tag and container inventory, and runs an isolated `/bin/sh -c 'exit 0'`
image probe. This probe starts no Blockscout application and has no network.

At `store_ready`, all recorded applications remain stopped. `--resume` records
its transition **before** starting any recorded service on the target, starts PostgreSQL and
waits for readiness, then starts only the previously running clients. The two
stopped init containers remain stopped. Original restart policies are restored.
Backend readiness, NFT connectivity, image/container settings and the backend's
actual overlay path on the new filesystem are checked before recording `active`.
These checks do not establish current chain-indexing progress or all API behavior.

## Status and recovery

```bash
python3 deploy/scripts/containerd_maintenance.py --status \
  --state /data/blockscout-dev-private/containerd-storage/plan-4xiv4wmt/state.json
```

Status reports both the saved phase and observed configuration/unit/container
state. While an operation holds the lock, a concurrent invocation can report
that the operation is still running. Use the running operation's phase output.

If the original executor stopped at `verifying_new_store`, the operator's
read-only diagnostic confirmed only `HostConfig.Dns`, `DnsOptions` and
`DnsSearch` changed from null to empty arrays on all 14 containers. Docker
initializes these three nil slices when loading saved container settings
([upstream implementation](https://github.com/moby/moby/blob/master/daemon/container/container.go#L567)).
The corrected comparison treats only those null/empty-list representations as
equal. Configured DNS values and their order, missing keys, other host settings
and network definitions remain checked. Saved plan/journal files stay intact.

For that verified-copy failure, update to the corrected executor and run
`--resume --state` with the same private plan, then `--status --state`.
Resume rechecks the copied store before starting services; it does not repeat
the copy, restart active daemons or run application migrations. A further mismatch
still stops recovery for review.

Before `--resume` has begun, restore the original configuration/store with:

```bash
python3 deploy/scripts/containerd_maintenance.py --abort \
  --state /data/blockscout-dev-private/containerd-storage/plan-4xiv4wmt/state.json
```

Abort quiesces the host, restores the saved configuration and removes only the
executor's matching mount-dependency drop-in, then restarts the original running
set with its policies. A failed target daemon can be stopped and the source
restored without successfully starting that target again. The partial/new target
is retained. Unexpected image bindings, outside configuration edits or target
container starts require review rather than being overwritten.

Once `--resume` has begun, abort is refused because applications may have newer
container-layer state on the target. Retry `--resume` to finish starting and
checking the same store. It never recopies old data over newer writes. The
original store is no longer a current copy after applications resume. A later
move back needs its own cold reconciliation procedure.

Normal SSH hangup/termination interrupts the supervised child command and leaves
the journal. A retry checks for any remaining rsync process touching either
store and refuses to run concurrently with it. A forced process/host failure
can still require operator recovery. Never recover by deleting either store.

## After successful resume

The original `/var/lib/containerd` directory remains on the root filesystem;
the move therefore does **not** reclaim its disk space yet. Cleanup/archival is
a separate action, described below. New image
store writes use `/data/containerd`; Docker's separate root stays `/data/docker`.
This storage move provides no database backup for the later v10 schema upgrade.

The operator subsequently confirmed successful resume on 2026-11-04: all 12
services running, backend/NFT checks passed, target configuration active. The
root disk still has about 1.2 GiB free, `/data` has 928 GiB free and the original
store occupies 30 GiB. A separate 30 GiB archive fits on `/data`, leaving roughly
898 GiB free. These figures do not establish a working-space budget for v10's
large database backfills.

The following helper copies **only the inactive original store** to
`original-store-archive/` inside the private plan directory. It checks active
maintenance status, the daemon and application mount references, source/archive
identities and a conservative free-space budget. It preserves metadata using
the same rsync flags as the cold copy, adds file fsync, performs a checksum and
metadata dry run, flushes the archive filesystem, then rechecks active health.
It saves a private receipt without modifying the maintenance execution journal.
It has no removal, service-control or database-mutation operation.

```bash
python3 deploy/scripts/archive_containerd_source.py --check \
  --state /data/blockscout-dev-private/containerd-storage/plan-4xiv4wmt/state.json
python3 deploy/scripts/archive_containerd_source.py --copy \
  --state /data/blockscout-dev-private/containerd-storage/plan-4xiv4wmt/state.json
```

Expected result: `original_store_archive_verified`, with `source_removed=false`.
Root space is reclaimed only by the separate source-retirement operation
below, checked against that receipt. Keep the entire private plan
directory on the server. The archive preserves the old pre-resume image store;
it is not a current backup of applications or PostgreSQL.

The operator ran this archive step successfully: checksum/metadata verification
passed, no service changed and 964,241,080,320 bytes remained available on
`/data`. The source still occupied the root disk after that operation.

To reclaim its root-disk space, run the separate retirement helper:

```bash
python3 deploy/scripts/retire_containerd_source.py --check \
  --state /data/blockscout-dev-private/containerd-storage/plan-4xiv4wmt/state.json
python3 deploy/scripts/retire_containerd_source.py --apply \
  --state /data/blockscout-dev-private/containerd-storage/plan-4xiv4wmt/state.json
python3 deploy/scripts/containerd_maintenance.py --status \
  --state /data/blockscout-dev-private/containerd-storage/plan-4xiv4wmt/state.json
df -h / /data
```

Check reads metadata only. Apply freshly compares source/archive checksums and
metadata, flushes the archive, then repeats active-store health/configuration,
mount-reference and archive-identity checks. It records the archive receipt's
hash before removing any source contents. The removal uses a fixed `find -P`
command with `-xdev`, `-mindepth 1`, `-depth` and `-delete` on the pinned original
directory. It follows no symlinks, retains that directory's inode and does not
target the active store, archive or database mounts. It requests no service stop
or restart. Heartbeats continue during long verification and removal commands.

The private retirement journal records `prepared`, `removing` and `complete`.
An interrupted `removing` run can continue with the same `--apply` command,
using the already flushed, verified archive and repeating active-state/identity
checks. It never copies partially removed source data back into the archive.
A surviving removal/copy process blocks another attempt. Keep the archive and
private records intact throughout; do not edit their hashes or phases.

Expected result: `source_retirement_verified`. Maintenance status then reports
`original_store_cleanup_performed=true`, `source_retained=false` and
`source_archive_retained=true`. The original directory remains empty so its
recorded identity can still be checked. New contents appearing there cause a
status failure. The archive-copy helper refuses runs after retirement has been
recorded, so it cannot invalidate the preserved archive receipt.

Use the executor's `--status` for this maintenance record. The older backend
release snapshot is retained intact; its strict check of unrelated containers'
start times can report the intentional host-maintenance restarts. The next
application release will capture a fresh lifecycle baseline.

The local validation covers the state machine with a deterministic host model,
failure/abort/resume paths, private journal handling, a real rsync cold copy with
ACL/xattr/hard-link/sparse-file checks, and real containerd 2.2.4 configuration
parsing, plus a real isolated test container's stop/start settings and network
aliases. The containerized checks have no host bind mounts, privileged mode or
external network. They do not rehearse the real host's systemd or full store size.
