#!/usr/bin/env python3
"""Execute the exact candidate eval expressions against source-derived OTP/config fixtures."""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile

ROOT=Path(__file__).resolve().parents[2]
SPEC=importlib.util.spec_from_file_location('candidate',ROOT/'deploy/scripts/candidate_build.py')
B=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(B)
IMAGE='elixir@sha256:0ff6cbfc498e86d21d1f8939672f32acaffe9e5be8924da7942ce69fd34e2c68'
FILES=['config/runtime.exs','config/config_helper.exs','apps/utils/lib/utils/config_helper.ex',
 'apps/indexer/lib/indexer/fetcher.ex','apps/indexer/lib/indexer/temporary/uncataloged_token_transfers.ex',
 'apps/indexer/lib/indexer/block/fetcher/receipt_fallback.ex','apps/explorer/lib/explorer/chain.ex',
 'apps/ethereum_jsonrpc/lib/ethereum_jsonrpc/utility/ranges_helper.ex',
 'apps/indexer/test/indexer/temporary/uncataloged_token_transfers_config_test.exs']
SCRIPT='''
ExUnit.start()
defmodule Extract do
 def functions(path,names) do
  {:defmodule,_,[_,[do: {:__block__,_,body}]]}=path |> File.read!() |> Code.string_to_quoted!()
  Enum.filter(body,fn
   {kind,_,[{name,_,_},_]} when kind in [:def,:defp] -> name in names
   _ -> false
  end)
 end
end
Code.require_file("apps/utils/lib/utils/config_helper.ex")
helpers=Extract.functions("config/config_helper.exs",[:safe_get_env,:parse_bool_env_var,:parse_integer_env_var,:parse_integer_or_nil_env_var,:parse_time_env_var])
Module.create(ConfigHelper,quote do
 alias Utils.ConfigHelper
 unquote_splicing(helpers)
end,Macro.Env.location(__ENV__))
Code.require_file("apps/indexer/lib/indexer/fetcher.ex")
worker=Extract.functions("apps/indexer/lib/indexer/temporary/uncataloged_token_transfers.ex",[:child_spec,:start_link])
Module.create(Indexer.Temporary.UncatalogedTokenTransfers,quote do
 use GenServer
 use Indexer.Fetcher
 unquote_splicing(worker)
 def init(opts),do: {:ok,opts}
end,Macro.Env.location(__ENV__))
Code.require_file("apps/indexer/lib/indexer/block/fetcher/receipt_fallback.ex")
Code.require_file("apps/ethereum_jsonrpc/lib/ethereum_jsonrpc/utility/ranges_helper.ex")
wrapper=Extract.functions("apps/explorer/lib/explorer/chain.ex",[:fetch_transaction_revert_reason])
Module.create(Explorer.Chain,quote do
 alias EthereumJSONRPC.Utility.RangesHelper
 unquote_splicing(wrapper)
 defp do_fetch_transaction_revert_reason(_),do: raise("unexpected historical RPC in candidate check")
end,Macro.Env.location(__ENV__))
Code.require_file("apps/indexer/test/indexer/temporary/uncataloged_token_transfers_config_test.exs")
{:__block__,_,expressions}="config/runtime.exs" |> File.read!() |> Code.string_to_quoted!()
System.put_env("CHAIN_ID","18417507517")
System.put_env("CACHE_TXS_COUNT_APPROXIMATE","true")
System.put_env("INDEXER_CONTRACT_CODE_RETRY_DELAY","60s")
System.put_env("INDEXER_CONTRACT_CODE_CONCURRENCY","1")
System.put_env("TRACE_BLOCK_RANGES","176114083..latest")
chain=Enum.find_value(expressions,fn
 {:config,_,[:block_scout_web,opts]} -> Keyword.get(opts,:chain_id)
 _ -> nil
end)
{chain_id,_}=Code.eval_quoted(chain)
Application.put_env(:block_scout_web,:chain_id,chain_id)
counter=Enum.find_value(expressions,fn
 {:config,_,[:explorer,{:__aliases__,_,[:Explorer,:Chain,:Cache,:Counters,:TransactionsCount]},opts]} -> opts
 _ -> nil
end)
{counter_config,_}=Code.eval_quoted(counter)
Application.put_env(:explorer,Explorer.Chain.Cache.Counters.TransactionsCount,counter_config)
trace_names=[:trace_first_block,:trace_last_block,:trace_block_ranges]
assignments=Enum.filter(expressions,fn
 {:=,_,[{name,_,_},_]} -> name in trace_names
 _ -> false
end)
{_,binding}=Code.eval_quoted({:__block__,[],assignments})
indexer=Enum.find_value(expressions,fn
 {:config,_,[:indexer,opts]} -> if Keyword.has_key?(opts,:contract_code_retry_delay),do: opts
 _ -> nil
end)
for key <- [:contract_code_retry_delay,:contract_code_concurrency,:trace_block_ranges] do
 {value,_}=Code.eval_quoted(Keyword.fetch!(indexer,key),binding)
 Application.put_env(:indexer,key,value)
end
repair=Enum.find_value(expressions,fn
 {:config,_,[:indexer,{:__aliases__,_,[:Indexer,:Temporary,:UncatalogedTokenTransfers,:Supervisor]},opts]} -> opts
 _ -> nil
end)
'''


def main():
 script=SCRIPT
 for mode in ('unset','false','true'):
  script+=('System.delete_env("'+B.FLAG+'")\n' if mode=='unset' else 'System.put_env("'+B.FLAG+'","'+mode+'")\n')
  script+='''{config,_}=Code.eval_quoted(repair)
Application.put_env(:indexer,Indexer.Temporary.UncatalogedTokenTransfers.Supervisor,config)
'''
  script+='Code.eval_string('+json.dumps(B.smoke_expression(mode))+')\n'
 script+='IO.puts("PASS: three exact candidate eval expressions with source-derived config/native supervisor")\n'
 data=io.BytesIO()
 with tarfile.open(fileobj=data,mode='w') as archive:
  for name in FILES:
   raw=(ROOT/name).read_bytes();entry=tarfile.TarInfo(name);entry.size=len(raw);archive.addfile(entry,io.BytesIO(raw))
  raw=script.encode();entry=tarfile.TarInfo('candidate-smoke-fixture.exs');entry.size=len(raw);archive.addfile(entry,io.BytesIO(raw))
 result=subprocess.run(['docker','run','--rm','-i','--network','none','--read-only',
  '--tmpfs','/work:rw,exec,size=64m','--workdir','/work','--cpus','2','--memory','256m',
  '-e','ERL_FLAGS=+S 2:2',IMAGE,'sh','-c','tar -xf - && exec elixir candidate-smoke-fixture.exs'],
  input=data.getvalue(),capture_output=True,timeout=60)
 print(result.stdout.decode(errors='replace'),end='')
 if result.returncode:print(result.stderr.decode(errors='replace')[-5000:],end='')
 return result.returncode


if __name__=='__main__':raise SystemExit(main())
