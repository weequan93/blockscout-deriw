import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("upgrade_plan", ROOT / "deploy/scripts/upgrade_plan.py")
U = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(U)


class UpgradePlanTests(unittest.TestCase):
    def test_reviewed_inventory_matches_the_source(self):
        plan = json.loads(U.PLAN.read_text())
        report = U.source_report(ROOT, plan)
        self.assertFalse(report["deployment_permitted"])
        self.assertFalse(report["live_pending_counts_known"])
        self.assertEqual(report["target_version"], "10.2.6")
        self.assertEqual(len(plan["migration_changes"]["added"]), 45)
        self.assertEqual(plan["migration_changes"]["removed"], [])

    def test_migration_tampering_is_rejected(self):
        plan = json.loads(U.PLAN.read_text())
        plan["target_migration_sha256"].pop(next(iter(plan["target_migration_sha256"])))
        with self.assertRaises(ValueError):
            U.source_report(ROOT, plan)

    def test_no_duplicate_versions_within_each_repository(self):
        versions = set()
        for name in U.migration_files(ROOT):
            directory, _, filename = name.split("/")
            key = directory, filename.split("_", 1)[0]
            self.assertNotIn(key, versions)
            versions.add(key)

    def test_source_version_is_checked(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "mix.exs").write_text('version: "9.0.1"')
            with self.assertRaises(ValueError):
                U.source_report(root, {"target_migration_sha256": {}, "target_version": "10.2.6"})

    def test_background_results_distinguish_absence_and_completion(self):
        result = U.safe_background({"status": "ok", "rows": [[U.BACKGROUND[0], "completed"]]})
        self.assertEqual([row["status"] for row in result["migrations"]], ["completed", "not_recorded"])
        self.assertEqual(U.safe_background({"status": "unavailable"}), {"status": "unavailable"})

    def test_unexpected_status_values_are_withheld(self):
        result = U.safe_background({"status": "ok", "rows": [[U.BACKGROUND[0], "private-fixture-value"]]})
        self.assertNotIn("private-fixture-value", json.dumps(result))

    def test_unexpected_or_duplicate_names_are_rejected(self):
        for rows in [[["unknown", "completed"]], [[U.BACKGROUND[0], "started"]] * 2]:
            with self.assertRaises(ValueError):
                U.safe_background({"status": "ok", "rows": rows})

    def test_live_evidence_uses_existing_rpc_and_checks_stability(self):
        checker = SimpleNamespace(
            pending_migrations=Mock(return_value=[{"status": "unavailable"}]),
            marked_json=Mock(return_value={"status": "ok", "rows": []}),
        )
        rows = {"backend": {"Image": "sha256:" + "a" * 64}}
        api = SimpleNamespace(
            load_tool=Mock(return_value=checker), configuration=Mock(return_value=({}, {}, {})),
            inspect_project=Mock(return_value=rows), ensure_stable=Mock(),
            lifecycle_summary=Mock(return_value={"status": "running"}), run=Mock(return_value="suppressed"),
        )
        report = U.live_report(ROOT, {"target_migration_sha256": {}}, api)
        self.assertFalse(report["live_pending_counts_known"])
        api.ensure_stable.assert_called_once()
        command = api.run.call_args.args[0]
        self.assertEqual(command[:5], ["docker", "exec", "backend", "/app/bin/blockscout", "rpc"])
        self.assertIn('"SET TRANSACTION READ ONLY"', command[5])
        self.assertIn("statement_timeout = '1500ms'", command[5])
        self.assertIn("LIMIT 3", command[5])


class ElixirReadTests(unittest.TestCase):
    def test_exact_background_expression_is_read_only_and_handles_failure(self):
        script = '''
defmodule Explorer.Repo do
  def transaction(fun, opts) do
    [timeout: 5000] = opts
    {:ok, fun.()}
  end
end
defmodule Ecto.Adapters.SQL do
  def query!(Explorer.Repo, query, params, opts) do
    [timeout: 2000, log: false] = opts
    if Process.get(:fail), do: raise("private-fixture-error")
    Process.put(:queries, Process.get(:queries, []) ++ [query])
    if String.starts_with?(query, "SELECT ") do
      [names] = params
      2 = length(names)
      true = String.ends_with?(query, "LIMIT 3")
    else
      [] = params
    end
    %{rows: []}
  end
end
defmodule Jason do
  def encode!(%{status: "ok", rows: []}) do
    ["SET TRANSACTION READ ONLY", "SET LOCAL statement_timeout = '1500ms'",
     "SET LOCAL lock_timeout = '500ms'", _query] = Process.get(:queries)
    "READ_ONLY_OK"
  end
  def encode!(%{status: "unavailable"}), do: "UNAVAILABLE_OK"
end
Process.register(self(), Explorer.Repo)
'''
        script += U.background_expression()
        script += '\nProcess.put(:fail, true)\n' + U.background_expression()
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w") as archive:
            raw = script.encode()
            entry = tarfile.TarInfo("read.exs")
            entry.size = len(raw)
            archive.addfile(entry, io.BytesIO(raw))
        result = subprocess.run([
            "docker", "run", "--rm", "--pull", "never", "-i", "--network", "none", "--read-only",
            "--tmpfs", "/work:rw,exec,size=16m", "--workdir", "/work", "--cpus", "2", "--memory", "256m",
            "-e", "ERL_FLAGS=+S 2:2", "elixir@sha256:0ff6cbfc498e86d21d1f8939672f32acaffe9e5be8924da7942ce69fd34e2c68",
            "sh", "-c", "tar -xf - && exec elixir read.exs",
        ], input=payload.getvalue(), capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(result.stdout.decode().splitlines(), [
            "BLOCKSCOUT_DEPLOY_CHECK=READ_ONLY_OK", "BLOCKSCOUT_DEPLOY_CHECK=UNAVAILABLE_OK"
        ])


if __name__ == "__main__":
    unittest.main()
