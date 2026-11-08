defmodule Explorer.Migrator.StartupControl do
  @moduledoc """
  Holds automatic Explorer data migrations during a staged version upgrade.

  This affects child selection at application startup. It does not stop workers
  that are already running or prevent explicitly supervised maintenance work.
  Migration status records and the background-migration cache are preserved.
  """

  @spec disabled?() :: boolean()
  def disabled?, do: Application.get_env(:explorer, :background_migrations_autostart_disabled, false) == true

  @spec filter([Supervisor.child_spec() | module() | {module(), term()}]) :: list()
  def filter(children) do
    if disabled?(), do: Enum.reject(children, &migration_child?/1), else: children
  end

  defp migration_child?({module, _options}), do: migration_child?(module)
  defp migration_child?(%{start: {module, _function, _arguments}}), do: migration_child?(module)

  defp migration_child?(module) when is_atom(module) do
    name = Atom.to_string(module)

    String.starts_with?(name, "Elixir.Explorer.Migrator.") or
      name == "Elixir.Explorer.TokenInstanceOwnerAddressMigration.Supervisor"
  end

  defp migration_child?(_child), do: false
end
