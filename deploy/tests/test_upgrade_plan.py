import importlib.util
import io
import json
from pathlib import Path
from copy import deepcopy
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
        script += '\nCode.string_to_quoted!(' + json.dumps(U.history_expression()) + ')\n'
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


class HistoryEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.data = {
            "relation": [[-1, 8192, 16384]],
            "indexes": [["internal_transactions_pkey", True, True, True, True, ["block_hash", "block_index"], False]],
            "statuses": [[U.HISTORY_WORKERS[0], "completed"]],
            "runtime": {"trace_block_ranges": "176114083..latest", "internal_fetcher_disabled": True,
                        "historical_scanner_disabled": True},
        }

    def test_estimates_and_missing_statuses_remain_explicit(self):
        result = U.safe_history(self.data)
        self.assertEqual(result["internal_transactions"]["estimated_rows"], -1)
        self.assertEqual(result["indexes"][0]["key_columns"], ["block_hash", "block_index"])
        self.assertEqual(result["background_migrations"][0]["status"], "completed")
        self.assertEqual(result["background_migrations"][1]["status"], "not_recorded")
        self.assertEqual(result["duplicate_presence"], "not_measured")

    def test_private_or_unexpected_runtime_and_status_values_are_withheld(self):
        self.data["runtime"] = {"trace_block_ranges": "private-fixture-value", "password": "private-fixture-value"}
        self.data["statuses"][0][1] = "private-fixture-value"
        result = U.safe_history(self.data)
        self.assertNotIn("private-fixture-value", json.dumps(result))
        self.assertIsNone(result["runtime"]["internal_fetcher_disabled"])

    def test_broken_sizes_unknown_states_and_raw_index_expressions_are_rejected(self):
        for mutate in (
            lambda d: d.update(relation=[]),
            lambda d: d.update(relation=[[0, -1, 1]]),
            lambda d: d.update(statuses=[["unknown", "completed"]]),
            lambda d: d["indexes"][0].__setitem__(5, ["private expression (secret)"]),
        ):
            data = deepcopy(self.data)
            mutate(data)
            with self.assertRaises(ValueError):
                U.safe_history(data)

    def test_live_history_read_is_single_existing_repo_rpc_with_stability_check(self):
        checker = SimpleNamespace(marked_json=Mock(return_value=self.data))
        rows = {"backend": {"Image": "sha256:" + "a" * 64}}
        api = SimpleNamespace(
            load_tool=Mock(return_value=checker), configuration=Mock(return_value=({"chain_id": "18417507517"}, {}, {})),
            inspect_project=Mock(return_value=rows), ensure_stable=Mock(),
            lifecycle_summary=Mock(return_value={"status": "running"}), run=Mock(return_value="suppressed"),
        )
        result = U.history_report(ROOT, api)
        self.assertFalse(result["deployment_permitted"])
        api.run.assert_called_once()
        api.ensure_stable.assert_called_once()
        command = api.run.call_args.args[0]
        self.assertEqual(command[:5], ["docker", "exec", "backend", "/app/bin/blockscout", "rpc"])
        self.assertIn("SET TRANSACTION READ ONLY", command[5])
        self.assertIn("statement_timeout = '1500ms'", command[5])
        self.assertNotIn("GROUP BY", command[5])


if __name__ == "__main__":
    unittest.main()
