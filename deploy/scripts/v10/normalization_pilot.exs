defmodule BlockscoutNormalizationCheck do
  def run(payload) do
    # Explicitly selected complete blocks. Starts only a Repo; no application or worker.
    import Ecto.Query
    import Explorer.QueryHelper, only: [select_ctid: 1]
    alias Explorer.Chain.InternalTransaction

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
    worker = Explorer.Migrator.EmptyInternalTransactionsData
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
          # A fixed snapshot bounds the compiled callback without a vacuum-conflicting
          # table lock. Concurrent updates cause serialization failure and rollback;
          # inserts after this snapshot are outside this stored-data pass.
          if payload["vacuum_compatible"],
            do: q.("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ", [])

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
            q.("SELECT version FROM public.schema_migrations ORDER BY version", [])
            |> List.flatten()

          true = applied == payload["applied_versions"]

          [[heap]] =
            q.(
              "SELECT pg_relation_filenode('public.internal_transactions'::regclass)::bigint",
              []
            )

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

          if mode == "migrate" and payload["vacuum_compatible"] != true,
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
            root["Node Type"] == "Limit" and
              scan["Node Type"] in ["Index Scan", "Index Only Scan"] and
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
                         (not is_nil(it.created_contract_address_hash) and
                            is_nil(it.to_address_hash))),
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

                  if payload["vacuum_compatible"] do
                    deadline = payload["health_valid_until_unix_ms"]
                    now = System.system_time(:millisecond)

                    unless is_integer(deadline) and now <= deadline and deadline - now <= 30000,
                      do: raise("health_check_expired_or_invalid_before_update")
                  end

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
            q.("SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(),$1::text::pg_lsn)::bigint", [
              lsn
            ])

          true = wal >= 0 and wal <= payload["wal_limit_bytes"]

          true =
            q.("SELECT version FROM public.schema_migrations ORDER BY version", [])
            |> List.flatten() ==
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
    report
  end
end

