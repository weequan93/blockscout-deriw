# DeriW bridge and rollup settlement metrics

For bridge metrics served inside the backend's existing `/metrics` endpoint,
see [native bridge metrics](native-bridge-metrics.md). The host service below
remains available while existing installations move to the native collector.

The `deriw-bridge-metrics.service` exporter observes L2 → L3 deposits, L3 → L2
withdrawals, and the settlement stages shown on explorer blocks/transactions.
It serves Prometheus text at `http://127.0.0.1:9160/metrics` on the dev host.
It uses the running backend's existing connection configuration and does not
require a new database password. Nothing changes in the frontend.

The exporter runs alongside the native normalization service. Installation starts
only this new monitoring service. It does not restart backend/frontend, restore
the held indexer metrics collector, change normalization, modify database rows,
run migrations, or enable other migrators. Keep all existing pinned worktrees.

## Final release and deployed state

The finalized release is tagged **`deriw-bridge-metrics-2026-11-07`** on branch
`feat/deriw-bridge-metrics`. Its exporter implementation is unchanged from
**`74f2bed18440fad335b5e66ff87174467b2261cd`**, which the operator deployed and
verified on dev at **2026-11-07 08:15:33 UTC / 16:15:33 SGT**.

The operator's output confirmed an active systemd service, `exporter_up=1`, all
five query sections successful, and complete indexed-message samples in both
directions. The [live qualification record](../../deploy/audits/deriw-bridge-metrics-live-2026-11-07.json)
captures that dated observation. The [local validation record](../../deploy/audits/deriw-bridge-metrics-validation-2026-11-07.json)
belongs to implementation commit `74f2bed184`; its file hashes and pre-deployment
status are historical evidence and are retained unchanged.

The existing dev service points to **`/data/blockscout-bridge-metrics` at
`74f2bed18440fad335b5e66ff87174467b2261cd`**. Leave that checkout pinned. The final
release adds the runbook, step file and live evidence; the already active exporter
does not need a reinstall or application rollout for these additions.

Prometheus scraping, an external private network route, and Grafana dashboard
installation have not been verified. The successful local `/metrics` response
qualifies the exporter endpoint only. This procedure targets the captured
**DeriW dev v10.2.6 deployment**; a production host needs its own reviewed inventory.
The [main dev operations guide](dev-v10.md) and [frontend rollout guide](frontend-v2.7.2.md)
cover the application deployment that this monitoring service observes.

## Meaning of the states

Blockscout uses `to_l2`/`from_l2` for the child rollup in its schema. On this
deployment the parent is Arbitrum Sepolia (L2), and the child is DeriW (L3).
Prometheus directions are explicitly `l2_to_l3` and `l3_to_l2`.

| Domain | State | Meaning |
| --- | --- | --- |
| Deposit/withdrawal | `initiated` | Originating message has been indexed. |
| Deposit/withdrawal | `sent` | Native indexer recorded the message as sent. |
| Deposit/withdrawal | `confirmed` | Native indexer recorded rollup confirmation. |
| Deposit/withdrawal | `relayed` | Native indexer recorded destination execution. |
| Rollup settlement | `processed` | “Processed on rollup”: no batch association for this indexed block. |
| Rollup settlement | `sealed` | Batch association exists, but no indexed commitment transaction. The backend also supports this intermediate state. |
| Rollup settlement | `sent` | “Sent to Arbitrum Sepolia”: commitment transaction is indexed. |
| Rollup settlement | `confirmed` | “Confirmed on Arbitrum Sepolia”: confirmation transaction is indexed. |

Message states and rollup settlement stages are different concepts. Deposit flows
normally move from `initiated` to `relayed`; do not require every intermediate
state. For withdrawals, `confirmed` does not mean the user has claimed/executed
the withdrawal on the parent. A `relayed` row is an indexed execution record,
not an independently verified token balance change or an audit of retryable
ticket success. The metrics reproduce the explorer's native recorded states.

