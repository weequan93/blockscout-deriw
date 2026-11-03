#!/usr/bin/env python3
"""Inspect the v10 upgrade source, optionally reading bounded live dev metadata.

This tool has no build, migration, application-start, or deployment operation.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "deploy/upgrades/v10.2.6.json"
BACKGROUND = (
    "heavy_indexes_create_internal_transactions_pkey",
    "heavy_indexes_create_internal_transactions_block_number_transaction_index_index_index",
)
HISTORY_WORKERS = (
    "empty_internal_transactions_data",
    "reindex_duplicated_internal_transactions",
    BACKGROUND[1],
    "heavy_indexes_create_internal_transactions_not_null_constraints",
    BACKGROUND[0],
)


def migration_files(root):
    result = {}
    for path in sorted((root / "apps/explorer/priv").glob("*/migrations/*.exs")):
        name = str(path.relative_to(root / "apps/explorer/priv"))
        if not re.fullmatch(r"[a-z_]+/migrations/[0-9]+_[A-Za-z0-9_.-]+\.exs", name):
            raise ValueError("Unsupported migration path")
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def source_report(root, plan):
    actual = migration_files(root)
    if actual != plan["target_migration_sha256"]:
        raise ValueError("Migration source differs from the reviewed rehearsal inventory")
    version = re.search(r'\bversion:\s*"([0-9.]+)"', (root / "mix.exs").read_text())
    if not version or version[1] != plan["target_version"]:
        raise ValueError("Source release version differs from the rehearsal target")
    return {
        "status": "source_prepared_not_rehearsed",
        "baseline_source": plan["baseline_source"],
        "upstream_source": plan["upstream_source"],
        "target_version": plan["target_version"],
        "source_migrations": plan["migration_changes"],
        "live_pending_counts_known": False,
        "deployment_permitted": False,
        "required_next_step": "Bounded metadata review and recovery/working-space checks for the selected rehearsal or in-place route",
    }


def background_expression():
    names = json.dumps(list(BACKGROUND))
    return '''
try do
  repo = Explorer.Repo;
  true = is_pid(Process.whereis(repo));
  {:ok, rows} = repo.transaction(fn ->
    Ecto.Adapters.SQL.query!(repo, "SET TRANSACTION READ ONLY", [], timeout: 2000, log: false);
    Ecto.Adapters.SQL.query!(repo, "SET LOCAL statement_timeout = '1500ms'", [], timeout: 2000, log: false);
    Ecto.Adapters.SQL.query!(repo, "SET LOCAL lock_timeout = '500ms'", [], timeout: 2000, log: false);
    Ecto.Adapters.SQL.query!(repo,
      "SELECT migration_name, status FROM public.migrations_status WHERE migration_name = ANY($1::text[]) LIMIT 3",
      [NAMES], timeout: 2000, log: false).rows
  end, timeout: 5000);
  IO.puts("BLOCKSCOUT_DEPLOY_CHECK=" <> Jason.encode!(%{status: "ok", rows: rows}))
rescue
  _ -> IO.puts("BLOCKSCOUT_DEPLOY_CHECK=" <> Jason.encode!(%{status: "unavailable"}))
catch
  _, _ -> IO.puts("BLOCKSCOUT_DEPLOY_CHECK=" <> Jason.encode!(%{status: "unavailable"}))
end
'''.replace("NAMES", names)


def safe_background(data):
    if not isinstance(data, dict) or data.get("status") != "ok":
        return {"status": "unavailable"}
    rows = data.get("rows")
    if not isinstance(rows, list) or len(rows) > len(BACKGROUND):
        raise ValueError("Unexpected background migration evidence")
    found = {}
    for row in rows:
        if not isinstance(row, list) or len(row) != 2 or row[0] not in BACKGROUND or row[0] in found:
            raise ValueError("Unexpected background migration identity")
        found[row[0]] = row[1] if row[1] in ("started", "completed") else "other"
    return {"status": "ok", "migrations": [
        {"name": name, "status": found.get(name, "not_recorded")} for name in BACKGROUND
    ]}


def history_body():
    return '''
repo = Explorer.Repo;
true = is_pid(Process.whereis(repo));
{:ok, data} = repo.transaction(fn ->
  Ecto.Adapters.SQL.query!(repo, "SET TRANSACTION READ ONLY", [], timeout: 2000, log: false);
  Ecto.Adapters.SQL.query!(repo, "SET LOCAL statement_timeout = '1500ms'", [], timeout: 2000, log: false);
  Ecto.Adapters.SQL.query!(repo, "SET LOCAL lock_timeout = '500ms'", [], timeout: 2000, log: false);
  relation = Ecto.Adapters.SQL.query!(repo,
    "SELECT c.reltuples::bigint, pg_relation_size(c.oid), pg_total_relation_size(c.oid) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname='internal_transactions' AND c.relkind='r'",
    [], timeout: 2000, log: false).rows;
  indexes = Ecto.Adapters.SQL.query!(repo,
    "SELECT c.relname, i.indisvalid, i.indisready, i.indisunique, i.indisprimary, ARRAY(SELECT COALESCE(a.attname::text, '<expression>') FROM unnest(i.indkey) WITH ORDINALITY AS k(attnum, n) LEFT JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum WHERE k.n<=i.indnkeyatts ORDER BY k.n), i.indpred IS NOT NULL FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_class t ON t.oid=i.indrelid JOIN pg_namespace n ON n.oid=t.relnamespace WHERE n.nspname='public' AND t.relname='internal_transactions' ORDER BY c.relname LIMIT 65",
    [], timeout: 2000, log: false).rows;
  statuses = Ecto.Adapters.SQL.query!(repo,
    "SELECT migration_name, status FROM public.migrations_status WHERE migration_name = ANY($1::text[]) LIMIT 6",
    [NAMES], timeout: 2000, log: false).rows;
  %{relation: relation, indexes: indexes, statuses: statuses}
end, timeout: 5000);
Map.put(data, :runtime, %{
  trace_block_ranges: Application.get_env(:indexer, :trace_block_ranges),
  internal_fetcher_disabled: Application.get_env(:indexer, Indexer.Fetcher.InternalTransaction.Supervisor)[:disabled?],
  historical_scanner_disabled: Application.get_env(:indexer, Indexer.Temporary.UncatalogedTokenTransfers.Supervisor)[:disabled?]
})
'''.replace("NAMES", json.dumps(list(HISTORY_WORKERS)))


def safe_history(data):
    if not isinstance(data, dict):
        raise ValueError("Unexpected history metadata")
    relation = data.get("relation")
    if not isinstance(relation, list) or len(relation) != 1 or not isinstance(relation[0], list) or len(relation[0]) != 3:
        raise ValueError("Internal transaction relation metadata unavailable")
    estimate, heap, total = relation[0]
    if not (type(estimate) is int and estimate >= -1 and type(heap) is int and heap >= 0 and type(total) is int and total >= heap):
        raise ValueError("Unexpected relation size metadata")
    indexes = data.get("indexes")
    if not isinstance(indexes, list) or len(indexes) > 65:
        raise ValueError("Unexpected index metadata")
    projected = []
    for row in indexes[:64]:
        if not (isinstance(row, list) and len(row) == 7 and isinstance(row[0], str)
                and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,62}", row[0])
                and all(type(row[n]) is bool for n in (1, 2, 3, 4, 6))
                and isinstance(row[5], list) and 1 <= len(row[5]) <= 32
                and all(isinstance(key, str) and (key == "<expression>" or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", key)) for key in row[5])):
            raise ValueError("Unexpected index identity or attributes")
        projected.append(dict(zip(("name", "valid", "ready", "unique", "primary", "key_columns", "partial"), row)))
    statuses = data.get("statuses")
    if not isinstance(statuses, list) or len(statuses) > len(HISTORY_WORKERS):
        raise ValueError("Unexpected history migration states")
    found = {}
    for row in statuses:
        if not isinstance(row, list) or len(row) != 2 or row[0] not in HISTORY_WORKERS or row[0] in found:
            raise ValueError("Unexpected history migration identity")
        found[row[0]] = row[1] if row[1] in ("started", "completed") else "other"
    runtime = data.get("runtime")
    if not isinstance(runtime, dict):
        raise ValueError("Runtime history policy unavailable")
    trace_range = runtime.get("trace_block_ranges")
    if not isinstance(trace_range, str) or not re.fullmatch(r"[0-9]+\.\.(?:[0-9]+|latest)(?:,[0-9]+\.\.(?:[0-9]+|latest))*", trace_range):
        trace_range = "unavailable"
    return {
        "internal_transactions": {"estimated_rows": estimate, "heap_bytes": heap, "total_bytes_including_indexes_and_toast": total},
        "indexes": projected, "index_list_limited": len(indexes) > 64,
        "background_migrations": [{"name": name, "status": found.get(name, "not_recorded")} for name in HISTORY_WORKERS],
        "runtime": {"trace_block_ranges": trace_range, **{
            key: runtime.get(key) if type(runtime.get(key)) is bool else None
            for key in ("internal_fetcher_disabled", "historical_scanner_disabled")}},
        "duplicate_presence": "not_measured",
        "note": "Estimates and metadata only. Completed migration states do not prove queued historical traces were recovered.",
    }


def history_expression():
    return "data = (fn ->\n" + history_body() + '\nend).(); IO.puts("BLOCKSCOUT_DEPLOY_CHECK=" <> Jason.encode!(data))'


def history_report(root, api):
    checker = api.load_tool("deploy_check")
    manifest, _, _ = api.configuration(root, "dev")
    before = api.inspect_project(manifest)
    data = checker.marked_json(api, api.run([
        "docker", "exec", "backend", "/app/bin/blockscout", "rpc", history_expression()
    ], timeout=15))
    report = safe_history(data)
    api.ensure_stable(before, api.inspect_project(manifest), "Dev deployment changed during the history metadata read")
    return {"status": "history_metadata_collected", "environment": "dev", "chain_id": manifest["chain_id"],
            "read_only": True, "deployment_permitted": False, "backend": api.lifecycle_summary(before["backend"]),
            "backend_image": before["backend"]["Image"], **report,
            "scope": "Existing Repo read-only transaction; 1.5s statements and 5s transaction. Catalog/size metadata and five status records only; no trace-row scan, duplicate query, RPC history fetch, migration or restart."}


def live_report(root, plan, api):
    checker = api.load_tool("deploy_check")
    manifest, _, _ = api.configuration(root, "dev")
    before = api.inspect_project(manifest)
    rows = checker.pending_migrations(api, {"migrations": plan["target_migration_sha256"]})
    data = checker.marked_json(api, api.run([
        "docker", "exec", "backend", "/app/bin/blockscout", "rpc", background_expression()
    ], timeout=15))
    background = safe_background(data)
    api.ensure_stable(before, api.inspect_project(manifest), "Dev deployment changed during the upgrade metadata read")
    return {
        "live_pending_counts_known": all(row["status"] == "ok" for row in rows),
        "live_database_migrations": rows,
        "background_prerequisites_for_later_v11_stage": background,
        "backend": api.lifecycle_summary(before["backend"]),
        "backend_image": before["backend"]["Image"],
        "scope": "Existing Repo connections only; read-only metadata transactions with 1.5s statements/5s transactions. No count, EXPLAIN, history RPC, migration, backup, container creation or restart.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="Read dev schema versions and two background status records")
    mode.add_argument("--history-risk", action="store_true", help="Read internal transaction sizes/indexes, selected migration states and trace policy")
    mode.add_argument("--in-place-preflight", action="store_true", help="Read PostgreSQL data/WAL/tablespace placement and selected WAL settings")
    args = parser.parse_args()
    try:
        plan = json.loads(PLAN.read_text())
        report = source_report(ROOT, plan)
        if args.live or args.history_risk or args.in_place_preflight:
            spec = importlib.util.spec_from_file_location("upgrade_dev_config", ROOT / "deploy/scripts/dev_config.py")
            api = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = api
            spec.loader.exec_module(api)
            if args.in_place_preflight:
                report = api.load_tool("in_place_preflight").report(api, ROOT)
            elif args.history_risk:
                report = history_report(ROOT, api)
            else:
                report.update(live_report(ROOT, plan, api))
        print(json.dumps(report, indent=2))
        return 0
    except Exception:
        # Docker/Repo exception strings can include private configuration.
        print(json.dumps({"status": "stopped", "reason": "Upgrade source or bounded metadata check failed; raw values suppressed", "deployment_permitted": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
