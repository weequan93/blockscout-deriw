defmodule Indexer.Prometheus.ArbitrumBridgeMetrics.Source do
  @moduledoc """
  Read-only bridge observations with bounded samples, query plans and deadlines.
  """

  alias Explorer.Repo

  @queries_file Path.expand("../../../../priv/arbitrum_bridge_metrics_queries.json", __DIR__)
  @external_resource @queries_file
  @queries @queries_file |> File.read!() |> Jason.decode!()
  @sections ~w(deposits withdrawals rollup published confirmed)
  @states ~w(initiated sent confirmed relayed)
  @rollup_states [
    {"processed", "not_applicable"},
    {"sealed", "not_applicable"},
    {"sent", "unfinalized"},
    {"sent", "finalized"},
    {"confirmed", "unfinalized"},
    {"confirmed", "finalized"}
  ]

  @doc false
  def queries, do: @queries

  def collect do
    %{
      "observed_at_unix" => System.system_time(:second),
      "sections" => Map.new(@sections, &{&1, sample(Map.fetch!(@queries, &1))}),
      "trackers" => %{
        "deposits" => tracker(Indexer.Fetcher.Arbitrum.TrackingMessagesOnL1, :check_new),
        "settlement" => tracker(Indexer.Fetcher.Arbitrum.TrackingBatchesStatuses, :new_batches)
      }
    }
  end

  @doc false
  def validate!(data) do
    valid? =
      number?(data["observed_at_unix"]) and
        Enum.sort(Map.keys(data["sections"])) == Enum.sort(@sections) and
        Enum.all?(data["sections"], fn {name, section} -> valid_section?(name, section) end) and
        Enum.sort(Map.keys(data["trackers"])) == ~w(deposits settlement) and
        Enum.all?(data["trackers"], fn {_, tracker} ->
          is_boolean(tracker["ok"]) and
            (not tracker["ok"] or
               (is_boolean(tracker["running"]) and optional_number?(tracker["next_parent_block"])))
        end)

    unless valid?, do: raise(ArgumentError, "Invalid bridge metrics snapshot")
    :ok
  end

  defp valid_section?(_name, %{"ok" => false}), do: true

  defp valid_section?(name, %{"ok" => true, "data" => row}) when name in ~w(deposits withdrawals) do
    is_boolean(row["complete"]) and number?(row["size"]) and row["size"] <= 10_000 and
      Enum.all?(row["states"], fn {state, count} -> state in @states and number?(count) end) and
      Enum.sum(Map.values(row["states"])) == row["size"] and
      Enum.all?(
        ~w(highest_message_id highest_relayed_message_id oldest_pending_timestamp),
        &optional_number?(row[&1])
      ) and
      Enum.all?(
        ~w(pending_without_timestamp missing_origination completion_recorded),
        &(number?(row[&1]) and row[&1] <= row["size"])
      )
  end

  defp valid_section?("rollup", %{"ok" => true, "data" => row}) do
    pairs = Enum.map(row["states"], &{&1["stage"], &1["parent_finality"]})

    number?(row["size"]) and row["size"] <= 1_000 and
      length(Enum.uniq(pairs)) == length(pairs) and Enum.all?(pairs, &(&1 in @rollup_states)) and
      Enum.all?(row["states"], &(number?(&1["count"]) and number?(&1["highest_block"]))) and
      Enum.sum(Enum.map(row["states"], & &1["count"])) == row["size"] and
      Enum.all?(~w(lowest_block highest_block latest_block_timestamp), &optional_number?(row[&1]))
  end

  defp valid_section?(name, %{"ok" => true, "data" => row}) when name in ~w(published confirmed) do
    row == %{} or
      (number?(row["block_number"]) and number?(row["parent_timestamp"]) and
         row["parent_finality"] in ~w(unfinalized finalized) and
         (name != "published" or number?(row["batch_number"])))
  end

  defp valid_section?(_, _), do: false
  defp number?(value), do: is_number(value) and value >= 0 and value < 9_223_372_036_854_775_808
  defp optional_number?(value), do: is_nil(value) or number?(value)

  @doc false
  def sample(sql, repo \\ Repo) do
    case repo.transaction(
           fn ->
             repo.query!("SET TRANSACTION READ ONLY", [], log: false)
             repo.query!("SET LOCAL statement_timeout = '1500ms'", [], log: false)
             repo.query!("SET LOCAL lock_timeout = '100ms'", [], log: false)

             [[[%{"Plan" => plan}]]] =
               repo.query!("EXPLAIN (FORMAT JSON) " <> sql, [], log: false, timeout: 2_000).rows

             if plan["Total Cost"] > 20_000, do: repo.rollback(:plan_budget)
             [[result]] = repo.query!(sql, [], log: false, timeout: 2_000).rows
             result
           end,
           log: false,
           timeout: 3_000,
           pool_timeout: 1_000
         ) do
      {:ok, data} -> %{"ok" => true, "data" => data}
      {:error, :plan_budget} -> failure("plan_budget")
      _ -> failure("query_failed")
    end
  rescue
    error in Postgrex.Error ->
      reason =
        case (error.postgres || %{})[:code] do
          :query_canceled -> "statement_timeout"
          :read_only_sql_transaction -> "read_only_denied"
          :lock_not_available -> "lock_timeout"
          _ -> "query_failed"
        end

      failure(reason)

    _ ->
      failure("query_failed")
  catch
    _, _ -> failure("query_failed")
  end

  defp failure(reason), do: %{"ok" => false, "reason" => reason}

  defp tracker(module, task) do
    pid = Process.whereis(module)
    state = if is_pid(pid), do: :sys.get_state(pid, 500), else: %{}
    data = Map.get(state, :callback_module_state, %{}) || %{}

    %{
      "ok" => true,
      "running" => is_pid(pid),
      "next_parent_block" => get_in(data, [:task_data, task, :start_block])
    }
  rescue
    _ -> %{"ok" => false, "running" => is_pid(Process.whereis(module))}
  catch
    _, _ -> %{"ok" => false, "running" => is_pid(Process.whereis(module))}
  end
end
