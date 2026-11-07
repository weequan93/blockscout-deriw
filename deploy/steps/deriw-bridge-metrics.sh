#!/usr/bin/env bash
# Dev host only. Default: read systemd and the cached metrics endpoint.
# See docs/deployment/deriw-bridge-metrics.md for the pinned release and network scope.
set -euo pipefail

case "${1:-check}" in
  -h|--help)
    printf '%s\n' 'Usage: bash deploy/steps/deriw-bridge-metrics.sh [check|install]' \
      'check: read the active service and cached loopback endpoint (default).' \
      'install: install the default loopback service from this pinned worktree, then check it.'
    exit 0
    ;;
  check|install) mode=${1:-check} ;;
  *) printf '%s\n' 'Expected check or install; see --help.' >&2; exit 2 ;;
esac
if [[ $# -gt 1 ]]; then
  printf '%s\n' 'This step uses the default 127.0.0.1:9160 endpoint; see the guide for private-interface setup.' >&2
  exit 2
fi
for executable in systemctl curl grep; do
  command -v "$executable" >/dev/null || {
    printf '%s\n' 'Run this step on the dev Linux host with systemd and curl available.' >&2
    exit 2
  }
done

if [[ "$mode" == install ]]; then
  repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
  python3 "$repo_dir/deploy/scripts/deriw_bridge_metrics.py" --install-service
fi
if ! systemctl is-active --quiet deriw-bridge-metrics.service; then
  printf '%s\n' 'deriw-bridge-metrics.service is not active.' >&2
  exit 1
fi

deadline=$((SECONDS + 45))
snapshot=''
while (( SECONDS < deadline )); do
  snapshot=$(curl -q --noproxy '*' --fail --silent --max-time 2 \
    http://127.0.0.1:9160/metrics) || snapshot=''
  ready=true
  for expected in \
    'deriw_bridge_exporter_up 1' \
    'deriw_bridge_query_success{section="deposits"} 1' \
    'deriw_bridge_query_success{section="withdrawals"} 1' \
    'deriw_bridge_query_success{section="rollup"} 1' \
    'deriw_bridge_query_success{section="published"} 1' \
    'deriw_bridge_query_success{section="confirmed"} 1'; do
    if ! grep -Fxq -- "$expected" <<< "$snapshot"; then
      ready=false
      break
    fi
  done
  if [[ "$ready" == true ]]; then
    printf '%s\n' "$snapshot"
    exit 0
  fi
  sleep 2
done
printf '%s\n' 'Fresh exporter data with all five successful query sections was not observed within 45 seconds.' >&2
printf '%s\n' "$snapshot"
exit 1
