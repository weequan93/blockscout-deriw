#!/usr/bin/env python3
"""Run focused ExUnit checks against an already compiled Arbitrum builder image.

The image must be built from this checkout; this is a local validation tool, not
a release receipt. No application, database, network, or host mounts are used.
"""
import argparse
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[2]
TESTS = [
    "apps/ethereum_jsonrpc/test/ethereum_jsonrpc/trace_range_dispatch_test.exs",
    "apps/explorer/test/explorer/chain/transaction/history_guard_test.exs",
    "apps/explorer/test/explorer/market/native_coin_price_only_test.exs",
    "apps/indexer/test/indexer/block/fetcher/receipt_fallback_test.exs",
    "apps/indexer/test/indexer/temporary/uncataloged_token_transfers_config_test.exs",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="deriw-blockscout:v10-upgrade-compile")
    parser.add_argument("--runtime-image", help="Also load release config in the final runtime image using synthetic inputs")
    args = parser.parse_args()
    script = '''
Code.require_file("config/config_helper.exs")
ExUnit.start(autorun: false)
'''
    for test in TESTS:
        script += 'Code.require_file("' + test + '")\n'
    script += "result = ExUnit.run()\nSystem.halt(if result.failures == 0, do: 0, else: 1)\n"
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        for name in TESTS + ["config/runtime.exs", "config/config_helper.exs"]:
            data = (ROOT / name).read_bytes()
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
        data = script.encode()
        entry = tarfile.TarInfo("check.exs")
        entry.size = len(data)
        archive.addfile(entry, io.BytesIO(data))
    result = subprocess.run([
        "docker", "run", "--rm", "--pull", "never", "-i", "--network", "none", "--read-only",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--cpus", "2", "--memory", "512m",
        "--tmpfs", "/work:rw,exec,size=32m", "--workdir", "/work", "-e", "ERL_FLAGS=+S 2:2",
        "--entrypoint", "sh", args.image, "-c",
        'tar -xf - && exec elixir -pa "/app/_build/prod/lib/*/ebin" check.exs',
    ], input=payload.getvalue(), capture_output=True, timeout=90)
    print(result.stdout.decode(errors="replace"), end="")
    if result.returncode:
        print(result.stderr.decode(errors="replace")[-6000:], end="")
    if result.returncode == 0 and args.runtime_image:
        spec = importlib.util.spec_from_file_location("upgrade_candidate", ROOT / "deploy/scripts/candidate_build.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        environment = {
            **builder.market_policy().ENVIRONMENT,
            "CHAIN_ID": "18417507517", "CHAIN_TYPE": "arbitrum", "RELEASE_VERSION": "10.2.6",
            "SECRET_KEY_BASE": "a" * 64,
            "DATABASE_URL": "postgresql://fixture:fixture@127.0.0.1:1/fixture",
            "ETHEREUM_JSONRPC_HTTP_URL": "http://127.0.0.1:1",
            "ETHEREUM_JSONRPC_RECEIPTS_BY_BLOCK": "false",
            "CACHE_TXS_COUNT_APPROXIMATE": "true",
            "INDEXER_CONTRACT_CODE_RETRY_DELAY": "60s", "INDEXER_CONTRACT_CODE_CONCURRENCY": "1",
            "TRACE_BLOCK_RANGES": "176114083..latest",
            "RELEASE_DISTRIBUTION": "none", "RELEASE_TMP": "/tmp",
        }
        for mode in ("unset", "false", "true"):
            values = dict(environment)
            if mode != "unset":
                values[builder.FLAG] = mode
            result = subprocess.run([
                "docker", "run", "--rm", "--pull", "never", "-i", "--network", "none", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--cpus", "2", "--memory", "512m",
                "--tmpfs", "/tmp:rw,mode=1777", "--entrypoint", "sh", args.runtime_image, "-c", builder.smoke_script(mode),
            ], input=json.dumps(values).encode(), capture_output=True, timeout=90)
            if result.returncode or builder.MARKER not in result.stdout.decode().splitlines():
                print(result.stdout.decode(errors="replace")[-3000:], end="")
                print(result.stderr.decode(errors="replace")[-6000:], end="")
                return 1
            print("Release config passed with historical scanner " + mode)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
