#!/usr/bin/env bash
set -euo pipefail
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
exec python3 "$repo_dir/deploy/scripts/v10_bridge_checkpoint_deploy.py" "$@"
