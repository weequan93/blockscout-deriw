"""Read PostgreSQL/storage metadata for planning an in-place upgrade.

No snapshot, backup, checkpoint, migration, worker start or service change.
"""
import json
import os
from pathlib import Path
import re

SETTINGS = (
    "archive_mode", "full_page_writes", "max_wal_size", "min_wal_size", "wal_compression",
    "wal_keep_size", "max_slot_wal_keep_size", "wal_level",
)
RELATIONS = (
    "internal_transactions", "blocks", "transactions", "tokens", "token_instances", "bridged_tokens",
    "contract_methods", "proxy_implementations", "signed_authorizations", "address_current_token_balances",
    "account_public_tags_requests", "contract_verification_status", "smart_contract_verification_statuses",
    "multichain_search_db_main_export_queue", "multichain_search_db_export_balances_queue",
    "multichain_search_db_export_counters_queue", "multichain_search_db_export_token_info_queue",
)


def body():
    return '''
repo = Explorer.Repo;
true = is_pid(Process.whereis(repo));
{:ok, data} = repo.transaction(fn ->
  Ecto.Adapters.SQL.query!(repo, "SET TRANSACTION READ ONLY", [], timeout: 2000, log: false);
  Ecto.Adapters.SQL.query!(repo, "SET LOCAL statement_timeout = '1500ms'", [], timeout: 2000, log: false);
  Ecto.Adapters.SQL.query!(repo, "SET LOCAL lock_timeout = '500ms'", [], timeout: 2000, log: false);
  server = Ecto.Adapters.SQL.query!(repo,
    "SELECT current_setting('server_version_num')::integer, current_setting('data_directory'), pg_is_in_recovery(), pg_database_size(current_database()), inet_server_addr()::text, inet_server_port()",
    [], timeout: 2000, log: false).rows;
  settings = Ecto.Adapters.SQL.query!(repo,
    "SELECT name, setting, unit FROM pg_settings WHERE name = ANY($1::text[]) ORDER BY name",
    [SETTINGS], timeout: 2000, log: false).rows;
  tablespaces = Ecto.Adapters.SQL.query!(repo,
    "SELECT oid::bigint, pg_tablespace_location(oid) FROM pg_tablespace WHERE pg_tablespace_location(oid) <> '' ORDER BY oid LIMIT 33",
    [], timeout: 2000, log: false).rows;
  slots = Ecto.Adapters.SQL.query!(repo,
    "SELECT count(*)::integer, count(*) FILTER (WHERE NOT active)::integer FROM pg_replication_slots",
    [], timeout: 2000, log: false).rows;
  relations = Ecto.Adapters.SQL.query!(repo,
    "SELECT c.relname, c.reltuples::bigint, pg_total_relation_size(c.oid) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind='r' AND c.relname = ANY($1::text[]) ORDER BY c.relname",
    [RELATIONS], timeout: 2000, log: false).rows;
  %{server: server, settings: settings, tablespaces: tablespaces, replication_slots: slots, relations: relations}
end, timeout: 5000);
route_keys = [:url, :hostname, :port, :database, :username, :password, :ssl, :socket_dir, :parameters];
main_route = Map.take(Map.new(repo.config()), route_keys);
routes = Enum.map(Explorer.ReleaseTasks.repos(), fn configured ->
  %{repository: inspect(configured), shares_main_configuration: Map.take(Map.new(configured.config()), route_keys) == main_route}
end);
Map.put(data, :repository_routes, routes)
'''.replace("[SETTINGS]", "[" + json.dumps(list(SETTINGS)) + "]").replace("[RELATIONS]", "[" + json.dumps(list(RELATIONS)) + "]")


def absolute_path(value):
    return isinstance(value, str) and value.startswith("/") and "\x00" not in value and ".." not in Path(value).parts


