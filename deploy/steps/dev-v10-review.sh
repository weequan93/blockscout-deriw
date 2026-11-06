#!/usr/bin/env bash
# Run on the dev server from the separate, reviewed ops worktree.
# Every step is read-only; the active controller checkout stays pinned.
set -euo pipefail
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
bash "$repo_dir/deploy/ops.sh" source-check
bash "$repo_dir/deploy/ops.sh" status "$@"
bash "$repo_dir/deploy/ops.sh" bridge "$@"
bash "$repo_dir/deploy/ops.sh" components
