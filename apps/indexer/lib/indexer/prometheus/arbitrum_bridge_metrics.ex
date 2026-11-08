defmodule Indexer.Prometheus.ArbitrumBridgeMetrics do
  @moduledoc """
  Periodically collects bounded DeriW bridge and settlement metrics for /metrics.

  Collection is independent of Indexer.Prometheus.Metrics. Scrapes only read an
  atomic ETS snapshot; they never execute bridge queries or wait for this worker.
  """

  use GenServer

  alias Indexer.Prometheus.ArbitrumBridgeMetrics.{Collector, Source}

  @interval :timer.minutes(1)

  def start_link(opts \\ []) do
    GenServer.start_link(__MODULE__, opts, name: __MODULE__)
  end

  @doc "Whether native bridge collection is enabled on this backend."
  def enabled? do
    Application.get_env(:indexer, __MODULE__, [])[:enabled] == true and
      Application.get_env(:explorer, :chain_type) == :arbitrum and
      not Application.get_env(:nft_media_handler, :standalone_media_worker?, false)
  end

  @doc false
  def snapshot do
    case :ets.lookup(__MODULE__, :snapshot) do
      [{:snapshot, snapshot}] -> snapshot
      [] -> nil
    end
  rescue
    ArgumentError -> nil
  end

  @impl GenServer
  def init(opts) do
    if enabled?() do
      :ets.new(__MODULE__, [:named_table, :protected, :set, read_concurrency: true])
      :ets.insert(__MODULE__, {:snapshot, %{data: nil, last_success: 0, success: false}})
      Prometheus.Registry.register_collector(Collector)
      send(self(), :poll)

      {:ok,
       %{sampler: Keyword.get(opts, :sampler, &Source.collect/0), interval: Keyword.get(opts, :interval, @interval)}}
    else
      :ignore
    end
  end

  @impl GenServer
  def handle_info(:poll, state) do
    previous = snapshot()

    observed =
      try do
        data = state.sampler.()
        Source.validate!(data)
        now = System.system_time(:second)
        result = %{data: data, last_success: now, success: true}
        # Reject malformed values before publishing them to the shared endpoint.
        Collector.metric_families(result, now)
        result
      rescue
        _ -> %{data: nil, last_success: previous.last_success, success: false}
      catch
        _, _ -> %{data: nil, last_success: previous.last_success, success: false}
      end

    :ets.insert(__MODULE__, {:snapshot, observed})
    Process.send_after(self(), :poll, state.interval)
    {:noreply, state}
  end
end
