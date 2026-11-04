# Existing Repo only. No worker starts, row updates or migration-state writes.
true = Explorer.Migrator.StartupControl.disabled?()

workers = [
  Explorer.Migrator.EmptyInternalTransactionsData,
  Explorer.Migrator.ReindexDuplicatedInternalTransactions,
  Explorer.Migrator.HeavyDbIndexOperation.CreateInternalTransactionsBlockNumberTransactionIndexIndexUniqueIndex,
  Explorer.Migrator.HeavyDbIndexOperation.ValidateInternalTransactionsBlockNumberTransactionIndexNotNull,
  Explorer.Migrator.HeavyDbIndexOperation.UpdateInternalTransactionsPrimaryKey
]

true = Enum.all?(workers, &is_nil(Process.whereis(&1)))

options = %{batch_sample: false, before_block: nil}
bound = options.before_block

seek =
  "SELECT block_number FROM public.internal_transactions WHERE block_number IS NOT NULL" <>
    if(is_nil(bound), do: "", else: " AND block_number<$1") <>
    " ORDER BY block_number DESC LIMIT 1"

seek_args = if is_nil(bound), do: [], else: [bound]

needs =
  "(trace_address IS NOT NULL OR value=0 OR (call_type_enum IS NULL AND call_type IS NOT NULL) OR error IS NOT NULL OR (created_contract_address_hash IS NOT NULL AND to_address_hash IS NULL)) IS TRUE"

batch =
  if options.batch_sample do
    "SELECT block_number," <>
      needs <>
      ~S|, pg_column_size(it) FROM public.internal_transactions it WHERE block_number<$1 ORDER BY block_number DESC,transaction_index DESC,"index" DESC LIMIT 1001|
  else
    "SELECT " <>
      needs <>
      ~S|, pg_column_size(it) FROM public.internal_transactions it WHERE block_number=$1 ORDER BY transaction_index DESC,"index" DESC LIMIT 1001|
  end

walk = fn root ->
  walk = fn recur, node -> [node | Enum.flat_map(node["Plans"] || [], &recur.(recur, &1))] end
  walk.(walk, root)
end

{:ok, data} =
  Explorer.Repo.transaction(
    fn ->
      q = fn sql, args ->
        Ecto.Adapters.SQL.query!(Explorer.Repo, sql, args, log: false, timeout: 2000).rows
      end

      q.("SET TRANSACTION READ ONLY", [])
      q.("SET LOCAL statement_timeout = '1500ms'", [])
      q.("SET LOCAL lock_timeout = '500ms'", [])
      # Scope this planner preference to the read-only transaction. Require an
      # actual index access path below; do not run EXPLAIN ANALYZE or a trace scan.
      q.("SET LOCAL enable_seqscan = off", [])
      q.("SET LOCAL enable_bitmapscan = off", [])
      q.("SET LOCAL enable_sort = off", [])
      q.("SET LOCAL enable_incremental_sort = off", [])

      indexes =
        q.(
          "SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=i.indkey[0] WHERE i.indrelid='public.internal_transactions'::regclass AND i.indisvalid AND i.indisready AND i.indpred IS NULL AND a.attname='block_number'",
          []
        )
        |> List.flatten()

      true = indexes != []

      explain = fn sql, args ->
        [[[%{"Plan" => root}]]] = q.("EXPLAIN (FORMAT JSON) " <> sql, args)
        nodes = walk.(root)
        [scan] = Enum.filter(nodes, &(&1["Relation Name"] == "internal_transactions"))

        unless scan["Node Type"] in ["Index Scan", "Index Only Scan"] and
                 scan["Index Name"] in indexes and root["Node Type"] == "Limit",
               do: raise("unsupported_sample_index_access")

        if Enum.any?(
             nodes,
             &(&1["Node Type"] in ["Sort", "Incremental Sort", "Seq Scan", "Bitmap Heap Scan"])
           ), do: raise("sample_plan_requires_unbounded_sort_or_scan")

        %{
          access: scan["Node Type"],
          index: scan["Index Name"],
          estimated_total_cost: root["Total Cost"]
        }
      end

      seek_plan = explain.(seek, seek_args)
      latest = q.(seek, seek_args)

      block =
        case latest do
          [[value]] when is_integer(value) and value >= 0 -> value
          [] when options.batch_sample -> nil
        end

      args = if options.batch_sample, do: [bound], else: [block]
      batch_plan = explain.(batch, args)
      records = q.(batch, args)

      true =
        Enum.all?(records, fn row ->
          {height, needed, bytes} =
            if options.batch_sample,
              do: List.to_tuple(row),
              else: {block, hd(row), List.last(row)}

          is_integer(height) and height >= 0 and is_boolean(needed) and is_integer(bytes) and
            bytes >= 0
        end)

      boundary =
        if options.batch_sample and length(records) == 1001, do: hd(List.last(records)), else: nil

      selected =
        if is_nil(boundary), do: records, else: Enum.reject(records, &(hd(&1) == boundary))

      groups =
        if options.batch_sample do
          selected
          |> Enum.chunk_by(&hd/1)
          |> Enum.map(fn rows ->
            %{
              block: hd(hd(rows)),
              rows: length(rows),
              needs: Enum.count(rows, &Enum.at(&1, 1)),
              row_bytes: Enum.reduce(rows, 0, fn row, n -> n + List.last(row) end)
            }
          end)
        else
          []
        end

      statuses =
        q.(
          "SELECT migration_name,status FROM public.migrations_status WHERE migration_name=ANY($1::text[]) ORDER BY migration_name",
          [Enum.map(workers, & &1.migration_name())]
        )

      [[wal_bytes, reset]] = q.("SELECT wal_bytes::text, stats_reset::text FROM pg_stat_wal", [])

      stats =
        q.(
          "SELECT n_tup_upd,n_tup_hot_upd,n_dead_tup,autovacuum_count FROM pg_stat_user_tables WHERE relid='public.internal_transactions'::regclass",
          []
        )

      base = %{
        seek_plan: seek_plan,
        batch_plan: batch_plan,
        statuses: statuses,
        table_update_stats: stats,
        wal_bytes: wal_bytes,
        wal_stats_reset: reset
      }

      if options.batch_sample do
        Map.merge(base, %{
          before_block: bound,
          groups: groups,
          raw_sample_rows: length(records),
          excluded_boundary_block: boundary,
          excluded_boundary_rows: length(records) - length(selected),
          older_non_null_range_exhausted: length(records) < 1001
        })
      else
        Map.merge(base, %{
          block: block,
          sample_rows: length(records),
          row_limit_exceeded: length(records) > 1000,
          rows_needing_normalization_in_sample: Enum.count(records, fn [needed, _] -> needed end),
          sampled_row_bytes: Enum.reduce(records, 0, fn [_, bytes], sum -> sum + bytes end),
          maximum_sampled_row_bytes: records |> Enum.map(&List.last/1) |> Enum.max(fn -> 0 end)
        })
      end
    end,
    timeout: 5000
  )

IO.puts("BLOCKSCOUT_DEPLOY_CHECK=" <> Jason.encode!(data))
