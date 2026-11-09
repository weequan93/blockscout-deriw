defmodule Indexer.Prometheus.ArbitrumBridgeVerificationTest do
  use ExUnit.Case, async: false

  alias EthereumJSONRPC.Arbitrum.Constants.Events
  alias Indexer.Prometheus.ArbitrumBridgeMetrics.{Collector, RPC, Source, Verification}

  @fixture Path.join(__DIR__, "fixtures/arbitrum_bridge_metrics.json")
  @outbox "0x" <> String.duplicate("1", 40)
  @inbox "0x" <> String.duplicate("2", 40)
  @rollup "0x" <> String.duplicate("3", 40)
  @hash "0x" <> String.duplicate("a", 64)
  @block_hash "0x" <> String.duplicate("b", 64)

  test "detects successful on-chain relay missing from the index and incomplete deposits" do
    result = observe()
    assert :ok = Verification.validate!(result)
    assert result["deposits"]["data"]["incomplete_relayed"] == 5
    assert result["withdrawals"]["data"]["onchain_relayed"] == 1
    assert result["withdrawals"]["data"]["missing_relay"] == 1
    assert result["withdrawals"]["data"]["missing_completion"] == 1
    assert result["withdrawals"]["data"]["unexpected_relay"] == 0
  end

  test "separates parent safe, strict finalized and indexed UI finality" do
    result = observe()
    assert result["batches"]["data"]["posted"] == 1
    assert result["batches"]["data"]["safe"] == 1
    assert result["batches"]["data"]["finalized"] == 0
    assert result["batches"]["data"]["finality_mismatch"] == 0
    assert result["batch_head"]["data"] == %{"batch_number" => 20, "indexing_lag" => 0, "index_ahead" => 0}

    changed = observe(batch_status: "unfinalized")
    assert changed["batches"]["data"]["finality_mismatch"] == 1
    assert observe(finalized: 95)["batches"]["data"]["finalized"] == 1
    assert observe(batch_count: 24)["batch_head"]["data"]["indexing_lag"] == 3
    assert observe(batch_count: 20)["batch_head"]["data"]["index_ahead"] == 1
  end

  test "wrong publication event, failed transaction and canonical reorg are visible" do
    for opts <- [[receipt_status: "0x0"], [event_number: 19], [canonical_hash: @hash], [parent_block: 89]] do
      result = observe(opts)
      assert result["batches"]["ok"]
      assert result["batches"]["data"]["publication_mismatch"] == 1
    end
  end

  test "RPC failure and unsupported finality omit affected values without losing deposit metadata" do
    for opts <- [[fail_id: 100], [fail_id: 200], [fail_id: 300], [missing_receipt: true], [fail_id: 3]] do
      data = observe(opts)
      assert data["deposits"]["ok"]
      section = if opts[:fail_id] == 100, do: "withdrawals", else: "batches"
      assert data[section] == %{"ok" => false}
      samples = samples(data)

      expected =
        if section == "withdrawals",
          do: "deriw_bridge_withdrawal_sample_missing_relay",
          else: "deriw_rollup_batch_sample_posted"

      refute Enum.any?(samples, fn {name, _, _} -> name == expected end)
      assert value(samples, "deriw_bridge_verification_success", section: section) == 0
    end

    data = observe(fail_id: 1)
    assert data["withdrawals"] == %{"ok" => false}
    assert data["batches"] == %{"ok" => false}
    assert data["deposits"]["ok"]
  end

  test "sample boundaries, stale snapshots and separate withdrawal cursor are explicit" do
    result = observe(truncated: 1)
    samples = samples(result)
    assert value(samples, "deriw_bridge_withdrawal_sample_truncated") == 1
    assert value(samples, "deriw_rollup_batch_sample_truncated") == 1
    assert value(samples, "deriw_bridge_tracker_lag_blocks", tracker: "withdrawals") == 11
    assert value(samples, "deriw_bridge_tracker_lag_blocks", tracker: "settlement") == nil
    assert value(samples, "deriw_rollup_latest_batch_parent_finalized", tag: "safe") == 1
    assert value(samples, "deriw_rollup_latest_batch_parent_finalized", tag: "finalized") == 0

    data = snapshot(result)
    stale = Collector.metric_families(%{data: data, last_success: 1000, success: true}, 1151)
    assert length(stale) == 2
  end

  test "partial RPC replies and invalid booleans are unknown rather than unrelayed" do
    for opts <- [[omit_id: 100], [spent_value: "0x2"], [spent_value: "not-a-number"]] do
      assert observe(opts)["withdrawals"] == %{"ok" => false}
    end

    result = observe(spent_value: "0x0", withdrawal_status: "relayed", completion_hash: @hash)
    assert result["withdrawals"]["data"]["unexpected_relay"] == 1
    assert result["withdrawals"]["data"]["missing_relay"] == 0
  end

  test "disabled RPC configuration does not discard the database completeness check" do
    sample = fn sql -> candidate(sql, []) end
    rpc = fn _, _ -> flunk("Unconfigured RPC must not be called") end
    result = Verification.collect(sample: sample, rpc: rpc, url: nil, rollup: nil)
    assert result["deposits"]["ok"]
    assert result["parent_latest"] == %{"ok" => false}
    assert :ok = Verification.validate!(result)
  end

  test "verification validates shapes, sample bounds and privacy" do
    good = observe()
    assert :ok = Source.validate!(snapshot(good))

    for invalid <- [
          put_in(good, ["withdrawals", "data", "size"], 33),
          put_in(good, ["deposits", "data", "truncated"], 2),
          put_in(good, ["batches", "data", "size"], -1),
          put_in(good, ["parent_latest", "data", "private_url"], "private")
        ] do
      assert_raise ArgumentError, fn -> Verification.validate!(invalid) end
    end
  end

  test "HTTP RPC wrapper rejects malformed, duplicated, missing and excessive replies" do
    request = [%{jsonrpc: "2.0", id: 1, method: "eth_blockNumber", params: []}]

    valid = fn _, _, _, options ->
      assert options[:timeout] == 4000
      {:ok, %{status_code: 200, body: Jason.encode!([%{id: 1, result: "0x1"}])}}
    end

    assert RPC.batch("https://rpc.invalid", request, valid) == %{1 => {:ok, "0x1"}}

    for responses <- [
          [],
          [%{id: 2, result: "0x1"}],
          [%{id: 1, result: true, error: %{}}],
          [%{id: 1, result: true}, %{id: 1, result: false}]
        ] do
      transport = fn _, _, _, _ -> {:ok, %{status_code: 200, body: Jason.encode!(responses)}} end
      assert RPC.batch("https://rpc.invalid", request, transport) in [%{}, %{1 => :error}]
    end

    failure = fn _, _, _, _ -> raise "private RPC credential" end
    assert RPC.batch("https://private.invalid", request, failure) == %{}
    assert RPC.batch("https://rpc.invalid", List.duplicate(hd(request), 65), valid) == %{}
  end

  test "an unresponsive RPC has a polling deadline and produces unknown results" do
    request = [%{jsonrpc: "2.0", id: 1, method: "eth_blockNumber", params: []}]
    stalled = fn _, _, _, _ -> Process.sleep(60_000) end
    assert RPC.batch("https://rpc.invalid", request, stalled) == %{}
  end

  test "an empty index cannot invent a posted batch or hide the parent batch count" do
    result = observe(empty: true)
    assert result["withdrawals"]["data"]["size"] == 0
    assert result["batches"]["data"]["size"] == 0
    assert result["batch_head"]["data"]["indexing_lag"] == 21
    refute Enum.any?(samples(result), fn {name, _, _} -> name == "deriw_rollup_latest_batch_onchain_posted" end)
  end

  defp observe(opts \\ []) do
    rpc = fn _, requests ->
      assert length(requests) <= 64

      Enum.reject(requests, &(&1.id == opts[:omit_id]))
      |> Map.new(fn request ->
        reply = if request.id == opts[:fail_id], do: :error, else: {:ok, response(request, opts)}
        {request.id, reply}
      end)
    end

    Verification.collect(
      url: "https://fixture.invalid",
      rollup: @rollup,
      sample: fn sql -> candidate(sql, opts) end,
      rpc: rpc
    )
  end

  defp candidate(sql, opts) do
    queries = Verification.queries()

    data =
      cond do
        sql == queries["deposits"] ->
          %{
            "size" => 6,
            "truncated" => 0,
            "fully_linked" => 1,
            "incomplete_relayed" => 5,
            "missing_hash" => 5,
            "missing_block" => 5,
            "missing_timestamp" => 5,
            "missing_sender" => 5,
            "missing_completion" => 0
          }

        sql == queries["withdrawals"] ->
          %{
            "truncated" => opts[:truncated] || 0,
            "rows" => [
              %{
                "id" => 512,
                "status" => opts[:withdrawal_status] || "confirmed",
                "completion_hash" => opts[:completion_hash]
              }
            ]
          }

        sql == queries["batches"] ->
          %{
            "truncated" => opts[:truncated] || 0,
            "rows" => [
              %{
                "number" => 20,
                "hash" => @hash,
                "parent_block" => opts[:parent_block] || 90,
                "status" => opts[:batch_status] || "finalized"
              }
            ]
          }
      end

    data = if opts[:empty] == true and Map.has_key?(data, "rows"), do: Map.put(data, "rows", []), else: data

    %{"ok" => true, "data" => data}
  end

  defp response(%{id: id}, opts) when id in 1..3 do
    number = %{1 => 100, 2 => 95, 3 => opts[:finalized] || 80}[id]
    %{"number" => hex(number), "hash" => @block_hash}
  end

  defp response(%{id: 4}, _opts), do: "0x" <> String.duplicate("0", 24) <> String.slice(@outbox, 2..-1//1)
  defp response(%{id: 5}, _opts), do: "0x" <> String.duplicate("0", 24) <> String.slice(@inbox, 2..-1//1)
  defp response(%{id: 6}, opts), do: hex(opts[:batch_count] || 21)

  defp response(%{id: 100, params: [_, tag]}, opts) do
    assert tag == "0x64"
    opts[:spent_value] || "0x1"
  end

  defp response(%{id: 200}, opts) do
    unless opts[:missing_receipt] do
      %{
        "transactionHash" => @hash,
        "blockHash" => @block_hash,
        "blockNumber" => "0x5A",
        "status" => opts[:receipt_status] || "0x1",
        "logs" => [
          %{"address" => @inbox, "topics" => [Events.sequencer_batch_delivered(), hex(opts[:event_number] || 20)]}
        ]
      }
    end
  end

  defp response(%{id: 300}, opts), do: %{"hash" => opts[:canonical_hash] || @block_hash, "number" => "0x5A"}
  defp hex(n), do: "0x" <> Integer.to_string(n, 16)

  defp snapshot(verification) do
    @fixture
    |> File.read!()
    |> Jason.decode!()
    |> Map.put("verification", verification)
    |> put_in(["trackers", "withdrawals"], %{"ok" => true, "running" => true, "next_parent_block" => 90})
  end

  defp samples(verification) do
    Collector.metric_families(%{data: snapshot(verification), last_success: 1000, success: true}, 1000)
    |> Enum.flat_map(fn family ->
      for metric <- elem(family, 4) do
        labels = Enum.map(elem(metric, 1), fn pair -> {to_string(elem(pair, 1)), to_string(elem(pair, 2))} end)
        {to_string(elem(family, 1)), Enum.sort(labels), metric |> elem(2) |> elem(1)}
      end
    end)
  end

  defp value(samples, name, labels \\ []) do
    labels = labels |> Enum.map(fn {k, v} -> {to_string(k), v} end) |> Enum.sort()
    Enum.find_value(samples, fn {n, actual, v} -> if n == name and actual == labels, do: v end)
  end
end
