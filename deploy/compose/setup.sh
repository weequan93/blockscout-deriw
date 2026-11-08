#!/usr/bin/env bash
# Initialize the current environment directory without replacing local settings.
set -euo pipefail
template_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
deployment_dir=$(pwd -P)
case "${deployment_dir##*/}" in
  docker-compose.dev|docker-compose.test|docker-compose.prod) ;;
  *) echo 'Run this command from docker-compose.dev, docker-compose.test or docker-compose.prod.' >&2; exit 1 ;;
esac
test -f "$deployment_dir/docker-compose.yml"
test -f "$deployment_dir/.env.sample"
umask 077
mkdir -p "$deployment_dir/envs"
copy_new() {
  if [[ ! -e "$2" && ! -L "$2" ]]; then
    cp -- "$1" "$2"
    chmod 600 "$2"
    echo "Created ${2#"$deployment_dir/"}"
  fi
}
copy_new "$deployment_dir/.env.sample" "$deployment_dir/.env"
copy_new "$template_dir/docker-compose.local.yml.sample" "$deployment_dir/docker-compose.local.yml"
for source in "$template_dir"/envs/*.env.sample; do
  filename=${source##*/}
  copy_new "$source" "$deployment_dir/envs/${filename%.sample}"
done
echo 'Keep existing server values. Fill blank settings before running docker compose config --quiet.'
