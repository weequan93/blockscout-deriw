defmodule BlockscoutV10Vacuum do
  @sql "VACUUM (VERBOSE, INDEX_CLEANUP ON, TRUNCATE OFF, PARALLEL 0) public.internal_transactions"
  @lease_ms 45000

  def sql, do: @sql
  def fingerprint, do: Base.encode16(:crypto.hash(:md5, @sql), case: :lower)

  # Invoked through the already running backend, never through the vacuum socket.
  def observe(payload) do
    true = Explorer.Migrator.StartupControl.disabled?()
    resources = BlockscoutV10VacuumResources.read(payload, "resources")

    {:ok, details} =
      Explorer.Repo.transaction(
        fn ->
          q = fn sql, args -> Explorer.Repo.query!(sql, args, log: false, timeout: 2000).rows end
          q.("SET TRANSACTION READ ONLY", [])
          q.("SET LOCAL statement_timeout='1500ms'", [])
          q.("SET LOCAL lock_timeout='500ms'", [])

          [[heap]] =
            q.(
              "SELECT pg_relation_filenode('public.internal_transactions'::regclass)::bigint",
              []
            )

          true = heap == payload["heap_identity"]

          versions =
            q.("SELECT version FROM public.schema_migrations ORDER BY version", [])
            |> List.flatten()

          true = versions == payload["applied_versions"]

          statuses =
            q.(
              "SELECT to_jsonb(s) FROM public.migrations_status s WHERE migration_name=ANY($1::text[]) ORDER BY migration_name",
              [payload["status_names"]]
            )
            |> List.flatten()

          true = statuses == payload["statuses"]
          target = payload["target"] || %{}

          sessions =
            q.(
              "SELECT jsonb_build_object('pid',pid,'backend_start',backend_start::text,'query_start',query_start::text,'application_name',application_name,'query_md5',md5(query),'state',state,'same_role',usename=current_user,'wait_type',wait_event_type,'wait_event',wait_event) FROM pg_stat_activity WHERE pid=$1 AND datname=current_database() AND backend_type='client backend'",
              [target["pid"] || 0]
            )
            |> List.flatten()

          owned =
            Enum.find(sessions, fn row ->
              row["backend_start"] == target["backend_start"] and
                row["application_name"] == payload["application_name"] and row["same_role"]
            end)

          current_start =
            if owned && owned["state"] in ["active", "idle"] &&
                 owned["query_md5"] == fingerprint(),
               do: owned["query_start"]

          saved_start = payload["vacuum_query_started_at"]
          if saved_start && current_start, do: true = saved_start == current_start

          boundary =
            if payload["transaction_guard_scope"] == "pre_vacuum_clients",
              do: saved_start || current_start

          age_limit = payload["oldest_transaction_seconds"] || 300
          true = is_integer(age_limit) and age_limit in 1..300

          args = [
            target["pid"] || 0,
            target["backend_start"] || "1970-01-01",
            payload["application_name"],
            boundary,
            age_limit
          ]

          [[transactions]] =
            q.(
              "SELECT jsonb_build_object('oldest_transaction_seconds',COALESCE(max(EXTRACT(epoch FROM clock_timestamp()-xact_start)),0)::bigint,'oldest_pre_vacuum_transaction_seconds',COALESCE(max(EXTRACT(epoch FROM clock_timestamp()-xact_start)) FILTER(WHERE $4::text IS NULL OR xact_start<=$4::text::timestamptz),0)::bigint,'post_vacuum_client_transactions_over_limit',count(*) FILTER(WHERE xact_start>$4::text::timestamptz AND xact_start<clock_timestamp()-$5::integer*interval '1 second'),'vacuum_boundary_known',$4::text IS NOT NULL,'oldest_snapshot_xid_age',COALESCE(max(age(backend_xmin)),0),'idle_in_transaction_sessions',count(*) FILTER(WHERE state LIKE 'idle in transaction%'),'prepared_transactions',(SELECT count(*) FROM pg_prepared_xacts WHERE database=current_database()),'slots_with_xmin',(SELECT count(*) FROM pg_replication_slots WHERE xmin IS NOT NULL)) FROM pg_stat_activity WHERE datname=current_database() AND backend_type='client backend' AND pid<>pg_backend_pid() AND NOT(pid=$1 AND backend_start=$2::text::timestamptz AND application_name=$3)",
              args
            )

          [[role]] = q.("SELECT oid::bigint FROM pg_roles WHERE rolname=current_user", [])

          %{
            transactions: transactions,
            target_sessions: sessions,
            vacuum_query_started_at: boundary,
            heap_identity: heap,
            observer_role_oid: role
          }
        end,
        timeout: 5000
      )

    held = fn supervisor, worker ->
      Enum.any?(Supervisor.which_children(supervisor), fn
        {^worker, :undefined, :worker, _} -> true
        _ -> false
      end) and is_nil(Process.whereis(worker))
    end

    true = held.(BlockScoutWeb.Supervisor, Indexer.Prometheus.Metrics)

    true =
      held.(Indexer.Fetcher.EmptyBlocksSanitizer.Supervisor, Indexer.Fetcher.EmptyBlocksSanitizer)

    diagnostics = BlockscoutV10VacuumDiagnostics.run(BlockscoutV10VacuumResources.activity_sql())

    anchor =
      if payload["verify_anchor"],
        do:
          BlockscoutV10VacuumAnchor.run(
            Map.merge(
              payload,
              Map.merge(payload["anchor"], %{
                "mode" => "inspect",
                "wal_limit_bytes" => 134_217_728,
                "vacuum_compatible" => true
              })
            )
          )

    Map.merge(resources, details)
    |> Map.put(:anchor, anchor)
    |> Map.put(:runtime_holds_verified, true)
    |> Map.put(:query_diagnostics, diagnostics.query_diagnostics)
  end

  defp emit(marker, value), do: IO.puts(marker <> Jason.encode!(value))

  defp lease(payload, previous, renewed_at) do
    case File.read("/tmp/channel/lease.json") do
      {:ok, raw} ->
        value = Jason.decode!(raw)
        true = value["token"] == payload["token"]
        sequence = value["sequence"]
        true = is_integer(sequence) and sequence > 0

        if sequence > previous,
          do: {sequence, System.monotonic_time(:millisecond)},
          else: {previous, renewed_at}

      {:error, :enoent} ->
        {previous, renewed_at}
    end
  end

  defp start(payload, sequence, renewed_at, until) do
    {sequence, renewed_at} = lease(payload, sequence, renewed_at)
    now = System.monotonic_time(:millisecond)

    cond do
      now > until ->
        raise("vacuum_start_window_expired")

      File.exists?("/tmp/channel/start.json") ->
        %{"token" => token} = Jason.decode!(File.read!("/tmp/channel/start.json"))
        true = token == payload["token"] and sequence > 0 and now - renewed_at < @lease_ms
        {sequence, renewed_at}

      true ->
        Process.sleep(100)
        start(payload, sequence, renewed_at, until)
    end
  end

  defp wait(task, payload, identity, sequence, renewed_at, started) do
    {sequence, renewed_at} = lease(payload, sequence, renewed_at)
    now = System.monotonic_time(:millisecond)

    reason =
      cond do
        now - renewed_at >= @lease_ms -> "host_lease_expired"
        now - started >= payload["duration_seconds"] * 1000 -> "duration_limit"
        true -> nil
      end

    if reason do
      emit("BLOCKSCOUT_VACUUM_RESULT=", %{
        token: payload["token"],
        identity: identity,
        mode: "interrupted",
        reason: reason
      })

      # This isolated VM owns only its maintenance socket. Exiting closes that
      # socket; no other backend is selected or signalled. Partial cleanup stays.
      System.halt(2)
    end

    case Task.yield(task, 200) do
      {:ok, result} ->
        emit(
          "BLOCKSCOUT_VACUUM_RESULT=",
          Map.merge(result, %{
            token: payload["token"],
            identity: identity,
            elapsed_ms: System.monotonic_time(:millisecond) - started
          })
        )

      nil ->
        wait(task, payload, identity, sequence, renewed_at, started)
    end
  end

  def run(payload) do
    Process.flag(:trap_exit, true)
    true = Explorer.Migrator.StartupControl.disabled?()
    nil = Process.whereis(Explorer.Supervisor)
    nil = Process.whereis(Explorer.Migrator.EmptyInternalTransactionsData)
    payload = Map.put_new(payload, "duration_seconds", 3600)
    duration = payload["duration_seconds"]
    true = is_integer(duration) and duration in [3600, 14400]
    duration_ms = duration * 1000

    options =
      Explorer.Repo.config()
      |> Keyword.take([
        :hostname,
        :port,
        :database,
        :username,
        :password,
        :ssl,
        :ssl_opts,
        :socket_dir,
        :socket,
        :types
      ])

    options =
      Keyword.merge(options,
        pool_size: 1,
        backoff_type: :stop,
        prepare: :unnamed,
        parameters: [
          application_name: payload["application_name"],
          statement_timeout: Integer.to_string(duration_ms),
          lock_timeout: "500",
          maintenance_work_mem: "512MB",
          vacuum_cost_limit: "200",
          vacuum_cost_delay: "2ms",
          client_connection_check_interval: "1s"
        ]
      )

    {:ok, conn} = Postgrex.start_link(options)
    q = fn sql -> Postgrex.query!(conn, sql, [], timeout: 5000).rows end

    [[database, oid, address, port, 15, false, heap]] =
      q.(
        "SELECT current_database(),(SELECT oid::bigint FROM pg_database WHERE datname=current_database()),host(inet_server_addr()),inet_server_port(),current_setting('server_version_num')::integer/10000,pg_is_in_recovery(),pg_relation_filenode('public.internal_transactions'::regclass)::bigint"
      )

    true =
      %{
        "database" => database,
        "oid" => oid,
        "address" => address,
        "port" => port,
        "postgres_major" => 15
      } == payload["database_identity"]

    true = heap == payload["heap_identity"]

    [[identity]] =
      q.(
        "SELECT jsonb_build_object('pid',pid,'backend_start',backend_start::text,'application_name',application_name,'role_oid',usesysid::bigint,'database_oid',datid::bigint) FROM pg_stat_activity WHERE pid=pg_backend_pid()"
      )

    identity = Map.put(identity, "query_md5", fingerprint())

    [[settings]] =
      q.(
        "SELECT jsonb_build_object('maintenance_work_mem',current_setting('maintenance_work_mem'),'vacuum_cost_limit',current_setting('vacuum_cost_limit'),'vacuum_cost_delay',current_setting('vacuum_cost_delay'),'lock_timeout',current_setting('lock_timeout'),'statement_timeout',current_setting('statement_timeout'),'client_connection_check_interval',current_setting('client_connection_check_interval'))"
      )

    identity = Map.put(identity, "settings", settings)
    File.mkdir_p!("/tmp/channel")
    emit("BLOCKSCOUT_VACUUM_READY=", identity)
    now = System.monotonic_time(:millisecond)
    {sequence, renewed_at} = start(payload, 0, now, now + 60000)
    [[pid]] = q.("SELECT pg_backend_pid()")
    true = pid == identity["pid"]

    task =
      Task.async(fn ->
        try do
          result = Postgrex.query!(conn, @sql, [], timeout: duration_ms + 5000)
          :vacuum = result.command
          # VERBOSE messages remain private in the durable client log.
          emit("BLOCKSCOUT_VACUUM_VERBOSE=", Map.get(result, :messages, []))
          Postgrex.query!(conn, "SELECT pg_stat_force_next_flush()", [], timeout: 2000)
          %{mode: "completed", command: "vacuum", index_cleanup_requested: true}
        rescue
          error ->
            IO.puts(Exception.format(:error, error, __STACKTRACE__))
            %{mode: "interrupted", reason: "database_error"}
        end
      end)

    wait(task, payload, identity, sequence, renewed_at, System.monotonic_time(:millisecond))
  end
end

# VACUUM_CLIENT_ENTRYPOINT
for app <- [:crypto, :ssl, :postgrex, :ecto_sql],
    do: {:ok, _} = Application.ensure_all_started(app)

BlockscoutV10Vacuum.run(Jason.decode!(File.read!("/tmp/upgrade/payload.json")))
