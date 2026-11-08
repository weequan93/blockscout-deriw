#!/usr/bin/env bash
# Validate and start exactly one environment; never create or replace its values.
set -euo pipefail
deployment_dir=$1
shift
if [[ ${1:-} == --help || ${1:-} == -h ]]; then
  echo 'Usage: ./start.sh [--build | --no-build] [other docker compose up options]'
  echo 'Run setup once and fill .env, envs/*.env and docker-compose.local.yml on this server.'
  exit 0
fi
case "${deployment_dir##*/}" in
  docker-compose.dev|docker-compose.test|docker-compose.prod) ;;
  *) echo 'Select a devnet, testnet or production Compose directory.' >&2; exit 1 ;;
esac
if [[ ! -f "$deployment_dir/.env" || ! -f "$deployment_dir/docker-compose.local.yml" ]]; then
  echo "Initialize ${deployment_dir##*/} with bash ../deploy/compose/setup.sh, then fill this server's settings." >&2
  exit 1
fi
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  compose=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  compose=(docker-compose)
else
  echo 'Install Docker Compose v2.20 or newer before starting this environment.' >&2
  exit 1
fi
cd -- "$deployment_dir"
# Explicit paths prevent an ambient COMPOSE_FILE from selecting another stack.
selection=(--project-directory "$deployment_dir" --env-file "$deployment_dir/.env" -f "$deployment_dir/docker-compose.yml")
"${compose[@]}" "${selection[@]}" config --quiet
exec "${compose[@]}" "${selection[@]}" up -d "$@"
