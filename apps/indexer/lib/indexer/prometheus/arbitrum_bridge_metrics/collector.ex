defmodule Indexer.Prometheus.ArbitrumBridgeMetrics.Collector do
  @moduledoc """
  Exposes cached bridge metrics through the existing default Prometheus registry.

  Metric names and labels match the legacy DeriW exporter. Failed sections omit
  their data; stale or failed observations expose only collection health.
  """

  use Prometheus.Collector

  alias Indexer.Prometheus.ArbitrumBridgeMetrics
  alias Prometheus.Model

  @max_age 150
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

  @impl :prometheus_collector
  def collect_mf(_registry, callback) do
    if ArbitrumBridgeMetrics.enabled?() do
      ArbitrumBridgeMetrics.snapshot()
      |> metric_families(System.system_time(:second))
      |> Enum.each(callback)
    end

    :ok
  end

  @impl :prometheus_collector
  def collect_metrics(_name, samples), do: Model.gauge_metrics(samples)

  @doc false
  def metric_families(snapshot, now) do
    snapshot = snapshot || %{data: nil, last_success: 0, success: false}
    data = snapshot.data

    fresh? =
      snapshot.success and is_map(data) and
        now - data["observed_at_unix"] >= 0 and now - data["observed_at_unix"] <= @max_age

    health = [
      metric(:deriw_bridge_exporter_up, "1 when the latest polling attempt succeeded and is fresh.", fresh?),
      metric(
        :deriw_bridge_exporter_last_success_timestamp_seconds,
        "Unix time of the last successful polling attempt.",
        snapshot.last_success
      )
    ]

    (health ++ if(fresh?, do: data_metrics(data), else: []))
    |> Enum.group_by(fn {name, _help, _sample} -> name end)
    |> Enum.sort_by(&elem(&1, 0))
    |> Enum.map(fn {name, [{_, help, _} | _] = series} ->
      Model.create_mf(name, help, :gauge, __MODULE__, Enum.map(series, &elem(&1, 2)))
    end)
  end

  defp data_metrics(data) do
    sections = data["sections"]

    [
      metric(:deriw_bridge_observation_timestamp_seconds, "Backend snapshot Unix timestamp.", data["observed_at_unix"])
    ] ++
      Enum.map(@sections, fn name ->
        metric(
          :deriw_bridge_query_success,
          "1 when this bounded read succeeded in the latest snapshot.",
          sections[name]["ok"],
          section: name
        )
      end) ++
      message_metrics(sections["deposits"], "l2_to_l3", data["observed_at_unix"]) ++
      message_metrics(sections["withdrawals"], "l3_to_l2", data["observed_at_unix"]) ++
      rollup_metrics(sections["rollup"]) ++
      stage_metrics(sections["published"], "sent") ++
      stage_metrics(sections["confirmed"], "confirmed") ++
      tracker_metrics(data["trackers"])
  end

  defp message_metrics(%{"ok" => false}, _direction, _now), do: []

  defp message_metrics(%{"ok" => true, "data" => row}, direction, now) do
    labels = [direction: direction]

    [
      metric(
        :deriw_bridge_sample_complete,
        "1 when the latest-10000-message sample covers every indexed message in this direction.",
        row["complete"],
        labels
      ),
      metric(
        :deriw_bridge_sample_size,
        "Number of indexed messages sampled, at most 10000 per direction.",
        row["size"],
        labels
      )
    ] ++
      Enum.map(@states, fn status ->
        metric(
          :deriw_bridge_sample_messages,
          "Current message state counts within the latest-10000 indexed messages; gauges, not lifetime counters.",
          Map.get(row["states"], status, 0),
          labels ++ [status: status]
        )
      end) ++
      optional_metrics(
        row,
        labels,
        [
          {:deriw_bridge_sample_highest_message_id, "highest_message_id"},
          {:deriw_bridge_sample_highest_relayed_message_id, "highest_relayed_message_id"}
        ],
        "Highest observed ID within the sampled indexed messages; does not imply gap-free progress."
      ) ++
      Enum.map(
        [
          {:deriw_bridge_sample_pending_without_timestamp, "pending_without_timestamp"},
          {:deriw_bridge_sample_missing_origination, "missing_origination"},
          {:deriw_bridge_sample_completion_recorded, "completion_recorded"}
        ],
        fn {name, key} ->
          metric(name, "Number of sampled messages with #{String.replace(key, "_", " ")}.", row[key], labels)
        end
      ) ++
      if is_nil(row["oldest_pending_timestamp"]) do
        []
      else
        [
          metric(
            :deriw_bridge_sample_oldest_pending_age_seconds,
            "Age of the oldest non-relayed sampled message with a known origination time.",
            max(0, now - row["oldest_pending_timestamp"]),
            labels
          )
        ]
      end
  end

  defp rollup_metrics(%{"ok" => false}), do: []

  defp rollup_metrics(%{"ok" => true, "data" => row}) do
    [
      metric(
        :deriw_rollup_sample_size,
        "Number of latest indexed consensus blocks classified, at most 1000.",
        row["size"]
      )
    ] ++
      Enum.flat_map(
        [
          {:deriw_rollup_sample_lowest_block, "lowest_block"},
          {:deriw_rollup_sample_highest_block, "highest_block"},
          {:deriw_rollup_sample_latest_block_timestamp, "latest_block_timestamp"}
        ],
        fn {name, key} ->
          optional_metrics(
            row,
            [],
            [{name, key}],
            "Latest-1000 indexed consensus block sample #{String.replace(key, "_", " ")}."
          )
        end
      ) ++
      Enum.map(@rollup_states, fn {stage, finality} ->
        state = Enum.find(row["states"], &(&1["stage"] == stage and &1["parent_finality"] == finality))

        metric(
          :deriw_rollup_sample_blocks,
          "Exclusive settlement stages within the latest-1000 indexed blocks; sent/confirmed match the explorer UI.",
          if(state, do: state["count"], else: 0),
          stage: stage,
          parent_finality: finality
        )
      end) ++
      optional_metrics(row, [stage: "processed"], [{:deriw_rollup_stage_block_number, "highest_block"}], stage_help())
  end

  defp stage_metrics(%{"ok" => false}, _stage), do: []

  defp stage_metrics(%{"ok" => true, "data" => row}, stage) do
    present? = map_size(row) > 0
    labels = [stage: stage]

    [
      metric(
        :deriw_rollup_stage_observed,
        "1 when this settlement-stage watermark was found in the index.",
        present?,
        labels
      )
    ] ++
      if present? do
        [
          metric(:deriw_rollup_stage_block_number, stage_help(), row["block_number"], labels),
          metric(
            :deriw_rollup_stage_parent_transaction_finalized,
            "Whether the transaction supplying this watermark is finalized on the parent; separate from rollup confirmation.",
            row["parent_finality"] == "finalized",
            labels
          ),
          metric(
            :deriw_rollup_stage_parent_timestamp_seconds,
            "Timestamp of the indexed parent-chain transaction for this stage.",
            row["parent_timestamp"],
            labels
          )
        ] ++
          if stage == "sent" do
            [
              metric(
                :deriw_rollup_latest_published_batch_number,
                "Latest batch number indexed from the parent chain; not batch poster health.",
                row["batch_number"]
              )
            ]
          else
            []
          end
      else
        []
      end
  end

  defp tracker_metrics(trackers) do
    Enum.flat_map(~w(deposits settlement), fn name ->
      row = trackers[name]
      labels = [tracker: name]

      [
        metric(
          :deriw_bridge_tracker_observation_success,
          "1 when tracker process state could be read.",
          row["ok"],
          labels
        )
      ] ++
        if row["ok"] do
          [
            metric(
              :deriw_bridge_tracker_running,
              "1 when the tracker process exists; does not prove its cursor advances.",
              row["running"],
              labels
            )
          ] ++
            optional_metrics(
              row,
              labels,
              [{:deriw_bridge_tracker_next_parent_block, "next_parent_block"}],
              "Next parent L2 block requested by the live discovery task; historical/backfill work is separate."
            )
        else
          []
        end
    end)
  end

  defp stage_help do
    "L3 block number recorded for this cumulative stage: indexed head, published batch end, or confirmed block; not a contiguous coverage proof."
  end

  defp optional_metrics(row, labels, fields, help) do
    for {name, key} <- fields, not is_nil(row[key]), do: metric(name, help, row[key], labels)
  end

  defp metric(name, help, value, labels \\ [])
  defp metric(name, help, true, labels), do: metric(name, help, 1, labels)
  defp metric(name, help, false, labels), do: metric(name, help, 0, labels)

  defp metric(name, help, value, labels) when is_number(value) and value >= 0 and value < 9_223_372_036_854_775_808 do
    {name, help, {labels, value}}
  end
end
