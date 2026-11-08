defmodule Indexer.Block.Fetcher.ReceiptFallbackTest do
  use ExUnit.Case, async: true

  alias Indexer.Block.Fetcher.ReceiptFallback

  def transaction do
    %{
      hash: "0x" <> String.duplicate("a", 64),
      block_hash: "0x" <> String.duplicate("b", 64),
      block_number: 37,
      transaction_index: 0,
      gas: 21_000
    }
  end

  def receipt do
    transaction = transaction()

    %{
      "transactionHash" => transaction.hash,
      "blockHash" => transaction.block_hash,
      "blockNumber" => "0x25",
      "transactionIndex" => "0x0",
      "status" => "0x1",
      "contractAddress" => nil,
      "cumulativeGasUsed" => "0x5208",
      "gasUsed" => "0x5208",
      "logs" => []
    }
  end

  def log do
    %{
      "transactionHash" => transaction().hash,
      "blockHash" => transaction().block_hash,
      "blockNumber" => "0x25",
      "transactionIndex" => "0x0",
      "logIndex" => "0x0",
      "address" => "0x" <> String.duplicate("d", 40),
      "data" => "0x",
      "topics" => [],
      "removed" => false
    }
  end

  test "selects the requested receipt while preserving its gas and logs" do
    wanted = Map.put(receipt(), "logs", [log()])
    extra = %{receipt() | "transactionHash" => "0x" <> String.duplicate("c", 64), "transactionIndex" => "0x1"}

    assert {:ok, [selected]} = ReceiptFallback.validate([%{id: 37, result: [wanted, extra]}], [transaction()])
    assert selected["transactionHash"] == transaction().hash
    assert selected["gas"] == 21_000
    assert selected["logs"] == [log()]
  end

  test "rejects a reorged block, wrong height, or wrong transaction index" do
    for bad <- [
          Map.put(receipt(), "blockHash", "0x" <> String.duplicate("e", 64)),
          Map.put(receipt(), "blockNumber", "0x26"),
          Map.put(receipt(), "transactionIndex", "0x1")
        ] do
      assert {:error, _} = ReceiptFallback.validate([%{id: 37, result: [bad]}], [transaction()])
    end
  end

  test "rejects missing, duplicate, unsolicited, errored and null block responses" do
    response = %{id: 37, result: [receipt()]}

    for bad <- [
          [],
          [response, response],
          [%{response | id: 38}],
          [%{id: 37, result: nil}],
          [%{id: 37, error: %{code: -32601}}]
        ] do
      assert {:error, _} = ReceiptFallback.validate(bad, [transaction()])
    end
  end

  test "rejects duplicate or incomplete receipts" do
    for bad <- [[], [receipt(), receipt()]] do
      assert {:error, _} = ReceiptFallback.validate([%{id: 37, result: bad}], [transaction()])
    end

    second = %{transaction() | hash: "0x" <> String.duplicate("c", 64), transaction_index: 1}
    assert {:error, _} = ReceiptFallback.validate([%{id: 37, result: [receipt()]}], [transaction(), second])
  end

  test "rejects removed, duplicated and mismatched logs" do
    for bad_logs <- [
          [%{log() | "removed" => true}],
          [%{log() | "blockNumber" => "0x26"}],
          [%{log() | "blockHash" => "0x" <> String.duplicate("e", 64)}],
          [%{log() | "transactionHash" => "0x" <> String.duplicate("e", 64)}],
          [%{log() | "transactionIndex" => "0x1"}],
          [Map.put(log(), "logIndex", nil)],
          [log(), log()]
        ] do
      assert {:error, _} =
               ReceiptFallback.validate([%{id: 37, result: [Map.put(receipt(), "logs", bad_logs)]}], [transaction()])
    end
  end

  test "rejects incomplete or inconsistent expected transaction metadata" do
    for bad <- [
          [Map.delete(transaction(), :block_hash)],
          [Map.delete(transaction(), :transaction_index)],
          [transaction(), transaction()],
          [
            transaction(),
            %{transaction() | hash: "0x" <> String.duplicate("c", 64), block_hash: "0x" <> String.duplicate("e", 64)}
          ]
        ] do
      assert {:error, _} = ReceiptFallback.validate([%{id: 37, result: [receipt()]}], bad)
    end
  end

  test "validates multiple blocks independently and rejects a missing block response" do
    second = %{transaction() | hash: "0x" <> String.duplicate("c", 64), block_number: 38}
    second_receipt = %{receipt() | "transactionHash" => second.hash, "blockNumber" => "0x26"}
    responses = [%{id: 38, result: [second_receipt]}, %{id: 37, result: [receipt()]}]

    assert {:ok, receipts} = ReceiptFallback.validate(responses, [transaction(), second])
    assert length(receipts) == 2
    assert {:error, _} = ReceiptFallback.validate(tl(responses), [transaction(), second])
  end

  test "rejects a duplicate block log index across different transactions" do
    second = %{transaction() | hash: "0x" <> String.duplicate("c", 64), transaction_index: 1}
    first_receipt = Map.put(receipt(), "logs", [log()])
    second_log = %{log() | "transactionHash" => second.hash, "transactionIndex" => "0x1"}

    second_receipt = %{
      receipt()
      | "transactionHash" => second.hash,
        "transactionIndex" => "0x1",
        "logs" => [second_log]
    }

    assert {:error, _} =
             ReceiptFallback.validate([%{id: 37, result: [first_receipt, second_receipt]}], [transaction(), second])
  end
end
