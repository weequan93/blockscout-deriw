# Nginx upgrade with the existing origin certificate

For the subsequently confirmed duplicate server blocks, use the
[combined cleanup and upgrade in revision r9](nginx-duplicate-servers.md).
It retains this explicit certificate-verification choice.

On 8 November 2026 the operator explicitly requested skipping HTTPS verification
for the Nginx patch. Revision `r8` adds `--skip-https-verification` for a fresh
Nginx `--prepare` or `--run`. It skips certificate chain, expiry and hostname
validation in the deployment's direct-origin curl probe. HTTPS must still
connect and return HTTP 200. This does not renew a certificate or establish
trusted origin TLS.

The option applies only to this deployment probe. It changes no certificate,
private key, Nginx TLS directive, browser/CDN policy, backend or database image.
HTTP deposits and API routes, HTTPS response, websocket upgrade, source-map
restrictions, configuration checks and all application/migration continuity
checks remain required. The candidate must pass staging before the live proxy
is replaced.

## Run on the dev server

Use a new pinned checkout and a fresh journal. Earlier failed preparations,
including `infra-20261108T022340Z-89c7a316`, retain their strict policy and must
not be edited. They stopped before activation or migration pause.

```bash
bash <<'UPGRADE_NGINX_EXISTING_CERTIFICATE'
set -euo pipefail
git -C /data/blockscout fetch origin tag deriw-infra-patches-2026-11-08-r8
if ! test -e /data/blockscout-v10-nginx-tls-option; then
  git -C /data/blockscout worktree add --detach /data/blockscout-v10-nginx-tls-option deriw-infra-patches-2026-11-08-r8
fi
cd /data/blockscout-v10-nginx-tls-option
test "$(git rev-parse HEAD)" = "$(git rev-parse deriw-infra-patches-2026-11-08-r8^{commit})"
test -z "$(git status --porcelain)"
bash deploy/steps/dev-v10-infrastructure.sh --run --component nginx \
  --skip-https-verification \
  --normalization-state /data/blockscout-dev-private/v10-native-normalization/job-20261106T103901Z-59bb4dc6/state.json
UPGRADE_NGINX_EXISTING_CERTIFICATE
```

Expect `infrastructure_finished`, target `1.31.8`,
`https_certificate_verification: skipped_by_operator` and
`https_route_check_required: true`. The final `normalization` result must be
running, waiting or completed. A waiting checkpoint retains its automatic
resume behavior. The job's original token, cursor, frozen inputs and disk
reserve remain; there is no cumulative growth cap.

## Resume, status and rollback

Keep the printed infrastructure state and every previous checkout/journal.
From the same new checkout, use the saved state without the option:

```bash
INFRA_STATE=/data/blockscout-dev-private/infrastructure-releases/infra-REPLACE_WITH_PRINTED_ID/state.json
bash deploy/steps/dev-v10-infrastructure.sh --status --state "$INFRA_STATE"
bash deploy/steps/dev-v10-infrastructure.sh --resume --state "$INFRA_STATE"
```

To return to its original Nginx binary, use `--rollback --state "$INFRA_STATE"`.
The choice is authenticated by the immutable input hash and applies to baseline,
candidate, live qualification and rollback checks. Resume cannot add or change
it. Existing journals without the field still require strict verification.
`--tls-ca` and this option are mutually exclusive, and the option is rejected
for Redis/PostgreSQL. Later infrastructure or frontend operations must inherit
the completed deployment proof as described in the
[infrastructure guide](infrastructure-patches.md).

The observed expired origin certificate remains a separate configuration item;
normal renewal can use the retained directory mount and the
[certificate guide](origin-certificate.md).

## Validation

The [validation record](../../deploy/audits/nginx-https-verification-validation-2026-11-08.json)
records 344 passing v10 unit tests and two disposable Nginx fixtures. The new
fixture proves strict validation rejects an expired certificate, then stages,
upgrades and rolls back with the explicit option while preserving certificate,
key, configuration and unrelated containers. The strict fixture still verifies
trusted HTTPS, renewal by reload and binary rollback. Existing native handoff
tests cover preserved inputs/cursor, completion and rejection of changed
deployment evidence. These tests do not establish that the live upgrade has
finished; that requires the server's final JSON.
