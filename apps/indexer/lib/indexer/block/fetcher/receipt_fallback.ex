defmodule Indexer.Block.Fetcher.ReceiptFallback do
  @moduledoc """
  Recovers receipts from their known blocks when transaction-hash lookup is missing.

  A fallback must cover every requested transaction and match the already fetched
  block hash, height, transaction index and log identities. Extra transactions in
  the same block are not imported by this receipt batch.
  """

  alias EthereumJSONRPC.{Logs, Receipts}
  alias EthereumJSONRPC.Receipts.ByBlockNumber

  def fetch(transactions, json_rpc_named_arguments) do
    with true <- valid_transactions?(transactions),
         requests =
           transactions
           |> Enum.map(& &1.block_number)
           |> Enum.uniq()
           |> Enum.map(&ByBlockNumber.request(%{id: &1, number: &1})),
         {:ok, responses} <- EthereumJSONRPC.json_rpc(requests, json_rpc_named_arguments),
         {:ok, raw_receipts} <- validate(responses, transactions) do
      elixir_receipts = Receipts.to_elixir(raw_receipts)

      {:ok,
       %{
         receipts: Receipts.elixir_to_params(elixir_receipts),
         logs: elixir_receipts |> Receipts.elixir_to_logs() |> Logs.elixir_to_params()
       }}
    else
      false -> {:error, :receipt_fallback_missing_transaction_metadata}
      error -> error
    end
  end

  @doc false
  def validate(responses, transactions) do
    with true <- valid_transactions?(transactions),
         true <- is_list(responses),
         expected_blocks = Map.new(transactions, &{&1.block_number, normalize_hash(&1.block_hash)}),
         true <- Enum.all?(responses, &valid_response?(&1, expected_blocks)),
         true <- Enum.sort(Enum.map(responses, & &1.id)) == Enum.sort(Map.keys(expected_blocks)),
         receipts = Enum.flat_map(responses, & &1.result),
         hashes = Enum.map(receipts, &normalize_hash(&1["transactionHash"])),
         true <- length(hashes) == MapSet.size(MapSet.new(hashes)),
         receipt_by_hash = Map.new(receipts, &{normalize_hash(&1["transactionHash"]), &1}),
         true <- Enum.all?(transactions, &valid_receipt?(receipt_by_hash[normalize_hash(&1.hash)], &1)),
         logs = Enum.flat_map(transactions, &receipt_by_hash[normalize_hash(&1.hash)]["logs"]),
         log_keys = Enum.map(logs, &{quantity(&1["blockNumber"]), quantity(&1["logIndex"])}),
         true <- length(log_keys) == MapSet.size(MapSet.new(log_keys)) do
      selected =
        Enum.map(transactions, fn transaction ->
          receipt_by_hash
          |> Map.fetch!(normalize_hash(transaction.hash))
          |> Map.put("gas", transaction.gas)
        end)

      {:ok, selected}
    else
      _ -> {:error, :receipt_fallback_identity_mismatch}
    end
  end

  defp valid_transactions?(transactions) when is_list(transactions) and transactions != [] do
    Enum.all?(transactions, fn transaction ->
      is_map(transaction) and is_integer(transaction[:block_number]) and transaction.block_number >= 0 and
        is_integer(transaction[:transaction_index]) and transaction.transaction_index >= 0 and
        is_integer(transaction[:gas]) and transaction.gas >= 0 and
        not is_nil(normalize_hash(transaction[:hash])) and not is_nil(normalize_hash(transaction[:block_hash]))
    end) and
      length(transactions) == MapSet.size(MapSet.new(transactions, &normalize_hash(&1.hash))) and
      length(transactions) == MapSet.size(MapSet.new(transactions, &{&1.block_number, &1.transaction_index})) and
      Enum.all?(Enum.group_by(transactions, & &1.block_number), fn {_number, entries} ->
        MapSet.size(MapSet.new(entries, &normalize_hash(&1.block_hash))) == 1
      end)
  end

  defp valid_transactions?(_), do: false

  defp valid_response?(%{id: number, result: receipts}, expected_blocks) when is_list(receipts) do
    Map.has_key?(expected_blocks, number) and
      Enum.all?(receipts, fn receipt ->
        is_map(receipt) and quantity(receipt["blockNumber"]) == number and
          normalize_hash(receipt["blockHash"]) == expected_blocks[number] and
          not is_nil(normalize_hash(receipt["transactionHash"]))
      end)
  end

  defp valid_response?(_, _), do: false

  defp valid_receipt?(receipt, transaction) when is_map(receipt) do
    quantity(receipt["blockNumber"]) == transaction.block_number and
      normalize_hash(receipt["blockHash"]) == normalize_hash(transaction.block_hash) and
      quantity(receipt["transactionIndex"]) == transaction.transaction_index and
      is_list(receipt["logs"]) and
      Enum.all?(receipt["logs"], &valid_log?(&1, transaction)) and
      unique_log_indexes?(receipt["logs"])
  end

  defp valid_receipt?(_, _), do: false

  defp valid_log?(log, transaction) when is_map(log) do
    normalize_hash(log["transactionHash"]) == normalize_hash(transaction.hash) and
      normalize_hash(log["blockHash"]) == normalize_hash(transaction.block_hash) and
      quantity(log["blockNumber"]) == transaction.block_number and
      quantity(log["transactionIndex"]) == transaction.transaction_index and
      is_integer(quantity(log["logIndex"])) and log["removed"] != true
  end

  defp valid_log?(_, _), do: false

  defp unique_log_indexes?(logs) do
    length(logs) == MapSet.size(MapSet.new(logs, &quantity(&1["logIndex"])))
  end

  defp normalize_hash(hash) when is_binary(hash) do
    if Regex.match?(~r/\A0x[0-9a-fA-F]{64}\z/, hash), do: String.downcase(hash)
  end

  defp normalize_hash(_), do: nil

  defp quantity(value) when is_integer(value) and value >= 0, do: value

  defp quantity("0x" <> hex) do
    case Integer.parse(hex, 16) do
      {value, ""} when value >= 0 -> value
      _ -> :invalid
    end
  end

  defp quantity(_), do: :invalid
end
