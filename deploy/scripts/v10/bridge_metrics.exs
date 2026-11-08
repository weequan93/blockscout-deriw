# Evaluated through the existing backend RPC. All DB work is read-only and bounded.
# The result contains aggregate numbers only; no hashes, addresses, URLs or SQL.
sample = fn sql ->
  try do
    case Explorer.Repo.transaction(fn ->
           Explorer.Repo.query!("SET TRANSACTION READ ONLY", [], log: false)
           Explorer.Repo.query!("SET LOCAL statement_timeout = '1500ms'", [], log: false)
           Explorer.Repo.query!("SET LOCAL lock_timeout = '100ms'", [], log: false)
           [[[%{"Plan" => plan}]]] = Explorer.Repo.query!("EXPLAIN (FORMAT JSON) " <> sql, [], log: false, timeout: 2000).rows
           if plan["Total Cost"] > 20000, do: Explorer.Repo.rollback(:plan_budget)
           [[result]] = Explorer.Repo.query!(sql, [], log: false, timeout: 2000).rows
           result
         end, log: false, timeout: 3000, pool_timeout: 1000) do
      {:ok, data} -> %{ok: true, data: data}
      {:error, :plan_budget} -> %{ok: false, reason: "plan_budget"}
      _ -> %{ok: false, reason: "query_failed"}
    end
  rescue
    error in Postgrex.Error ->
      reason = case (error.postgres || %{})[:code] do
        :query_canceled -> "statement_timeout"
        :read_only_sql_transaction -> "read_only_denied"
        :lock_not_available -> "lock_timeout"
        _ -> "query_failed"
      end
      %{ok: false, reason: reason}
    _ -> %{ok: false, reason: "query_failed"}
  catch
    _, _ -> %{ok: false, reason: "query_failed"}
  end
end

tracker = fn module, task ->
  pid = Process.whereis(module)
  try do
    state = if is_pid(pid), do: :sys.get_state(pid, 500), else: %{}
    data = Map.get(state, :callback_module_state, %{}) || %{}
    %{ok: true, running: is_pid(pid), next_parent_block: get_in(data, [:task_data, task, :start_block])}
  rescue
    _ -> %{ok: false, running: is_pid(pid)}
  catch
    _, _ -> %{ok: false, running: is_pid(pid)}
  end
end

result = %{
  observed_at_unix: System.os_time(:second),
  sections: Map.new(bridge_metric_queries, fn {name, sql} -> {name, sample.(sql)} end),
  trackers: %{
    deposits: tracker.(Indexer.Fetcher.Arbitrum.TrackingMessagesOnL1, :check_new),
    settlement: tracker.(Indexer.Fetcher.Arbitrum.TrackingBatchesStatuses, :new_batches)
  }
}
IO.puts("BLOCKSCOUT_DEPLOY_CHECK=" <> Jason.encode!(result))
