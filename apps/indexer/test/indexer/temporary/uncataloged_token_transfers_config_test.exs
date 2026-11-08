defmodule Indexer.Temporary.UncatalogedTokenTransfersConfigTest do
  use ExUnit.Case, async: false

  alias Indexer.Temporary.UncatalogedTokenTransfers.Supervisor, as: RepairSupervisor

  @setting "INDEXER_UNCATALOGED_TOKEN_TRANSFERS_ENABLED"
  @runtime_config Path.expand("../../../../../config/runtime.exs", __DIR__)

  setup do
    previous_env = System.get_env(@setting)
    previous_config = Application.get_env(:indexer, RepairSupervisor)

    on_exit(fn ->
      if is_nil(previous_env), do: System.delete_env(@setting), else: System.put_env(@setting, previous_env)

      if is_nil(previous_config),
        do: Application.delete_env(:indexer, RepairSupervisor),
        else: Application.put_env(:indexer, RepairSupervisor, previous_config)
    end)

    :ok
  end

  test "unset or empty setting prevents the historical worker tree from starting" do
    for value <- [nil, ""] do
      configure(value)
      assert RepairSupervisor.disabled?()
      assert :ignore = RepairSupervisor.start_link([])
    end
  end

  test "explicit opt-in enables the historical worker supervisor" do
    for value <- ["true", "TRUE"] do
      configure(value)
      refute RepairSupervisor.disabled?()
    end
  end

  test "false or unrecognized values keep automatic repair disabled" do
    for value <- ["false", "FALSE", "yes", "invalid"] do
      configure(value)
      assert RepairSupervisor.disabled?()
      assert :ignore = RepairSupervisor.start_link([])
    end
  end

  defp configure(value) do
    if is_nil(value), do: System.delete_env(@setting), else: System.put_env(@setting, value)

    {:__block__, _, expressions} = @runtime_config |> File.read!() |> Code.string_to_quoted!()

    options =
      Enum.find_value(expressions, fn
        {:config, _,
         [:indexer, {:__aliases__, _, [:Indexer, :Temporary, :UncatalogedTokenTransfers, :Supervisor]}, options]} ->
          options

        _ ->
          nil
      end)

    assert options
    {config, _binding} = Code.eval_quoted(options)
    Application.put_env(:indexer, RepairSupervisor, config)
  end
end
