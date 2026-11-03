#!/usr/bin/env python3
"""Exercise v9 -> v10 migrations on a disposable local PostgreSQL 15 database.

Uses synthetic rows, an internal Docker network, tmpfs data, no published ports,
no host mounts and no supplied database credentials or environment profiles.
This does not qualify a full-size database upgrade or test background backfills.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
BASELINE = "6b39b8b7fc471c39701ed6fb1ac6a2eadf78205a"
LABEL = "org.deriw.upgrade-schema-fixture"
MARKER = "BLOCKSCOUT_SCHEMA_FIXTURE="


def run(args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, timeout=60, **kwargs)
    if result.returncode:
        raise RuntimeError("Local fixture command failed: " + result.stderr[-2500:])
    return result.stdout


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / ("deploy/scripts/" + name + ".py"))
    result = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = result
    spec.loader.exec_module(result)
    return result


def image_inventory(api, checker, reference):
    info = json.loads(run(["docker", "image", "inspect", reference]))[0]
    return info, checker.image_inventory(api, info["Id"])


def baseline_inventory():
    prefix = "apps/explorer/priv/"
    names = run(["git", "-C", str(ROOT), "ls-tree", "-r", "--name-only", BASELINE, prefix]).splitlines()
    return {name[len(prefix):]: hashlib.sha256(subprocess.check_output([
        "git", "-C", str(ROOT), "show", BASELINE + ":" + name
    ])).hexdigest() for name in names if "/migrations/" in name and name.endswith(".exs")}


def expression(stage, token, versions):
    common = (ROOT / "deploy/tests/schema_fixture/migrate.exs").read_text()
    return common.replace("FIXTURE_TOKEN", json.dumps(token)).replace(
        "FIXTURE_STAGE", json.dumps(stage)
    ).replace("FIXTURE_VERSIONS", "%{" + ", ".join(
        json.dumps(key) + " => " + repr(value) for key, value in versions.items()
    ) + "}")


def evaluate(image, stage, token, network, password, versions, directory):
    name = "blockscout-schema-" + stage + "-" + token
    environment = {
        "UPGRADE_SCHEMA_FIXTURE": token, "CHAIN_TYPE": "arbitrum", "CHAIN_ID": "18417507517",
        "DATABASE_URL": "postgresql://fixture:" + password + "@pg:5432/fixture",
        "ECTO_USE_SSL": "false", "POOL_SIZE": "2", "ACCOUNT_POOL_SIZE": "2",
        "RELEASE_DISTRIBUTION": "none", "RELEASE_TMP": "/tmp", "ERL_FLAGS": "+S 2:2",
        "SECRET_KEY_BASE": "a" * 64, "DISABLE_FILE_LOGGING": "true",
        "DISABLE_INDEXER": "true", "INDEXER_UNCATALOGED_TOKEN_TRANSFERS_ENABLED": "false",
        "SHRINK_INTERNAL_TRANSACTIONS_ENABLED": "false", "MUD_INDEXER_ENABLED": "false",
        "BRIDGED_TOKENS_ENABLED": "false", "DISABLE_MARKET": "true", "MICROSERVICE_ACCOUNT_ABSTRACTION_ENABLED": "false",
        "ETHEREUM_JSONRPC_HTTP_URL": "http://127.0.0.1:1", "ETHEREUM_JSONRPC_TRACE_URL": "http://127.0.0.1:1",
        "TRACE_BLOCK_RANGES": "176114083..latest",
    }
    command = ["docker", "run", "--rm", "-i", "--pull", "never", "--name", name,
               "--label", LABEL + "=" + token, "--network", network, "--read-only", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges", "--cpus", "2", "--memory", "768m", "--tmpfs", "/tmp:rw,mode=1777"]
    for key, value in environment.items():
        command.extend(["--env", key + "=" + value])
    command.extend(["--entrypoint", "sh", image, "-c",
                    'cat > /tmp/migrate.exs && exec /app/bin/blockscout eval \'Code.eval_file("/tmp/migrate.exs")\''])
    script = expression(stage, token, versions)
    seed = (ROOT / "deploy/tests/schema_fixture/seed.sql").read_text()
    verify = (ROOT / "deploy/tests/schema_fixture/verify.exs").read_text()
    # Embed test inputs as Elixir strings without shell interpolation.
    script = script.replace("FIXTURE_SEED", json.dumps(seed)).replace("FIXTURE_VERIFY", json.dumps(verify))
    try:
        result = subprocess.run(command, input=script, capture_output=True, text=True, timeout=240)
        (directory / (stage + ".log")).write_text(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError(stage + " migration fixture failed; inspect " + str(directory / (stage + ".log")))
        marked = [line[len(MARKER):] for line in result.stdout.splitlines() if line.startswith(MARKER)]
        if len(marked) != 1:
            raise RuntimeError(stage + " fixture result marker unavailable")
        return json.loads(marked[0])
    finally:
        cleanup_container(name, token)


def cleanup_container(name, token):
    row = subprocess.run(["docker", "container", "inspect", name], capture_output=True, text=True, timeout=15)
    if row.returncode:
        return
    labels = json.loads(row.stdout)[0]["Config"].get("Labels") or {}
    if labels.get(LABEL) != token:
        raise RuntimeError("Refusing to remove a container outside this fixture")
    run(["docker", "rm", "-f", name])


def summary(report):
    result = dict(report)
    if "results" in result:
        result["results"] = [
            {**{key: value for key, value in row.items() if key != "migrated"},
             "applied_counts": {item["directory"]: item["count"] for item in row["migrated"]}}
            for row in result["results"]
        ]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-image", default="deriw-blockscout:v9-upgrade-baseline")
    parser.add_argument("--candidate-image", default="deriw-blockscout:v10-upgrade-local-validation")
    parser.add_argument("--postgres-image", default="postgres:15-alpine")
    args = parser.parse_args()
    token = uuid.uuid4().hex[:16]
    directory = Path(tempfile.mkdtemp(prefix="blockscout-schema-"))
    os.chmod(directory, 0o700)
    network = "blockscout-schema-" + token
    pg_name = "blockscout-schema-pg-" + token
    network_created = False
    pg_created = False
    report = {"status": "failed", "logs": str(directory), "live_services_accessed": False,
              "full_database_rehearsal": False, "background_backfills_tested": False}
    try:
        api = module("dev_config")
        checker = module("deploy_check")
        baseline, a = image_inventory(api, checker, args.baseline_image)
        candidate, b = image_inventory(api, checker, args.candidate_image)
        plan = json.loads((ROOT / "deploy/upgrades/v10.2.6.json").read_text())
        if a["migrations"] != baseline_inventory() or b["migrations"] != plan["target_migration_sha256"]:
            raise RuntimeError("Image migrations do not match the pinned source inventories")
        pg = json.loads(run(["docker", "image", "inspect", args.postgres_image]))[0]
        if {baseline["Architecture"], candidate["Architecture"], pg["Architecture"]} != {candidate["Architecture"]}:
            raise RuntimeError("Fixture images have different architectures")
        versions = checker.migration_versions(api, b)
        versions = {key: versions[key] for key in ("repo", "account", "arbitrum")}
        print(json.dumps({"phase": "images_checked", "logs": str(directory)}), flush=True)
        run(["docker", "network", "create", "--internal", "--label", LABEL + "=" + token, network])
        network_created = True
        password = uuid.uuid4().hex
        run(["docker", "run", "--rm", "-d", "--pull", "never", "--name", pg_name, "--label", LABEL + "=" + token,
             "--network", network, "--network-alias", "pg", "--read-only", "--cpus", "2", "--memory", "1g",
             "--shm-size", "128m", "--tmpfs", "/var/lib/postgresql/data:rw,mode=0700,size=768m",
             "--tmpfs", "/var/run/postgresql:rw,mode=1777,size=16m", "--tmpfs", "/tmp:rw,size=16m",
             "-e", "POSTGRES_USER=fixture", "-e", "POSTGRES_PASSWORD=" + password,
             "-e", "POSTGRES_DB=fixture", pg["Id"]])
        pg_created = True
        for _ in range(40):
            ready = subprocess.run(["docker", "exec", pg_name, "pg_isready", "-U", "fixture", "-d", "fixture"],
                                   capture_output=True, timeout=5)
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("Temporary PostgreSQL did not become ready")
        results = []
        for stage, image in (("baseline", baseline["Id"]), ("upgrade", candidate["Id"]), ("repeat", candidate["Id"])):
            print(json.dumps({"phase": stage}), flush=True)
            results.append(evaluate(image, stage, token, network, password, versions, directory))
        report.update(status="passed", postgres_image=pg["Id"], baseline_image=baseline["Id"],
                      candidate_image=candidate["Id"], architecture=candidate["Architecture"], results=results)
    except Exception as error:
        report["reason"] = str(error)
    finally:
        if pg_created:
            cleanup_container(pg_name, token)
        if network_created:
            run(["docker", "network", "rm", network])
        report["fixture_resources_removed"] = True
        (directory / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(summary(report), indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
