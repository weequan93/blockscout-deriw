defmodule Indexer.Prometheus.ArbitrumBridgeMetrics.Verification do
  @moduledoc """
  Bounded read-only comparisons between indexed bridge records and their parent chain.

  Deposit metadata covers the latest 10000 indexed deposits. RPC comparisons cover
  the latest 32 indexed withdrawals and 16 indexed batches, with explicit truncation.
  Failed or unsupported checks expose health without publishing reassuring zeros.
  """

  alias EthereumJSONRPC.Arbitrum.Constants.{Contracts, Events}
  alias Indexer.Prometheus.ArbitrumBridgeMetrics.{RPC, Source}

  @queries_file Path.expand("../../../../priv/arbitrum_bridge_verification_queries.json", __DIR__)
  @external_resource @queries_file
  @queries @queries_file |> File.read!() |> Jason.decode!()
  @batch_count "0x" <> Base.encode16(binary_part(ExKeccak.hash_256("batchCount()"), 0, 4), case: :lower)
  @shapes %{
    "deposits" =>
      ~w(size truncated fully_linked incomplete_relayed missing_hash missing_block missing_timestamp missing_sender missing_completion),
    "parent_latest" => ~w(block_number),
    "parent_safe" => ~w(block_number),
    "parent_finalized" => ~w(block_number),
    "withdrawals" => ~w(size truncated onchain_relayed missing_relay unexpected_relay missing_completion),
    "batches" =>
      ~w(size truncated posted safe finalized publication_mismatch finality_mismatch latest_posted latest_safe latest_finalized latest_number),
    "batch_head" => ~w(batch_number indexing_lag index_ahead)
  }

  def queries, do: @queries

  def collect(opts \\ []) do
    sample = Keyword.get(opts, :sample, &Source.sample/1)
    rpc = Keyword.get(opts, :rpc, &RPC.batch/2)
    config = Application.get_env(:indexer, Indexer.Fetcher.Arbitrum, [])
    url = Keyword.get(opts, :url, config[:l1_rpc])
    rollup = Keyword.get(opts, :rollup, config[:l1_rollup_address])
    selected = Map.new(@queries, fn {name, sql} -> {name, sample.(sql)} end)
    result = Map.new(@shapes, fn {name, _} -> {name, failure()} end)
    result = Map.put(result, "deposits", selected["deposits"])

    if is_binary(url) and url != "" and address?(rollup) do
      observe(result, selected, url, rollup, rpc)
    else
      result
    end
  rescue
    _ -> Map.new(@shapes, fn {name, _} -> {name, failure()} end)
  catch
    _, _ -> Map.new(@shapes, fn {name, _} -> {name, failure()} end)
  end

  def validate!(verification) do
    true = Enum.sort(Map.keys(verification)) == Enum.sort(Map.keys(@shapes))

    Enum.each(verification, fn
      {_name, %{"ok" => false}} ->
        :ok

      {name, %{"ok" => true, "data" => data}} ->
        true = Enum.sort(Map.keys(data)) == Enum.sort(@shapes[name])

        true =
          Enum.all?(data, fn {_, value} -> is_integer(value) and value >= 0 and value < 9_223_372_036_854_775_808 end)

        if Map.has_key?(data, "truncated"), do: true = data["truncated"] in [0, 1]

        limit = %{"deposits" => 10_000, "withdrawals" => 32, "batches" => 16}[name]
        if limit, do: true = data["size"] <= limit

        if limit do
          exempt = ~w(size truncated latest_number latest_posted latest_safe latest_finalized)
          true = Enum.all?(data, fn {key, n} -> key in exempt or n <= data["size"] end)

          for key <- ~w(latest_posted latest_safe latest_finalized),
              Map.has_key?(data, key),
              do: true = data[key] in [0, 1]
        end

      _ ->
        raise ArgumentError, "Invalid bridge verification snapshot"
    end)

    :ok
  rescue
    _ -> raise ArgumentError, "Invalid bridge verification snapshot"
  end

  defp observe(result, selected, url, rollup, rpc) do
    first =
      rpc.(url, [
        request(1, "eth_getBlockByNumber", ["latest", false]),
        request(2, "eth_getBlockByNumber", ["safe", false]),
        request(3, "eth_getBlockByNumber", ["finalized", false]),
        call(4, rollup, "0x" <> Contracts.outbox_selector(), "latest"),
        call(5, rollup, "0x" <> Contracts.sequencer_inbox_selector(), "latest")
      ])

    heads = Map.new(Enum.with_index(~w(latest safe finalized), 1), fn {tag, id} -> {tag, head(first[id])} end)

    result =
      Enum.reduce(heads, result, fn {tag, value}, acc ->
        Map.put(acc, "parent_" <> tag, if(is_integer(value), do: success(%{"block_number" => value}), else: failure()))
      end)

    with latest when is_integer(latest) <- heads["latest"] do
      tag = quantity(latest)
      outbox = contract(first[4])
      inbox = contract(first[5])
      withdrawals = rows(selected["withdrawals"], 32)
      batches = rows(selected["batches"], 16)

      withdrawals_requests =
        if address?(outbox) do
          Enum.with_index(withdrawals || [], 100)
          |> Enum.map(fn {row, id} ->
            call(id, outbox, "0x" <> Contracts.is_spent_selector() <> word(row["id"]), tag)
          end)
        else
          []
        end

      requests =
        withdrawals_requests ++
          Enum.map(Enum.with_index(batches || [], 200), fn {row, id} ->
            request(id, "eth_getTransactionReceipt", [row["hash"]])
          end)

      requests = if address?(inbox), do: [call(6, inbox, @batch_count, tag) | requests], else: requests
      second = if requests == [], do: %{}, else: rpc.(url, requests)
      canonical = canonical_blocks(url, batches, second, rpc)

      result
      |> Map.put(
        "withdrawals",
        protect(fn -> withdrawal_result(selected["withdrawals"], withdrawals, second, outbox) end)
      )
      |> Map.put(
        "batches",
        protect(fn -> batch_result(selected["batches"], batches, second, canonical, heads, inbox) end)
      )
      |> Map.put("batch_head", protect(fn -> batch_head(selected["batches"], second[6]) end))
    else
      _ -> result
    end
  end

  defp canonical_blocks(url, batches, responses, rpc) do
    numbers =
      Enum.with_index(batches || [], 200)
      |> Enum.flat_map(fn {_row, id} ->
        case responses[id] do
          {:ok, %{"blockNumber" => number}} -> [number]
          _ -> []
        end
      end)
      |> Enum.uniq()

    responses =
      if numbers == [],
        do: %{},
        else:
          rpc.(
            url,
            Enum.with_index(numbers, 300)
            |> Enum.map(fn {number, id} ->
              request(id, "eth_getBlockByNumber", [number, false])
            end)
          )

    Map.new(Enum.with_index(numbers, 300), fn {number, id} -> {number, responses[id]} end)
  end

  defp withdrawal_result(selected, rows, responses, outbox) do
    true = address?(outbox) and is_list(rows)

    data =
      Enum.with_index(rows, 100)
      |> Enum.reduce(
        %{
          "size" => length(rows),
          "truncated" => truncated(selected),
          "onchain_relayed" => 0,
          "missing_relay" => 0,
          "unexpected_relay" => 0,
          "missing_completion" => 0
        },
        fn {row, id}, acc ->
          spent = boolean(responses[id])
          indexed = row["status"] == "relayed"
          true = row["status"] in ~w(initiated sent confirmed relayed)

          acc
          |> increment("onchain_relayed", spent)
          |> increment("missing_relay", spent and not indexed)
          |> increment("unexpected_relay", not spent and indexed)
          |> increment("missing_completion", spent and is_nil(row["completion_hash"]))
        end
      )

    success(data)
  end

  defp batch_result(selected, rows, responses, canonical, heads, inbox) do
    true = address?(inbox) and is_list(rows) and is_integer(heads["safe"]) and is_integer(heads["finalized"])
    true = heads["finalized"] <= heads["safe"] and heads["safe"] <= heads["latest"]

    {data, _} =
      Enum.with_index(rows, 200)
      |> Enum.reduce(
        {%{
           "size" => length(rows),
           "truncated" => truncated(selected),
           "posted" => 0,
           "safe" => 0,
           "finalized" => 0,
           "publication_mismatch" => 0,
           "finality_mismatch" => 0,
           "latest_posted" => 0,
           "latest_safe" => 0,
           "latest_finalized" => 0,
           "latest_number" => 0
         }, true},
        fn {row, id}, {acc, first?} ->
          {:ok, receipt} = responses[id]
          true = is_map(receipt) and hash?(receipt["blockHash"])
          {:ok, block} = canonical[receipt["blockNumber"]]
          true = is_map(block) and hash?(block["hash"])
          number = integer(receipt["blockNumber"])
          true = integer(block["number"]) == number and number <= heads["latest"]

          posted =
            receipt["status"] == "0x1" and receipt["blockHash"] == block["hash"] and
              receipt["transactionHash"] == row["hash"] and
              Enum.any?(receipt["logs"] || [], fn log ->
                String.downcase(log["address"] || "") == inbox and
                  log["removed"] != true and
                  Enum.at(log["topics"] || [], 0) == Events.sequencer_batch_delivered() and
                  integer(Enum.at(log["topics"] || [], 1)) == row["number"]
              end)

          safe = posted and number <= heads["safe"]
          finalized = posted and number <= heads["finalized"]
          true = row["status"] in ~w(finalized unfinalized)

          acc =
            acc
            |> increment("posted", posted)
            |> increment("safe", safe)
            |> increment("finalized", finalized)
            |> increment("publication_mismatch", not posted or number != row["parent_block"])
            |> increment("finality_mismatch", row["status"] == "finalized" != safe)

          acc =
            if first?,
              do:
                Map.merge(acc, %{
                  "latest_posted" => flag(posted),
                  "latest_safe" => flag(safe),
                  "latest_finalized" => flag(finalized),
                  "latest_number" => row["number"]
                }),
              else: acc

          {acc, false}
        end
      )

    success(data)
  end

  defp batch_head(%{"ok" => true, "data" => %{"rows" => rows}}, {:ok, value}) do
    count = integer(value)
    true = count > 0

    indexed =
      case rows do
        [row | _] -> row["number"]
        [] -> -1
      end

    success(%{
      "batch_number" => count - 1,
      "indexing_lag" => max(count - 1 - indexed, 0),
      "index_ahead" => max(indexed - count + 1, 0)
    })
  end

  defp rows(%{"ok" => true, "data" => %{"rows" => rows}}, limit) when is_list(rows) and length(rows) <= limit, do: rows
  defp rows(_, _), do: nil
  defp truncated(%{"data" => %{"truncated" => n}}) when n in [0, 1], do: n

  defp protect(fun) do
    fun.()
  rescue
    _ -> failure()
  catch
    _, _ -> failure()
  end

  defp head({:ok, %{"number" => value, "hash" => hash}}) do
    if hash?(hash), do: integer(value)
  rescue
    _ -> nil
  end

  defp head(_), do: nil

  defp contract({:ok, "0x" <> value}) when byte_size(value) == 64 do
    if String.slice(value, 0, 24) == String.duplicate("0", 24),
      do: "0x" <> String.downcase(String.slice(value, 24, 40))
  end

  defp contract(_), do: nil

  defp address?(value),
    do: is_binary(value) and Regex.match?(~r/^0x[0-9a-fA-F]{40}$/, value) and value != "0x" <> String.duplicate("0", 40)

  defp hash?(value), do: is_binary(value) and Regex.match?(~r/^0x[0-9a-fA-F]{64}$/, value)

  defp boolean({:ok, value}) do
    n = integer(value)
    true = n in [0, 1]
    n == 1
  end

  defp integer("0x" <> value) do
    {n, ""} = Integer.parse(value, 16)
    true = n >= 0 and n < 9_223_372_036_854_775_808
    n
  end

  defp quantity(n), do: "0x" <> Integer.to_string(n, 16)
  defp word(n) when is_integer(n) and n >= 0, do: String.pad_leading(Integer.to_string(n, 16), 64, "0")
  defp request(id, method, params), do: %{jsonrpc: "2.0", id: id, method: method, params: params}
  defp call(id, address, data, tag), do: request(id, "eth_call", [%{to: address, data: data}, tag])
  defp success(data), do: %{"ok" => true, "data" => data}
  defp failure, do: %{"ok" => false}
  defp flag(true), do: 1
  defp flag(false), do: 0
  defp increment(data, key, yes?), do: Map.update!(data, key, &(&1 + flag(yes?)))
end
