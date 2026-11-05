defmodule BlockscoutNormalizationDiagnostics do
  import Ecto.Query, only: [from: 2]

  @queries_sql ~S"""
  WITH candidates AS MATERIALIZED (
    SELECT * FROM pg_stat_activity
    WHERE datname=current_database() AND backend_type='client backend'
      AND pid<>pg_backend_pid() AND xact_start<clock_timestamp()-interval '60 seconds'
    ORDER BY xact_start,pid LIMIT 20
  )
  SELECT jsonb_build_object(
    'pid',pid,'backend_started_at',backend_start::text,
    'transaction_started_at',xact_start::text,'query_started_at',query_start::text,
    'state',state,'transaction_age_seconds',EXTRACT(epoch FROM clock_timestamp()-xact_start)::bigint,
    'snapshot_present',backend_xmin IS NOT NULL,'assigned_xid_present',backend_xid IS NOT NULL,
    'wait_type',wait_event_type,'wait_event',wait_event,
    'same_role_as_observer',usename=current_user,'client_address',host(client_addr),
    'query_md5',md5(query),'query_bytes',octet_length(query),
    'query_shape',CASE WHEN upper(ltrim(query)) LIKE 'SELECT%' THEN 'select'
                       WHEN upper(ltrim(query)) LIKE 'WITH%' THEN 'with' ELSE 'other' END,
    'fingerprints',jsonb_build_object(
      'count_aggregate',position('count(' in lower(query))>0,
      'sum_aggregate',position('sum(' in lower(query))>0,
      'gas_used',position('gas_used' in lower(query))>0,
      'distinct',position('distinct' in lower(query))>0,
      'order_by',position('order by' in lower(query))>0,
      'limit',position('limit' in lower(query))>0),
    'mentioned_relations',to_jsonb(ARRAY(
      SELECT name FROM unnest(ARRAY['internal_transactions','transactions','blocks','addresses',
        'tokens','token_transfers','logs','smart_contracts','last_fetched_counters',
        'transaction_stats','address_coin_balances','address_token_balances']) name
      WHERE query ~* ('(^|[^a-z_])'||name||'([^a-z_]|$)')
    ))
  ) FROM candidates ORDER BY xact_start,pid
  """

  # These queries are compiled to SQL, never executed. A matching SQL template
  # suggests a source query; it does not prove which process called that query.
  defp source_queries do
    {:ok, address} = Explorer.Chain.Hash.Address.cast("0x0000000000000000000000000000000000000000")

    candidates = [
      {"blocks_consensus_count",
       from(b in Explorer.Chain.Block, select: count(b.hash), where: b.consensus == true)},
      {"transactions_exact_count", from(t in Explorer.Chain.Transaction, select: count(t.hash))},
      {"addresses_exact_count", from(a in Explorer.Chain.Address, select: count())},
      {"transactions_gas_usage_sum",
       from(t0 in Explorer.Chain.Transaction, select: fragment("SUM(t0.gas_used)"))},
      {"address_incoming_transaction_count",
       from(t in Explorer.Chain.Transaction, where: t.to_address_hash == ^address, select: count(t.hash))},
      {"address_incoming_gas_usage_sum",
       from(t in Explorer.Chain.Transaction, where: t.to_address_hash == ^address, select: sum(t.gas_used))}
    ]

    pairs =
      Enum.map(candidates, fn {name, query} ->
        {sql, _args} = Ecto.Adapters.SQL.to_sql(:all, Explorer.Repo, query)
        {Base.encode16(:crypto.hash(:md5, sql), case: :lower), name}
      end)

    Enum.group_by(
      [
        {"d63aea58d81cc7d2d9c7ad5cd82809dd", "missing_blocks_metrics"},
        {"f7d49ed68ea1ac040431775d295eaae5", "empty_blocks_sanitizer_empty_denormalized"} | pairs
      ],
      &elem(&1, 0),
      &elem(&1, 1)
    )
  end

  def run(activity_sql) do
    true = Explorer.Migrator.StartupControl.disabled?()
    "10.2.6" = Application.spec(:explorer, :vsn) |> to_string()
    sources = source_queries()

    {:ok, report} =
      Explorer.Repo.transaction(
        fn ->
          q = fn sql -> Explorer.Repo.query!(sql, [], log: false, timeout: 2000).rows end
          q.("SET TRANSACTION READ ONLY")
          q.("SET LOCAL statement_timeout='1500ms'")
          q.("SET LOCAL lock_timeout='500ms'")

          [[identity]] =
            q.(
              "SELECT jsonb_build_object('database',current_database(),'oid',(SELECT oid::bigint FROM pg_database WHERE datname=current_database()),'address',host(inet_server_addr()),'port',inet_server_port(),'postgres_major',current_setting('server_version_num')::integer/10000)"
            )

          [[transactions]] = q.(activity_sql)

          queries =
            q.(@queries_sql)
            |> List.flatten()
            |> Enum.map(fn row ->
              Map.put(row, "source_query_matches", Map.get(sources, row["query_md5"], []))
            end)

          %{database_identity: identity, transactions: transactions, query_diagnostics: queries}
        end,
        timeout: 5000
      )

    report
  end
end
