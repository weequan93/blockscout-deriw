# Check dev parent-chain RPC capacity before bridge catchup

The bridge scanners currently use small `eth_getLogs` ranges. Before increasing
them, run the bounded probe against the RPC settings already loaded by the dev
backend. It needs no RPC credentials on the command line:

```bash
git -C /data/blockscout fetch origin deriw-dev
docker exec backend bin/blockscout rpc "$(git -C /data/blockscout show origin/deriw-dev:deploy/scripts/probe_dev_bridge_rpc.exs)"
```

Fetching does not change the checkout. The probe does not rebuild/restart any
container, change environment values, advance scan cursors, import records or
send blockchain transactions. It uses the running dev deposit and withdrawal
trackers' configuration and checks the known dev chain ID and rollup address.
Keep the scanners running during the test.

The probe makes at most **68 sequential log requests**, with an eight-second
deadline per request and a two-minute total budget. It stops on the first RPC
failure, timeout, malformed response or inconsistent log set. It does not retry
failed calls. A request already in progress can take up to eight seconds beyond
the total budget before it is cancelled.

For each contract, it checks a historical window containing a known event
(deposit at parent block 278124046; withdrawal relay at block 327286816).
The 10000-block result must equal the union of ten 1000-block results, including
the event returned by a one-block query. This detects missing or silently
truncated responses that a successful empty-range request would miss. The
probe also repeats 50-block and 1000-block queries around that event.

It then alternates both contracts at a target rate of two requests/second and
four requests/second, 16 requests at each rate. Requests remain sequential;
slower calls lower the achieved rate. Results include each call's latency and
log count, completeness checks, paced-phase duration and the final result.
RPC URLs, credentials, raw response bodies and transaction data are not printed.

`{"phase":"finished","passed":true,...}` means the sampled historical windows
were complete and the brief paced test succeeded. `phase="stopped"` identifies
the failed tracker and check; keep the existing scan settings and investigate.
This is a short capacity sample, not discovery of the provider's official
requests/second, daily or monthly quota. Cold ranges, denser events, other RPC
users and later provider throttling can behave differently. Repeated historical
reads may benefit from provider caching.

At one 10000-block request every 10 seconds for deposits and every 3 seconds for
withdrawals, the two scanners need approximately **0.43 log requests/second**,
plus their parent-head, transaction and block reads and the other indexers'
existing traffic. Wider ranges increase work per query, not the configured
number of polling iterations. Monitor actual latency, errors and lag after any
configuration change.

The probe logic was checked locally with four isolated Elixir fixture cases:
complete nonempty results, silently truncated wide results, RPC rejection and
unfiltered results. No live server was accessed by those fixture checks.
