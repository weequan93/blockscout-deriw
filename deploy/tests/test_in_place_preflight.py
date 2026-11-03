import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("in_place_preflight", ROOT / "deploy/scripts/in_place_preflight.py")
P = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(P)


class PreflightTests(unittest.TestCase):
    def fixture(self):
        return {
            "server": [[150019, "/var/lib/postgresql/data", False, 1024, "172.28.0.2", 5432]],
            "settings": [[key, "off" if key == "archive_mode" else "1", None] for key in P.SETTINGS],
            "tablespaces": [], "replication_slots": [[0, 0]], "relations": [["internal_transactions", -1, 8192]],
            "repository_routes": [{"repository": "Explorer.Repo", "shares_main_configuration": True}],
        }

    def test_longest_matching_mount_covers_nested_wal(self):
        mounts = [
            {"Type": "bind", "Source": "/data/pg", "Destination": "/pg"},
            {"Type": "bind", "Source": "/wal/pg", "Destination": "/pg/pg_wal"},
        ]
        self.assertEqual(P.mount_for("/pg/base", mounts)["host_path"], "/data/pg/base")
        self.assertEqual(P.mount_for("/pg/pg_wal", mounts)["host_path"], "/wal/pg")

    def test_path_prefix_is_not_a_mount_boundary(self):
        result = P.mount_for("/pg-other/data", [{"Type": "bind", "Source": "/data/pg", "Destination": "/pg"}])
        self.assertEqual(result["status"], "unmapped_container_storage")

    def test_volume_and_unmapped_or_tmpfs_paths_are_explicit(self):
        self.assertEqual(P.mount_for("/pg", [{"Type": "volume", "Source": "/volumes/pg", "Destination": "/pg"}])["mount_type"], "volume")
        self.assertEqual(P.mount_for("/pg", [{"Type": "tmpfs", "Destination": "/pg"}])["status"], "unsupported_storage_type")
        for path in ("relative", "/pg/../private", "/pg/\x00bad"):
            with self.assertRaises(ValueError):
                P.mount_for(path, [])

    def test_sql_projection_withholds_route_addresses_and_extra_private_values(self):
        data = self.fixture()
        data["password"] = "private-fixture-value"
        data["repository_routes"][0]["url"] = "private-fixture-value"
        public, connection, tablespaces = P.safe_database(data)
        self.assertEqual(connection, ("172.28.0.2", 5432))
        self.assertEqual(tablespaces, [])
        self.assertNotIn("172.28.0.2", json.dumps(public))
        self.assertNotIn("private-fixture-value", json.dumps(public))

    def test_only_reviewed_setting_names_and_simple_values_are_exported(self):
        data = self.fixture()
        data["settings"][0] = ["archive_command", "private-fixture-value", None]
        with self.assertRaises(ValueError):
            P.safe_database(data)
        data = self.fixture()
        data["settings"][0][1] = "https://private.invalid/?token=value"
        with self.assertRaises(ValueError):
            P.safe_database(data)

    def test_tablespace_and_server_limits_are_checked(self):
        data = self.fixture()
        data["tablespaces"] = [[1, "/tablespace"]] * 33
        with self.assertRaises(ValueError):
            P.safe_database(data)
        data = self.fixture()
        data["server"][0][0] = 160000
        with self.assertRaises(ValueError):
            P.safe_database(data)

    def test_direct_database_route_is_verified_before_reading_container_paths(self):
        backend = {"NetworkSettings": {"Networks": {"shared": {"IPAddress": "172.28.0.3"}}}}
        db = {"NetworkSettings": {"Networks": {"shared": {"IPAddress": "172.28.0.4"}}}}
        checker = SimpleNamespace(marked_json=Mock(return_value=self.fixture()))
        def require(value, message):
            if not value:
                raise ValueError(message)
        api = SimpleNamespace(
            load_tool=Mock(return_value=checker), configuration=Mock(return_value=({}, {}, {})),
            inspect_project=Mock(return_value={"backend": backend, "db": db}), run=Mock(return_value="withheld"),
            require=require,
        )
        with self.assertRaises(P.PreflightError) as caught:
            P.report(api, ROOT)
        self.assertEqual(caught.exception.stage, "database_route")
        api.run.assert_called_once()

    def test_rpc_diagnostics_export_only_fixed_stage_and_error_labels(self):
        checker = SimpleNamespace(marked_json=Mock(return_value={
            "status": "unavailable", "stage": "wal_settings", "error_kind": "permission_denied",
            "message": "private-fixture-value",
        }))
        api = SimpleNamespace(
            load_tool=Mock(return_value=checker), configuration=Mock(return_value=({}, {}, {})),
            inspect_project=Mock(return_value={}), run=Mock(return_value="withheld"),
        )
        with self.assertRaises(P.PreflightError) as caught:
            P.report(api, ROOT)
        self.assertEqual(caught.exception.public(), {"stage": "wal_settings", "error_kind": "permission_denied"})
        self.assertNotIn("private-fixture-value", str(caught.exception))
        api.run.assert_called_once()
        rejected = P.PreflightError("private-fixture-value", "private-fixture-value")
        self.assertEqual(rejected.public(), {"stage": "backend_rpc", "error_kind": "check_failed"})

    def test_command_failure_withholds_raw_error_and_identifies_failed_stage(self):
        api = SimpleNamespace(
            load_tool=Mock(), configuration=Mock(return_value=({}, {}, {})),
            inspect_project=Mock(side_effect=RuntimeError("private-fixture-value")),
        )
        with self.assertRaises(P.PreflightError) as caught:
            P.report(api, ROOT)
        self.assertEqual(caught.exception.public(), {"stage": "inspect_project", "error_kind": "check_failed"})
        self.assertNotIn("private-fixture-value", str(caught.exception))

    def test_full_storage_report_deduplicates_devices_and_never_confirms_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            wal_dir = Path(directory) / "wal"
            data_dir.mkdir()
            wal_dir.mkdir()
            backend = {"NetworkSettings": {"Networks": {"shared": {"IPAddress": "172.28.0.3"}}}}
            db = {"Id": "b" * 64, "Image": "sha256:" + "a" * 64,
                  "NetworkSettings": {"Networks": {"shared": {"IPAddress": "172.28.0.2"}}},
                  "Mounts": [
                      {"Type": "bind", "Destination": "/var/lib/postgresql/data", "Source": str(data_dir)},
                      {"Type": "bind", "Destination": "/wal", "Source": str(wal_dir)},
                  ], "Config": {"Env": ["PASSWORD=private-fixture-value"]}}
            def command(args, **_kwargs):
                if args[:3] == ["docker", "image", "inspect"]:
                    return json.dumps([{"Architecture": "amd64", "Os": "linux"}])
                if "readlink" in args:
                    return "/wal\n" if args[-1].endswith("pg_wal") else "/var/lib/postgresql/data\n"
                return "withheld"
            def require(value, message):
                if not value:
                    raise ValueError(message)
            checker = SimpleNamespace(marked_json=Mock(return_value=self.fixture()))
            api = SimpleNamespace(
                load_tool=Mock(return_value=checker), configuration=Mock(return_value=({}, {}, {})),
                inspect_project=Mock(return_value={"backend": backend, "db": db}), run=Mock(side_effect=command),
                require=require, ensure_stable=Mock(), lifecycle_summary=Mock(return_value={"status": "running"}),
            )
            report = P.report(api, ROOT)
            self.assertEqual([row["status"] for row in report["storage"]], ["mapped", "mapped"])
            self.assertEqual(len(report["filesystem_devices"]), 1)
            self.assertFalse(report["snapshot_verified"])
            self.assertFalse(report["deployment_permitted"])
            self.assertNotIn("private-fixture-value", json.dumps(report))
            api.ensure_stable.assert_called_once()


