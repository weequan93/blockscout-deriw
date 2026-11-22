defmodule BlockscoutNativeNormalization.RowChunks do
  alias BlockscoutNativeNormalization.Guard
  alias BlockscoutNativeNormalization.RowCallback

  @needs ~S|(it.trace_address IS NOT NULL OR it.value=0 OR (it.call_type_enum IS NULL AND it.call_type IS NOT NULL) OR it.error IS NOT NULL OR (it.created_contract_address_hash IS NOT NULL AND it.to_address_hash IS NULL)) IS TRUE|

  def drain(block, committed \\ 0) do
    {updated, wal, rows} = chunk(block)
    committed = committed + updated
    Guard.status("running", nil, %{last_batch_updated_rows: updated,
      last_batch_wal_bytes: wal, last_batch_rows: rows,
      row_chunk_block: block, row_chunk_committed_rows: committed})

    # FillingMigration may advance its block checkpoint only after a fresh
    # transaction finds no remaining targets in this block. Every committed
    # chunk is replay-safe if this task/container stops before that checkpoint.
    if updated == 0, do: :ok, else: drain(block, committed)
  end

  defp chunk(block) do
    # Never wait for host health/space/autovacuum authorization in a transaction.
    Guard.await_lease()
    {:ok, result} = Explorer.Repo.transaction(fn ->
      Explorer.Repo.query!("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ", [], log: false)
      Explorer.Repo.query!("SET LOCAL statement_timeout='30s'", [], log: false)
      Explorer.Repo.query!("SET LOCAL lock_timeout='500ms'", [], log: false)
      Explorer.Repo.query!("SET LOCAL enable_seqscan=off", [], log: false)
      Guard.identity!()
      Guard.own_lock!()
      [[status, token, cursor]] = Explorer.Repo.query!(~S"""
      SELECT status, meta->>'v10_native_job', (meta->>'max_block_number')::bigint
      FROM public.migrations_status WHERE migration_name='empty_internal_transactions_data'
      """, [], log: false, timeout: 2000).rows
      unless status == "started" and token == Guard.payload()["token"] and cursor == block,
        do: Guard.fail!("native_row_chunk_checkpoint_changed")
      [[lsn]] = Explorer.Repo.query!("SELECT pg_current_wal_insert_lsn()::text", [], log: false).rows

      # Materialize and lock a fixed row set. The callback's two queries must
      # use these same CTIDs, not independently select two LIMIT-sized sets.
      selected = Explorer.Repo.query!(
        "SELECT it.ctid::text, pg_column_size(it) FROM public.internal_transactions it " <>
        "WHERE it.block_number=$1 AND " <> @needs <>
        " ORDER BY it.ctid LIMIT 5000 FOR UPDATE",
        [block], log: false, timeout: 6000).rows
      rows = length(selected)
      bytes = Enum.reduce(selected, 0, fn [_, size], total -> total + size end)
      unless rows <= 5000 and bytes <= 16777216,
        do: Guard.fail!("native_row_chunk_size_limit")
      unless Guard.authorized?(), do: Guard.fail!("native_lease_expired_before_update")

      updated = if rows == 0 do
        0
      else
        ctids = Enum.map(selected, &hd/1)
        {:ok, {updated, nil}} = RowCallback.update_batch({[block], ctids})
        unless updated == rows and updated <= 5000,
          do: Guard.fail!("native_row_chunk_count_changed")
        updated
      end
      [[wal]] = Explorer.Repo.query!("SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(),$1::text::pg_lsn)::bigint",
        [lsn], log: false).rows
      unless wal <= 134217728, do: Guard.fail!("native_transaction_wal_limit")
      {updated, wal, rows}
    end, timeout: 40000)
    result
  rescue
    error in Postgrex.Error ->
      reason = if error.postgres[:code] in [:query_canceled, :deadlock_detected,
        :lock_not_available, :serialization_failure],
        do: "native_transient_database_error", else: "native_database_error"
      Guard.status("needs_attention", reason)
      reraise error, __STACKTRACE__
    error in DBConnection.ConnectionError ->
      Guard.status("needs_attention", "native_transient_database_error")
      reraise error, __STACKTRACE__
  end
end
