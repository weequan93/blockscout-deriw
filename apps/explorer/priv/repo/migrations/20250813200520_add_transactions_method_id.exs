
defmodule Explorer.Repo.Migrations.AddMethodIdToTransactions do
  use Ecto.Migration

  def up do
    execute("""
    ALTER TABLE transactions
    ADD COLUMN method_id bytea GENERATED ALWAYS AS (substring(input FROM 1 FOR 4)) STORED
    """)
    create(index(:transactions, :method_id))
  end

  def down do
    drop(index(:transactions, :method_id))
    execute("ALTER TABLE transactions DROP COLUMN method_id")
  end
end
