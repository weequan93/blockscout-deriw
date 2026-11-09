defmodule Indexer.Prometheus.ArbitrumBridgeMetrics.RPC do
  @moduledoc false

  @timeout 4_000
  @limit 64

  # Use the existing HTTP adapter with a short deadline and no indexer retries.
  # Only the metrics polling worker calls this; scrapes never make RPC calls.
  def batch(url, requests, transport \\ &EthereumJSONRPC.HTTP.Tesla.json_rpc/4) do
    task =
      Task.async(fn ->
        try do
          true = is_binary(url) and url != "" and length(requests) in 1..@limit
          ids = Enum.map(requests, & &1.id)
          true = length(Enum.uniq(ids)) == length(ids)
          headers = Application.get_env(:ethereum_jsonrpc, EthereumJSONRPC.HTTP, [])[:headers] || []

          {:ok, %{status_code: 200, body: body}} =
            transport.(url, Jason.encode!(requests), headers,
              timeout: @timeout,
              recv_timeout: @timeout,
              pool: :ethereum_jsonrpc
            )

          responses = Jason.decode!(body)
          true = is_list(responses) and length(responses) == length(ids)
          true = Enum.sort(Enum.map(responses, & &1["id"])) == Enum.sort(ids)

          Map.new(responses, fn response ->
            result =
              if Map.has_key?(response, "result") and not Map.has_key?(response, "error"),
                do: {:ok, response["result"]},
                else: :error

            {response["id"], result}
          end)
        rescue
          _ -> %{}
        catch
          _, _ -> %{}
        end
      end)

    case Task.yield(task, @timeout + 500) || Task.shutdown(task, :brutal_kill) do
      {:ok, result} -> result
      _ -> %{}
    end
  end
end
