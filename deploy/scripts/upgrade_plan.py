#!/usr/bin/env python3
"""Inspect the v10 rehearsal source, optionally reading bounded live dev metadata.

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
        "required_next_step": "Bounded metadata review, followed by backup/restore rehearsal on separate storage",
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
    parser.add_argument("--live", action="store_true", help="Read dev schema versions and two background status records")
    args = parser.parse_args()
    try:
        plan = json.loads(PLAN.read_text())
        report = source_report(ROOT, plan)
        if args.live:
            spec = importlib.util.spec_from_file_location("upgrade_dev_config", ROOT / "deploy/scripts/dev_config.py")
            api = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = api
            spec.loader.exec_module(api)
            report.update(live_report(ROOT, plan, api))
        print(json.dumps(report, indent=2))
        return 0
    except Exception:
        # Docker/Repo exception strings can include private configuration.
        print(json.dumps({"status": "stopped", "reason": "Upgrade source or bounded metadata check failed; raw values suppressed", "deployment_permitted": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
