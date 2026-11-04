# Loaded by release eval; starts repositories/dependencies, never the app tree.
payload = Jason.decode!(File.read!("/tmp/upgrade/payload.json"))
mode = payload["mode"]
true = mode in ["inspect", "migrate"]
true = Explorer.Migrator.StartupControl.disabled?()
nil = Process.whereis(Explorer.Supervisor)
repos = [Explorer.Repo, Explorer.Repo.Account, Explorer.Repo.Arbitrum]
^repos = Explorer.ReleaseTasks.repos()

for app <- [:crypto, :ssl, :postgrex, :ecto_sql],
    do: {:ok, _} = Application.ensure_all_started(app)

parameters = [
  application_name: payload["application_name"],
  lock_timeout: "2000",
  statement_timeout: "120000"
]

for repo <- repos, do: {:ok, _} = repo.start_link(pool_size: 2, parameters: parameters)
q = fn sql, args -> Explorer.Repo.query!(sql, args, log: false, timeout: 125_000).rows end
route_keys = [:hostname, :port, :database, :username, :password, :ssl, :socket_dir]
route = Map.take(Map.new(Explorer.Repo.config()), route_keys)
true = Enum.all?(repos, &(Map.take(Map.new(&1.config()), route_keys) == route))

# A dedicated connection holds one session lock across all repositories and
# nontransactional VACUUM operations. A surviving older writer blocks a retry.
{:ok, guard} =
  Postgrex.start_link(
    Keyword.take(Explorer.Repo.config(), route_keys) ++ [parameters: parameters]
  )

{:ok, %{rows: [[true]]}} =
  Postgrex.query(guard, "SELECT pg_try_advisory_lock(184175075, 1026)", [])

[[database, oid, address, port, pg, false]] =
  q.(
    "SELECT current_database(), (SELECT oid::bigint FROM pg_database WHERE datname=current_database()), host(inet_server_addr()), inet_server_port(), current_setting('server_version_num')::integer, pg_is_in_recovery()",
    []
  )

true = pg >= 150_000 and pg < 160_000

identity = %{
  "database" => database,
  "oid" => oid,
  "address" => address,
  "port" => port,
  "postgres_major" => 15
}

if payload["database_identity"], do: true = identity == payload["database_identity"]

priv = to_string(:code.priv_dir(:explorer))

actual_files =
  Path.wildcard(Path.join([priv, "*", "migrations", "*.exs"]))
  |> Map.new(fn file ->
    {Path.relative_to(file, priv),
     Base.encode16(:crypto.hash(:sha256, File.read!(file)), case: :lower)}
  end)

true = actual_files == payload["migration_inventory"]

paths = [
  {Explorer.Repo, "repo"},
  {Explorer.Repo.Account, "account"},
  {Explorer.Repo.Arbitrum, "arbitrum"}
]

versions =
  Map.new(paths, fn {_, directory} ->
    list =
      actual_files
      |> Map.keys()
      |> Enum.filter(&String.starts_with?(&1, directory <> "/migrations/"))

    {directory,
     Enum.map(
       list,
       &(Path.basename(&1) |> String.split("_", parts: 2) |> hd() |> String.to_integer())
     )
     |> Enum.sort()}
  end)

applied = fn ->
  q.("SELECT version FROM public.schema_migrations ORDER BY version", []) |> List.flatten()
end

pending = fn ->
  existing = applied.()
  Map.new(versions, fn {directory, values} -> {directory, values -- existing} end)
end

existing = applied.()
allowed = payload["upstream_versions"] |> Map.values() |> List.flatten()

if payload["baseline_applied"] do
  true = payload["baseline_applied"] -- existing == []
  true = (existing -- payload["baseline_applied"]) -- allowed == []
end

expected_pending =
  Map.new(payload["upstream_versions"], fn {directory, values} ->
    deferred = if directory == "repo", do: [20_260_721_000_000, 20_260_812_000_000], else: []
    {directory, Enum.sort((values -- existing) ++ deferred)}
  end)

true = pending.() == expected_pending

{schema, _} = Code.eval_string(payload["schema_probe"])
{_, bindings} = Code.eval_string(payload["presence_probe"])
%{status: "ok", tables: tables} = Keyword.fetch!(bindings, :data)
true = Enum.all?(tables, &(&1.has_rows != true))

%{"trace_address" => "integer[]", "value" => "numeric(100,0)"} =
  Map.new(schema.types, fn [name, type] -> {name, type} end)

required =
  ~w(tokens token_instances contract_methods proxy_implementations signed_authorizations migrations_status address_current_token_balances)

true = required -- Enum.map(schema.relations, &hd/1) == []
true = Enum.reduce(schema.relations, 0, fn [_, size], acc -> size + acc end) <= 1_073_741_824
heap = schema.internal_transactions_heap_identity
if payload["heap_identity"], do: true = heap == payload["heap_identity"]

status_names =
  ~w(empty_internal_transactions_data reindex_duplicated_internal_transactions heavy_indexes_create_internal_transactions_block_number_transaction_index_index_index heavy_indexes_create_internal_transactions_not_null_constraints heavy_indexes_create_internal_transactions_pkey)

statuses = fn ->
  q.(
    "SELECT migration_name, status FROM public.migrations_status WHERE migration_name=ANY($1::text[]) ORDER BY migration_name",
    [status_names]
  )
end

before_statuses = statuses.()
if payload["background_statuses"], do: true = before_statuses == payload["background_statuses"]

migrated =
  if mode == "migrate" do
    Enum.map(paths, fn {repo, directory} ->
      maximum = Enum.max(payload["upstream_versions"][directory])
      # Cutoffs are pinned independently of the payload and custom versions.
      true =
        maximum ==
          %{
            "repo" => 20_260_220_073_250,
            "account" => 20_251_029_115_930,
            "arbitrum" => 20_250_731_001_757
          }[directory]

      remaining = payload["upstream_versions"][directory] -- existing

      actual =
        Ecto.Migrator.run(repo, Path.join([priv, directory, "migrations"]), :up,
          to: maximum,
          log: false,
          timeout: 125_000
        )

      true = Enum.sort(actual) == Enum.sort(remaining)
      %{directory: directory, versions: actual}
    end)
  else
    []
  end

[[after_heap]] =
  q.("SELECT pg_relation_filenode('public.internal_transactions'::regclass)::bigint", [])

^heap = after_heap
true = statuses.() == before_statuses

if mode == "migrate",
  do:
    true =
      pending.() == %{
        "repo" => [20_260_721_000_000, 20_260_812_000_000],
        "account" => [],
        "arbitrum" => []
      }

nil = Process.whereis(Explorer.Supervisor)

IO.puts(
  "BLOCKSCOUT_DEPLOY_CHECK=" <>
    Jason.encode!(%{
      database_identity: identity,
      applied_versions: applied.(),
      pending: pending.(),
      schema: schema,
      tables: tables,
      heap_identity: heap,
      background_statuses: before_statuses,
      migrated: migrated,
      application_started: false,
      mode: mode
    })
)
