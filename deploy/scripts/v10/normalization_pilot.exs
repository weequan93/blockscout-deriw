# Explicitly selected complete blocks. Starts only a Repo; no application or worker.
import Ecto.Query
import Explorer.QueryHelper, only: [select_ctid: 1]
alias Explorer.Chain.InternalTransaction

payload = Jason.decode!(File.read!("/tmp/upgrade/payload.json"))
mode = payload["mode"]
true = mode in ["inspect", "migrate"]
block = payload["block"]
batch? = is_list(payload["blocks"])
blocks = if batch?, do: payload["blocks"], else: [block]
expected_rows = payload["expected_rows"]
true = is_integer(expected_rows) and expected_rows in 1..1000

true =
  length(blocks) in 1..1000 and
    Enum.all?(blocks, &(is_integer(&1) and &1 >= 0 and &1 < 9_223_372_036_854_775_808))

true = blocks == Enum.sort(Enum.uniq(blocks), :desc)

if batch? do
  true = Enum.map(payload["block_rows"], & &1["block"]) == blocks
  true = Enum.all?(payload["block_rows"], &(is_integer(&1["rows"]) and &1["rows"] in 1..1000))
  true = Enum.sum(Enum.map(payload["block_rows"], & &1["rows"])) == expected_rows
end

args = if batch?, do: [blocks], else: [block]
true = Explorer.Migrator.StartupControl.disabled?()

for app <- [:crypto, :ssl, :postgrex, :ecto_sql],
    do: {:ok, _} = Application.ensure_all_started(app)

{:ok, _} =
  Explorer.Repo.start_link(
    pool_size: 2,
    parameters: [
      application_name: "blockscout-v10-normalization-pilot",
      statement_timeout: "5000",
      lock_timeout: "500",
      idle_in_transaction_session_timeout: "10000"
    ]
  )

nil = Process.whereis(Explorer.Supervisor)
worker = Explorer.Migrator.EmptyInternalTransactionsData
nil = Process.whereis(worker)

q = fn sql, args -> Explorer.Repo.query!(sql, args, log: false, timeout: 6000).rows end
stage = fn name -> IO.puts("BLOCKSCOUT_NORMALIZATION_STAGE=" <> name) end

walk = fn root ->
  walk = fn recur, node -> [node | Enum.flat_map(node["Plans"] || [], &recur.(recur, &1))] end
  walk.(walk, root)
end

# Semantic form preserves the effective value/error/call type/destination and
# all other columns. Legacy trace_address is intentionally removed by upstream.
semantic = ~S"""
(to_jsonb(it) - ARRAY['trace_address','value','error','error_id','call_type','call_type_enum','to_address_hash'] || jsonb_build_object('value', CASE WHEN it.value=0 THEN NULL ELSE it.value END, 'effective_error',COALESCE(it.error,te.message),'effective_call_type',COALESCE(it.call_type_enum::text,it.call_type),'to_address_hash',COALESCE(it.to_address_hash,it.created_contract_address_hash)::text))
"""

needs =
  ~S|(it.trace_address IS NOT NULL OR it.value=0 OR (it.call_type_enum IS NULL AND it.call_type IS NOT NULL) OR it.error IS NOT NULL OR (it.created_contract_address_hash IS NOT NULL AND it.to_address_hash IS NULL)) IS TRUE|

target =
  if batch?,
    do:
      ~S|it.block_number=ANY($1::bigint[]) ORDER BY it.block_number DESC,it.transaction_index DESC,it."index" DESC|,
    else: ~S|it.block_number=$1 ORDER BY it.transaction_index DESC,it."index" DESC|

sql =
  "SELECT to_jsonb(it), md5(to_jsonb(it)::text), md5((" <>
    semantic <>
    ")::text), " <>
    needs <>
    ~S|, pg_column_size(it) FROM public.internal_transactions it LEFT JOIN public.transaction_errors te ON te.id=it.error_id WHERE | <>
    target <> " LIMIT 1001"

status_names =
  ~w(empty_internal_transactions_data reindex_duplicated_internal_transactions heavy_indexes_create_internal_transactions_block_number_transaction_index_index_index heavy_indexes_create_internal_transactions_not_null_constraints heavy_indexes_create_internal_transactions_pkey)

started = System.monotonic_time(:millisecond)

