




defmodule Explorer.Repo.Migrations.AddAdvancedIndexes do
  use Ecto.Migration

  def up do
    # Composite index for internal_transactions
    drop(index(:internal_transactions, [:transaction_hash, :type, :index, :block_number, :transaction_index], name: :internal_transactions_hash_type_block_idx))
    create(
      index(
        :internal_transactions,
        [:block_number, :type, :index,  :transaction_hash],
        name: :internal_transactions_block_type_index_hash_idx
      )
    )

  end

  def down do
    drop(index(
      :internal_transactions,
      [:block_number, :type, :index,  :transaction_hash],
      name: :internal_transactions_block_type_index_hash_idx
    ))
  end
end
