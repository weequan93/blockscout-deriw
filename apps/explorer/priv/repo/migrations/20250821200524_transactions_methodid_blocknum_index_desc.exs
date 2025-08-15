




defmodule Explorer.Repo.Migrations.AddAdvancedIndexes do
  use Ecto.Migration

  def up do

    execute("""
    CREATE INDEX  IF NOT EXISTS transactions_methodid_blocknum_index_desc_idx
ON transactions (
  method_id,
  block_number DESC,
  "index" DESC
)
WHERE block_consensus = TRUE;
    """)


  end

  def down do
    execute("""
    DROP INDEX IF EXISTS transactions_methodid_blocknum_index_desc_idx;
    """)
  end
end
