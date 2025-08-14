defmodule Explorer.Repo.Migrations.AddAdvancedIndexes do
  use Ecto.Migration

  def up do
    # Composite index for transactions
    create(
      index(
        :transactions,
        [:method_id, :block_consensus, :block_number, :index],
        name: :transactions_method_consensus_block_index
      )
    )

    # Index for internal_transactions.transaction_hash
    create(
      index(
        :internal_transactions,
        [:transaction_hash],
        name: :internal_transactions_transaction_hash_index
      )
    )
  end

  def down do
    drop(index(:transactions, [:method_id, :block_consensus, :block_number, :index], name: :transactions_method_consensus_block_index))
    drop(index(:internal_transactions, [:transaction_hash], name: :internal_transactions_transaction_hash_index))
  end
end
