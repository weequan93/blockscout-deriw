# Dev origin certificate recovery

The 7 November 2026 Nginx patch preflight found an expired certificate at
`/data/blockscout/docker-compose/proxy/ssl/dev.deriw.com/fullchain.pem`.
It expired **18 October 2026 at 02:11:54 UTC**, matches the explorer hostname,
and was issued by Alibaba Cloud Computing Ltd. The server has no Certbot,
acme.sh or discovered renewal configuration. This diagnosis concerns the
origin certificate served by `proxy`; it does not establish the certificate
served by any external CDN.

The Nginx preparation
`/data/blockscout-dev-private/infrastructure-releases/infra-20261107T124139Z-0223ab4a/state.json`
is still `prepared`. Staging failed before proxy replacement or migration pause.
Keep it and the earlier incomplete preparation. Redis and PostgreSQL can proceed
independently using the [infrastructure guide](infrastructure-patches.md).

## Obtain and stage the replacement

The certificate administrator/DevOps team must obtain a **newly issued**
certificate through the existing Alibaba Cloud account or their certificate
management process. Downloading the old expired package does not renew it.
Issuance, renewal and deployment are separate steps; this repository does not
purchase certificates or access that account.

Download the **Nginx PEM and matching KEY** package. A manually generated CSR
keeps its private key with the original administrator; Alibaba's downloaded
package may contain only the certificate in that case. Follow Alibaba's
[download instructions](https://www.alibabacloud.com/help/en/ssl-certificate/download-an-ssl-certificate).
Do not put the key in Git, chat, tickets or public logs.

Cover **all TLS server names** sharing the certificate in the captured Nginx
configuration, not just the explorer. The prior check found four HTTPS listeners.
A wildcard covers only one DNS label: `*.deriw.com` does not cover
`explorer.dev.deriw.com`; use the appropriate exact names or wildcard.
See Alibaba's [Nginx installation and domain coverage guide](https://www.alibabacloud.com/help/en/ssl-certificate/install-ssl-certificates-on-nginx-servers-or-tengine-servers).

Transfer the files securely to a new root-owned directory on the dev server,
for example `/data/blockscout-dev-private/origin-cert-incoming`. Set that
directory to mode `0700` and the key to `0600`. Prepare these PEM files there:

- `fullchain.pem`: leaf certificate first, then the supplied intermediates.
- `privkey.pem`: the matching unencrypted Nginx private key.
- `chain.pem`: intermediates, if the current `ssl_trusted_certificate` directive
  references that separate file. Preserve the complete issued chain.

## Review before installing

Select the pinned `r4` tools checkout using the infrastructure guide. Run on
the dev server as root after the replacement files have been staged:

```bash
cd /data/blockscout-v10-origin-certificate
python3 deploy/scripts/v10_certificate_review.py \
  --nginx-state /data/blockscout-dev-private/infrastructure-releases/infra-20261107T124139Z-0223ab4a/state.json \
  --certificate /data/blockscout-dev-private/origin-cert-incoming/fullchain.pem \
  --private-key /data/blockscout-dev-private/origin-cert-incoming/privkey.pem
```

If intermediates are supplied separately, add `--chain /private/path/chain.pem`.
The default trust source is the server CA store. Supply `--ca-bundle` only for
a separately authenticated CA bundle; do not treat the presented leaf as a
trusted root. The final origin probe must use the same authenticated trust
policy, with no insecure TLS option.

Expect `certificate_bundle_reviewed`, key and chain checks true, and a future
expiry. The reviewer checks **every captured TLS name**, validity dates, the
matching public key, trust chain and private-key ownership/permissions. It
prints no key material, changes no files/services and makes no database calls.
A successful review does not itself install the replacement.

## Install and reload the existing proxy

DevOps performs this step using its certificate deployment procedure. Schedule
it separately from an infrastructure operation; do not change certificate
files between an operation's preparation and activation.

1. Confirm `proxy` still sees the certificate directory through its original
   directory bind mount. The failed `r3` attempt did not activate any individual
   certificate overlay. If runtime/mounts have changed since that attempt,
   review them before installation.
2. Make a root-only backup of the existing certificate directory, with its
   ownership and permissions, outside Git under
   `/data/blockscout-dev-private/`. Retain all existing infrastructure journals.
3. Install the reviewed files at the existing certificate targets in
   `/data/blockscout/docker-compose/proxy/ssl/dev.deriw.com/`. Keep the private
   key root-owned `0600`; preserve any stricter deployment policy. If the
   configuration references a separate chain file, replace that too. Stage
   complete files and rename them into place; finish the matching pair before
   requesting reload. No Nginx configuration, backend or database change is
   needed for this certificate replacement.
4. Check syntax, then gracefully reload:

   ```bash
   docker exec proxy nginx -t
   docker exec proxy nginx -s reload
   ```

   If syntax fails, restore the backed-up files and investigate before reload.
   The running workers retain their loaded certificate until a successful
   reload. A reload command alone is not proof of successful TLS deployment.
5. Verify the certificate actually served and strict origin HTTPS. From a shell
   in the pinned tools checkout, the following read-only probe bypasses public
   DNS routing and tests `proxy` with the original hostname/SNI:

   ```bash
   python3 - <<'PY'
   import json, sys
   from pathlib import Path
   sys.path.insert(0, 'deploy/scripts')
   import v10_infrastructure_deploy as I
   path = Path('/data/blockscout-dev-private/infrastructure-releases/infra-20261107T124139Z-0223ab4a/state.json')
   _, inputs = I.load(path)
   print(json.dumps({'read_only': True, 'origin_https': I.S.http_probe(
       I.D, inputs['public_host'], 'proxy', '/deposits', tls=True)}, indent=2))
   PY
   ```

   Expect `http_status: 200` and `transport_exit: 0`. Curl exit `60` means
   certificate verification still fails. Do not bypass it. If a private CA is
   actually required, the administrator must supply an authenticated trust
   bundle consistently to the probe and the fresh Nginx preparation.

The native migration keeps its saved cursor throughout a certificate-only
reload. Confirm its current saved timestamp/status with the existing native
status command. No migration restart or cursor edit is part of renewal.

## Prepare Nginx again

Once strict origin HTTPS succeeds, use `r4` and create a **fresh** Nginx
preparation, inheriting any completed Redis/PostgreSQL upgrades:

```bash
cd /data/blockscout-v10-origin-certificate
bash deploy/steps/dev-v10-infrastructure.sh --run --component nginx \
  --normalization-state /data/blockscout-dev-private/v10-native-normalization/job-20261106T103901Z-59bb4dc6/state.json
```

Do not resume the old `0223ab4a` preparation: it captured expired certificate
bytes and the earlier peer baseline. It never requested activation, so there
is no image rollback to perform for it. Keep that evidence unchanged.

New `r4` preparations retain private historical certificate copies but leave
TLS assets already covered by an existing directory bind mount live through
that mount. Future normal file replacement and reload therefore work without
recreating the proxy. Loaded Nginx configuration remains frozen. Earlier
journals keep their original mount contract. Do not repoint any existing
controller checkout or edit a saved journal to change these roles.

DevOps should also establish certificate expiry monitoring and its renewal
owner/process. No automatic issuance/renewal service was installed by this work.
