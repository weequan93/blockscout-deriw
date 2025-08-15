




defmodule Explorer.Repo.Migrations.AddAdvancedIndexes do
  use Ecto.Migration

  def up do

    execute("""
    CREATE INDEX IF NOT EXISTS internal_transactions_txhash_blocknum_filtered_idx
    ON internal_transactions (
      transaction_hash,
      block_number DESC,
      transaction_index DESC,
      index DESC
    )
    WHERE ((type = 'call' AND index > 0) OR type != 'call');
    """)


  end

  def down do
    execute("""
    DROP INDEX IF EXISTS internal_transactions_txhash_blocknum_filtered_idx;
    """)
  end
end
