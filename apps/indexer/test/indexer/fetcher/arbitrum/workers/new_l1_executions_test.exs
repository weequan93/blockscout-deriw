defmodule Indexer.Fetcher.Arbitrum.Workers.NewL1ExecutionsTest do
  use ExUnit.Case, async: false

  import Mox

  alias Explorer.Chain.Arbitrum.{L1Execution, Message}
  alias Explorer.Repo
  alias Indexer.Fetcher.Arbitrum
  alias Indexer.Fetcher.Arbitrum.Utils.Db.MessageScanCursor
  alias Indexer.Fetcher.Arbitrum.Workers.NewL1Executions

  setup :verify_on_exit!

  setup do
    :ok = Ecto.Adapters.SQL.Sandbox.checkout(Repo)
    Ecto.Adapters.SQL.Sandbox.mode(Repo, {:shared, self()})
    original = Application.get_env(:indexer, Arbitrum)
    Application.put_env(:indexer, Arbitrum, l1_finalization_threshold: 1_000)

    on_exit(fn ->
      if original,
        do: Application.put_env(:indexer, Arbitrum, original),
        else: Application.delete_env(:indexer, Arbitrum)
    end)

    state = %{
      config: %{
        l1_outbox_address: "0x0000000000000000000000000000000000000001",
        l1_executions_scan_cursor_key:
          MessageScanCursor.execution_key(
            421_614,
            "0x0000000000000000000000000000000000000002",
            "0x0000000000000000000000000000000000000001"
          ),
        l1_rpc: %{
          json_rpc_named_arguments: [transport: EthereumJSONRPC.Mox, transport_options: []],
          logs_block_range: 50,
          chunk_size: 20,
          track_finalization: false
        }
      },
      task_data: %{
        new_executions: %{start_block: 319_000_001},
        historical_executions: %{end_block: 300_000_000},
        new_batches: %{start_block: 328_000_001}
      }
    }

    {:ok, state: state}
  end

  test "empty withdrawal ranges persist and a fresh process resumes them", %{state: state} do
    expect_head()
    expect_logs(319_000_001, 319_000_050)
    assert {:ok, next} = NewL1Executions.discover_new_l1_messages_executions(state)
    assert next.task_data.new_executions.start_block == 319_000_051
    assert next.task_data.new_batches == state.task_data.new_batches

    task = Task.async(fn -> MessageScanCursor.resume(state.config.l1_executions_scan_cursor_key, 319_000_001) end)
    assert Task.await(task) == 319_000_051
  end

  test "withdrawal range override leaves the other parent-chain scan ranges unchanged", %{state: state} do
    state = put_in(state, [:config, :l1_executions_block_range], 10_000)
    expect_head()
    expect_logs(319_000_001, 319_010_000)
    assert {:ok, next} = NewL1Executions.discover_new_l1_messages_executions(state)
    assert next.task_data.new_executions.start_block == 319_010_001
    assert next.config.l1_rpc.logs_block_range == 50
    assert next.task_data.new_batches == state.task_data.new_batches
  end

  test "a failed RPC does not save the failed range", %{state: state} do
    key = state.config.l1_executions_scan_cursor_key
    MessageScanCursor.advance(key, 319_000_001)
    expect_head()
    expect(EthereumJSONRPC.Mox, :json_rpc, fn %{method: "eth_getLogs"}, _ -> raise "RPC interrupted" end)

    assert_raise RuntimeError, "RPC interrupted", fn ->
      NewL1Executions.discover_new_l1_messages_executions(state)
    end

    assert MessageScanCursor.resume(key, 0) == 319_000_001
  end

  test "overlapping chunks keep completed progress if the next chunk fails", %{state: state} do
    state = put_in(state, [:task_data, :new_executions, :start_block], 300_000_001)
    state = put_in(state, [:task_data, :historical_executions, :end_block], 299_000_000)
    expect_head(299_999_999, 300_000_100)
    expect_logs(299_999_999, 300_000_048)
    expect(EthereumJSONRPC.Mox, :json_rpc, fn %{method: "eth_getLogs"}, _ -> raise "later chunk interrupted" end)

    assert_raise RuntimeError, "later chunk interrupted", fn ->
      NewL1Executions.discover_new_l1_messages_executions(state)
    end

    assert MessageScanCursor.resume(state.config.l1_executions_scan_cursor_key, 0) == 300_000_049
  end

  test "a saved cursor still replays the unsafe window at the head", %{state: state} do
    state = put_in(state, [:task_data, :new_executions, :start_block], 300_000_011)
    state = put_in(state, [:task_data, :historical_executions, :end_block], 300_000_000)
    MessageScanCursor.advance(state.config.l1_executions_scan_cursor_key, 300_000_011)
    expect_head(299_999_999, 300_000_010)
    expect_logs(300_000_001, 300_000_010)
    assert {:ok, next} = NewL1Executions.discover_new_l1_messages_executions(state)
    assert next.task_data.new_executions.start_block == 300_000_011
  end

  test "a relay event imports the execution and updates the confirmed message before checkpointing", %{state: state} do
    hash = "0x" <> String.duplicate("a", 64)
    assert {:ok, _} = Repo.insert(%Message{direction: :from_l2, message_id: 512, status: :confirmed})
    expect_relay(hash)

    assert {:ok, _} = NewL1Executions.discover_new_l1_messages_executions(state)
    assert Repo.get!(L1Execution, 512).execution_id
    message = Repo.get_by!(Message, direction: :from_l2, message_id: 512)
    assert message.status == :relayed
    assert to_string(message.completion_transaction_hash) == hash
    assert MessageScanCursor.resume(state.config.l1_executions_scan_cursor_key, 0) == 319_000_051
  end

  test "a failed message import keeps the cursor and replays an already imported execution", %{state: state} do
    hash = "0x" <> String.duplicate("a", 64)
    key = state.config.l1_executions_scan_cursor_key
    MessageScanCursor.advance(key, 319_000_001)
    assert {:ok, _} = Repo.insert(%Message{direction: :from_l2, message_id: 512, status: :confirmed})

    Repo.query!("""
    ALTER TABLE arbitrum_crosslevel_messages ADD CONSTRAINT bridge_cursor_fixture_no_relay
    CHECK (status <> 'relayed') NOT VALID
    """)

    expect_relay(hash)
    trapping = Process.flag(:trap_exit, true)

    try do
      result =
        try do
          NewL1Executions.discover_new_l1_messages_executions(state)
        rescue
          _ -> :import_failed
        catch
          :exit, _ -> :import_failed
        end

      assert result == :import_failed
    after
      Process.flag(:trap_exit, trapping)
    end

    assert MessageScanCursor.resume(key, 0) == 319_000_001
    assert Repo.get!(L1Execution, 512).execution_id
    assert Repo.get_by!(Message, direction: :from_l2, message_id: 512).status == :confirmed
    Repo.query!("ALTER TABLE arbitrum_crosslevel_messages DROP CONSTRAINT bridge_cursor_fixture_no_relay")
    expect_relay(hash)
    assert {:ok, _} = NewL1Executions.discover_new_l1_messages_executions(state)
    assert Repo.get_by!(Message, direction: :from_l2, message_id: 512).status == :relayed
    assert MessageScanCursor.resume(key, 0) == 319_000_051
  end

  defp expect_relay(hash) do
    expect_head()

    expect_logs(319_000_001, 319_000_050, [
      %{
        "transactionHash" => hash,
        "blockNumber" => EthereumJSONRPC.integer_to_quantity(319_000_005),
        "data" => "0x" <> String.pad_leading(Integer.to_string(512, 16), 64, "0")
      }
    ])

    expect(EthereumJSONRPC.Mox, :json_rpc, fn [%{id: id, method: "eth_getBlockByNumber"}], _ ->
      {:ok,
       [%{id: id, result: %{"number" => EthereumJSONRPC.integer_to_quantity(319_000_005), "timestamp" => "0x6553f100"}}]}
    end)
  end

  test "withdrawals have an independent monotonic checkpoint scoped by parent and contracts", %{state: state} do
    rollup = "0x0000000000000000000000000000000000000002"
    outbox = state.config.l1_outbox_address
    key = state.config.l1_executions_scan_cursor_key
    refute key == MessageScanCursor.key(421_614, rollup, outbox)
    refute key == MessageScanCursor.execution_key(421_615, rollup, outbox)
    refute key == MessageScanCursor.execution_key(421_614, outbox, outbox)
    assert MessageScanCursor.advance(key, 123) == 123
    assert MessageScanCursor.advance(key, 100) == 123
    assert MessageScanCursor.resume(key, 0) == 123
    assert MessageScanCursor.resume(MessageScanCursor.key(421_614, rollup, outbox), 0) == 0
  end

  defp expect_head(safe \\ 327_000_000, latest \\ 328_000_000) do
    for {tag, number} <- [{"safe", safe}, {"latest", latest}] do
      expect(EthereumJSONRPC.Mox, :json_rpc, fn
        [%{id: id, method: "eth_getBlockByNumber", params: [^tag, false]}], _ ->
          {:ok,
           [
             %{
               id: id,
               result: %{
                 "difficulty" => "0x0",
                 "extraData" => "0x",
                 "gasLimit" => "0x0",
                 "gasUsed" => "0x0",
                 "hash" => "0x" <> String.duplicate("0", 64),
                 "logsBloom" => "0x" <> String.duplicate("0", 512),
                 "miner" => "0x" <> String.duplicate("0", 40),
                 "number" => EthereumJSONRPC.integer_to_quantity(number),
                 "parentHash" => "0x" <> String.duplicate("0", 64),
                 "receiptsRoot" => "0x" <> String.duplicate("0", 64),
                 "size" => "0x0",
                 "stateRoot" => "0x" <> String.duplicate("0", 64),
                 "timestamp" => "0x0",
                 "totalDifficulty" => "0x0",
                 "transactionsRoot" => "0x" <> String.duplicate("0", 64),
                 "transactions" => [],
                 "uncles" => []
               }
             }
           ]}
      end)
    end
  end

  defp expect_logs(first, last, logs \\ []) do
    expect(EthereumJSONRPC.Mox, :json_rpc, fn %{method: "eth_getLogs", params: [filter]}, _ ->
      assert EthereumJSONRPC.quantity_to_integer(filter.fromBlock) == first
      assert EthereumJSONRPC.quantity_to_integer(filter.toBlock) == last
      {:ok, logs}
    end)
  end
end
