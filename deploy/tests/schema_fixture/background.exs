alias Explorer.Chain.Cache.BackgroundMigrations
alias Explorer.Migrator.{EmptyInternalTransactionsData, MigrationStatus, ReindexDuplicatedInternalTransactions}
alias Explorer.Migrator.HeavyDbIndexOperation, as: Heavy

alias Heavy.{
  CreateInternalTransactionsBlockNumberTransactionIndexIndexUniqueIndex,
  UpdateInternalTransactionsPrimaryKey,
  ValidateInternalTransactionsBlockNumberTransactionIndexNotNull
}

unique = CreateInternalTransactionsBlockNumberTransactionIndexIndexUniqueIndex
validate = ValidateInternalTransactionsBlockNumberTransactionIndexNotNull
primary = UpdateInternalTransactionsPrimaryKey
workers = [EmptyInternalTransactionsData, ReindexDuplicatedInternalTransactions, unique, validate, primary]

# These are declared Explorer dependencies used by the standalone workers.
{:ok, _} = Application.ensure_all_started(:mix)
false = Mix.env() == :test
{:ok, _} = Application.ensure_all_started(:con_cache)
{:ok, _} = Supervisor.start_link([BackgroundMigrations.child_spec()], strategy: :one_for_one)
Application.put_env(:explorer, Heavy, check_interval: 20)

for worker <- [EmptyInternalTransactionsData, ReindexDuplicatedInternalTransactions] do
  Application.put_env(:explorer, worker, batch_size: 5, concurrency: 1, timeout: 1)
end

pk_columns = fn ->
  [[columns]] =
    q.("""
    SELECT array_agg(a.attname::text ORDER BY k.ordinality)
    FROM pg_constraint c
    CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ordinality)
    JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum
    WHERE c.conrelid='public.internal_transactions'::regclass AND c.contype='p'
    """).rows

  columns
end

["block_hash", "block_index"] = pk_columns.()
[[5]] = q.("SELECT count(*) FROM internal_transactions").rows
true = Enum.all?(workers, &is_nil(MigrationStatus.get_status(&1.migration_name())))

await = fn pid ->
  ref = Process.monitor(pid)

  receive do
    {:DOWN, ^ref, :process, ^pid, reason} when reason in [:normal, :noproc] -> :ok
    {:DOWN, ^ref, :process, ^pid, reason} -> raise "Background fixture worker failed: #{inspect(reason)}"
  after
    15_000 -> raise "Background fixture worker did not complete"
  end
end

# Start the index worker before its prerequisites and prove that it waits.
{:ok, unique_pid} = unique.start_link([])
Process.sleep(100)
true = Process.alive?(unique_pid)
nil = MigrationStatus.get_status(unique.migration_name())
[[nil]] = q.("SELECT to_regclass('public.internal_transactions_block_number_transaction_index_index_index')::text").rows

# The real filling workers, not test code, write their own completion records.
{:ok, normalize_pid} = EmptyInternalTransactionsData.start_link([])
await.(normalize_pid)
"completed" = MigrationStatus.get_status(EmptyInternalTransactionsData.migration_name())
true = BackgroundMigrations.get_empty_internal_transactions_data_finished()

[
  [10, nil, nil, nil, "call", nil, 2],
  [11, nil, "7", nil, "call", "synthetic revert", 2],
  [12, nil, nil, nil, nil, nil, 4]
] =
  q.("""
  SELECT it.block_number, it.trace_address, it.value::text, it.call_type, it.call_type_enum::text,
         te.message, get_byte(it.to_address_hash,19)
  FROM internal_transactions it LEFT JOIN transaction_errors te ON te.id=it.error_id
  WHERE it.block_number < 20 ORDER BY it.block_number
  """).rows