The frontend marks sent/confirmed stages as pending when their parent transaction
is `unfinalized`. The exporter keeps this distinction in `parent_finality` and
`deriw_rollup_stage_parent_transaction_finalized`. A confirmation transaction
existing and that transaction being finalized on the parent are separate facts.

## Available series

| Metric | Labels / meaning |
| --- | --- |
| `deriw_bridge_sample_messages` | `direction`, `status`: number of messages currently in each state in the sampled window. Gauges, not counters. |
| `deriw_bridge_sample_complete` | `direction`: 1 if the sample includes all indexed messages in that direction. |
| `deriw_bridge_sample_size` | `direction`: up to 10,000 latest message IDs. |
| `deriw_bridge_sample_highest_message_id` | `direction`: highest indexed message ID in the sample. |
| `deriw_bridge_sample_highest_relayed_message_id` | `direction`: highest relayed ID found; omitted if none. Does not prove every lower ID completed. |
| `deriw_bridge_sample_oldest_pending_age_seconds` | `direction`: oldest non-relayed sampled message with known origin time. |
| `deriw_bridge_sample_pending_without_timestamp` | `direction`: non-relayed rows lacking origin time, which cannot contribute to the age metric. |
| `deriw_bridge_sample_missing_origination` | `direction`: rows missing origin transaction metadata, including relayed rows. |
| `deriw_bridge_sample_completion_recorded` | `direction`: rows with a completion hash, independently of their native status. |
| `deriw_bridge_tracker_next_parent_block` | `tracker=deposits/settlement`: live discovery cursor in **parent L2** block numbers, if readable. |
| `deriw_bridge_tracker_running` | Tracker process exists. A live process does not prove progress. |
| `deriw_bridge_tracker_observation_success` | Whether the tracker state was readable this poll. |
| `deriw_rollup_stage_block_number` | `stage=processed/sent/confirmed`: **L3** block progress. Processed is latest indexed consensus block; sent is latest indexed batch's end block; confirmed is highest indexed block with a confirmation association. These are cumulative stage watermarks. |
| `deriw_rollup_stage_parent_transaction_finalized` | `stage=sent/confirmed`: parent transaction supplying the watermark is finalized (1) or unfinalized (0). |
| `deriw_rollup_stage_parent_timestamp_seconds` | Timestamp of parent transaction supplying that watermark. |
| `deriw_rollup_stage_observed` | `stage=sent/confirmed`: 0 means the successful query found no record, distinct from a failed query. |
| `deriw_rollup_latest_published_batch_number` | Latest batch indexed from the parent chain. |
| `deriw_rollup_sample_blocks` | `stage`, `parent_finality`: mutually exclusive current stage counts for the latest 1,000 indexed consensus blocks. |
| `deriw_rollup_sample_size` | Number of classified consensus blocks, up to 1,000. |
| `deriw_rollup_sample_lowest_block`, `deriw_rollup_sample_highest_block` | Actual block-number bounds of that recent sample. |
| `deriw_rollup_sample_latest_block_timestamp` | Latest sampled block's timestamp. |
| `deriw_bridge_query_success` | `section`: whether that read completed within its query/plan budgets. |
| `deriw_bridge_exporter_up` | Latest poll succeeded, runtime was stable, and observation is at most 150 seconds old. This alone does not mean every section succeeded. |
| `deriw_bridge_observation_timestamp_seconds` | Backend snapshot timestamp. |
| `deriw_bridge_exporter_last_success_timestamp_seconds` | Host time of last successful polling attempt. |

All state counts are of **indexed** messages/blocks. Undiscovered chain activity
is absent until the tracker catches up. A message sample with `complete=1` covers
all indexed rows, not all messages that exist onchain. `complete=0` means older
indexed messages are outside the window; never present the counts as lifetime
totals or infer that an old withdrawal is absent/completed. The recent rollup
sample can contain no confirmed blocks while older blocks are confirmed.

