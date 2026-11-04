#!/usr/bin/env python3
"""Exercise the actual metrics worker's reversible hold against disposable PG15."""
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import check_v10_normalization_plan as F
import check_upgrade_database as G
import deploy_check as C
import dev_config as D
import v10_upgrade as U


def check(image, network, token, environment, sql, pg, directory):
    locker = subprocess.Popen(['docker', 'exec', '-i', pg, 'psql', '-U', 'fixture', '-d', 'fixture', '-v', 'ON_ERROR_STOP=1', '-At'],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    name = 'metrics-hold-' + uuid.uuid4().hex[:16]
    command = U.client_command(image, network, name, token)
    command[2:2] = ['--label', G.LABEL + '=' + token]
    try:
        locker.stdin.write(b'BEGIN; LOCK TABLE public.blocks IN ACCESS EXCLUSIVE MODE;\n')
        locker.stdin.flush()
        deadline = time.monotonic() + 5
        while sql("SELECT count(*) FROM pg_locks WHERE relation='blocks'::regclass AND mode='AccessExclusiveLock' AND granted").strip() != '1':
            assert time.monotonic() < deadline and locker.poll() is None
            time.sleep(0.1)
        source = (ROOT / 'deploy/scripts/v10/metrics_hold.exs').read_text() + r'''
true = Explorer.Migrator.StartupControl.disabled?()
for app <- [:crypto,:ssl,:postgrex,:ecto_sql,:prometheus_ex], do: {:ok,_} = Application.ensure_all_started(app)
{:ok,_} = Explorer.Repo.start_link(pool_size: 8)
Application.put_env(:indexer,:block_ranges,"0..latest")
Application.put_env(:indexer,Indexer.Prometheus.Metrics,enabled: true,specific_metrics_enabled?: %{})
Indexer.Prometheus.Instrumenter.setup()
{:ok,_} = Supervisor.start_link([{Agent,fn -> :unchanged end},{Indexer.Prometheus.Metrics,[]}],strategy: :one_for_one,name: BlockScoutWeb.Supervisor)
{Agent,sibling,_,_} = Enum.find(Supervisor.which_children(BlockScoutWeb.Supervisor),&(elem(&1,0)==Agent))
find_target = fn ->
  Enum.reduce_while(1..50,nil,fn _,_ ->
    rows = Explorer.Repo.query!("SELECT pid,query_start::text,backend_start::text FROM pg_stat_activity WHERE md5(query)=$1 AND pid<>pg_backend_pid() AND state='active'",["d63aea58d81cc7d2d9c7ad5cd82809dd"],log: false).rows
    case rows do
      [[pid,start,backend]] -> {:halt,%{"pid"=>pid,"query_start"=>start,"backend_start"=>backend,"query_md5"=>"d63aea58d81cc7d2d9c7ad5cd82809dd"}}
      [] -> Process.sleep(50); {:cont,nil}
    end
  end)
end
payload = find_target.()
true = is_map(payload)
before = Explorer.Repo.query!("SELECT to_jsonb(s) FROM migrations_status s ORDER BY migration_name",[],log: false).rows
# An identity mismatch must not stop the owner or signal a database query.
try do
  BlockscoutV10MetricsHold.run(Map.merge(payload,%{"mode"=>"hold","query_start"=>"2000-01-01 00:00:00+00"}))
  raise "mismatched_target_was_accepted"
rescue
  _ in MatchError -> :ok
end
true = is_pid(Process.whereis(Indexer.Prometheus.Metrics))
held = BlockscoutV10MetricsHold.run(Map.put(payload,"mode","hold"))
true = held.metrics_worker_held and not held.original_query_active and held.other_web_children_preserved
true = Process.alive?(sibling)
:unchanged = Agent.get(sibling,& &1)
again = BlockscoutV10MetricsHold.run(Map.put(payload,"mode","hold"))
true = again.metrics_worker_held and not again.original_query_active
restored = BlockscoutV10MetricsHold.run(Map.put(payload,"mode","restore"))
true = restored.metrics_worker_running and restored.other_web_children_preserved
second = find_target.()
true = is_map(second)
held_again = BlockscoutV10MetricsHold.run(Map.put(second,"mode","hold"))
true = held_again.metrics_worker_held and not held_again.original_query_active
true = Process.alive?(sibling)
true = before == Explorer.Repo.query!("SELECT to_jsonb(s) FROM migrations_status s ORDER BY migration_name",[],log: false).rows
nil = Process.whereis(Explorer.Supervisor)
IO.puts("BLOCKSCOUT_DEPLOY_CHECK=" <> Jason.encode!(%{status: "passed",actual_compiled_metrics_worker: true,known_missing_blocks_query_observed: true,wrong_query_identity_rejected: true,hold_and_idempotent_hold_checked: true,restore_and_second_hold_checked: true,unrelated_sibling_preserved: true,original_query_released: true,migration_records_unchanged: true,application_restart: false,live_services_accessed: false}))
'''
        result = subprocess.run(command, input=U.archive(environment, {}, source.encode()), capture_output=True, timeout=75)
        (directory / 'metrics-hold.log').write_bytes(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError('Metrics fixture failed: ' + (result.stdout + result.stderr).decode(errors='replace')[-4500:])
        return C.marked_json(D, result.stdout.decode())
    finally:
        G.cleanup_container(name, token)
        if locker.poll() is None:
            locker.stdin.write(b'ROLLBACK;\n\\q\n')
            locker.stdin.flush()
            locker.communicate(timeout=10)


if __name__ == '__main__':
    raise SystemExit(F.main(pilot_check=check))
