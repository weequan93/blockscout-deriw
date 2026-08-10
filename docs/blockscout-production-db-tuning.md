# Blockscout Production DB Tuning Runbook

This runbook records the database indexes and runtime settings needed before promoting this Blockscout branch to a production environment with a large existing chain history.

It is based on the high-disk-read incidents where backend restart triggered long `COUNT`, stats, replaced-transaction, and empty-block sanitizer queries.

## Required Code And Migration

Ship these code changes together:

- `apps/explorer/lib/explorer/chain.ex`
  - Makes the new-contract counter timestamp predicate sargable.
- `apps/indexer/lib/indexer/fetcher/empty_blocks_sanitizer.ex`
  - Restricts sanitizer transaction joins to consensus transactions.
- `apps/explorer/priv/repo/migrations/20260721000000_add_transactions_status_created_contract_code_indexed_at_index.exs`
  - Adds the production indexes listed below.

After applying the migration, run:

```sql
ANALYZE transactions;
ANALYZE blocks;
```

## Required Indexes

The migration creates these indexes:

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS transactions_status_created_contract_code_indexed_at_index
ON transactions (status, created_contract_code_indexed_at)
WHERE created_contract_code_indexed_at IS NOT NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS transactions_consensus_timestamp_stats_idx
ON transactions (block_timestamp)
WHERE block_consensus = true;

CREATE INDEX CONCURRENTLY IF NOT EXISTS transactions_consensus_block_number_gas_used_idx
ON transactions (block_number)
INCLUDE (gas_used)
WHERE block_consensus = true;

CREATE INDEX CONCURRENTLY IF NOT EXISTS blocks_consensus_number_timestamp_hash_idx
ON blocks (number)
INCLUDE (timestamp, hash)
WHERE consensus = true;

CREATE INDEX CONCURRENTLY IF NOT EXISTS blocks_empty_sanitizer_hash_number_idx
ON blocks (hash)
INCLUDE (number)
WHERE is_empty IS NULL
  AND consensus = true
  AND refetch_needed = false;
```

Create concurrent indexes one at a time on production. They do not block normal reads/writes, but they still scan large tables and can consume heavy disk I/O.

## Invalid Concurrent Index Stubs

If a previous `CREATE INDEX CONCURRENTLY` was interrupted, PostgreSQL may keep a zero-byte invalid index. `IF NOT EXISTS` will skip it by name, so always verify validity:

```sql
SELECT
  c.relname,
  i.indisvalid,
  i.indisready,
  pg_size_pretty(pg_relation_size(c.oid)) AS size
FROM pg_class c
JOIN pg_index i ON i.indexrelid = c.oid
WHERE c.relname IN (
  'transactions_status_created_contract_code_indexed_at_index',
  'transactions_consensus_timestamp_stats_idx',
  'transactions_consensus_block_number_gas_used_idx',
  'blocks_consensus_number_timestamp_hash_idx',
  'blocks_empty_sanitizer_hash_number_idx'
);
```

Every row must show `indisvalid = true`, `indisready = true`, and non-zero size.

If an index is invalid, drop and recreate it outside a transaction:

```sql
DROP INDEX CONCURRENTLY IF EXISTS public.index_name_here;
```

Then run the matching `CREATE INDEX CONCURRENTLY` again.

## Runtime Environment Settings

Set these in `docker-compose/envs/common-blockscout.env` or the equivalent production environment source.

Cache-heavy counters should refresh less often:

```env
CACHE_BLOCK_COUNT_PERIOD=24h
CACHE_TXS_COUNT_PERIOD=24h
CACHE_TRANSACTIONS_24H_STATS_PERIOD=24h
CACHE_TOTAL_GAS_USAGE_PERIOD=24h
```

If total gas usage is not required on the UI/API, disable its expensive counter:

```env
CACHE_TOTAL_GAS_USAGE_COUNTER_ENABLED=false
```

Keep the empty-block sanitizer enabled, but tune it so the backlog progresses without overwhelming disk:

```env
INDEXER_EMPTY_BLOCKS_SANITIZER_BATCH_SIZE=100
INDEXER_EMPTY_BLOCKS_SANITIZER_INTERVAL=30s
```

If production disk I/O is already saturated, use these only as temporary emergency switches:

```env
INDEXER_DISABLE_EMPTY_BLOCKS_SANITIZER=true
INDEXER_DISABLE_REPLACED_TRANSACTION_FETCHER=true
```

Do not leave `INDEXER_DISABLE_EMPTY_BLOCKS_SANITIZER=true` permanently unless you accept the risk that blocks missing transactions will not be automatically detected and refetched.

## Rollout Order

1. Apply the code branch that contains the sargable counter query and empty-block sanitizer join change.
2. Run the migration, or create the indexes manually with `CREATE INDEX CONCURRENTLY`.
3. Run `ANALYZE transactions;` and `ANALYZE blocks;`.
4. Verify all required indexes are valid and non-zero size.
5. Apply the runtime environment settings.
6. Restart backend during a low-traffic window.
7. Watch active queries and index usage for at least one restart cycle.

## Verification Queries

Active PostgreSQL work:

```sql
SELECT
  pid,
  now() - query_start AS runtime,
  application_name,
  client_addr,
  client_port,
  wait_event_type,
  wait_event,
  left(query, 1200) AS query
