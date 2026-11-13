defmodule DeriW.DevBridgeRPCProbe do
  @moduledoc """
  A bounded, read-only probe of the running dev indexers' parent RPC configuration.
  At most 68 eth_getLogs requests, sequential, at most four per second, with an
  eight-second deadline per request and a two-minute overall budget. Stops on the
  first failed/inconsistent response. Does not alter configuration or scan cursors.
  This is a short capacity sample, not discovery of a provider's official quota.
  """

  alias EthereumJSONRPC.Arbitrum.Constants.Events

  def contexts do
    true = System.get_env("CHAIN_ID") == "18417507517"

    for {name, module, anchor, topic} <- [
          {:deposits, Indexer.Fetcher.Arbitrum.TrackingMessagesOnL1, 278_124_046, Events.message_delivered()},
          {:withdrawals, Indexer.Fetcher.Arbitrum.TrackingBatchesStatuses, 327_286_816,
           Events.outbox_transaction_executed()}
        ] do
      state = :sys.get_state(Process.whereis(module), 5_000).callback_module_state
      config = state.config
      true = String.downcase(config.l1_rollup_address) == "0xddf009b4879eaffa6a23782f9aa30f86cd2c64e6"

      %{
        name: name,
        anchor: anchor,
        topic: topic,
        address: if(name == :deposits, do: config.l1_bridge_address, else: config.l1_outbox_address),
        args:
          if(name == :deposits, do: config.json_l1_rpc_named_arguments, else: config.l1_rpc.json_rpc_named_arguments)
      }
    end
  end

  def run(contexts, rpc \\ &request/3, pause \\ &Process.sleep/1) do
    started = System.monotonic_time(:millisecond)

    try do
      for context <- contexts do
        baseline = sample(context, context.anchor, 1, rpc, pause, 250, started)
        if MapSet.size(baseline) == 0, do: fail(context, "known_event_missing")

        # Compare an identical historical range against ten narrower requests.
        # Successful empty responses alone cannot demonstrate complete log reads.
        first = context.anchor - 5_000
        wide = sample(context, first, 10_000, rpc, pause, 250, started)

        narrow =
          Enum.reduce(0..9, MapSet.new(), fn index, acc ->
            MapSet.union(acc, sample(context, first + index * 1_000, 1_000, rpc, pause, 250, started))
          end)

        unless wide == narrow and MapSet.subset?(baseline, wide),
          do: fail(context, "wide_and_narrow_logs_disagree")

        for width <- [50, 1_000], _attempt <- 1..3 do
          logs = sample(context, context.anchor, width, rpc, pause, 250, started)
          unless MapSet.subset?(baseline, logs), do: fail(context, "known_event_missing")
        end

        emit(%{phase: "completeness", tracker: context.name, tested_blocks: 10_000, passed: true})
      end

      # Alternate both contracts. Pacing includes request time; slow calls reduce
      # the achieved rate rather than generating overlapping requests or bursts.
      for rate <- [2, 4] do
        phase_started = System.monotonic_time(:millisecond)

        for index <- 0..15 do
          context = Enum.at(contexts, rem(index, length(contexts)))
          sample(context, context.anchor - 5_000, 10_000, rpc, pause, div(1_000, rate), started)
        end

        emit(%{
          phase: "paced_calls",
          target_requests_per_second: rate,
          requests: 16,
          elapsed_ms: System.monotonic_time(:millisecond) - phase_started,
          errors: 0
        })
      end

      emit(%{phase: "finished", passed: true, requests: 68, elapsed_ms: System.monotonic_time(:millisecond) - started})
      :ok
    catch
      {:probe_failed, result} ->
        emit(Map.merge(%{phase: "stopped", passed: false}, result))
        :error
    end
  end

  defp sample(context, first, width, rpc, pause, interval, started) do
    if System.monotonic_time(:millisecond) - started >= 120_000, do: fail(context, "overall_deadline")
    before = System.monotonic_time(:millisecond)

    logs =
      case rpc.(context, first, first + width - 1) do
        {:ok, logs} when is_list(logs) -> logs
        _ -> fail(context, "rpc_error_or_timeout")
      end

    normalized =
      try do
        for log <- logs do
          block = EthereumJSONRPC.quantity_to_integer(log["blockNumber"])
          true = block >= first and block < first + width
          true = String.downcase(log["address"]) == String.downcase(context.address)
          true = hd(log["topics"]) == context.topic
          true = log["removed"] in [nil, false]
          true = Enum.all?(["blockHash", "transactionHash", "logIndex", "data"], &is_binary(log[&1]))
          Map.take(log, ["blockHash", "blockNumber", "transactionHash", "logIndex", "topics", "data"])
        end
      rescue
        _ -> fail(context, "malformed_or_unfiltered_logs")
      end

    result = MapSet.new(normalized)
    if MapSet.size(result) != length(logs), do: fail(context, "duplicate_logs")
    elapsed = System.monotonic_time(:millisecond) - before
    emit(%{phase: "request", tracker: context.name, blocks: width, elapsed_ms: elapsed, log_count: length(logs)})
    pause.(max(interval - elapsed, 0))
    result
  end

  defp request(context, first, last) do
    task =
      Task.async(fn ->
        try do
          EthereumJSONRPC.json_rpc(
            EthereumJSONRPC.request(%{
              id: 1,
              method: "eth_getLogs",
              params: [
                %{
                  address: context.address,
                  topics: [context.topic],
                  fromBlock: EthereumJSONRPC.integer_to_quantity(first),
                  toBlock: EthereumJSONRPC.integer_to_quantity(last)
                }
              ]
            }),
            context.args
          )
        rescue
          _ -> {:error, :request_failed}
        catch
          _, _ -> {:error, :request_failed}
        end
      end)

    case Task.yield(task, 8_000) || Task.shutdown(task, :brutal_kill) do
      {:ok, result} -> result
      _ -> {:error, :request_timeout}
    end
  end

  defp fail(context, reason), do: throw({:probe_failed, %{tracker: context.name, reason: reason}})
  defp emit(result), do: IO.puts(Jason.encode!(result))
end

DeriW.DevBridgeRPCProbe.run(DeriW.DevBridgeRPCProbe.contexts())
