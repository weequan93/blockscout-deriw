token = FIXTURE_TOKEN
stage = FIXTURE_STAGE
target_versions = FIXTURE_VERSIONS
seed = FIXTURE_SEED
verify = FIXTURE_VERIFY
background = FIXTURE_BACKGROUND
history_probe = FIXTURE_HISTORY_PROBE

^token = System.get_env("UPGRADE_SCHEMA_FIXTURE")
%URI{host: "pg", path: "/fixture", port: 5432} = URI.parse(System.fetch_env!("DATABASE_URL"))
"false" = System.fetch_env!("ECTO_USE_SSL")
repos = [Explorer.Repo, Explorer.Repo.Account, Explorer.Repo.Arbitrum]
^repos = Explorer.ReleaseTasks.repos()

for app <- [:crypto, :ssl, :postgrex, :ecto_sql] do
  {:ok, _} = Application.ensure_all_started(app)
end

for repo <- repos do
  {:ok, _} = repo.start_link(pool_size: 2)
end

q = fn sql -> Explorer.Repo.query!(sql, [], log: false, timeout: 10_000) end

[["fixture", version, false]] =
  q.("SELECT current_database(), current_setting('server_version_num')::integer, pg_is_in_recovery()").rows

true = version >= 150_000 and version < 160_000
deferred = [20_260_721_000_000, 20_260_812_000_000]

path_for = fn repo ->
  directory = repo |> Module.split() |> List.last() |> Macro.underscore()
  path = Path.join([to_string(:code.priv_dir(:explorer)), directory, "migrations"])
  {directory, path}
end

pending = fn ->
  applied = q.("SELECT version FROM public.schema_migrations").rows |> List.flatten()
  Map.new(target_versions, fn {directory, versions} -> {directory, versions -- applied} end)
end

if stage != "baseline" do
  expected =
    if stage == "upgrade",
      do: %{"repo" => 28, "account" => 2, "arbitrum" => 1},
      else: %{"repo" => 0, "account" => 0, "arbitrum" => 0}

  ^expected = Map.new(pending.(), fn {directory, versions} -> {directory, length(versions)} end)
end

started = System.monotonic_time(:millisecond)

migrated =
  Enum.map(repos, fn repo ->
    {directory, path} = path_for.(repo)

    options =
      if stage == "baseline" and directory == "repo" do
        versions =
          Path.wildcard(Path.join(path, "*.exs"))
          |> Enum.map(fn file ->
            file |> Path.basename() |> String.split("_", parts: 2) |> hd() |> String.to_integer()
          end)

        [to: Enum.max(versions -- deferred), log: false]
      else
        [all: true, log: false]
      end

    applied = Ecto.Migrator.run(repo, path, :up, options)
    %{repository: inspect(repo), directory: directory, applied_versions: applied, count: length(applied)}
  end)

elapsed = System.monotonic_time(:millisecond) - started

if stage == "baseline" do
  # Main and account share one identical historical migration version/table.
  # On a shared database it is executed once, leaving 26 account executions.
  [294, 26, 7] = Enum.map(migrated, & &1.count)

  %{"repo" => 28, "account" => 2, "arbitrum" => 1} =
    Map.new(pending.(), fn {directory, versions} -> {directory, length(versions)} end)

  for statement <- String.split(seed, ";", trim: true), do: q.(statement)
else
  expected = if stage == "upgrade", do: [28, 2, 1], else: [0, 0, 0]
  ^expected = Enum.map(migrated, & &1.count)
  true = Enum.all?(pending.(), fn {_, versions} -> versions == [] end)
  Code.eval_string(verify, q: q)
end

history_metadata_before =
  if stage in ["baseline", "background"] do
    {data, _} = Code.eval_string(history_probe)
    data
  end

background_checks =
  if stage == "background" do
    {checks, _} = Code.eval_string(background, q: q)
    checks
  else
    nil
  end

history_metadata_after =
  if stage == "background" do
    {data, _} = Code.eval_string(history_probe)
    data
  end

IO.puts(
  "BLOCKSCOUT_SCHEMA_FIXTURE=" <>
    Jason.encode!(%{
      stage: stage,
      migrated: migrated,
      migration_elapsed_ms_on_fixture: elapsed,
      pending_counts: Map.new(pending.(), fn {directory, versions} -> {directory, length(versions)} end),
      synthetic_checks_passed: stage != "baseline",
      postgres_version_num: version,
      full_database_rehearsal: false,
      background_backfills_tested: stage == "background",
      background_checks: background_checks,
      history_metadata_before: history_metadata_before,
      history_metadata_after: history_metadata_after
    })
)
