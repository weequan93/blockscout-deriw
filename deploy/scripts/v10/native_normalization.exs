defmodule BlockscoutNativeNormalization.Guard do
  @name "empty_internal_transactions_data"
  @lease "/tmp/native-lease.json"
  @status "/tmp/native-status.json"
  @needs ~S|(it.trace_address IS NOT NULL OR it.value=0 OR (it.call_type_enum IS NULL AND it.call_type IS NOT NULL) OR it.error IS NOT NULL OR (it.created_contract_address_hash IS NOT NULL AND it.to_address_hash IS NULL)) IS TRUE|

  def payload, do: :persistent_term.get({__MODULE__, :payload})
  def configure(payload), do: :persistent_term.put({__MODULE__, :payload}, payload)

  def fail!(reason) do
    status("needs_attention", reason)
    raise reason
  end

  def status(phase, reason \\ nil, details \\ %{}) do
    value = Map.merge(%{phase: phase, reason: reason, token: payload()["token"]}, details)
    File.write!(@status <> ".tmp", Jason.encode!(value))
    File.rename!(@status <> ".tmp", @status)
    :ok
  end

  def authorized? do
    with {:ok, raw} <- File.read(@lease),
         {:ok, lease} <- Jason.decode(raw),
         true <- lease["token"] == payload()["token"],
         true <- lease["mode"] == "run",
         deadline when is_integer(deadline) <- lease["valid_until_unix_ms"] do
      remaining = deadline - System.system_time(:millisecond)
      remaining > 0 and remaining <= 45000
    else
      _ -> false
    end
  end

  def batch_config do
    with {:ok, raw} <- File.read(@lease),
         {:ok, lease} <- Jason.decode(raw),
         true <- lease["token"] == payload()["token"],
         blocks when is_integer(blocks) and blocks >= 1 and blocks <= 1000 <- lease["batch_blocks"] do
      for module <- [Explorer.Migrator.EmptyInternalTransactionsData, BlockscoutNativeNormalization] do
        Application.put_env(:explorer, module, batch_size: blocks, concurrency: 1, timeout: 0)
      end
    else
      _ -> :ok
    end
  end

  # Waiting never retains a database transaction or snapshot.
  def await_lease do
    if authorized?() do
      :ok
    else
      status("waiting", "host_authorization_required")
      Process.sleep(1000)
      await_lease()
    end
  end

  def identity! do
    q = fn sql -> Explorer.Repo.query!(sql, [], log: false, timeout: 2000).rows end
    [[database, oid, address, port, 15, false]] =
      q.("SELECT current_database(),(SELECT oid::bigint FROM pg_database WHERE datname=current_database()),host(inet_server_addr()),inet_server_port(),current_setting('server_version_num')::integer/10000,pg_is_in_recovery()")
    true = %{"database" => database, "oid" => oid, "address" => address,
             "port" => port, "postgres_major" => 15} == payload()["database_identity"]
    [[heap]] = q.("SELECT pg_relation_filenode('public.internal_transactions'::regclass)::bigint")
    true = heap == payload()["heap_identity"]
    versions = q.("SELECT version FROM public.schema_migrations ORDER BY version") |> List.flatten()
    true = versions == payload()["applied_versions"]
    :ok
  end

  def own_lock! do
    owner = :persistent_term.get({__MODULE__, :owner})
    [[true]] = Explorer.Repo.query!(~S"""
    SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=$1 AND locktype='advisory'
      AND classid=184175075::oid AND objid=1026::oid AND objsubid=2 AND granted)
    """, [owner], log: false, timeout: 2000).rows
    :ok
  end

  def hold_lock do
    caller = self()
    spawn_link(fn ->
      Explorer.Repo.checkout(fn ->
        [[true]] = Explorer.Repo.query!("SELECT pg_try_advisory_lock(184175075,1026)", [], log: false).rows
        [[pid]] = Explorer.Repo.query!("SELECT pg_backend_pid()", [], log: false).rows
        send(caller, {:native_lock, self(), pid})
        receive do
          :release -> :ok
        end
      end, timeout: :infinity)
    end)
    receive do
      {:native_lock, process, backend} ->
        :persistent_term.put({__MODULE__, :owner}, backend)
        process
    after
      5000 -> raise "native_owner_unavailable"
    end
  end

  def seed! do
    {:ok, :ok} = Explorer.Repo.transaction(fn ->
      Explorer.Repo.query!("SET LOCAL lock_timeout='500ms'", [], log: false)
      identity!()
      own_lock!()
      rows = Explorer.Repo.query!("SELECT to_jsonb(s) FROM public.migrations_status s WHERE migration_name=$1 FOR UPDATE", [@name], log: false).rows
      current = case rows do [] -> nil; [[row]] -> row end
      meta = (current && current["meta"]) || %{}
      if meta["v10_native_job"] == payload()["token"] do
        true = current["status"] in ["started", "completed"]
        true = is_integer(meta["max_block_number"]) and meta["max_block_number"] in -1..(payload()["start_before_block"] - 1)
      else
        true = current == payload()["native_status_before"]
        true = is_nil(current) or current["status"] != "completed"
        new_meta = Map.merge(meta, %{"max_block_number" => payload()["start_before_block"] - 1,
                                    "v10_native_job" => payload()["token"]})
        Explorer.Repo.query!(~S"""
        INSERT INTO public.migrations_status(migration_name,status,meta,inserted_at,updated_at)
        VALUES($1,'started',$2,now(),now()) ON CONFLICT(migration_name)
        DO UPDATE SET status='started',meta=EXCLUDED.meta,updated_at=now()
        """, [@name, new_meta], log: false)
      end
      [[options]] = Explorer.Repo.query!("SELECT COALESCE(to_jsonb(reloptions),'[]'::jsonb) FROM pg_class WHERE oid='public.internal_transactions'::regclass", [], log: false).rows
      desired = ~w(autovacuum_vacuum_scale_factor=0 autovacuum_vacuum_threshold=5000000 autovacuum_vacuum_cost_limit=400 autovacuum_vacuum_cost_delay=2)
      expected = payload()["table_options_before"]
      keys = Enum.map(desired, &(hd(String.split(&1, "="))))
      amended = Enum.reject(expected, &(hd(String.split(&1, "=")) in keys)) ++ desired
      true = Enum.sort(options) in [Enum.sort(expected), Enum.sort(amended)]
      if Enum.sort(options) != Enum.sort(amended) do
        Explorer.Repo.query!("ALTER TABLE public.internal_transactions SET (autovacuum_vacuum_scale_factor=0,autovacuum_vacuum_threshold=5000000,autovacuum_vacuum_cost_limit=400,autovacuum_vacuum_cost_delay=2)", [], log: false)
      end
      :ok
    end, timeout: 10000)
    :ok
  end

  def update(blocks) do
    await_lease()
    {:ok, result} = Explorer.Repo.transaction(fn ->
      Explorer.Repo.query!("SET LOCAL statement_timeout='30s'", [], log: false)
      Explorer.Repo.query!("SET LOCAL lock_timeout='500ms'", [], log: false)
      Explorer.Repo.query!("SET LOCAL enable_seqscan=off", [], log: false)
      own_lock!()
      [[rows, bytes]] = Explorer.Repo.query!(~S"""
      SELECT count(*),COALESCE(sum(pg_column_size(it)),0)::bigint
      FROM (SELECT it.* FROM public.internal_transactions it WHERE block_number=ANY($1::bigint[]) LIMIT 10001) it
      """, [blocks], log: false, timeout: 6000).rows
      unless rows <= 10000 and bytes <= 16777216, do: fail!("native_batch_requires_smaller_range")
      unless authorized?(), do: fail!("native_lease_expired_before_update")
      [[lsn]] = Explorer.Repo.query!("SELECT pg_current_wal_insert_lsn()::text", [], log: false).rows
      {:ok, {updated, nil}} = Explorer.Migrator.EmptyInternalTransactionsData.update_batch(blocks)
      unless updated <= 10000, do: fail!("native_batch_requires_smaller_range")
      [[wal]] = Explorer.Repo.query!("SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(),$1::text::pg_lsn)::bigint", [lsn], log: false).rows
      unless wal <= 134217728, do: fail!("native_transaction_wal_limit")
      {updated, wal, rows}
    end, timeout: 40000)
    status("running", nil, %{last_batch_updated_rows: elem(result, 0),
      last_batch_wal_bytes: elem(result, 1), last_batch_rows: elem(result, 2)})
    result
  rescue
    error in Postgrex.Error ->
      reason = if error.postgres[:code] in [:query_canceled, :deadlock_detected, :lock_not_available],
        do: "native_transient_database_error", else: "native_database_error"
      status("needs_attention", reason)
      reraise error, __STACKTRACE__
    error in DBConnection.ConnectionError ->
      status("needs_attention", "native_transient_database_error")
      reraise error, __STACKTRACE__
  end

  def finish! do
    await_lease()
    status("checking_residuals")
    # One final read-only proof includes NULL blocks and concurrent insertions.
    # It must succeed before the native framework may set its completion record.
    {:ok, :ok} = Explorer.Repo.transaction(fn ->
      Explorer.Repo.query!("SET TRANSACTION READ ONLY", [], log: false)
      Explorer.Repo.query!("SET LOCAL statement_timeout='4h'", [], log: false)
      Explorer.Repo.query!("SET LOCAL lock_timeout='500ms'", [], log: false)
      identity!()
      own_lock!()
      [[remaining]] = Explorer.Repo.query!("SELECT EXISTS(SELECT 1 FROM public.internal_transactions it WHERE " <> @needs <> " LIMIT 1)", [], log: false, timeout: 14410000).rows
      unless remaining == false, do: fail!("native_residual_rows_require_review")
      :ok
    end, timeout: 14420000)
    :ok
  end
