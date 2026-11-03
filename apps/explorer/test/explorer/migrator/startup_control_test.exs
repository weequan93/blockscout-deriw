defmodule Explorer.Migrator.StartupControlTest do
  use ExUnit.Case, async: false

  alias Explorer.Migrator.StartupControl

  setup do
    previous = Application.fetch_env(:explorer, :background_migrations_autostart_disabled)

    on_exit(fn ->
      case previous do
        {:ok, value} -> Application.put_env(:explorer, :background_migrations_autostart_disabled, value)
        :error -> Application.delete_env(:explorer, :background_migrations_autostart_disabled)
      end
    end)
  end

  test "normal startup preserves all selected children by default" do
    Application.delete_env(:explorer, :background_migrations_autostart_disabled)
    children = [Explorer.Repo, Explorer.Migrator.EmptyInternalTransactionsData]
    refute StartupControl.disabled?()
    assert StartupControl.filter(children) == children
    Application.put_env(:explorer, :background_migrations_autostart_disabled, false)
    assert StartupControl.filter(children) == children
  end

  test "hold removes data and index workers while keeping repos and status caches" do
    Application.put_env(:explorer, :background_migrations_autostart_disabled, true)
    preserved = [Explorer.Repo, Explorer.Chain.Cache.BackgroundMigrations, Indexer.Block.Supervisor]

    workers = [
      Explorer.Migrator.EmptyInternalTransactionsData,
      {Explorer.Migrator.ReindexDuplicatedInternalTransactions, []},
      Explorer.Migrator.SwitchPendingOperations,
      Explorer.TokenInstanceOwnerAddressMigration.Supervisor,
      %{
        id: :structural_index,
        start: {Explorer.Migrator.HeavyDbIndexOperation.UpdateInternalTransactionsPrimaryKey, :start_link, [[]]}
      }
    ]

    assert StartupControl.filter(preserved ++ workers) == preserved
  end

  test "holding children does not invoke child specs or alter migration records" do
    Application.put_env(:explorer, :background_migrations_autostart_disabled, true)
    assert StartupControl.filter([Explorer.Migrator.DoesNotExist, Explorer.Repo]) == [Explorer.Repo]
  end

  test "runtime environment uses explicit true and preserves the default" do
    runtime = Path.expand("../../../../../config/runtime.exs", __DIR__)
    {:__block__, _, expressions} = runtime |> File.read!() |> Code.string_to_quoted!()

    expression =
      Enum.find_value(expressions, fn
        {:config, _, [:explorer, :background_migrations_autostart_disabled, expression]} -> expression
        _ -> nil
      end)

    previous = System.get_env("MIGRATION_BACKGROUND_AUTOSTART_DISABLED")

    try do
      for {setting, expected} <- [{nil, false}, {"false", false}, {"true", true}] do
        if is_nil(setting),
          do: System.delete_env("MIGRATION_BACKGROUND_AUTOSTART_DISABLED"),
          else: System.put_env("MIGRATION_BACKGROUND_AUTOSTART_DISABLED", setting)

        {value, _binding} = Code.eval_quoted(expression)
        assert value == expected
      end
    after
      if is_nil(previous),
        do: System.delete_env("MIGRATION_BACKGROUND_AUTOSTART_DISABLED"),
        else: System.put_env("MIGRATION_BACKGROUND_AUTOSTART_DISABLED", previous)
    end
  end
end
