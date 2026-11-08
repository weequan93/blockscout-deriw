#!/usr/bin/env python3
"""Check the compiled sanitizer worker and exact-query fallback on local PG15."""
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
    name = 'empty-blocks-hold-' + uuid.uuid4().hex[:16]
    command = U.client_command(image, network, name, token)
    command[2:2] = ['--label', G.LABEL + '=' + token]
    try:
        locker.stdin.write(b'BEGIN; LOCK TABLE public.blocks IN ACCESS EXCLUSIVE MODE;\n')
        locker.stdin.flush()
        deadline = time.monotonic() + 5
        while sql("SELECT count(*) FROM pg_locks WHERE relation='blocks'::regclass AND mode='AccessExclusiveLock' AND granted").strip() != '1':
            assert time.monotonic() < deadline and locker.poll() is None
            time.sleep(0.1)
        source = (ROOT / 'deploy/scripts/v10/empty_blocks_hold.exs').read_text() + r'''
true = Explorer.Migrator.StartupControl.disabled?()
for app <- [:crypto,:ssl,:postgrex,:ecto_sql,:con_cache], do: {:ok,_} = Application.ensure_all_started(app)
{:ok,_} = Explorer.Repo.start_link(pool_size: 8)
{:ok,_} = ConCache.start_link(name: :background_migrations_status,ttl_check_interval: false)
ConCache.put(:background_migrations_status,:transactions_denormalization_finished,true)
{:ok,_} = ConCache.start_link(name: :block_number,ttl_check_interval: false)
Explorer.Chain.Cache.BlockNumber.set_max(2000)
Application.put_env(:indexer,Indexer.Fetcher.EmptyBlocksSanitizer.Supervisor,disabled?: false)
Application.put_env(:indexer,Indexer.Fetcher.EmptyBlocksSanitizer,batch_size: 10,head_offset: 1000,interval: 60000)
{:ok,_} = Supervisor.start_link([{Agent,fn -> :unchanged end},{Indexer.Fetcher.EmptyBlocksSanitizer.Supervisor,[[json_rpc_named_arguments: []]]}],strategy: :one_for_one,name: Indexer.Supervisor)
{Agent,sibling,_,_} = Enum.find(Supervisor.which_children(Indexer.Supervisor),&(elem(&1,0)==Agent))
task_supervisor = Process.whereis(Indexer.Fetcher.EmptyBlocksSanitizer.TaskSupervisor)
true = is_pid(task_supervisor)
%{query_md5: "f7d49ed68ea1ac040431775d295eaae5",query_bytes: 310} = BlockscoutV10EmptyBlocksHold.query_fingerprint()
[[address]] = Explorer.Repo.query!("SELECT host(inet_client_addr())",[],log: false).rows
base = %{"pid"=>nil,"query_start"=>nil,"backend_start"=>nil,"query_md5"=>"f7d49ed68ea1ac040431775d295eaae5","backend_client_addresses"=>[address]}
{select_sql,select_args} = BlockscoutV10EmptyBlocksHold.query_template()
start_query = fn -> Task.async(fn -> Explorer.Repo.query(select_sql,select_args,log: false,timeout: :infinity) end) end
find_target = fn ->
  Enum.reduce_while(1..50,nil,fn _,_ ->
    result = BlockscoutV10EmptyBlocksHold.run(Map.put(base,"mode","observe"))
    case result.current_sanitizer_queries do
      [selected] -> {:halt,Map.merge(base,selected)}
      [] -> Process.sleep(50); {:cont,nil}
    end
  end)
end
before = Explorer.Repo.query!("SELECT to_jsonb(s) FROM migrations_status s ORDER BY migration_name",[],log: false).rows
# A same-template SELECT deliberately outlives the worker, exercising the
# narrowly pinned fallback cancellation after the real owner has been held.
first = start_query.()
payload = find_target.()
true = is_map(payload)
[[unrelated_pid,unrelated_start,unrelated_backend]] = Explorer.Repo.query!("SELECT a.pid,a.query_start::text,a.backend_start::text FROM pg_stat_activity a JOIN pg_locks l ON l.pid=a.pid WHERE l.relation='blocks'::regclass AND l.mode='AccessExclusiveLock' AND l.granted",[],log: false).rows
stale = Map.merge(payload,%{"pid"=>unrelated_pid,"query_start"=>unrelated_start,"backend_start"=>unrelated_backend,"mode"=>"hold"})
held_stale = BlockscoutV10EmptyBlocksHold.run(stale)
true = held_stale.sanitizer_worker_held and not held_stale.cancellation_requested
[[1]] = Explorer.Repo.query!("SELECT count(*) FROM pg_locks WHERE pid=$1 AND relation='blocks'::regclass AND mode='AccessExclusiveLock' AND granted",[unrelated_pid],log: false).rows
held = BlockscoutV10EmptyBlocksHold.run(Map.put(payload,"mode","hold"))
true = held.sanitizer_worker_held and held.cancellation_accepted and not held.original_query_active
{:error,_} = Task.await(first,5000)
true = Process.alive?(sibling) and Process.whereis(Indexer.Fetcher.EmptyBlocksSanitizer.TaskSupervisor) == task_supervisor
:unchanged = Agent.get(sibling,& &1)
again = BlockscoutV10EmptyBlocksHold.run(Map.put(payload,"mode","hold"))
true = again.sanitizer_worker_held and not again.cancellation_requested
restored = BlockscoutV10EmptyBlocksHold.run(Map.put(payload,"mode","restore"))
true = restored.sanitizer_worker_running and restored.other_indexer_children_preserved
worker_only = BlockscoutV10EmptyBlocksHold.run(Map.put(base,"mode","hold"))
true = worker_only.sanitizer_worker_held and not worker_only.cancellation_requested
# A known query on a connection outside the frozen backend address set must
# not be discovered or cancelled, even with the other target fields correct.
second = start_query.()
second_payload = find_target.()
wrong_address = Map.merge(second_payload,%{"backend_client_addresses"=>["192.0.2.123"],"mode"=>"hold"})
foreign = BlockscoutV10EmptyBlocksHold.run(wrong_address)
false = foreign.cancellation_requested
[] = foreign.current_sanitizer_queries
true = foreign.original_query_active
released = BlockscoutV10EmptyBlocksHold.run(Map.put(second_payload,"mode","hold"))
true = released.cancellation_accepted and not released.original_query_active
{:error,_} = Task.await(second,5000)
true = before == Explorer.Repo.query!("SELECT to_jsonb(s) FROM migrations_status s ORDER BY migration_name",[],log: false).rows
true = Process.alive?(sibling) and Process.whereis(Indexer.Fetcher.EmptyBlocksSanitizer.TaskSupervisor) == task_supervisor
nil = Process.whereis(Explorer.Supervisor)
IO.puts("BLOCKSCOUT_DEPLOY_CHECK=" <> Jason.encode!(%{status: "passed",actual_compiled_sanitizer_worker: true,observed_310_byte_query_fingerprint_matched: true,hold_idempotency_and_restore_checked: true,pinned_outliving_select_cancelled: true,stale_target_and_unrelated_lock_preserved: true,foreign_connection_not_cancelled: true,root_and_inner_siblings_preserved: true,migration_records_unchanged: true,fixture_select_executed_in_separate_task: true,application_restart: false,historical_rpc_used: false,live_services_accessed: false}))
'''
        result = subprocess.run(command, input=U.archive(environment, {}, source.encode()), capture_output=True, timeout=75)
        (directory / 'empty-blocks-hold.log').write_bytes(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError('Empty-block sanitizer fixture failed: ' + (result.stdout + result.stderr).decode(errors='replace')[-4500:])
        return C.marked_json(D, result.stdout.decode())
    finally:
        G.cleanup_container(name, token)
        if locker.poll() is None:
            locker.stdin.write(b'ROLLBACK;\n\\q\n')
            locker.stdin.flush()
            locker.communicate(timeout=10)


if __name__ == '__main__':
    raise SystemExit(F.main(pilot_check=check))
