defmodule EthereumJSONRPC.TraceRangeDispatchTest do
  use ExUnit.Case, async: false

  defmodule Variant do
    def fetch_internal_transactions(params, _opts), do: {:ok, params}
    def fetch_first_trace(params, _opts), do: {:ok, params}
  end

  setup do
    previous = Application.fetch_env(:indexer, :trace_block_ranges)
    Application.put_env(:indexer, :trace_block_ranges, "176114083..latest")

    on_exit(fn ->
      case previous do
        {:ok, value} -> Application.put_env(:indexer, :trace_block_ranges, value)
        :error -> Application.delete_env(:indexer, :trace_block_ranges)
      end
    end)
  end

  test "unavailable history and pending transactions never reach a variant" do
    params = [%{block_number: 176_114_082}, %{block_number: nil}]
    # No variant supplied: dispatch would raise if either request escaped.
    assert {:ok, []} = EthereumJSONRPC.fetch_internal_transactions(params, [])
    assert :ignore = EthereumJSONRPC.fetch_first_trace(params, [])
  end

  test "mixed batches retain the first available block and newer transactions" do
    first = %{block_number: 176_114_083, hash_data: "first"}
    newer = %{block_number: 176_114_084, hash_data: "newer"}
    params = [%{block_number: 176_114_082}, first, newer]

    assert {:ok, [^first, ^newer]} = EthereumJSONRPC.fetch_internal_transactions(params, variant: Variant)
    assert {:ok, [^first, ^newer]} = EthereumJSONRPC.fetch_first_trace(params, variant: Variant)
  end

  test "default full history remains available" do
    Application.put_env(:indexer, :trace_block_ranges, "0..latest")
    params = [%{block_number: 0}, %{block_number: 1}]
    assert {:ok, ^params} = EthereumJSONRPC.fetch_internal_transactions(params, variant: Variant)
    assert {:ok, ^params} = EthereumJSONRPC.fetch_first_trace(params, variant: Variant)
  end
end
