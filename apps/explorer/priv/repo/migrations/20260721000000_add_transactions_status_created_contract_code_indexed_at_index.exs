defmodule Explorer.Repo.Migrations.AddBlockscoutWarmupIndexes do
  use Ecto.Migration

  @disable_ddl_transaction true
  @disable_migration_lock true

  def up do
    create_if_not_exists(
      index(
        :blocks,
        [:number],
        name: :blocks_consensus_number_timestamp_hash_idx,
        where: "consensus = true",
        include: [:timestamp, :hash],
        concurrently: true
      )
    )

    create_if_not_exists(
      index(
        :blocks,
        [:hash],
        name: :blocks_empty_sanitizer_hash_number_idx,
        where: "is_empty IS NULL AND consensus = true AND refetch_needed = false",
        include: [:number],
        concurrently: true
      )
    )

    create_if_not_exists(
      index(
        :transactions,
        [:status, :created_contract_code_indexed_at],
        name: :transactions_status_created_contract_code_indexed_at_index,
        where: "created_contract_code_indexed_at IS NOT NULL",
        concurrently: true
      )
    )

    create_if_not_exists(
      index(
        :transactions,
        [:block_timestamp],
        name: :transactions_consensus_timestamp_stats_idx,
        where: "block_consensus = true",
        concurrently: true
      )
    )

    create_if_not_exists(
      index(
        :transactions,
        [:block_number],
        name: :transactions_consensus_block_number_gas_used_idx,
        where: "block_consensus = true",
        include: [:gas_used],
        concurrently: true
      )
    )
  end

  def down do
    drop_if_exists(
      index(
        :blocks,
        [:hash],
        name: :blocks_empty_sanitizer_hash_number_idx,
        where: "is_empty IS NULL AND consensus = true AND refetch_needed = false",
        include: [:number],
        concurrently: true
      )
    )

    drop_if_exists(
      index(
        :transactions,
        [:block_number],
        name: :transactions_consensus_block_number_gas_used_idx,
        where: "block_consensus = true",
        include: [:gas_used],
        concurrently: true
      )
    )

    drop_if_exists(
      index(
        :transactions,
        [:block_timestamp],
        name: :transactions_consensus_timestamp_stats_idx,
        where: "block_consensus = true",
        concurrently: true
      )
    )

    drop_if_exists(
      index(
        :transactions,
        [:status, :created_contract_code_indexed_at],
        name: :transactions_status_created_contract_code_indexed_at_index,
        where: "created_contract_code_indexed_at IS NOT NULL",
        concurrently: true
      )
    )

    drop_if_exists(
      index(
        :blocks,
        [:number],
        name: :blocks_consensus_number_timestamp_hash_idx,
        where: "consensus = true",
        include: [:timestamp, :hash],
        concurrently: true
      )
    )
  end
end
