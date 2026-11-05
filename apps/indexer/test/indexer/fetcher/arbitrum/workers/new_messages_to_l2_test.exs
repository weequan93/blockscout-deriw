defmodule Indexer.Fetcher.Arbitrum.Workers.NewMessagesToL2Test do
  use ExUnit.Case, async: false

  import Mox

  alias Indexer.Fetcher.Arbitrum
  alias Indexer.Fetcher.Arbitrum.Workers.NewMessagesToL2

  setup :verify_on_exit!

  setup do
    original_config = Application.get_env(:indexer, Arbitrum)

    Application.put_env(
      :indexer,
      Arbitrum,
      Keyword.put(original_config || [], :l1_finalization_threshold, 1_000)
    )

    on_exit(fn ->
      if is_nil(original_config) do
        Application.delete_env(:indexer, Arbitrum)
      else
        Application.put_env(:indexer, Arbitrum, original_config)
      end
    end)

    state = %{
      config: %{
        json_l1_rpc_named_arguments: [transport: EthereumJSONRPC.Mox, transport_options: []],
        l1_rpc_chunk_size: 20,
        l1_rpc_block_range: 50,
        l1_bridge_address: "0x0000000000000000000000000000000000000001",
        l1_rollup_init_block: 286_461_007
      },
      task_data: %{
        check_new: %{start_block: 278_124_097},
        check_historical: %{end_block: 200_000_000}
      },
      completed_tasks: %{check_historical: false, check_missing_origination: true}
    }

    {:ok, state: state}
  end

  test "finishing historical discovery preserves a cursor already below the configured floor", %{state: state} do
    assert {:ok, finished} = NewMessagesToL2.check_historical(state)
    assert finished.completed_tasks.check_historical
    assert finished.task_data == state.task_data
    assert finished.config == state.config
    assert finished.completed_tasks.check_missing_origination
  end

  test "live discovery continues through empty ranges after historical discovery finishes", %{state: state} do
    assert {:ok, finished} = NewMessagesToL2.check_historical(state)

    for {first, last} <- [{278_124_097, 278_124_146}, {278_124_147, 278_124_196}] do
      expect_head_and_logs(first, last, 300_000_000, 300_000_010)
    end

    assert {:ok, first} = NewMessagesToL2.check_new(finished)
    assert first.task_data.check_new.start_block == 278_124_147
    assert {:ok, second} = NewMessagesToL2.check_new(first)
    assert second.task_data.check_new.start_block == 278_124_197
    assert second.task_data.check_historical == state.task_data.check_historical
    assert second.completed_tasks == finished.completed_tasks
  end

  test "the final historical chunk stays inside the floor and then completes", %{state: state} do
    floor = state.config.l1_rollup_init_block
    state = put_in(state, [:task_data, :check_historical, :end_block], floor + 24)
    expect_logs(floor, floor + 24)

    assert {:ok, scanned} = NewMessagesToL2.check_historical(state)
    assert scanned.task_data.check_historical.end_block == floor - 1
    refute scanned.completed_tasks.check_historical
    assert {:ok, finished} = NewMessagesToL2.check_historical(scanned)
    assert finished.completed_tasks.check_historical
    assert finished.task_data == scanned.task_data
  end

  test "live discovery still revisits unsafe blocks without crossing the historical boundary", %{state: state} do
    state = put_in(state, [:task_data, :check_new, :start_block], 300_000_005)
    state = put_in(state, [:task_data, :check_historical, :end_block], 300_000_000)
    expect_head_and_logs(300_000_001, 300_000_010, 299_999_999, 300_000_010)

    assert {:ok, updated} = NewMessagesToL2.check_new(state)
    assert updated.task_data.check_new.start_block == 300_000_011
  end

  test "live discovery waits when no blocks exist above the historical boundary", %{state: state} do
    state = put_in(state, [:task_data, :check_new, :start_block], 300_000_011)
    state = put_in(state, [:task_data, :check_historical, :end_block], 300_000_010)
    expect_head(300_000_000, 300_000_010)

    assert {:ok, ^state} = NewMessagesToL2.check_new(state)
  end

  defp expect_head_and_logs(first, last, safe, latest) do
    expect_head(safe, latest)
    expect_logs(first, last)
  end

  defp expect_head(safe, latest) do
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

  defp expect_logs(first, last) do
    expect(EthereumJSONRPC.Mox, :json_rpc, fn %{method: "eth_getLogs", params: [filter]}, _ ->
      assert EthereumJSONRPC.quantity_to_integer(filter.fromBlock) == first
      assert EthereumJSONRPC.quantity_to_integer(filter.toBlock) == last
      {:ok, []}
    end)
  end
end
