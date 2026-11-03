# Dev containerd storage preparation — 2026-11-03

The operator confirmed containerd.io 2.2.4, configuration dump version 3, root
`/var/lib/containerd` and runtime state `/run/containerd`. Its systemd service is
active. Docker uses `io.containerd.snapshotter.v1` and reports `/data/docker`,
but the filesystem containing `/var/lib/containerd` has only about 1.3 GiB free.
The separate `/data` filesystem has about 959 GiB free. The 15-second directory
size read returned no size; its exit status was not provided. No directory-size
estimate is inferred from that result.

The next operation prepares a host storage-maintenance plan, separately from
the Blockscout v10 application and database upgrade. The current v9 release
remains active. No source version, Compose service, image tag, application
configuration or PostgreSQL schema is changed by this preparation.

```bash
python3 deploy/scripts/containerd_storage.py --check
python3 deploy/scripts/containerd_storage.py --prepare
```

`--check` reads the actual systemd process's selected storage flags, uses that
process's executable to load configuration, and checks the running backend's
overlay root mount. It validates a root-only configuration candidate through
containerd's own `config dump` command using stdin. The original source format
version, plugin settings and transient runtime-state directory are preserved.
Both effective dumps must match apart from the root. No temporary config file
or directory scan is used in this mode.

The checker inventories the dev Docker containers and selected containerd
namespaces. Additional host containers, Swarm, managed Docker plugins, unrelated
containerd workloads, real imported configuration files, CLI storage overrides,
an existing destination, or a redirected source/destination require review.
The default config import glob is permitted only when it matches no files.
It checks destination capacity with a preliminary guard of twice the source
filesystem's total capacity plus a 100 GiB free-space reserve. This is an
operational planning guard, not a bound on logical copy size, a measured copy
duration or proof of sufficient space for the later database backfills.

`--prepare` repeats those checks, then writes the original/candidate configuration
and Docker metadata to a new private directory beneath
`/data/blockscout-dev-private/containerd-storage`. Directories use mode 0700 and
files mode 0600. The output contains only public planning fields and the plan
location. The saved files may contain registry/application credentials; keep
the entire directory on the server.

## Maintenance sequence to finalize from the private plan

1. Confirm the exact affected running containers, available copy capacity and
   a maintenance window. Retain the private plan and a way to access the host
   while Docker is stopped. This storage copy does not provide a database
   backup for the later v10 migrations.
2. Journal and temporarily disable container restart policies, so daemon startup
   cannot start applications before the databases or start an intentionally
   stopped one-off service. Stop applications first. Stop both PostgreSQL
   services using fast, clean shutdown (`SIGINT`), with no forced-kill timeout.
   Confirm that all containers are stopped, PostgreSQL exited cleanly and no
   other containerd tasks or nested mounts remain.
3. Stop the Docker socket/daemon and containerd. Copy the cold store to a new
   `/data/containerd` directory with ownership, permissions, hard links, ACLs,
   xattrs and sparse files preserved. Monitor the free-space reserve while the
   copy runs. Retain the original directory. Do not run live rsync deletion.
4. Verify the copied data and metadata before atomically installing the validated
   candidate configuration with the original file's ownership and mode. Preserve
   the source configuration format, effective version 3 and `/run/containerd`;
   no daemon version change is part of this procedure.
5. Start containerd and Docker. Verify that the original images, container IDs,
   configurations, host mounts and network assignments are available. Start only
   previously running services, with PostgreSQL first. Restore restart policies
   and verify backend readiness, NFT connectivity and public API routing.
6. Retain the original store until that evidence has been reviewed. Merely copying
   it does not reclaim root-disk space. Cleanup requires a separate check that
   the running daemon uses the new root and a separate deletion decision.

Before applications resume, an abort can restore the original configuration and
use the untouched source store. After applications, builds or pulls have used the
new store, switching to the old store can lose newer container-layer/image state.
That recovery must preserve the newer store and use a cold reconciliation or
another reviewed recovery method. Do not call the old directory a current backup
after writes resume. Bind-mounted PostgreSQL data remains a separate resource.

The helper deliberately has **no apply, restart, copy, cleanup or rollback command**.
The maintenance executor and its recovery checks remain to be finalized after
the live plan confirms the active paths and complete host workload scope. The
local tests validate configuration preservation, relevant process/mount parsing
and private output handling; they do not rehearse a live daemon/storage move.

References:

* [Docker daemon and containerd data directories](https://docs.docker.com/engine/daemon/#configure-the-data-directory-location)
* [Containerd 2.2.4 configuration](https://github.com/containerd/containerd/blob/v2.2.4/docs/man/containerd-config.toml.5.md)
* [Docker stop signals and timeout behavior](https://docs.docker.com/reference/cli/docker/container/stop/)
* [PostgreSQL 15 shutdown modes](https://www.postgresql.org/docs/15/server-shutdown.html)