No message IDs, hashes, addresses or URLs are labels. The highest-ID metrics
contain numeric values, not per-message series. Watermarks can decrease after a
reorg or index correction; the exporter does not preserve a misleading maximum.
Watermarks are not a proof of contiguous history or complete node synchronization.
Do not subtract a parent-chain cursor from an L3 block number.

## Bounded reads and freshness

The process polls once per 60 seconds. Prometheus scrapes return a cached snapshot
and do not execute SQL or contact RPC. Sections use independent short transactions,
so they can differ slightly in observation time. Each read uses a separate read-only
transaction, a 1.5 second PostgreSQL statement timeout, 100 ms lock timeout and
an EXPLAIN cost budget. Messages use an indexed direction/ID seek with 10,001
rows maximum (one sentinel); recent block classification uses 1,000 consensus
blocks and bounded primary-key probes. There is no full-table block/transaction
count. The confirmed watermark query matches the explorer's highest-confirmed
block query; it is omitted if its plan or runtime exceeds the budget. This can
happen when the index is far behind or its statistics are stale. Do not increase
the budget or add indexes on the large tables without a separate review.

A failed section exports `query_success=0` and omits its data series; it never
substitutes zero or a previous value. On a failed overall read, backend lifecycle
change during observation, or expired snapshot, only exporter health/time gauges
remain. Query errors and private configuration are suppressed. `/healthz` checks
only that the exporter process serves HTTP, not that data collection succeeds.

## One-time installation on the dev server

For an **existing active installation**, use the check instructions below. For a
**new installation** on the configured dev deployment, run this entire block in
the dev server terminal. It fetches the immutable release tag, creates a pinned
worktree, runs the live read preflight, installs the service, and waits for a
fresh metrics response with all five query sections successful:

```bash
bash <<'INSTALL_DERIW_BRIDGE_METRICS'
set -euo pipefail
git -C /data/blockscout fetch origin tag deriw-bridge-metrics-2026-11-07
metrics_commit=$(git -C /data/blockscout rev-parse 'refs/tags/deriw-bridge-metrics-2026-11-07^{commit}')
if ! test -e /data/blockscout-bridge-metrics; then
  git -C /data/blockscout worktree add --detach /data/blockscout-bridge-metrics "$metrics_commit"
fi
cd /data/blockscout-bridge-metrics
test "$(git rev-parse HEAD)" = "$metrics_commit"
test -z "$(git status --porcelain)"
bash deploy/steps/deriw-bridge-metrics.sh install
INSTALL_DERIW_BRIDGE_METRICS
```

If `/data/blockscout-bridge-metrics` already contains the qualified `74f2bed184`
installation, the revision check intentionally stops this new-install sequence.
Keep the running installation and use its existing endpoint; do not move its
checkout or replace its service just to obtain documentation updates.

For a bounded live diagnostic without installing a service, the pinned exporter
checkout also provides:

```bash
python3 deploy/scripts/deriw_bridge_metrics.py --once
```

The JSON contains only aggregate numbers, state names and process/cursor metadata.
Review `sections.*.ok`; false means data is unavailable within the fixed budgets.

The service restarts after process failure/host reboot. It has no migration cursor
or transactional work to resume. A backend restart merely invalidates a poll;
collection retries automatically. Existing normalization remains managed by its
own service and journal. Installation refuses a different existing service unit.
It also performs a live read before writing the unit, and refuses installation
if a required section fails. Its public preflight JSON reports a fixed failure
category such as `plan_budget` or `statement_timeout` without raw SQL/errors.

Default binding is host loopback. A Prometheus process on another machine or in a
container cannot reach the host's loopback via its own `127.0.0.1`. DevOps can
select a trusted private host IPv4 address with `--listen ADDRESS` at installation
and allow that port only from the monitoring network, or arrange a private proxy.
This exporter does not add a public explorer route or modify firewall rules.

