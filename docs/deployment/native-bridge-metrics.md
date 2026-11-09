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

## Compare the explorer with the parent chain

The native collector also checks deposit metadata and compares a bounded sample
of withdrawals and batches with the configured `INDEXER_ARBITRUM_L1_RPC` and
`INDEXER_ARBITRUM_L1_ROLLUP_CONTRACT`. These checks are read-only. They do not
repair records, advance any scanner cursor or execute bridge transactions.
All RPC work happens during polling; scrapes continue to read the cached snapshot.

Check `deriw_bridge_verification_success{section="..."}=1` before interpreting
the corresponding values. Sections are `deposits`, `withdrawals`, `batches`,
`batch_head`, `parent_latest`, `parent_safe` and `parent_finalized`. RPC failure,
unsupported tags, malformed responses and missing/pruned receipts omit the
affected comparison values and report verification failure. They never become
zero mismatches or a claim that a transaction was not relayed.

| Question | Metric | Interpretation |
| --- | --- | --- |
| Are relayed deposits missing information? | `deriw_bridge_deposit_sample_incomplete_relayed` | Desired value `0`; checks all four parent-origin fields and the completion hash. |
| Which deposit fields are missing? | `deriw_bridge_deposit_sample_missing_field{field="hash"}` | Counts missing fields; pending deposits can legitimately lack completion. Each field is a separate series. |
| Has an on-chain withdrawal relay been missed? | `deriw_bridge_withdrawal_sample_missing_relay` | Desired value `0`; counts `Outbox.isSpent(id)=true` while the explorer status is not `relayed`. |
| Is a relayed withdrawal missing its parent transaction hash? | `deriw_bridge_withdrawal_sample_missing_completion` | Desired value `0`; checks spent withdrawals. |
| Does the explorer say relayed before the Outbox does? | `deriw_bridge_withdrawal_sample_unexpected_relay` | Desired value `0`; investigate RPC lag, a reorg or index inconsistency. |
| Did the latest indexed batch get posted? | `deriw_rollup_latest_batch_onchain_posted` | `1` verifies a successful canonical receipt containing `SequencerBatchDelivered` for that batch and configured SequencerInbox. |
| Has that publication reached the parent safe/finalized checkpoint? | `deriw_rollup_latest_batch_parent_finalized{tag="safe"}` / `{tag="finalized"}` | Separate `0`/`1` values; a newly posted batch can normally be `0`. |
| Does the explorer's batch publication record disagree? | `deriw_rollup_batch_sample_publication_mismatch` | Desired value `0`; checks canonical receipt, successful publication event and recorded parent block. |
| Is the explorer's displayed parent finality behind/ahead? | `deriw_rollup_batch_sample_finality_mismatch` | Desired value `0`; compares the indexed flag against the parent **safe** checkpoint, following Blockscout's rule. |
| Is the explorer missing recently published batches? | `deriw_rollup_batch_indexing_lag` | Difference between parent `SequencerInbox.batchCount()-1` and highest indexed batch; does not detect internal gaps. |
| Is finality updating configured? | `deriw_rollup_finality_tracking_enabled` | `1` when lifecycle finality tracking is configured. Also check that `tracker="settlement"` is running. |
| Is a scanner behind the parent head? | `deriw_bridge_tracker_lag_blocks{tracker="withdrawals"}` | Parent head minus the task's next block plus one, clamped at zero. Each tracker is a separate series. |

`tracker="withdrawals"` observes the `new_executions` cursor. The older
`tracker="settlement"` observes `new_batches`; the two cursors can differ greatly.
The parent heads are also exposed as `deriw_bridge_parent_block_number{tag="latest"}`,
`{tag="safe"}` and `{tag="finalized"}`. The configured finality flag comes from
`INDEXER_ARBITRUM_BATCHES_TRACKING_L1_FINALIZATION_CHECK_ENABLED`.

Blockscout's lifecycle updater labels transactions `finalized` once they reach
the parent RPC **safe** block. This is separate from the RPC's stricter
**finalized** tag and from confirmation of a DeriW rollup assertion. When the
updater is disabled, newly imported lifecycle transactions are immediately
labelled finalized by the existing indexer; the configuration gauge and
independent comparisons make that behavior visible.

Coverage is explicit. Deposit completeness covers the latest 10000 indexed
deposits, withdrawal comparisons cover the latest 32 indexed withdrawals and
batch receipt comparisons cover the latest 16 indexed batches. Each group
exports `..._sample_size` and `..._sample_truncated`; `truncated=1` means older
records are outside the check. These gauges cannot establish complete historical
coverage or detect bridge messages entirely absent from the index. The existing
`deriw_bridge_sample_complete` describes sample coverage, not metadata completeness.

Parent contract getters and head requests, at most 32 `isSpent` calls, at most
16 receipts, a batch-count read and at most 16 canonical block reads total at
most 70 RPC items per polling cycle. Three HTTP batches each have a 4-second
HTTP timeout and 4.5-second task deadline, with no scanner retries. Contract
reads are pinned to the observed parent head. A missing receipt or unavailable
finality tag is unknown, not proof of an unpublished batch. Receipt availability
and both tags are required for the combined batch comparison.

Allow transient indexing/observation races when alerting, for example require a
mismatch to persist for 5–10 minutes. Useful PromQL expressions include:

```promql
deriw_bridge_deposit_sample_incomplete_relayed > 0
deriw_bridge_withdrawal_sample_missing_relay > 0
deriw_rollup_batch_sample_publication_mismatch > 0
deriw_rollup_batch_sample_finality_mismatch > 0
deriw_bridge_verification_success == 0
```

Comparison values are omitted on failure, so verification-failure alerts must
accompany mismatch alerts. Do not require the latest batch's strict-finalized
gauge to be permanently `1`: new publications normally have a finality delay.

## Upgrade the existing dev metrics through Git

The checked-in helper targets the already-upgraded dev chain `18417507517`, its
`docker-compose.dev` directory and project `docker-compose`. Run as root on that
server after preserving any local source edits:

```bash
cd /data/blockscout
git switch deriw-dev
git pull --ff-only origin deriw-dev
python3 deploy/scripts/deploy_dev_bridge_metrics.py
```

The helper builds committed source through `git archive`, backs up private
configuration, updates the image tag and source revision, and recreates only
`backend`. It preserves the other services, database mounts, start-only startup
and paused normalization. Readiness checks require the five existing database
queries and all seven new verification checks. Positive mismatch counts are
reported as data; they do not make a successful comparison fail readiness.
The helper does not disable a host exporter or change Prometheus targets.

This restarts the backend. The existing withdrawal scanner still has no durable
empty-range checkpoint and can replay history from the last stored relay after
a restart; these metrics do not fix that indexing behavior. The deployment keeps
the old image and private configuration backup. If verification fails after
activation, it prints the backup path and retains diagnostics; it does not
automatically roll back the backend. Do not run this dev-specific helper on
testnet or production.

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
original native series with the legacy renderer, and verifies read-only
enforcement, statement timeouts and plan budgets. Extended checks cover
deposit metadata, relay mismatches, canonical publication receipts, safe versus
finalized semantics, unknown RPC results and sample boundaries:

```bash
python3 deploy/tests/check_native_bridge_metrics.py \
  --app-image deriw-blockscout:v10-nft-compile \
  --postgres-image postgres:15-alpine
```

This check uses locally cached images containing the compiled dependency tree,
an internal Docker network and disposable tmpfs storage. It does not start the
Blockscout application or read a deployed environment.