FROM pg_stat_activity
WHERE state = 'active'
  AND pid <> pg_backend_pid()
ORDER BY query_start;
```

Index build progress:

```sql
SELECT *
FROM pg_stat_progress_create_index;
```

Index definitions:

```sql
SELECT indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'public'
  AND indexname IN (
    'transactions_status_created_contract_code_indexed_at_index',
    'transactions_consensus_timestamp_stats_idx',
    'transactions_consensus_block_number_gas_used_idx',
    'blocks_consensus_number_timestamp_hash_idx',
    'blocks_empty_sanitizer_hash_number_idx'
  )
ORDER BY indexname;
```

Index usage:

```sql
SELECT
  indexrelname,
  idx_scan,
  idx_tup_read,
  idx_tup_fetch,
  pg_size_pretty(pg_relation_size(indexrelid)) AS size
FROM pg_stat_user_indexes
WHERE indexrelname IN (
  'transactions_status_created_contract_code_indexed_at_index',
  'transactions_consensus_timestamp_stats_idx',
  'transactions_consensus_block_number_gas_used_idx',
  'blocks_consensus_number_timestamp_hash_idx',
  'blocks_empty_sanitizer_hash_number_idx',
  'pending_txs_index',
  'transactions_nonce_from_address_hash_block_hash_index'
)
ORDER BY indexrelname;
```

Empty-block sanitizer backlog estimate:

```sql
SELECT
  reltuples::bigint AS estimated_empty_sanitizer_candidates,
  pg_size_pretty(pg_relation_size('blocks_empty_sanitizer_hash_number_idx')) AS index_size
FROM pg_class
WHERE relname = 'blocks_empty_sanitizer_hash_number_idx';
```

Backend environment check after restart:

```bash
docker exec backend printenv | grep -E 'CACHE_BLOCK_COUNT_PERIOD|CACHE_TXS_COUNT_PERIOD|CACHE_TRANSACTIONS_24H_STATS_PERIOD|CACHE_TOTAL_GAS_USAGE_PERIOD|CACHE_TOTAL_GAS_USAGE_COUNTER_ENABLED|INDEXER_EMPTY_BLOCKS_SANITIZER_BATCH_SIZE|INDEXER_EMPTY_BLOCKS_SANITIZER_INTERVAL|INDEXER_DISABLE_EMPTY_BLOCKS_SANITIZER|INDEXER_DISABLE_REPLACED_TRANSACTION_FETCHER'
```

## Success Criteria

- Required indexes are `indisvalid = true` and `indisready = true`.
- `idx_scan` increases for the relevant indexes after backend restart.
- Restart no longer leaves long-running `DataFileRead` queries for basic counters or empty-block sanitizer.
- The empty-block sanitizer backlog trends downward after `ANALYZE blocks`.
- No repeated block-catchup loop remains for known bad reorg/RPC receipt gaps.
