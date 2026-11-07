#!/usr/bin/env bash
# Targeted patch release. See docs/deployment/infrastructure-patches.md.
set -euo pipefail
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
exec python3 "$repo_dir/deploy/scripts/v10_infrastructure_deploy.py" "$@"
