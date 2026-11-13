# Catch up dev deposits and withdrawal relays

On 2026-11-13 the dev deposit scanner was running every 10 seconds and the live
withdrawal execution scanner every 3 seconds, both covering 50 parent blocks
per iteration. Deposits were 48,327,733 parent blocks behind, withdrawals
4,094,569 blocks behind. The tasks were running; the throughput was too low.
The five recent deposits lacked parent-origin metadata and withdrawal #512
still lacked its already-executed parent relay.

The [read-only RPC probe](bridge-rpc-capacity-probe.md) passed all 68 calls on
the dev server in 21.085 seconds. Both event-bearing 10000-block windows matched
ten 1000-block reads. The two/four requests-per-second phases had zero errors;
most requests took 2–4ms. This sample supports widening the scan ranges while
retaining the existing polling intervals; it does not establish an official
provider quota or guarantee latency for all other ranges.

## Source and configuration changes

Two optional positive integer environment variables select parent block ranges:

| Variable | Scope | When unset |
| --- | --- | --- |
| `INDEXER_ARBITRUM_DEPOSITS_L1_BLOCKS_RANGE` | Deposit tracker, including its historical discovery | `INDEXER_ARBITRUM_L1_RPC_HISTORICAL_BLOCKS_RANGE` (default 1000) |
| `INDEXER_ARBITRUM_WITHDRAWALS_L1_BLOCKS_RANGE` | Live withdrawal execution discovery | Same common range |

The dev deployment sets both overrides to `10000`. Batch publication,
confirmations and historical withdrawal discovery keep the common range.
Deposits keep their 10-second polling interval; withdrawals keep 3 seconds.
Nominal forward log traffic is about 0.43 requests/second, plus parent-head
reads and event enrichment. Near the head, the safe-window overlap can require
additional log chunks, as it already did for deposits.

Withdrawal progress is now persisted in the existing `last_fetched_counters`
table after each successful RPC/import/matching chunk, including empty chunks.
The new `arbitrum_to_l1_execution_scan_v1:` key is scoped to parent chain ID,
rollup and outbox, separately from deposit checkpoints. Restart resumes from
the saved progress or latest indexed execution, whichever is later. The live
worker replays the unsafe window at the head, using the same range safeguards
as the deposit scanner. Partial failures never checkpoint a failed chunk.
No database schema migration is needed.

## Deploy to the existing dev server

Keep the current backend running so the deployment can capture its completed
scan progress. Run on the existing dev server:

```bash
bash <<'DEPLOY_BRIDGE_CATCHUP'
set -euo pipefail
cd /data/blockscout
git switch deriw-dev
git pull --ff-only origin deriw-dev
python3 deploy/scripts/deploy_dev_bridge_catchup.py
DEPLOY_BRIDGE_CATCHUP
```

This release does not modify `.dockerignore`, so the previously preserved
server edit to that file does not conflict with this fast-forward. Other
tracked edits or branch divergence still require review before pulling.

The script builds a committed-source archive while the backend continues to
run. Before replacement, it snapshots both completed callback cursors into
their database checkpoints and writes a private handoff record. It changes the
two range overrides and backend image in the server-owned
`docker-compose.dev/docker-compose.yml`, recreates only `backend`, then checks
metrics health, runtime ranges, original polling intervals, key identities and
resumption at or beyond both saved cursors. A failed handoff prevents backend
replacement. Work completed after the handoff may be replayed safely.

Private configuration/container backups and cursor records are saved beneath
`/data/blockscout-dev-private/bridge-catchup-deployments/`. The script preserves
the other services, data mounts, normalization pause, migration holds and
start-only startup. The existing NFT image remains pinned. No bridge
transaction or manual message-status update is performed.

Success ends with `phase="bridge_catchup_deployed"`. If verification fails
after replacement, the script keeps the running candidate and private
diagnostics for review; it does not silently reset cursors or roll back to an
older scanner without durable withdrawal progress. The existing metrics-only
deployment command does not perform this initial two-cursor handoff; use the
catchup command above for this upgrade.

## Verify forward progress and repaired records

Wait at least 60 seconds between metric snapshots:

```bash
docker exec backend curl -fsS --max-time 10 http://127.0.0.1:4000/metrics |
  grep -E '^deriw_bridge_(observation_timestamp_seconds|tracker_next_parent_block|tracker_lag_blocks|tracker_running|verification_success|deposit_sample_incomplete_relayed|withdrawal_sample_missing_relay)'
```

Compare fresh snapshots: deposit and withdrawal cursors should advance faster
than the parent head, reducing their respective lag. At nominal rates the
withdrawal backlog would take roughly 20 minutes and the deposit backlog about
14 hours. Other jobs, retries and event imports can increase these times.
Withdrawal #512 can update once its parent relay block 327286816 is scanned.
The deposit metadata updates when forward discovery reaches each parent-origin
event; the deployment does not skip the intervening history.

With corresponding verification-success metrics at 1, expect
`deriw_bridge_deposit_sample_incomplete_relayed` to fall from 5 to 0 and
`deriw_bridge_withdrawal_sample_missing_relay` from 1 to 0 after those events
are indexed. Check the pages for parent transaction hashes as well. Metrics
are bounded samples; zero does not prove that all older history is complete.

## Validation

- 19 Elixir tests against disposable PostgreSQL 15, including a real relay
  import that updates a confirmed withdrawal, empty-range persistence, RPC
  failure, partial chunk failure, failed message import with replay, and
  unsafe-window replay.
- Actual rollout handoff expressions checked against real Ecto checkpoints;
  a fresh BEAM process resumes both saved directions.
- Existing metrics rollout and new catchup rollout simulated with real Git and
  Compose rendering, private values, backend-only replacement and database
  sentinels; failed handoff leaves the original Compose and backend untouched.
