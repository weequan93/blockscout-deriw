#!/usr/bin/env bash
# Targeted frontend release; see docs/deployment/frontend-v2.7.2.md.
set -euo pipefail
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
exec python3 "$repo_dir/deploy/scripts/v10_frontend_deploy.py" "$@"
