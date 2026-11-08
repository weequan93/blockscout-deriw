defmodule Explorer.Market.NativeCoinPriceOnlyTest do
  use ExUnit.Case, async: false

  alias Explorer.Market
  alias Explorer.Market.{Source, Token}
  alias Explorer.Market.Fetcher.Coin

  @table :native_coin_price_only_test

  setup do
    previous = for module <- [Market, Source, Coin], do: {module, Application.get_env(:explorer, module)}

    on_exit(fn ->
      for {module, value} <- previous do
        if is_nil(value),
          do: Application.delete_env(:explorer, module),
          else: Application.put_env(:explorer, module, value)
      end
    end)

    :ets.new(@table, [:named_table, :public, :set])
    Application.put_env(:explorer, Coin, enabled: true, store: :ets, table_name: @table)
    Application.put_env(:explorer, Market, native_coin_price_only: true)

    Application.put_env(:explorer, Source,
      native_coin_source: Source.CoinGecko,
      native_coin_history_source: Source.CoinGecko,
      market_cap_history_source: Source.CoinGecko
    )

    coin = %{
      Token.null()
      | fiat_value: Decimal.new("2250.77"),
        total_supply: Decimal.new("122186161"),
        available_supply: Decimal.new("122186161"),
        market_cap: Decimal.new("275012947542"),
        volume_24h: Decimal.new("1000"),
        symbol: "ETH",
        last_updated: ~U[2026-11-03 00:00:00Z]
    }

    :ets.insert(@table, [{false, coin}, {true, coin}])
    {:ok, coin: coin}
  end

  test "borrows the price while withholding foreign supply and market size", %{coin: coin} do
    rate = Market.get_coin_exchange_rate()
    assert rate.fiat_value == coin.fiat_value
    assert rate.symbol == "ETH"
    assert rate.last_updated == coin.last_updated
    assert is_nil(rate.total_supply)
    assert is_nil(rate.available_supply)
    assert is_nil(rate.market_cap)
    assert is_nil(rate.volume_24h)
    # The cached provider response is not mutated, nor is the secondary coin.
    assert [{false, ^coin}] = :ets.lookup(@table, false)
    assert Market.get_secondary_coin_exchange_rate() == coin
  end

  test "existing deployments retain full market data unless opted in", %{coin: coin} do
    Application.delete_env(:explorer, Market)
    assert Market.get_coin_exchange_rate() == coin
    assert Source.market_cap_history_source() == Source.CoinGecko
  end

  test "price history stays available while foreign market-cap history is disabled" do
    assert Source.native_coin_source() == Source.CoinGecko
    assert Source.native_coin_price_history_source() == Source.CoinGecko
    assert is_nil(Source.market_cap_history_source())
  end
end
