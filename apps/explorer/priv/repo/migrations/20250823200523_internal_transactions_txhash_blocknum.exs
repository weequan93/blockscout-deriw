




defmodule Explorer.Repo.Migrations.AddAdvancedIndexes do
  use Ecto.Migration

  def up do

    execute("""
    CREATE INDEX IF NOT EXISTS internal_transactions_txhash_blocknum_desc_idx
    ON internal_transactions (
      transaction_hash,
      block_number DESC,
      transaction_index DESC,
      index DESC
    );
    """)


  end

  def down do
    execute("""
    DROP INDEX IF EXISTS internal_transactions_txhash_blocknum_desc_idx;
    """)
  end
end
