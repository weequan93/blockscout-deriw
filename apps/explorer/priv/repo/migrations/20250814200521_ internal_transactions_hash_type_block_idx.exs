def up do
  # Composite index for internal_transactions
  create(
    index(
      :internal_transactions,
      [:transaction_hash, :type, :index, :block_number, :transaction_index],
      name: :internal_transactions_hash_type_block_idx
    )
  )
end

def down do
  drop(index(:internal_transactions, [:transaction_hash, :type, :index, :block_number, :transaction_index], name: :internal_transactions_hash_type_block_idx))
end
