defmodule BlockscoutV10MetricsHold do
  @worker Indexer.Prometheus.Metrics
  @supervisor BlockScoutWeb.Supervisor
  @query_md5 "d63aea58d81cc7d2d9c7ad5cd82809dd"

  defp query(sql, args) do
    {:ok, rows} =
      Explorer.Repo.transaction(
        fn ->
          Explorer.Repo.query!("SET TRANSACTION READ ONLY", [], log: false)
          Explorer.Repo.query!("SET LOCAL statement_timeout='1500ms'", [], log: false)
          Explorer.Repo.query!("SET LOCAL lock_timeout='500ms'", [], log: false)
          Explorer.Repo.query!(sql, args, log: false, timeout: 2000).rows
        end,
        timeout: 5000
      )

    rows
  end

  defp children do
    all = Supervisor.which_children(@supervisor)
    [{@worker, pid, :worker, [@worker]}] = Enum.filter(all, &(elem(&1, 0) == @worker))
    {:ok, spec} = :supervisor.get_childspec(@supervisor, @worker)
    {@worker, :start_link, _} = spec.start
    true = is_integer(spec.shutdown) and spec.shutdown >= 0 and spec.shutdown <= 5000
    {pid, Enum.reject(all, &(elem(&1, 0) == @worker)) |> Enum.sort()}
  end

  defp target(payload) do
    query(
      "SELECT jsonb_build_object('pid',pid,'backend_start',backend_start::text,'query_start',query_start::text,'state',state,'expected_query',md5(query)=$2 AND query_start=$3::text::timestamptz,'snapshot_present',backend_xmin IS NOT NULL,'assigned_xid_present',backend_xid IS NOT NULL,'same_role',usename=current_user) FROM pg_stat_activity WHERE pid=$1 AND datname=current_database() AND backend_type='client backend' AND pid<>pg_backend_pid()",
      [payload["pid"], @query_md5, payload["query_start"]]
    )
    |> List.flatten()
  end

  def run(payload) do
    true = Explorer.Migrator.StartupControl.disabled?()
    true = is_pid(Process.whereis(Explorer.Repo))
    "10.2.6" = Application.spec(:explorer, :vsn) |> to_string()
    true = payload["query_md5"] == @query_md5
    true = is_integer(payload["pid"]) and payload["pid"] > 0
    true = payload["mode"] in ["observe", "hold", "restore"]
    {before_pid, siblings} = children()
    observed = target(payload)

    if payload["mode"] == "hold" and is_pid(before_pid) do
      # An exited target is harmless. A PID reused for another query is never
      # cancelled. The fixed fingerprint identifies the source-reviewed metric.
      true =
        observed == [] or
          Enum.all?(
            observed,
            &(&1["expected_query"] and &1["same_role"] and not &1["assigned_xid_present"])
          )
    end

    cancellation =
      case payload["mode"] do
        "hold" ->
          if is_pid(before_pid), do: :ok = Supervisor.terminate_child(@supervisor, @worker)
          {:undefined, ^siblings} = children()
          nil = Process.whereis(@worker)
          # Terminating the owner normally releases its database task. Cancel only
          # the still-running, exact original SELECT if it outlived that owner.
          query(
            "SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE pid=$1 AND md5(query)=$2 AND query_start=$3::text::timestamptz AND backend_start=$4::text::timestamptz AND datname=current_database() AND usename=current_user AND backend_type='client backend' AND state='active' AND backend_xid IS NULL AND pid<>pg_backend_pid()",
            [payload["pid"], @query_md5, payload["query_start"], payload["backend_start"]]
          )
          |> List.flatten()

        "restore" ->
          if before_pid == :undefined do
            {:ok, _} = Supervisor.restart_child(@supervisor, @worker)
          end

          []

        "observe" ->
          []
      end

    {after_pid, ^siblings} = children()

    remaining =
      Enum.reduce_while(1..5, [], fn _, _ ->
        rows = target(payload)

        if payload["mode"] == "hold" and
             Enum.any?(rows, &(&1["expected_query"] and &1["state"] == "active")),
           do:
             (
               Process.sleep(100)
               {:cont, rows}
             ),
           else: {:halt, rows}
      end)

    %{
      mode: payload["mode"],
      metrics_worker_running: is_pid(after_pid),
      metrics_worker_held: after_pid == :undefined,
      other_web_children_preserved: true,
      target: remaining,
      cancellation_requested: cancellation != [],
      cancellation_accepted: cancellation == [true],
      application_restarted: false,
      database_rows_modified: false,
      original_query_active:
        Enum.any?(remaining, &(&1["expected_query"] and &1["state"] == "active")),
      affected:
        "Periodic indexer health-metric refreshes; existing gauges may remain stale. Live indexing, API and NFT services retain their supervisors."
    }
  end
end
