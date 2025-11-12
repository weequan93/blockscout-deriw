




defmodule Explorer.Repo.Migrations.AddAdvancedIndexes do
  use Ecto.Migration

  def up do

    execute("""
    CREATE INDEX IF NOT EXISTS address_hash_idx ON logs (address_hash);
    """)


  end

  def down do
    execute("""
    DROP INDEX IF EXISTS address_hash_idx;
    """)
  end
end
