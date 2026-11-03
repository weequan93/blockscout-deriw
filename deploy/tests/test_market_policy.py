"""Reviewed market configuration must not permit arbitrary deployment drift."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from unittest.mock import Mock

ROOT=Path(__file__).resolve().parents[2]
def load(name):
 spec=importlib.util.spec_from_file_location(name,ROOT/'deploy/scripts'/(name+'.py'))
 module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module);return module

T=load('dev_config');P=load('market_policy');B=load('candidate_build');C=load('deploy_check')


class MarketPolicyTests(unittest.TestCase):
 def test_exact_eth_profile_and_original_disabled_profile_are_supported(self):
  P.validate(T,P.ENVIRONMENT)
  P.validate(T,{'DISABLE_MARKET':'true','COIN':'','COIN_NAME':''})
 def test_incomplete_or_changed_profile_is_refused(self):
  with self.assertRaises(T.ConfigError):P.validate(T,{'DISABLE_MARKET':'false','COIN':'ETH'})
  for key in P.ENVIRONMENT:
   for replacement in ('unexpected',None):
    values=dict(P.ENVIRONMENT)
    if replacement is None:values.pop(key)
    else:values[key]=replacement
    with self.subTest(key=key,replacement=replacement),self.assertRaises(T.ConfigError):P.validate(T,values)
 def test_preflight_permits_only_reviewed_market_fields(self):
  report={'changed_runtime_fields':sorted(P.FIELDS),'live_declared_environment_drift':[],
   'live_images_differing_from_capture':[]}
  B.approved_preflight(T,report)
  for field in ('services.db.command','services.backend.environment.DATABASE_URL',
   'services.backend.environment.MARKET_COINGECKO_BASE_URL','services.backend.environment.TRACE_BLOCK_RANGES'):
   changed=deepcopy(report);changed['changed_runtime_fields'].append(field)
   with self.subTest(field=field),self.assertRaises(T.ConfigError):B.approved_preflight(T,changed)
 def test_market_changes_do_not_mask_unrelated_image_defaults(self):
  baseline={'Config':{'Env':['COIN=','COIN_NAME=','DISABLE_MARKET=true','DATABASE_URL=fixture']}}
  candidate={'Config':{'Env':['DATABASE_URL=another-fixture']}}
  service={'environment':dict(P.ENVIRONMENT)}
  result=C.inherited_image_changes(T,baseline,candidate,service,baseline)
  self.assertEqual(result['unexpected_environment_keys'],['DATABASE_URL'])
  service['environment']['MARKET_COINGECKO_COIN_ID']='another-asset'
  with self.assertRaises(T.ConfigError):C.inherited_image_changes(T,baseline,candidate,service,baseline)
 def test_provider_probe_is_bounded_and_accepts_only_a_positive_eth_usd_quote(self):
  import json
  good={'id':'ethereum','market_data':{'current_price':{'usd':2250.77}}}
  with patch.object(T,'run',return_value=json.dumps(good)) as call:
   self.assertEqual(P.provider_check(T,P.ENVIRONMENT)['status'],'available')
  command=call.call_args.args[0]
  self.assertEqual(command[:3],['docker','exec','backend'])
  self.assertIn('--max-filesize',command);self.assertIn('--max-time',command)
  self.assertNotIn('-k',command);self.assertNotIn('-L',command)
  for body in ('{}','not json',json.dumps(dict(good,id='another-asset')),
   json.dumps({'id':'ethereum','market_data':{'current_price':{'usd':0}}})):
   with patch.object(T,'run',return_value=body):
    self.assertEqual(P.provider_check(T,P.ENVIRONMENT)['status'],'unavailable')
  with patch.object(T,'run') as call:
   self.assertEqual(P.provider_check(T,{})['status'],'not_requested');call.assert_not_called()
 def test_eth_price_check_never_activates_and_apply_requires_verified_candidate(self):
  from pathlib import Path
  import contextlib, io
  model={'services':{'backend':{'environment':dict(P.ENVIRONMENT)}}}
  builder=Mock();checker=Mock();releases=Mock()
  args=(T,builder,checker,releases,ROOT,{}, {}, {},Path('/private.json'),Path('/compose'))
  with patch.object(T,'managed_model',return_value=model):
   builder.build_candidate.return_value={'status':'candidate_build_plan_ready'}
   self.assertEqual(P.enable(*args,check_only=True)['status'],'eth_price_plan_ready')
   releases.deploy.assert_not_called()
   with self.assertRaises(T.ConfigError):P.enable(*args)
   releases.deploy.assert_not_called()
   builder.build_candidate.return_value={'status':'candidate_verified','receipt':'/candidate.json'}
   releases.deploy.return_value={'status':'verified'}
   with contextlib.redirect_stdout(io.StringIO()):result=P.enable(*args)
   self.assertEqual(result['status'],'verified')
   self.assertEqual(releases.deploy.call_args.args[-2:],(Path('/candidate.json'),Path('/compose')))
   self.assertTrue(releases.deploy.call_args.kwargs['apply'])


if __name__=='__main__':unittest.main()