defmodule BlockscoutNormalizationSession do
  @transaction_activity_sql ~S"""
  WITH clients AS MATERIALIZED (
    SELECT pid,state,xact_start,backend_xmin,backend_xid,wait_event_type,wait_event,usename,application_name
    FROM pg_stat_activity
    WHERE datname=current_database() AND backend_type='client backend' AND pid<>pg_backend_pid()
  ), old_clients AS MATERIALIZED (
    SELECT * FROM clients WHERE xact_start<clock_timestamp()-interval '300 seconds'
  ), details AS (
    SELECT jsonb_build_object(
      'pid',pid,'state',state,
      'transaction_age_seconds',EXTRACT(epoch FROM clock_timestamp()-xact_start)::bigint,
      'snapshot_present',backend_xmin IS NOT NULL,
      'snapshot_age_xids',CASE WHEN backend_xmin IS NULL THEN NULL ELSE age(backend_xmin) END,
      'assigned_xid_present',backend_xid IS NOT NULL,
      'wait_type',wait_event_type,'wait_event',wait_event,
      'same_role_as_observer',usename=current_user,
      'application_kind',CASE WHEN application_name='psql' THEN 'psql' WHEN application_name='pg_dump' THEN 'pg_dump' WHEN application_name LIKE 'blockscout-v10-%' THEN 'v10_maintenance' WHEN application_name='' THEN 'unset' ELSE 'other_named' END
    ) AS item FROM old_clients ORDER BY xact_start,pid LIMIT 20
  )
  SELECT jsonb_build_object(
    'observed_at',clock_timestamp()::text,
    'observer',jsonb_build_object('role_oid',(SELECT oid::bigint FROM pg_roles WHERE rolname=current_user),'can_read_all_stats',pg_has_role(current_user,'pg_read_all_stats','MEMBER'),'database_oid',(SELECT oid::bigint FROM pg_database WHERE datname=current_database())),
    'oldest_transaction_seconds',COALESCE((SELECT max(EXTRACT(epoch FROM clock_timestamp()-xact_start)) FROM clients),0)::bigint,
    'oldest_snapshot_xid_age',COALESCE((SELECT max(age(backend_xmin)) FROM clients),0),
    'idle_in_transaction_sessions',(SELECT count(*) FROM clients WHERE state LIKE 'idle in transaction%'),
    'client_transactions_over_300s',(SELECT count(*) FROM old_clients),
    'long_transactions_with_snapshot',(SELECT count(*) FROM old_clients WHERE backend_xmin IS NOT NULL),
    'long_transactions_with_assigned_xid',(SELECT count(*) FROM old_clients WHERE backend_xid IS NOT NULL),
    'long_client_sessions',COALESCE((SELECT jsonb_agg(item) FROM details),'[]'::jsonb)
  )
  """

  # All waits for the host occur outside transactions. No open snapshot is kept
  # while the host fsyncs a checkpoint or checks application health.
  def read(payload, mode) do
    {:ok, result} =
      Explorer.Repo.transaction(
        fn ->
          q = fn sql, args -> Explorer.Repo.query!(sql, args, log: false, timeout: 2000).rows end
          q.("SET TRANSACTION READ ONLY", [])
          q.("SET LOCAL statement_timeout='1500ms'", [])
          q.("SET LOCAL enable_seqscan=off", [])
          q.("SET LOCAL enable_bitmapscan=off", [])
          q.("SET LOCAL enable_sort=off", [])
          q.("SET LOCAL enable_incremental_sort=off", [])
          q.("SET LOCAL jit=off", [])

          [[database, oid, address, port, 15, false]] =
            q.(
              "SELECT current_database(),(SELECT oid::bigint FROM pg_database WHERE datname=current_database()),host(inet_server_addr()),inet_server_port(),current_setting('server_version_num')::integer/10000,pg_is_in_recovery()",
              []
            )

          true =
            %{
              "database" => database,
              "oid" => oid,
              "address" => address,
              "port" => port,
              "postgres_major" => 15
            } == payload["database_identity"]

          if mode == "sample" do
            bound = payload["before_block"]
            true = is_integer(bound) and bound >= 0

            sql =
              ~S|SELECT block_number,pg_column_size(it) FROM public.internal_transactions it WHERE block_number<$1 ORDER BY block_number DESC,transaction_index DESC,"index" DESC LIMIT 1001|

            [[[%{"Plan" => root}]]] = q.("EXPLAIN (FORMAT JSON) " <> sql, [bound])

            walk = fn recur, node ->
              [node | Enum.flat_map(node["Plans"] || [], &recur.(recur, &1))]
            end

            nodes = walk.(walk, root)
            [scan] = Enum.filter(nodes, &(&1["Relation Name"] == "internal_transactions"))

            indexes =
              q.(
                "SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=i.indkey[0] WHERE i.indrelid='public.internal_transactions'::regclass AND i.indisvalid AND i.indisready AND i.indpred IS NULL AND a.attname='block_number'",
                []
              )
              |> List.flatten()

            true =
              root["Node Type"] == "Limit" and
                scan["Node Type"] in ["Index Scan", "Index Only Scan"] and
                scan["Index Name"] in indexes and
                String.contains?(scan["Index Cond"] || "", "block_number")

            false =
              Enum.any?(
                nodes,
                &(&1["Node Type"] in ["Sort", "Incremental Sort", "Seq Scan", "Bitmap Heap Scan"])
              )

            records = q.(sql, [bound])

            true =
              Enum.all?(records, fn [height, size] ->
                is_integer(height) and height >= 0 and height < bound and is_integer(size) and
                  size >= 0
              end)

            boundary = if length(records) == 1001, do: hd(List.last(records)), else: nil
            selected = if boundary, do: Enum.reject(records, &(hd(&1) == boundary)), else: records

            groups =
              selected
              |> Enum.chunk_by(&hd/1)
              |> Enum.map(fn rows -> %{block: hd(hd(rows)), rows: length(rows)} end)

            %{
              block_rows: groups,
              selected_rows: length(selected),
              selected_bytes: Enum.reduce(selected, 0, fn [_, size], sum -> sum + size end),
              excluded_boundary_block: boundary,
              older_non_null_range_exhausted: length(records) < 1001
            }
          else
            [[table]] =
              q.(
                "SELECT jsonb_build_object('estimated_rows',c.reltuples::bigint,'heap_bytes',pg_relation_size(c.oid),'index_bytes',pg_indexes_size(c.oid),'total_bytes',pg_total_relation_size(c.oid),'frozen_xid_age',age(c.relfrozenxid),'options',COALESCE(to_jsonb(c.reloptions),'[]'::jsonb),'updates',s.n_tup_upd,'hot_updates',s.n_tup_hot_upd,'dead_rows',s.n_dead_tup,'vacuum_count',s.vacuum_count,'autovacuum_count',s.autovacuum_count,'last_vacuum',s.last_vacuum::text,'last_autovacuum',s.last_autovacuum::text,'stats_reset',d.stats_reset::text) FROM pg_class c JOIN pg_stat_user_tables s ON s.relid=c.oid JOIN pg_stat_database d ON d.datname=current_database() WHERE c.oid='public.internal_transactions'::regclass",
                []
              )

            [[transactions]] =
              q.(
                @transaction_activity_sql,
                []
              )

            [[wal]] =
              q.(
                "SELECT jsonb_build_object('directory_bytes',(SELECT COALESCE(sum(size),0)::bigint FROM pg_ls_waldir()),'slot_count',(SELECT count(*) FROM pg_replication_slots),'retained_slot_bytes',(SELECT COALESCE(max(pg_wal_lsn_diff(pg_current_wal_insert_lsn(),restart_lsn)),0)::bigint FROM pg_replication_slots),'lsn',pg_current_wal_insert_lsn()::text,'bytes_since_stats_reset',wal_bytes::text,'stats_reset',stats_reset::text) FROM pg_stat_wal",
                []
              )

            vacuum =
              q.(
                "SELECT jsonb_build_object('phase',phase,'heap_blocks_total',heap_blks_total,'heap_blocks_scanned',heap_blks_scanned,'heap_blocks_vacuumed',heap_blks_vacuumed,'index_vacuum_count',index_vacuum_count,'max_dead_tuples',max_dead_tuples,'dead_tuples',num_dead_tuples) FROM pg_stat_progress_vacuum WHERE datname=current_database() AND relid='public.internal_transactions'::regclass",
                []
              )
              |> List.flatten()

            settings =
              q.(
                "SELECT name,setting,unit FROM pg_settings WHERE name=ANY($1::text[]) ORDER BY name",
                [
                  ~w(autovacuum track_counts autovacuum_vacuum_threshold autovacuum_vacuum_scale_factor autovacuum_analyze_threshold autovacuum_analyze_scale_factor autovacuum_max_workers autovacuum_naptime autovacuum_work_mem maintenance_work_mem autovacuum_vacuum_cost_delay autovacuum_vacuum_cost_limit vacuum_cost_limit vacuum_cost_delay vacuum_cost_page_hit vacuum_cost_page_miss vacuum_cost_page_dirty max_wal_size wal_compression full_page_writes archive_mode track_io_timing)
                ]
              )

            [[io]] =
              q.(
                "SELECT jsonb_build_object('heap_blocks_read',heap_blks_read,'heap_blocks_hit',heap_blks_hit,'index_blocks_read',idx_blks_read,'index_blocks_hit',idx_blks_hit) FROM pg_statio_user_tables WHERE relid='public.internal_transactions'::regclass",
                []
              )

            [[clock]] = q.("SELECT clock_timestamp()::text", [])

            %{
              table: table,
              transactions: transactions,
              wal: wal,
              vacuum: vacuum,
              settings: settings,
              io: io,
              observed_at: clock
            }
          end
        end,
        timeout: 5000
      )

    result
  end

  # Combine existing read-only operations across one mailbox request. Each Repo
  # transaction finishes before the next; the host still journals the complete
  # baseline before requesting any write. No migration worker is started.
  def prepare_batch(payload) do
    true = payload["execution_profile"] == "lean-v1"
    resources = read(payload, "resources")
    sample = read(payload, "sample")

    inspection =
      if sample.selected_rows > 0 do
        BlockscoutNormalizationCheck.run(
          Map.merge(payload, %{
            "mode" => "inspect",
            "vacuum_compatible" => true,
            "blocks" => Enum.map(sample.block_rows, & &1.block),
            "block_rows" =>
              Enum.map(sample.block_rows, &%{"block" => &1.block, "rows" => &1.rows}),
            "expected_rows" => sample.selected_rows
          })
        )
      end

    %{resources: resources, sample: sample, inspection: inspection}
  end

  def loop(base, remaining) when remaining > 0 do
    path = "/tmp/channel/request.json"
    # A dead host cannot leave a process or an old transaction running forever.
    received =
      Enum.reduce_while(1..2000, nil, fn _, _ ->
        if File.exists?(path),
          do: {:halt, Jason.decode!(File.read!(path))},
          else:
            (
              Process.sleep(30)
              {:cont, nil}
            )
      end)

    if received do
      File.rm!(path)
      id = received["id"]

      try do
        payload = Map.merge(base, received["payload"])

        result =
          case received["command"] do
            "prepare_batch" ->
              true = base["execution_profile"] == "lean-v1"
              prepare_batch(payload)

            "sample" ->
              read(payload, "sample")

            "resources" ->
              read(payload, "resources")

            command when command in ["inspect", "migrate"] ->
              BlockscoutNormalizationCheck.run(
                Map.merge(payload, %{"mode" => command, "vacuum_compatible" => true})
              )
          end

        respond(%{id: id, ok: true, data: result})
        loop(base, remaining - 1)
      rescue
        error ->
          IO.puts(Exception.format(:error, error, __STACKTRACE__))

          reason =
            if match?(
                 %RuntimeError{message: "health_check_expired_or_invalid_before_update"},
                 error
               ),
               do: "health_freshness_before_update",
               else: "transaction_or_read_check_failed"

          respond(%{id: id, ok: false, reason: reason})
          # Keep the failed response readable until the host collects it and
          # removes this client. The Repo transaction has already rolled back.
          # A lost host leaves no transaction open and exits after ten seconds.
          Process.sleep(10000)
      end
    end
  end

  def loop(_, 0), do: :ok

  defp respond(response) do
    path = "/tmp/channel/response.tmp"
    File.write!(path, Jason.encode!(response), [:sync])
    File.chmod!(path, 0o600)
    File.rename!(path, "/tmp/channel/response.json")
  end
end

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
nil = Process.whereis(Explorer.Migrator.EmptyInternalTransactionsData)

payload = Jason.decode!(File.read!("/tmp/upgrade/payload.json"))

if payload["session"] do
  # Bound requests as well as idle time. The host retains the durable budget.
  File.mkdir_p!("/tmp/channel")
  IO.puts("BLOCKSCOUT_SESSION_READY")
  # Frozen legacy clients retain 200 requests. The reviewed observation profile
  # needs enough commands for 250 small transactions, including read checks.
  requests = Map.get(payload, "session_request_limit", 200)
  true = requests in [200, 2000]
  BlockscoutNormalizationSession.loop(payload, requests)
else
  report = BlockscoutNormalizationCheck.run(payload)
  IO.puts("BLOCKSCOUT_DEPLOY_CHECK=" <> Jason.encode!(report))
end