end

defmodule BlockscoutNativeNormalization do
  use Explorer.Migrator.FillingMigration
  alias Explorer.Migrator.EmptyInternalTransactionsData, as: Native
  alias BlockscoutNativeNormalization.Guard
  def migration_name, do: Native.migration_name()
  def last_unprocessed_identifiers(state) do
    Guard.batch_config()
    Native.last_unprocessed_identifiers(state)
  end
  def unprocessed_data_query, do: nil
  def update_batch(blocks), do: Guard.update(blocks)
  def update_cache, do: Native.update_cache()
  def on_finish, do: Guard.finish!()
end

# NATIVE_CLIENT_ENTRYPOINT
true = Explorer.Migrator.StartupControl.disabled?()
"10.2.6" = Application.spec(:explorer, :vsn) |> to_string()
nil = Process.whereis(Explorer.Supervisor)
Process.flag(:trap_exit, true)
for app <- [:logger, :crypto, :ssl, :postgrex, :ecto_sql, :con_cache], do: ({:ok, _} = Application.ensure_all_started(app))
payload = Jason.decode!(File.read!("/tmp/upgrade/payload.json"))
BlockscoutNativeNormalization.Guard.configure(payload)
Application.put_env(:explorer, Explorer.Migrator.EmptyInternalTransactionsData,
  batch_size: payload["batch_blocks"], concurrency: 1, timeout: 0)