An existing unit is tied to its installed source directory and bind arguments.
The installer refuses a different unit definition. To change the private bind
address, DevOps should first review the current unit with
`systemctl cat deriw-bridge-metrics.service`, keep its pinned source root, and
plan an explicit service-unit replacement. A second `--install-service` call
with different arguments does not silently reconfigure the running service.

For Prometheus running on the same host (outside Docker):

```yaml
scrape_configs:
  - job_name: deriw_bridge
    scrape_interval: 30s
    scrape_timeout: 5s
    static_configs:
      - targets: ["127.0.0.1:9160"]
```

After DevOps adds the scrape job, verify Prometheus reports
`up{job="deriw_bridge"} == 1` and `deriw_bridge_exporter_up == 1`, with all five
`deriw_bridge_query_success` sections equal to 1. HTTP scrape success alone
does not establish successful database collection. Check the configured target's
address from the actual Prometheus network namespace before changing the host.

Uninstall only this observer with `systemctl disable --now deriw-bridge-metrics.service`
and remove its reviewed unit file followed by `systemctl daemon-reload`. Do not
stop the separate native normalization service or delete any deployment journals.

## Check an existing installation

On the current dev server, no fetch or reinstall is needed to inspect the active
endpoint:

```bash
systemctl is-active deriw-bridge-metrics.service
curl -q --noproxy '*' --fail --silent --show-error --max-time 5 \
  http://127.0.0.1:9160/metrics
```

From a documentation/ops checkout containing the final release step file, use:

```bash
bash deploy/steps/deriw-bridge-metrics.sh check
```

`check` is the default. It reads systemd and the cached HTTP endpoint, waits up
to 45 seconds for fresh successful collection, and prints the metrics. It starts
no services, creates no maintenance client and performs no direct database/RPC
read. Exit 0 means fresh exporter data and all five query sections were observed;
exit 1 means inactive/unavailable data, and exit 2 means incorrect usage or missing
host tools. The original `74f2bed184` checkout predates this step file; use the
direct `systemctl`/`curl` commands there.

## Dashboard queries

Use these table/graph queries after checking exporter/section health:

```promql
# Deposit state counts
deriw_bridge_sample_messages{direction="l2_to_l3"}

# Withdrawal state counts
deriw_bridge_sample_messages{direction="l3_to_l2"}

# Three cumulative L3 block progress lines
deriw_rollup_stage_block_number

# Current settlement states of the recent block sample, preserving parent finality
deriw_rollup_sample_blocks

# Live discovery cursor progress (parent L2 block numbers)
deriw_bridge_tracker_next_parent_block
```

Map the `stage` legend to **Processed on rollup**, **Sent to Arbitrum Sepolia**,
and **Confirmed on Arbitrum Sepolia**. Preserve the finality indicator so pending
parent transactions are visible. Counts are gauges: `rate()` does not represent
message throughput. Alert on `deriw_bridge_exporter_up == 0`, individual
`deriw_bridge_query_success == 0`, and `deriw_bridge_sample_complete == 0` before
interpreting count/watermark charts. Missing series must display as unavailable.
Choose business delay thresholds using the actual chain's posting/confirmation
cadence; no generic fixed withdrawal completion SLA is assumed.

## Local validation

```bash
python3 -m unittest discover -s deploy/tests -p 'test_deriw_bridge_metrics.py' -v
python3 deploy/tests/check_deriw_bridge_metrics.py
```

The second command uses locally cached application and PG15 images on a disposable
internal Docker network. It checks the actual RPC/SQL, message states, origin
metadata gaps, settlement/finality distinctions, reorg regression, window limits,
read-only enforcement, timeouts and plan budgets. It accesses no live deployment.

Finalization retained the deployed exporter/query file hashes. The new step file
passed `bash -n`, cached-endpoint check/install flow verification with stubbed host
tools, invalid-argument rejection, and inactive-service reporting. No systemd or
Docker operation was executed on the development Mac for those step-file checks.
