# Bridge metrics in the backend Prometheus endpoint

The backend can expose the DeriW deposit, withdrawal and rollup metrics through
its existing `/metrics` endpoint. Enable the native collector in the backend's
server-owned Compose configuration:

```yaml
services:
  backend:
    environment:
      ARBITRUM_BRIDGE_METRICS_ENABLED: "true"
```

Add this entry to the existing backend environment; keep its other entries.
For a deployment using `env_file`, put the variable in the backend env file.
The feature defaults to disabled and runs only for `CHAIN_TYPE=arbitrum`.
Standalone NFT workers do not start the collector. Dev, test and production
each use their own backend and private configuration.

The collector is independent of `INDEXER_METRICS_ENABLED`. That flag can remain
`false` while native bridge metrics are enabled. The heavy indexer collector
and native normalization keep their existing settings.

## Collection and metric meanings

The supervised worker runs inside the backend. It collects an initial snapshot
on startup and waits 60 seconds between collections. Prometheus scrapes read
one cached snapshot without executing bridge SQL or waiting for the worker.
Snapshots older than 150 seconds expose collection health without stale data.

The native collector preserves the `deriw_bridge_*` and `deriw_rollup_*` metric
names, labels and meanings from the [host exporter](deriw-bridge-metrics.md).
It uses the same SQL with the same limits: 10,000 messages per direction,
1,000 recent consensus blocks, a query plan cost cap of 20,000, a 1.5-second
statement timeout, a 100ms lock timeout and read-only transactions. Query
sections fail independently and omit their data series instead of publishing
zero or retaining previous values. Exceptions do not expose private details.

The metrics are registered only in the default registry used by `/metrics`.
They are not added to `/public-metrics`. Existing runtime/application metrics
continue to use the same endpoint and keep their existing collection behavior.

`deriw_bridge_exporter_up=1` means collection is fresh; also check all five
`deriw_bridge_query_success` series. Message counts describe the latest sampled
indexed records. `missing_origination` measures missing parent metadata and
does not mean the child transaction failed. A relayed message is an indexed
execution record, not independent proof of token balance changes. Parent
transaction finality remains separate from rollup confirmation.

## Move an existing host exporter to the native collector

Release the updated backend source with the flag enabled, using the image tag
and source revision for that release. Keep the existing database mounts and the
start-only command used by the already-upgraded dev database. No schema
migration or checkpoint reset is required for these metrics.

From the appropriate private Compose directory, rebuild/recreate the backend:

```bash
docker compose up -d --build --no-deps backend
```

Check the native endpoint on the server:

```bash
docker exec backend curl -fsS --max-time 10 http://127.0.0.1:4000/metrics
```

Allow the initial collection to finish. Expect
`deriw_bridge_exporter_up 1` and all five query sections to report success.
Prometheus should scrape the existing backend `/metrics` target. When it shares
the backend's Docker network, the target is `backend:4000` with path `/metrics`.
Use the existing backend access configuration for other monitoring layouts.

After verifying the native series, retire the duplicate host observer:

```bash
systemctl disable --now deriw-bridge-metrics.service
```

Remove its port-9160 target from Prometheus after the backend target is working,
so both targets do not contribute duplicate bridge observations. Preserve the
old pinned checkout and journals for recovery. Native normalization stays under
its existing controller; disabling the metrics service does not resume it.

To stop native bridge collection in a later release, set the flag to `false`
and recreate the backend. The historical host exporter remains available as a
separate observer if needed.

## Validation

The indexer and web `prometheus/arbitrum_bridge_metrics_test.exs` tests cover
freshness, partial failures, sample truncation, reorg regressions, startup guards,
failure privacy and cached scrapes through the existing exporter plug.

The isolated PostgreSQL check additionally runs the actual SQL, compares all
native series with the legacy renderer, and verifies read-only enforcement,
statement timeouts and plan budgets:

```bash
python3 deploy/tests/check_native_bridge_metrics.py \
  --app-image deriw-blockscout:v10-nft-compile \
  --postgres-image postgres:15-alpine
```

This check uses locally cached images containing the compiled dependency tree,
an internal Docker network and disposable tmpfs storage. It does not start the
Blockscout application or read a deployed environment.
