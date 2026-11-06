defmodule Explorer.Chain.Transaction.HistoryGuardTest do
  use ExUnit.Case, async: false

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

  test "revert lookup for unavailable history returns without accessing RPC or Repo" do
    # The deliberately incomplete map would fail inside the lookup body.
    assert is_nil(Explorer.Chain.Transaction.fetch_transaction_revert_reason(%{block_number: 176_114_082}))
    assert is_nil(Explorer.Chain.fetch_transaction_revert_reason(%{block_number: 176_114_082}))
  end

  test "pending transactions have no historical revert state" do
    assert is_nil(Explorer.Chain.Transaction.fetch_transaction_revert_reason(%{block_number: nil}))
  end
end
