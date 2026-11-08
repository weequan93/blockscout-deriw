# Remove duplicate Nginx server entries

**Completed on dev at 10:54 SGT on 8 November 2026.** The operator's final JSON
confirmed Nginx `1.31.8`, eight removed blocks and a resumed migration waiting
for autovacuum at cursor `13711054`. The journal is
`/data/blockscout-dev-private/infrastructure-releases/infra-20261108T025332Z-2683201e/state.json`.
The [rollout record](../../deploy/audits/infrastructure-rollout-status-2026-11-08.json)
retains the final output. The application procedure below is historical for
this dev window; use the saved journal to inspect or recover that operation.

The operator's 8 November 2026 read-only inventory found Nginx `1.31.1`
with 13 loaded HTTP server blocks. The first five in `default.conf` handle
the two HTTPS names and ports 80, 8080 and 8081. Another five in that file
repeat them. `explorer.conf` repeats the HTTP entry, and `microservices.conf`
repeats the two auxiliary ports. Those last two bodies differ from the active
ones, so replacing the active entries with them would change configuration.

Revision `r9` removes only names or complete server blocks already shadowed
by earlier entries on the same address and port. It follows the master's
actual include structure, including ordered wildcard includes. The first
effective server, its routes and all unrelated configuration bytes stay.
Unique aliases remain. Ambiguous address overlaps, explicit shadowed defaults,
dynamic names/includes and socket protocol differences stop for review.
Primary alias changes also stop when server-name variables or redirects could
change behavior. This follows Nginx's distinction between
[server names and socket defaults](https://nginx.org/en/docs/http/request_processing.html).

The tool creates independent immutable forward and rollback configurations.
It does not edit a previously qualified snapshot or a controller's old checkout.
The selected configuration is mounted into the proxy and the Nginx entrypoint
uses it directly, including after a restart. The latest qualified Compose
snapshot and native runtime binding remain the deployment source for later
operations; generic Compose commands using old declarations are not that source.

## Apply the repair and pending upgrade

Run on the **dev Linux server as root**. This fresh operation can both repair
the loaded configuration and upgrade Nginx to `1.31.8`. If that exact reviewed
image is already installed, it performs a configuration repair with the same
image instead. The explicit certificate-verification skip requested by the
operator is retained; HTTPS responses and all existing route checks still apply.

```bash
bash <<'REPAIR_NGINX_SERVER_NAMES'
set -euo pipefail
git -C /data/blockscout fetch origin tag deriw-infra-patches-2026-11-08-r9
if ! test -e /data/blockscout-v10-nginx-cleanup; then
  git -C /data/blockscout worktree add --detach /data/blockscout-v10-nginx-cleanup deriw-infra-patches-2026-11-08-r9
fi
cd /data/blockscout-v10-nginx-cleanup
test "$(git rev-parse HEAD)" = "$(git rev-parse deriw-infra-patches-2026-11-08-r9^{commit})"
test -z "$(git status --porcelain)"
bash deploy/steps/dev-v10-infrastructure.sh --run --component nginx \
  --repair-nginx-duplicates --skip-https-verification \
  --normalization-state /data/blockscout-dev-private/v10-native-normalization/job-20261106T103901Z-59bb4dc6/state.json
REPAIR_NGINX_SERVER_NAMES
```

The candidate must pass `nginx -t` with **no conflicting-server-name warnings**,
plus HTTP/HTTPS routes, websocket and source-map checks before activation.
The same checks repeat on the live forward selection. The operation briefly
pauses the same native job, recreates only `proxy`, verifies the database
checkpoint and application health, and resumes that job. Backend/frontend and
database containers are preserved. Certificate/key bytes remain unchanged.

Success is `infrastructure_finished`, target `1.31.8`, and a native result of
running, waiting or completed. The public `nginx_duplicate_server_review`
reports planned removed blocks/names. For the inventoried layout, expect eight
removed blocks, leaving five effective servers. `configuration_only` indicates
whether the reviewed target image was already installed. The usual disk reserve,
worker holds and absence of a cumulative growth cap remain.

## Resume and rollback

Retain the new printed infrastructure journal and all previous checkouts and
evidence. After interruption, use that state from the same `-nginx-cleanup`
checkout. Do not prepare again after an activation request.

```bash
INFRA_STATE=/data/blockscout-dev-private/infrastructure-releases/infra-REPLACE_WITH_PRINTED_ID/state.json
bash deploy/steps/dev-v10-infrastructure.sh --status --state "$INFRA_STATE"
bash deploy/steps/dev-v10-infrastructure.sh --resume --state "$INFRA_STATE"
```

The two preparation flags cannot be added when resuming. Their choices are
authenticated in the new inputs. `--rollback --state "$INFRA_STATE"` selects
the captured original configuration and image; it can therefore restore the
original duplicate warnings. Rollback retains current data and the native cursor.
Earlier journals without the repair field keep their original contract.

## Validation

The [validation record](../../deploy/audits/nginx-duplicate-servers-validation-2026-11-08.json)
contains unit tests and two local disposable Nginx fixtures. They exercise
cleanup with the `1.31.1` to `1.31.8` upgrade and cleanup when `1.31.8` is already
installed, then rollback. The fixtures preserve certificate/key bytes, unique
alias behavior, stats/visualizer ports, public routes and unrelated containers,
and require the candidate and live forward configuration to have no duplicate
name warnings. Native handoff tests preserve frozen inputs and cursor.
The operator's subsequent final JSON, recorded separately above, establishes
completion of the live operation.
