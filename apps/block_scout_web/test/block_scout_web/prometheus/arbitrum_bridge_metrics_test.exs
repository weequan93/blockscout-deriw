defmodule BlockScoutWeb.Prometheus.ArbitrumBridgeMetricsTest do
  use ExUnit.Case, async: false

  alias BlockScoutWeb.Prometheus.Exporter
  alias Indexer.Prometheus.ArbitrumBridgeMetrics
  alias Indexer.Prometheus.ArbitrumBridgeMetrics.Collector

  @fixture Path.expand("../../../../indexer/test/indexer/prometheus/fixtures/arbitrum_bridge_metrics.json", __DIR__)

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

  test "bridge metrics share /metrics and repeated scrapes never poll the sampler" do
    owner = self()
    data = Map.put(fixture(), "observed_at_unix", System.system_time(:second))

    worker =
      start_supervised!(
        {ArbitrumBridgeMetrics,
         sampler: fn ->
           send(owner, :sampled)
           data
         end}
      )

    :sys.get_state(worker)
    assert_receive :sampled
    assert Application.get_env(:indexer, Indexer.Prometheus.Metrics)[:enabled] == false

    Prometheus.Metric.Gauge.declare(name: :native_bridge_existing_metric_fixture, help: "Existing metric fixture")
    Prometheus.Metric.Gauge.set([name: :native_bridge_existing_metric_fixture], 7)
    Exporter.setup()

    for _ <- 1..3 do
      conn = Plug.Test.conn("GET", "/metrics") |> Exporter.call(Exporter.init([]))
      assert conn.status == 200
      assert conn.resp_body =~ "native_bridge_existing_metric_fixture 7"
      assert conn.resp_body =~ "deriw_bridge_exporter_up 1"
      assert conn.resp_body =~ "deriw_rollup_stage_block_number"
    end

    refute_receive :sampled, 20
    refute Prometheus.Registry.collector_registered?(:public, Collector)
  end

  defp fixture, do: @fixture |> File.read!() |> Jason.decode!()
end
