defmodule Explorer.Repo.Migrations.AddLogsTokenTransferTopicsBlockNumberCoverIndex do
  use Ecto.Migration

  @disable_ddl_transaction true
  @disable_migration_lock true

  def up do
    execute("""
    CREATE INDEX CONCURRENTLY IF NOT EXISTS logs_token_transfer_topics_block_number_cover_idx
    ON public.logs (block_number)
    INCLUDE (block_hash, "index", transaction_hash)
    WHERE first_topic IN (
      '\\xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'::bytea,
      '\\xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62'::bytea,
      '\\x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb'::bytea
    )
    """)
  end

  def down do
    execute("""
    DROP INDEX CONCURRENTLY IF EXISTS public.logs_token_transfer_topics_block_number_cover_idx
    """)
  end
end
