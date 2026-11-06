defmodule NFTMediaHandler.V10PeerConfigTest do
  use ExUnit.Case, async: false

  @producer "producer@backend.blockscout.internal"

  setup do
    previous = System.get_env("NFT_MEDIA_HANDLER_NODES_MAP")

    on_exit(fn ->
      if is_nil(previous),
        do: System.delete_env("NFT_MEDIA_HANDLER_NODES_MAP"),
        else: System.put_env("NFT_MEDIA_HANDLER_NODES_MAP", previous)
    end)
  end

  test "legacy map provides static peer names and keeps absent maps optional" do
    System.delete_env("NFT_MEDIA_HANDLER_NODES_MAP")
    assert ConfigHelper.nft_media_peer_nodes() == []
    System.put_env("NFT_MEDIA_HANDLER_NODES_MAP", Jason.encode!(%{@producer => "/folder_1"}))
    assert ConfigHelper.nft_media_peer_nodes() == [:"producer@backend.blockscout.internal"]
  end

  test "invalid or oversized node maps fail before creating atoms" do
    for value <- [
          [],
          nil,
          %{"invalid" => "/folder_1"},
          Map.new(1..17, &{"n#{&1}@host", "/folder"})
        ] do
      System.put_env("NFT_MEDIA_HANDLER_NODES_MAP", Jason.encode!(value))
      assert_raise ArgumentError, fn -> ConfigHelper.nft_media_peer_nodes() end
    end
  end

  test "static topology is selected only for remote standalone workers" do
    System.put_env("NFT_MEDIA_HANDLER_NODES_MAP", Jason.encode!(%{@producer => "/folder_1"}))
    runtime = Path.expand("../../../../config/runtime.exs", __DIR__)
    {:__block__, _, expressions} = runtime |> File.read!() |> Code.string_to_quoted!()

    expression =
      Enum.find_value(expressions, fn
        {:=, _, [{:nft_media_topologies, _, _}, value]} -> value
        _ -> nil
      end)

    {topologies, _} =
      Code.eval_quoted(expression, nmh_enabled?: true, nmh_remote?: true, nmh_worker?: true)

    assert topologies[:nft_media_handler][:strategy] == Cluster.Strategy.Epmd

    assert topologies[:nft_media_handler][:config][:hosts] == [
             :"producer@backend.blockscout.internal"
           ]

    assert topologies[:nft_media_handler][:config][:timeout] == 5_000

    {[], _} =
      Code.eval_quoted(expression, nmh_enabled?: true, nmh_remote?: true, nmh_worker?: false)
  end

  test "matching v10 queue and consumer preserve producer folder without Repo access" do
    keys = [:r2_folder, :remote?, Indexer.NFTMediaHandler.Backfiller]
    previous = Map.new(keys, &{&1, Application.fetch_env(:nft_media_handler, &1)})

    directory =
      Path.join(System.tmp_dir!(), "v10-nft-queue-#{System.unique_integer([:positive])}")

    File.mkdir_p!(directory)

    try do
      Application.put_env(:nft_media_handler, :r2_folder, "/folder_1")
      Application.put_env(:nft_media_handler, :remote?, false)
      Application.put_env(:nft_media_handler, Indexer.NFTMediaHandler.Backfiller, enabled?: false)

      File.cd!(directory, fn ->
        {:ok, queue} = Indexer.NFTMediaHandler.Queue.start_link([])

        try do
          assert {[], :self, "/folder_1"} == NFTMediaHandler.DispatcherInterface.get_urls(1)
        after
          GenServer.stop(queue)
        end
      end)
    after
      for {key, value} <- previous do
        case value do
          {:ok, config} -> Application.put_env(:nft_media_handler, key, config)
          :error -> Application.delete_env(:nft_media_handler, key)
        end
      end

      File.rm_rf!(directory)
    end
  end
end
