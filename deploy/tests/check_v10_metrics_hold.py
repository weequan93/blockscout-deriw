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
inspection = BlockscoutV10MetricsHold.run(%{"mode"=>"observe","pid"=>nil,"query_start"=>nil,"backend_start"=>nil,"query_md5"=>"d63aea58d81cc7d2d9c7ad5cd82809dd"})
[selected] = inspection.current_metric_queries
true = selected == payload
before = Explorer.Repo.query!("SELECT to_jsonb(s) FROM migrations_status s ORDER BY migration_name",[],log: false).rows
# A stale target may now be an unrelated query. Hold the known collector, but
# leave that database session and its unrelated lock entirely alone.
[[unrelated_pid,unrelated_start,unrelated_backend]] = Explorer.Repo.query!("SELECT a.pid,a.query_start::text,a.backend_start::text FROM pg_stat_activity a JOIN pg_locks l ON l.pid=a.pid WHERE l.relation='blocks'::regclass AND l.mode='AccessExclusiveLock' AND l.granted",[],log: false).rows
stale = Map.merge(payload,%{"pid"=>unrelated_pid,"query_start"=>unrelated_start,"backend_start"=>unrelated_backend,"mode"=>"hold"})
stale_hold = BlockscoutV10MetricsHold.run(stale)
true = stale_hold.metrics_worker_held and not stale_hold.cancellation_requested and not stale_hold.cancellation_eligible_at_check
[[1]] = Explorer.Repo.query!("SELECT count(*) FROM pg_locks WHERE pid=$1 AND relation='blocks'::regclass AND mode='AccessExclusiveLock' AND granted",[unrelated_pid],log: false).rows
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
collector_only = BlockscoutV10MetricsHold.run(%{"mode"=>"hold","pid"=>nil,"query_start"=>nil,"backend_start"=>nil,"query_md5"=>"d63aea58d81cc7d2d9c7ad5cd82809dd"})
true = collector_only.metrics_worker_held and not collector_only.query_target_provided and not collector_only.cancellation_requested
# If owner shutdown released the query, no signal is needed at all. Otherwise
# the exact original target can still be reconciled by an explicit pinned hold.
held_again = BlockscoutV10MetricsHold.run(Map.put(second,"mode","hold"))
true = held_again.metrics_worker_held and not held_again.original_query_active
true = Process.alive?(sibling)
true = before == Explorer.Repo.query!("SELECT to_jsonb(s) FROM migrations_status s ORDER BY migration_name",[],log: false).rows
nil = Process.whereis(Explorer.Supervisor)
IO.puts("BLOCKSCOUT_DEPLOY_CHECK=" <> Jason.encode!(%{status: "passed",actual_compiled_metrics_worker: true,known_missing_blocks_query_observed: true,current_query_fingerprint_discovery_checked: true,stale_query_not_cancelled: true,unrelated_database_lock_preserved: true,collector_only_hold_checked: true,hold_and_idempotent_hold_checked: true,restore_and_second_hold_checked: true,unrelated_sibling_preserved: true,original_query_released: true,migration_records_unchanged: true,application_restart: false,live_services_accessed: false}))
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
