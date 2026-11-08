defmodule Indexer.Prometheus.ArbitrumBridgeMetricsTest do
  use ExUnit.Case, async: false

  alias Indexer.Prometheus.ArbitrumBridgeMetrics
  alias Indexer.Prometheus.ArbitrumBridgeMetrics.{Collector, Source}

  @fixture Path.join(__DIR__, "fixtures/arbitrum_bridge_metrics.json")

  setup do
    settings = [
      {:indexer, ArbitrumBridgeMetrics, [enabled: true]},
      {:indexer, Indexer.Prometheus.Metrics, [enabled: false]},
      {:explorer, :chain_type, :arbitrum},
      {:nft_media_handler, :standalone_media_worker?, false}
    ]

    previous =
      Enum.map(settings, fn {app, key, value} ->
        old = Application.fetch_env(app, key)
        Application.put_env(app, key, value)
        {app, key, old}
      end)

    on_exit(fn ->
      Prometheus.Registry.deregister_collector(Collector)

      Enum.each(previous, fn
        {app, key, {:ok, value}} -> Application.put_env(app, key, value)
        {app, key, :error} -> Application.delete_env(app, key)
      end)
    end)

    :ok
  end

  test "preserves bridge states, message coverage and separate parent finality" do
    data = fixture()
    assert :ok = Source.validate!(data)
    samples = samples(data)

    assert value(samples, "deriw_bridge_sample_messages", direction: "l2_to_l3", status: "initiated") == 2
    assert value(samples, "deriw_bridge_sample_messages", direction: "l3_to_l2", status: "confirmed") == 0
    assert value(samples, "deriw_bridge_sample_missing_origination", direction: "l2_to_l3") == 1
    assert value(samples, "deriw_bridge_sample_oldest_pending_age_seconds", direction: "l2_to_l3") == 100
    assert value(samples, "deriw_rollup_stage_block_number", stage: "processed") == 102
    assert value(samples, "deriw_rollup_stage_parent_transaction_finalized", stage: "sent") == 0
    assert value(samples, "deriw_rollup_stage_parent_transaction_finalized", stage: "confirmed") == 1
    assert length(samples) == length(Enum.uniq_by(samples, fn {name, labels, _} -> {name, labels} end))
  end

  test "expired, failed or future-dated observations expose only health" do
    for {success, now} <- [{true, 1151}, {false, 1001}, {true, 999}] do
      samples = samples(fixture(), success, now)
      assert value(samples, "deriw_bridge_exporter_up") == 0
      assert length(samples) == 2
    end
  end

  test "failed query sections omit their series without dropping successful sections" do
    data =
      fixture()
      |> put_in(["sections", "deposits"], %{"ok" => false})
      |> put_in(["sections", "confirmed"], %{"ok" => false})

    samples = samples(data)

    assert value(samples, "deriw_bridge_query_success", section: "deposits") == 0
    assert value(samples, "deriw_bridge_query_success", section: "confirmed") == 0
    assert value(samples, "deriw_bridge_sample_size", direction: "l2_to_l3") == nil
    assert value(samples, "deriw_bridge_sample_size", direction: "l3_to_l2") == 3
    assert value(samples, "deriw_rollup_stage_block_number", stage: "confirmed") == nil
    assert value(samples, "deriw_rollup_stage_observed", stage: "confirmed") == nil
  end

  test "an absent watermark is distinct from a failed query, and reorg regressions are visible" do
    empty = put_in(fixture(), ["sections", "confirmed", "data"], %{}) |> samples()
    assert value(empty, "deriw_rollup_stage_observed", stage: "confirmed") == 0
    assert value(empty, "deriw_rollup_stage_block_number", stage: "confirmed") == nil

    regressed = put_in(fixture(), ["sections", "confirmed", "data", "block_number"], 95) |> samples()
    assert value(regressed, "deriw_rollup_stage_block_number", stage: "confirmed") == 95
  end

  test "truncated samples and inactive trackers are explicit" do
    data = put_in(fixture(), ["sections", "withdrawals", "data", "complete"], false)
    samples = samples(data)
    assert value(samples, "deriw_bridge_sample_complete", direction: "l3_to_l2") == 0
    assert value(samples, "deriw_bridge_tracker_running", tracker: "settlement") == 0
    assert value(samples, "deriw_bridge_tracker_next_parent_block", tracker: "settlement") == nil
  end

  test "invalid states, duplicate stages and unbounded samples are rejected" do
    invalid = [
      put_in(fixture(), ["sections", "deposits", "data", "states", "private-label"], 1),
      put_in(fixture(), ["sections", "deposits", "data", "size"], 10001),
      update_in(fixture(), ["sections", "rollup", "data", "states"], fn [first | _] = states -> [first | states] end),
      put_in(fixture(), ["sections", "confirmed", "data", "parent_finality"], "unexpected")
    ]

    for data <- invalid do
      assert_raise ArgumentError, fn -> Source.validate!(data) end
    end
  end

  test "failed polls remove previous values and suppress exception details" do
    data = Map.put(fixture(), "observed_at_unix", System.system_time(:second))
    {:ok, agent} = start_supervised({Agent, fn -> {:ok, data} end})

    sampler = fn ->
      case Agent.get(agent, & &1) do
        {:ok, value} -> value
        :fail -> raise "private connection URL"
      end
    end

    worker = start_supervised!({ArbitrumBridgeMetrics, sampler: sampler})
    :sys.get_state(worker)
    assert ArbitrumBridgeMetrics.snapshot().success

    Agent.update(agent, fn _ -> :fail end)
    send(worker, :poll)
    :sys.get_state(worker)
    output = Prometheus.Format.Text.format()
    assert output =~ "deriw_bridge_exporter_up 0"
    refute output =~ "deriw_bridge_sample_messages"
    refute output =~ "private connection URL"
  end

  test "disabled, other-chain and standalone NFT workers do not collect" do
    sampler = fn -> flunk("disabled collector must not query") end
    Application.put_env(:indexer, ArbitrumBridgeMetrics, enabled: false)
    assert :ignore = ArbitrumBridgeMetrics.start_link(sampler: sampler)

    Application.put_env(:indexer, ArbitrumBridgeMetrics, enabled: true)
    Application.put_env(:explorer, :chain_type, :ethereum)
    assert :ignore = ArbitrumBridgeMetrics.start_link(sampler: sampler)

    Application.put_env(:explorer, :chain_type, :arbitrum)
    Application.put_env(:nft_media_handler, :standalone_media_worker?, true)
    assert :ignore = ArbitrumBridgeMetrics.start_link(sampler: sampler)
    assert ArbitrumBridgeMetrics.snapshot() == nil
  end

  defp fixture, do: @fixture |> File.read!() |> Jason.decode!()

  defp samples(data, success \\ true, now \\ 1000) do
    Collector.metric_families(%{data: data, last_success: 1000, success: success}, now)
    |> Enum.flat_map(fn family ->
      name = family |> elem(1) |> to_string()

      for metric <- elem(family, 4) do
        labels = for pair <- elem(metric, 1), do: {to_string(elem(pair, 1)), to_string(elem(pair, 2))}
        {name, Enum.sort(labels), metric |> elem(2) |> elem(1)}
      end
    end)
  end

  defp value(samples, name, labels \\ []) do
    normalized = labels |> Enum.map(fn {key, value} -> {to_string(key), value} end) |> Enum.sort()
    Enum.find_value(samples, fn {metric, actual, value} -> if metric == name and actual == normalized, do: value end)
  end
end
