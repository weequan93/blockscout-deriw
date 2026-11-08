defmodule Indexer.Fetcher.Arbitrum.Utils.Db.MessageScanCursor do
  @moduledoc """
  Persists the next parent block after successfully scanned live deposit ranges.

  Empty ranges are checkpoints too. A crash after message import but before this
  write replays the range through the existing idempotent import. Checkpoints
  are scoped to the parent chain and rollup/bridge contracts; RPC endpoints and
  credentials are not part of the key. Reorg overlap is still chosen by the
  live scanner on every iteration.
  """

  alias Explorer.Chain.Cache.Counters.LastFetchedCounter
  alias Explorer.Repo

  @prefix "arbitrum_to_l2_scan_v1:"

  @spec key(non_neg_integer(), binary(), binary()) :: binary()
  def key(parent_chain_id, rollup, bridge)
      when is_integer(parent_chain_id) and parent_chain_id >= 0 do
    true = Enum.all?([rollup, bridge], &Regex.match?(~r/^0x[0-9a-fA-F]{40}$/, &1))

    identity = Enum.join([parent_chain_id, String.downcase(rollup), String.downcase(bridge)], ":")
    @prefix <> Base.encode16(:crypto.hash(:sha256, identity), case: :lower)
  end

  @spec resume(binary(), non_neg_integer()) :: non_neg_integer()
  def resume(key, fallback) when is_integer(fallback) and fallback >= 0 do
    case LastFetchedCounter.get(key, nullable: true) do
      nil -> fallback
      value -> max(fallback, integer!(value))
    end
  end

  @spec advance(binary(), non_neg_integer()) :: non_neg_integer()
  def advance(key, next_block) when is_integer(next_block) and next_block >= 0 do
    %Postgrex.Result{rows: [[value]]} =
      Repo.query!(
        """
        INSERT INTO last_fetched_counters (counter_type,value,inserted_at,updated_at)
        VALUES ($1,$2,timezone('UTC',now()),timezone('UTC',now()))
        ON CONFLICT (counter_type) DO UPDATE
        SET value=GREATEST(last_fetched_counters.value,EXCLUDED.value),updated_at=EXCLUDED.updated_at
        RETURNING value
        """,
        [key, Decimal.new(next_block)],
        log: false,
        timeout: 5_000
      )

    integer!(value)
  end

  defp integer!(value) do
    number = Decimal.to_integer(value)

    unless number >= 0 and Decimal.equal?(value, Decimal.new(number)),
      do: raise(ArgumentError, "Invalid durable deposit cursor")

    number
  end
end
