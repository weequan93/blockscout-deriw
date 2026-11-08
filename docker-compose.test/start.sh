#!/usr/bin/env bash
set -euo pipefail
deployment_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec bash "$deployment_dir/../deploy/compose/start.sh" "$deployment_dir" "$@"