{:ok, duplicates_pid} = ReindexDuplicatedInternalTransactions.start_link([])
await.(duplicates_pid)
"completed" = MigrationStatus.get_status(ReindexDuplicatedInternalTransactions.migration_name())
[[3]] = q.("SELECT count(*) FROM internal_transactions").rows
[[20, 20]] = q.("SELECT block_number, get_byte(block_hash,31) FROM pending_block_operations").rows
nil = Process.whereis(Indexer.Fetcher.InternalTransaction)
false = EthereumJSONRPC.Utility.RangesHelper.traceable_block_number?(20)

await.(unique_pid)
"completed" = MigrationStatus.get_status(unique.migration_name())
{:ok, validate_pid} = validate.start_link([])
await.(validate_pid)
"completed" = MigrationStatus.get_status(validate.migration_name())
{:ok, primary_pid} = primary.start_link([])
await.(primary_pid)
"completed" = MigrationStatus.get_status(primary.migration_name())
true = BackgroundMigrations.get_heavy_indexes_update_internal_transactions_primary_key_finished()
["block_number", "transaction_index", "index"] = pk_columns.()

[[true], [true]] =
  q.(
    "SELECT convalidated FROM pg_constraint WHERE conname IN ('internal_transactions_block_number_not_null','internal_transactions_transaction_index_not_null') ORDER BY conname"
  ).rows

[[true, true, true]] =
  q.(
    "SELECT indisvalid, indisready, indisprimary FROM pg_index WHERE indexrelid='public.internal_transactions_pkey'::regclass"
  ).rows

# Legacy key columns can now be null; roll the probe back to preserve the row.
{:error, :fixture_probe} =
  Explorer.Repo.transaction(fn ->
    q.("UPDATE internal_transactions SET block_hash=NULL, block_index=NULL WHERE block_number=10")
    [[nil, nil]] = q.("SELECT block_hash, block_index FROM internal_transactions WHERE block_number=10").rows
    Explorer.Repo.rollback(:fixture_probe)
  end)

try do
  q.("""
  INSERT INTO internal_transactions(block_number, transaction_index, "index", transaction_hash,
    type, call_type_enum, input, output, gas, gas_used, from_address_hash, to_address_hash, inserted_at, updated_at)
  SELECT block_number, transaction_index, "index", transaction_hash, type, call_type_enum, input, output,
    gas, gas_used, from_address_hash, to_address_hash, inserted_at, updated_at
  FROM internal_transactions WHERE block_number=10
  """)

  raise "New primary key unexpectedly accepted a duplicate"
rescue
  error in Postgrex.Error -> :unique_violation = error.postgres.code
end

snapshot =
  q.(
    "SELECT block_number, transaction_index, \"index\", block_hash, block_index, error_id, call_type_enum::text, value FROM internal_transactions ORDER BY block_number"
  ).rows

BackgroundMigrations.set_empty_internal_transactions_data_finished(false)
BackgroundMigrations.set_heavy_indexes_update_internal_transactions_primary_key_finished(false)

# Persisted completion allows all five workers to restart without data changes.
for worker <- workers do
  {:ok, pid} = worker.start_link([])
  await.(pid)
end

^snapshot =
  q.(
    "SELECT block_number, transaction_index, \"index\", block_hash, block_index, error_id, call_type_enum::text, value FROM internal_transactions ORDER BY block_number"
  ).rows

true = BackgroundMigrations.get_empty_internal_transactions_data_finished()
true = BackgroundMigrations.get_heavy_indexes_update_internal_transactions_primary_key_finished()

%{
  selected_workers: Enum.map(workers, &inspect/1),
  completion_records:
    Enum.map(workers, &%{name: &1.migration_name(), status: MigrationStatus.get_status(&1.migration_name())}),
  dependency_gate_checked: true,
  normalized_rows_checked: true,
  primary_key_columns: pk_columns.(),
  completed_restart_checked: true,
  traces_before: 5,
  traces_after_duplicate_repair: 3,
  untraceable_reindex_blocks_left_pending: [20],
  trace_fetcher_started: false,
  full_database_rehearsal: false
}