{:ok, report} =
  Explorer.Repo.transaction(
    fn ->
      if mode == "inspect", do: q.("SET TRANSACTION READ ONLY", [])
      q.("SET LOCAL enable_seqscan=off", [])
      q.("SET LOCAL enable_bitmapscan=off", [])
      q.("SET LOCAL enable_sort=off", [])
      q.("SET LOCAL enable_incremental_sort=off", [])
      q.("SET LOCAL jit=off", [])

      [[true]] = q.("SELECT pg_try_advisory_xact_lock(184175075,1026)", [])

      [[database, oid, address, port, 15, false]] =
        q.(
          "SELECT current_database(),(SELECT oid::bigint FROM pg_database WHERE datname=current_database()),host(inet_server_addr()),inet_server_port(),current_setting('server_version_num')::integer/10000,pg_is_in_recovery()",
          []
        )

      identity = %{
        "database" => database,
        "oid" => oid,
        "address" => address,
        "port" => port,
        "postgres_major" => 15
      }

      true = identity == payload["database_identity"]

      applied =
        q.("SELECT version FROM public.schema_migrations ORDER BY version", []) |> List.flatten()

      true = applied == payload["applied_versions"]

      [[heap]] =
        q.("SELECT pg_relation_filenode('public.internal_transactions'::regclass)::bigint", [])

      true = heap == payload["heap_identity"]

      statuses =
        q.(
          "SELECT to_jsonb(s) FROM public.migrations_status s WHERE migration_name=ANY($1::text[]) ORDER BY migration_name",
          [status_names]
        )
        |> List.flatten()

      status_map = Map.new(statuses, &{&1["migration_name"], &1["status"]})
      true = status_map["reindex_duplicated_internal_transactions"] == "completed"
      false = status_map["empty_internal_transactions_data"] == "completed"

      false =
        Enum.any?(status_map, fn {name, status} ->
          String.starts_with?(name, "heavy_indexes_") and status == "started"
        end)

      if payload["baseline"], do: true = statuses == payload["baseline"]["statuses"]

      # Held only for the short write transaction. This prevents new rows from
      # making the bounded block grow while update_batch/1 processes the block.
      stage.("table_lock")

      if mode == "migrate",
        do: q.("LOCK TABLE public.internal_transactions IN SHARE ROW EXCLUSIVE MODE", [])

      indexes =
        q.(
          "SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=i.indkey[0] WHERE i.indrelid='public.internal_transactions'::regclass AND i.indisvalid AND i.indisready AND i.indpred IS NULL AND a.attname='block_number'",
          []
        )
        |> List.flatten()

      stage.("snapshot_plan")
      [[[%{"Plan" => root}]]] = q.("EXPLAIN (FORMAT JSON) " <> sql, args)
      nodes = walk.(root)
      [scan] = Enum.filter(nodes, &(&1["Relation Name"] == "internal_transactions"))

      true =
        root["Node Type"] == "Limit" and scan["Node Type"] in ["Index Scan", "Index Only Scan"] and
          scan["Index Name"] in indexes

      false = Enum.any?(nodes, &(&1["Node Type"] in ["Sort", "Incremental Sort"]))

      snapshot = fn ->
        records =
          q.(sql, args)
          |> Enum.sort_by(fn [row, _, _, _, _] -> {row["block_hash"], row["block_index"]} end)

        true = length(records) == expected_rows
        bytes = Enum.reduce(records, 0, fn [_, _, _, _, size], sum -> sum + size end)
        true = bytes <= 16 * 1024 * 1024
        rows = Enum.map(records, &Enum.at(&1, 0))
        true = byte_size(Jason.encode!(rows)) <= 16 * 1024 * 1024

        if batch? do
          true =
            Enum.frequencies_by(rows, & &1["block_number"]) ==
              Map.new(payload["block_rows"], &{&1["block"], &1["rows"]})
        end

        %{
          rows: rows,
          full_hashes: Enum.map(records, &Enum.at(&1, 1)),
          semantic_hashes: Enum.map(records, &Enum.at(&1, 2)),
          needs_normalization: Enum.count(records, &Enum.at(&1, 3)),
          sampled_row_bytes: bytes
        }
      end

      stage.("before_snapshot")
      before = snapshot.()
      baseline = payload["baseline"]
      if baseline, do: true = before.semantic_hashes == baseline["semantic_hashes"]

      # Snapshot ordering and callback ordering differ. The callback sorts only
      # the selected rows by transaction hash. Discouraging that sort can make
      # PostgreSQL walk the whole transaction-hash index instead of seeking the
      # selected blocks. Keep its normal sort/bitmap choices in this transaction.
      q.("SET LOCAL enable_sort=on", [])
      q.("SET LOCAL enable_incremental_sort=on", [])
      q.("SET LOCAL enable_bitmapscan=on", [])
      stage.("callback_plan")

      callback_plan =
        if batch? do
          # This is the lock query from the pinned compiled callback. EXPLAIN
          # without ANALYZE does not execute it or acquire its row locks.
          lock_query =
            from(it in InternalTransaction,
              select: select_ctid(it),
              select_merge: %{error: it.error},
              where:
                it.block_number in ^blocks and
                  (not is_nil(it.trace_address) or it.value == ^0 or
                     (is_nil(it.call_type_enum) and not is_nil(it.call_type)) or
                     not is_nil(it.error) or
                     (not is_nil(it.created_contract_address_hash) and is_nil(it.to_address_hash))),
              order_by: [asc: it.transaction_hash, asc: it.index],
              lock: "FOR UPDATE"
            )

          {lock_sql, lock_args} = Ecto.Adapters.SQL.to_sql(:all, Explorer.Repo, lock_query)
          [[[%{"Plan" => lock_root}]]] = q.("EXPLAIN (FORMAT JSON) " <> lock_sql, lock_args)
          lock_nodes = walk.(lock_root)

          [access] =
            Enum.filter(lock_nodes, &(&1["Relation Name"] == "internal_transactions"))

          seeks =
            Enum.filter(lock_nodes, fn node ->
              node["Node Type"] in ["Index Scan", "Index Only Scan", "Bitmap Index Scan"] and
                node["Index Name"] in indexes and
                String.contains?(node["Index Cond"] || "", "block_number")
            end)

          unless access["Node Type"] in ["Index Scan", "Index Only Scan", "Bitmap Heap Scan"] and
                   seeks != [] and
                   not Enum.any?(lock_nodes, &(&1["Node Type"] == "Seq Scan")),
                 do: raise("callback_requires_bounded_block_index")

          %{
            access: access["Node Type"],
            index: hd(seeks)["Index Name"],
            planned_rows: lock_root["Plan Rows"],
            estimated_total_cost: lock_root["Total Cost"]
          }
        end

      [[lsn]] = q.("SELECT pg_current_wal_insert_lsn()::text", [])

      {changed, already} =
        if mode == "migrate" do
          true = is_map(baseline)

          cond do
            before.needs_normalization == 0 ->
              {0, true}

            true ->
              true = before.full_hashes == baseline["full_hashes"]
              stage.("callback_update")
              {:ok, {count, nil}} = worker.update_batch(blocks)
              true = count == before.needs_normalization
              {count, false}
          end
        else
          {0, false}
        end

      q.("SET LOCAL enable_sort=off", [])
      q.("SET LOCAL enable_incremental_sort=off", [])
      q.("SET LOCAL enable_bitmapscan=off", [])
      stage.("after_snapshot")
      after_data = snapshot.()
      true = before.semantic_hashes == after_data.semantic_hashes
      if mode == "migrate", do: 0 = after_data.needs_normalization

      stage.("final_checks")

      after_statuses =
        q.(
          "SELECT to_jsonb(s) FROM public.migrations_status s WHERE migration_name=ANY($1::text[]) ORDER BY migration_name",
          [status_names]
        )
        |> List.flatten()

      true = after_statuses == statuses

      [[wal]] =
        q.("SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(),$1::text::pg_lsn)::bigint", [lsn])

      true = wal >= 0 and wal <= payload["wal_limit_bytes"]

      true =
        q.("SELECT version FROM public.schema_migrations ORDER BY version", []) |> List.flatten() ==
          applied

      elapsed = System.monotonic_time(:millisecond) - started
      true = elapsed <= 10000

      %{
        mode: mode,
        application_started: false,
        block: block,
        snapshot: after_data,
        statuses: statuses,
        updated_rows: changed,
        already_normalized: already,
        cluster_wal_delta_bytes: wal,
        access: scan["Node Type"],
        index: scan["Index Name"],
        callback_plan: callback_plan,
        elapsed_ms: elapsed
      }
    end,
    timeout: 10000
  )

nil = Process.whereis(Explorer.Supervisor)
nil = Process.whereis(worker)
stage.(if mode == "migrate", do: "committed", else: "inspected")
IO.puts("BLOCKSCOUT_DEPLOY_CHECK=" <> Jason.encode!(report))