class ElixirFailureTests(unittest.TestCase):
    def test_exact_rpc_expression_reports_sql_stage_without_private_error_fields(self):
        script = '''
defmodule Postgrex.Error do
  defexception [:postgres, :message]
end
defmodule Explorer.Repo do
  def transaction(fun, opts) do
    [timeout: 5000] = opts
    {:ok, fun.()}
  end
end
defmodule Ecto.Adapters.SQL do
  def query!(Explorer.Repo, query, _params, opts) do
    [timeout: 2000, log: false] = opts
    if String.contains?(query, Process.get(:fail_query)) do
      raise Postgrex.Error, postgres: %{code: Process.get(:fail_code), detail: "private-fixture-value"},
        message: "private-fixture-value"
    end
    %{rows: []}
  end
end
defmodule Jason do
  def encode!(%{status: "unavailable", stage: stage, error_kind: kind}), do: stage <> ":" <> kind
end
Process.register(self(), Explorer.Repo)
'''
        for query, code in (
            ("data_directory", "insufficient_privilege"),
            ("pg_settings", "query_canceled"),
            ("pg_tablespace", "undefined_column"),
        ):
            script += '\nProcess.put(:fail_query, ' + json.dumps(query) + ')\n'
            script += 'Process.put(:fail_code, :' + code + ')\n' + P.expression()
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
            "BLOCKSCOUT_DEPLOY_CHECK=server_metadata:permission_denied",
            "BLOCKSCOUT_DEPLOY_CHECK=wal_settings:statement_timeout_or_cancel",
            "BLOCKSCOUT_DEPLOY_CHECK=tablespace_metadata:unsupported_database_schema",
        ])
        self.assertNotIn("private-fixture-value", result.stdout.decode())


if __name__ == "__main__":
    unittest.main()