Application.put_env(:explorer, BlockscoutNativeNormalization,
  batch_size: payload["batch_blocks"], concurrency: 1, timeout: 0)
{:ok, _} = Explorer.Repo.start_link(pool_size: 3, log: false,
  parameters: [application_name: payload["application_name"], client_connection_check_interval: "1s"])
{:ok, _} = Supervisor.start_link([Explorer.Chain.Cache.BackgroundMigrations], strategy: :one_for_one)
_owner = BlockscoutNativeNormalization.Guard.hold_lock()
BlockscoutNativeNormalization.Guard.seed!()
BlockscoutNativeNormalization.Guard.status("ready")
{:ok, worker} = BlockscoutNativeNormalization.start_link([])
reference = Process.monitor(worker)
receive do
  {:DOWN, ^reference, :process, ^worker, :normal} ->
    true = Explorer.Migrator.EmptyInternalTransactionsData.migration_finished?()
    BlockscoutNativeNormalization.Guard.status("completed")
  {:DOWN, ^reference, :process, ^worker, _reason} ->
    previous = Jason.decode!(File.read!("/tmp/native-status.json"))
    unless previous["phase"] == "needs_attention",
      do: BlockscoutNativeNormalization.Guard.status("needs_attention", "native_worker_stopped")
end
Process.sleep(:infinity)
