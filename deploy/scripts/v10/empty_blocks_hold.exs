defmodule BlockscoutV10EmptyBlocksHold do
  import Ecto.Query

  @worker Indexer.Fetcher.EmptyBlocksSanitizer
  @supervisor Indexer.Fetcher.EmptyBlocksSanitizer.Supervisor
  @root Indexer.Supervisor
  @query_md5 "f7d49ed68ea1ac040431775d295eaae5"

  # Exact reconstruction of the empty-block SELECT in the pinned source.
  # This compiles SQL only; it never scans the application tables.
  def query_fingerprint do
    {sql, _args} = query_template()

    %{
      query_md5: Base.encode16(:crypto.hash(:md5, sql), case: :lower),
      query_bytes: byte_size(sql)
    }
  end

  def query_template do
    candidate =
      from(block in Explorer.Chain.Block,
        as: :block,
        select: %{hash: block.hash, number: block.number},
        where: is_nil(block.is_empty),
        where: block.number <= ^0,
        where: block.consensus == true,
        where: block.refetch_needed == false,
        limit: ^10
      )

    query = from(block in candidate, where: not exists(any_block_transactions_query()))
    Ecto.Adapters.SQL.to_sql(:all, Explorer.Repo, query)
  end

  defp any_block_transactions_query do
    from(t in Explorer.Chain.Transaction,
      select: 1,
      where: t.block_number == parent_as(:block).number and t.block_consensus == true
    )
  end

  defp root_children do
    all = Supervisor.which_children(@root) |> Enum.sort()

    [{@supervisor, pid, :supervisor, [@supervisor]}] =
      Enum.filter(all, &(elem(&1, 0) == @supervisor))

    true = is_pid(pid) and Process.whereis(@supervisor) == pid
    all
  end

  defp query(sql, args) do
    {:ok, rows} =
      Explorer.Repo.transaction(
        fn ->
          Explorer.Repo.query!("SET TRANSACTION READ ONLY", [], log: false)
          Explorer.Repo.query!("SET LOCAL statement_timeout='1500ms'", [], log: false)
          Explorer.Repo.query!("SET LOCAL lock_timeout='500ms'", [], log: false)
          Explorer.Repo.query!(sql, args, log: false, timeout: 2000).rows
        end,
        timeout: 5000
      )

    rows
  end

  defp children do
    all = Supervisor.which_children(@supervisor)
    [{@worker, pid, :worker, [@worker]}] = Enum.filter(all, &(elem(&1, 0) == @worker))
    {:ok, spec} = :supervisor.get_childspec(@supervisor, @worker)
    {@worker, :start_link, _} = spec.start
    true = is_integer(spec.shutdown) and spec.shutdown >= 0 and spec.shutdown <= 5000
    {pid, Enum.reject(all, &(elem(&1, 0) == @worker)) |> Enum.sort()}
  end

  defp target(%{"pid" => nil}), do: []

  defp target(payload) do
    query(
      "SELECT jsonb_build_object('pid',pid,'backend_start',backend_start::text,'query_start',query_start::text,'state',state,'expected_query',md5(query)=$2 AND query_start=$3::text::timestamptz,'snapshot_present',backend_xmin IS NOT NULL,'assigned_xid_present',backend_xid IS NOT NULL,'same_role',usename=current_user,'same_backend_address',host(client_addr)=ANY($4::text[])) FROM pg_stat_activity WHERE pid=$1 AND datname=current_database() AND backend_type='client backend' AND pid<>pg_backend_pid()",
      [payload["pid"], @query_md5, payload["query_start"], payload["backend_client_addresses"]]
    )
    |> List.flatten()
  end

  defp current_sanitizer_queries(payload) do
    query(
      "SELECT jsonb_build_object('pid',pid,'backend_start',backend_start::text,'query_start',query_start::text,'query_md5',md5(query)) FROM pg_stat_activity WHERE md5(query)=$1 AND host(client_addr)=ANY($2::text[]) AND datname=current_database() AND usename=current_user AND backend_type='client backend' AND state='active' AND backend_xid IS NULL AND pid<>pg_backend_pid() ORDER BY query_start,pid LIMIT 2",
      [@query_md5, payload["backend_client_addresses"]]
    )
    |> List.flatten()
  end

  def run(payload) do
    true = Explorer.Migrator.StartupControl.disabled?()
    true = is_pid(Process.whereis(Explorer.Repo))
    "10.2.6" = Application.spec(:explorer, :vsn) |> to_string()
    true = payload["query_md5"] == @query_md5
    %{query_md5: @query_md5, query_bytes: 310} = query_fingerprint()

    true =
      (is_nil(payload["pid"]) and is_nil(payload["query_start"])) or
        (is_integer(payload["pid"]) and payload["pid"] > 0 and is_binary(payload["query_start"]))

    true = payload["mode"] in ["observe", "hold", "restore"]
    root_before = root_children()
    {before_pid, siblings} = children()
    observed = target(payload)

    # The supervisor child and a historical database query are separate targets.
    # Pausing the known sanitizer is valid after its old query finishes. A stale
    # query identity never authorizes a cancellation or changes the saved target.
    cancel_eligible =
      Enum.any?(observed, fn row ->
        row["expected_query"] and row["same_role"] and row["same_backend_address"] and
          not row["assigned_xid_present"] and
          row["backend_start"] == payload["backend_start"]
      end)

    cancellation =
      case payload["mode"] do
        "hold" ->
          if is_pid(before_pid), do: :ok = Supervisor.terminate_child(@supervisor, @worker)
          {:undefined, ^siblings} = children()
          nil = Process.whereis(@worker)
          # Terminating the owner normally releases its database task. Cancel only
          # the still-running, exact original SELECT if it outlived that owner.
          if cancel_eligible do
            query(
              "SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE pid=$1 AND md5(query)=$2 AND query_start=$3::text::timestamptz AND backend_start=$4::text::timestamptz AND host(client_addr)=ANY($5::text[]) AND datname=current_database() AND usename=current_user AND backend_type='client backend' AND state='active' AND backend_xid IS NULL AND pid<>pg_backend_pid()",
              [
                payload["pid"],
                @query_md5,
                payload["query_start"],
                payload["backend_start"],
                payload["backend_client_addresses"]
              ]
            )
            |> List.flatten()
          else
            []
          end

        "restore" ->
          if before_pid == :undefined do
            {:ok, _} = Supervisor.restart_child(@supervisor, @worker)
          end

          []

        "observe" ->
          []
      end

    {after_pid, ^siblings} = children()
    ^root_before = root_children()

    remaining =
      Enum.reduce_while(1..5, [], fn _, _ ->
        rows = target(payload)

        if payload["mode"] == "hold" and
             Enum.any?(rows, &(&1["expected_query"] and &1["state"] == "active")),
           do:
             (
               Process.sleep(100)
               {:cont, rows}
             ),
           else: {:halt, rows}
      end)

    %{
      mode: payload["mode"],
      sanitizer_worker_running: is_pid(after_pid),
      sanitizer_worker_held: after_pid == :undefined,
      other_sanitizer_children_preserved: true,
      other_indexer_children_preserved: true,
      source_query_match_verified: true,
      target: remaining,
      cancellation_requested: cancellation != [],
      cancellation_accepted: cancellation == [true],
      query_target_provided: not is_nil(payload["pid"]),
      cancellation_eligible_at_check: cancel_eligible,
      current_sanitizer_queries: current_sanitizer_queries(payload),
      application_restarted: false,
      direct_database_row_update_requested: false,
      sanitizer_work_resumed: payload["mode"] == "restore",
      original_query_active:
        Enum.any?(remaining, &(&1["expected_query"] and &1["state"] == "active")),
      affected:
        "Empty-block repair scans and their block/refetch bookkeeping are paused. Live indexing, API and NFT services retain their supervisors."
    }
  end
end