def mount_for(path, mounts):
    if not absolute_path(path):
        raise ValueError("Unexpected container storage path")
    matches = []
    for mount in mounts:
        destination = mount.get("Destination")
        if absolute_path(destination) and (path == destination or destination == "/" or path.startswith(destination.rstrip("/") + "/")):
            matches.append(mount)
    if not matches:
        return {"status": "unmapped_container_storage", "container_path": path}
    mount = max(matches, key=lambda item: len(item["Destination"]))
    source = mount.get("Source")
    if mount.get("Type") not in ("bind", "volume") or not absolute_path(source):
        return {"status": "unsupported_storage_type", "container_path": path}
    relative = Path(path).relative_to(mount["Destination"])
    host = Path(source) / relative
    return {"status": "mapped", "container_path": path, "host_path": str(host), "mount_type": mount["Type"]}


def safe_database(data):
    server = data.get("server")
    if not isinstance(server, list) or len(server) != 1 or len(server[0]) != 6:
        raise ValueError("Database server metadata unavailable")
    version, directory, recovery, size, address, port = server[0]
    if not (type(version) is int and 150000 <= version < 160000 and absolute_path(directory)
            and type(recovery) is bool and type(size) is int and size >= 0
            and isinstance(address, str) and type(port) is int and 0 < port < 65536):
        raise ValueError("Unsupported database server metadata")
    settings = data.get("settings")
    if not isinstance(settings, list) or len(settings) != len(SETTINGS):
        raise ValueError("PostgreSQL settings unavailable")
    public_settings = {}
    for row in settings:
        if not isinstance(row, list) or len(row) != 3 or row[0] not in SETTINGS or row[0] in public_settings:
            raise ValueError("Unexpected PostgreSQL setting")
        if not isinstance(row[1], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", row[1]):
            raise ValueError("Unexpected PostgreSQL setting value")
        if row[2] not in (None, "B", "kB", "MB", "GB", "TB", "ms", "s", "min"):
            raise ValueError("Unexpected PostgreSQL setting unit")
        public_settings[row[0]] = {"value": row[1], "unit": row[2]}
    tablespaces = data.get("tablespaces")
    if not isinstance(tablespaces, list) or len(tablespaces) > 32 or not all(
        isinstance(row, list) and len(row) == 2 and type(row[0]) is int and row[0] > 0 and absolute_path(row[1]) for row in tablespaces
    ):
        raise ValueError("Tablespace inventory unavailable or exceeds limit")
    slots = data.get("replication_slots")
    if not isinstance(slots, list) or len(slots) != 1 or len(slots[0]) != 2 or not all(type(value) is int and value >= 0 for value in slots[0]) or slots[0][1] > slots[0][0]:
        raise ValueError("Replication slot metadata unavailable")
    routes = data.get("repository_routes")
    if not isinstance(routes, list) or not 1 <= len(routes) <= 10 or not all(
        isinstance(row, dict) and re.fullmatch(r"Explorer\.Repo(?:\.[A-Za-z]+)?", str(row.get("repository", "")))
        and type(row.get("shares_main_configuration")) is bool for row in routes
    ):
        raise ValueError("Repository route metadata unavailable")
    public_routes = [{key: row[key] for key in ("repository", "shares_main_configuration")} for row in routes]
    relations = data.get("relations")
    if not isinstance(relations, list) or len(relations) > len(RELATIONS):
        raise ValueError("Migration relation sizes unavailable")
    public_relations = []
    seen = set()
    for row in relations:
        if not (isinstance(row, list) and len(row) == 3 and row[0] in RELATIONS and row[0] not in seen
                and type(row[1]) is int and row[1] >= -1 and type(row[2]) is int and row[2] >= 0):
            raise ValueError("Unexpected migration relation metadata")
        seen.add(row[0])
        public_relations.append({"name": row[0], "estimated_rows": row[1], "total_bytes": row[2]})
    return {"server_version_num": version, "data_directory": directory, "in_recovery": recovery,
            "main_database_bytes": size, "settings": public_settings,
            "replication_slots": {"total": slots[0][0], "inactive": slots[0][1]}, "repository_routes": public_routes,
            "migration_relations": public_relations}, (address, port), tablespaces


def report(api, root):
    checker = api.load_tool("deploy_check")
    manifest, _, _ = api.configuration(root, "dev")
    before = api.inspect_project(manifest)
    expression = "data = (fn ->\n" + body() + '\nend).(); IO.puts("BLOCKSCOUT_DEPLOY_CHECK=" <> Jason.encode!(data))'
    raw = checker.marked_json(api, api.run(["docker", "exec", "backend", "/app/bin/blockscout", "rpc", expression], timeout=15))
    database, (address, port), tablespaces = safe_database(raw)
    db = before["db"]
    networks = db.get("NetworkSettings", {}).get("Networks") or {}
    shared = set(networks) & set(before["backend"].get("NetworkSettings", {}).get("Networks") or {})
    addresses = {values.get(key) for name, values in networks.items() if name in shared for key in ("IPAddress", "GlobalIPv6Address")}
    api.require(address in addresses and port == 5432, "Direct backend route to the inspected PostgreSQL container is unconfirmed")
    image_rows = json.loads(api.run(["docker", "image", "inspect", db["Image"]]))
    api.require(len(image_rows) == 1 and image_rows[0].get("Os") == "linux" and image_rows[0].get("Architecture") in ("amd64", "arm64"), "PostgreSQL architecture unavailable")
    paths = [("data", database["data_directory"]), ("wal", str(Path(database["data_directory"]) / "pg_wal"))]
    paths.extend(("tablespace_" + str(oid), path) for oid, path in tablespaces)
    storage = []
    for kind, path in paths:
        resolved = api.run(["docker", "exec", db["Id"], "readlink", "-f", path], timeout=5).strip()
        api.require(absolute_path(resolved), "Container storage path could not be resolved")
        entry = {"kind": kind, **mount_for(resolved, db.get("Mounts") or [])}
        if entry["status"] == "mapped":
            try:
                host = Path(entry["host_path"]).resolve(strict=True)
                stats = host.stat()
                capacity = os.statvfs(host)
                entry.update(host_path=str(host), filesystem_device=stats.st_dev,
                             filesystem_total_bytes=capacity.f_blocks * capacity.f_frsize,
                             filesystem_available_bytes=capacity.f_bavail * capacity.f_frsize)
            except OSError:
                entry["status"] = "host_capacity_unavailable"
        storage.append(entry)
    api.ensure_stable(before, api.inspect_project(manifest), "Deployment changed during the in-place preflight")
    devices = sorted({item["filesystem_device"] for item in storage if "filesystem_device" in item})
    root_stats = Path("/").stat()
    root_capacity = os.statvfs("/")
    return {"status": "in_place_preflight_collected", "read_only": True, "environment": "dev",
            "deployment_permitted": False, "snapshot_verified": False, "database": database,
            "database_container_image": db["Image"], "architecture": image_rows[0]["Architecture"],
            "storage": storage, "filesystem_devices": devices, "backend": api.lifecycle_summary(before["backend"]),
            "root_filesystem": {"filesystem_device": root_stats.st_dev,
                                "filesystem_total_bytes": root_capacity.f_blocks * root_capacity.f_frsize,
                                "filesystem_available_bytes": root_capacity.f_bavail * root_capacity.f_frsize},
            "note": "Data/WAL/tablespace paths and filesystem capacities only. Shared filesystem free space must not be added. Matching device IDs do not identify a cloud disk or establish snapshot coverage. Snapshot recovery and migration working-space budget remain unverified.",
            "scope": "One bounded existing-Repo read-only transaction, selected Docker metadata/readlink calls and host stat/statvfs. No database file contents or raw env/command values exported; no cloud calls, backup, checkpoint, persistent settings change, migration, worker start or restart."}
