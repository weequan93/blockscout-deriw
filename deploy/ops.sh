#!/usr/bin/env bash
# Read-only operations for the deployed dev v10 release. See docs/deployment/dev-v10.md.
set -euo pipefail
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
exec python3 "$repo_dir/deploy/scripts/current_ops.py" "$@"
