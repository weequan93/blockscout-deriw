"""The reviewed dev ETH price profile. No provider credentials are public inputs."""
from decimal import Decimal, InvalidOperation
import json

ENVIRONMENT = {
    'COIN': 'ETH',
    'COIN_NAME': 'Ether',
    'DISABLE_MARKET': 'false',
    'MARKET_COIN_FETCHER_ENABLED': 'true',
    'MARKET_COINGECKO_COIN_ID': 'ethereum',
    'MARKET_HISTORY_FETCHER_ENABLED': 'true',
    'MARKET_HISTORY_FIRST_FETCH_DAY_COUNT': '2',
    'MARKET_NATIVE_COIN_HISTORY_SOURCE': 'coin_gecko',
    'MARKET_NATIVE_COIN_PRICE_ONLY': 'true',
    'MARKET_NATIVE_COIN_SOURCE': 'coin_gecko',
    'MARKET_TOKENS_FETCHER_ENABLED': 'false',
}
FIELDS = frozenset('services.backend.environment.' + key for key in ENVIRONMENT)


def validate(api, environment):
    # The original captured profile is still supported for rollback/review.
    if not any(key.startswith('MARKET_') and key in environment for key in ENVIRONMENT):
        api.require(environment.get('DISABLE_MARKET') in (None, 'true')
                    and environment.get('COIN') in (None, '') and environment.get('COIN_NAME') in (None, ''),
                    'Enabling dev market data requires the complete reviewed ETH price-only profile')
        return
    api.require(all(environment.get(key) == value for key, value in ENVIRONMENT.items()),
                'Dev market settings differ from the reviewed ETH price-only profile')


def provider_check(api, environment):
    if environment.get('MARKET_NATIVE_COIN_PRICE_ONLY') != 'true':
        return {'status': 'not_requested'}
    validate(api, environment)
    url = ('https://api.coingecko.com/api/v3/coins/ethereum?localization=false&tickers=false'
           '&market_data=true&community_data=false&developer_data=false&sparkline=false')
    try:
        body = api.run(['docker', 'exec', 'backend', 'curl', '-q', '--noproxy', '*', '-fsS',
                        '--proto', '=https', '--connect-timeout', '3', '--max-time', '10',
                        '--max-filesize', '2097152', url], timeout=15)
        data = json.loads(body)
        price = Decimal(str(data['market_data']['current_price']['usd']))
        if data['id'] != 'ethereum' or not price.is_finite() or price <= 0:
            raise ValueError('invalid_price')
    except (api.ConfigError, KeyError, TypeError, ValueError, InvalidOperation):
        return {'status': 'unavailable', 'asset_id': 'ethereum', 'currency': 'usd',
                'note': 'No valid unauthenticated quote from the fixed HTTPS provider endpoint; raw response suppressed.'}
    return {'status': 'available', 'asset_id': 'ethereum', 'currency': 'usd',
            'note': 'One bounded public HTTPS read from existing backend; no service change.'}


def enable(api, builder, checker, releases, root, manifest, requirements, values, private_path,
           live_directory, check_only=False, reuse=False):
    environment = api.managed_model(root, manifest, values)['services']['backend'].get('environment') or {}
    api.require(all(environment.get(key) == value for key, value in ENVIRONMENT.items()),
                'eth-price requires the committed ETH price-only dev profile')
    api.require(not (check_only and reuse), '--reuse applies only to eth-price --apply')
    candidate = builder.build_candidate(api, root, manifest, requirements, values, private_path,
                                        live_directory, api.MANAGED_RELEASES, check_only=check_only, reuse=reuse)
    if check_only:
        return {**candidate, 'status': 'eth_price_plan_ready',
                'effect': 'Apply builds and validates the committed candidate, then recreates only backend without startup migrations.'}
    api.require(candidate.get('status') == 'candidate_verified', 'ETH candidate is not verified; no activation requested')
    print(json.dumps(candidate), flush=True)
    from pathlib import Path
    return releases.deploy(api, builder, checker, root, manifest, requirements, values, private_path,
                           Path(candidate['receipt']), live_directory, apply=True)


def smoke_expression():
    # Conditional so previous managed releases keep their original checks.
    return '''
if System.get_env("MARKET_NATIVE_COIN_PRICE_ONLY") == "true" do
  true = Explorer.Market.native_coin_price_only?();
  "ETH" = Application.get_env(:explorer, :coin);
  "Ether" = Application.get_env(:explorer, :coin_name);
  Explorer.Market.Source.CoinGecko = Explorer.Market.Source.native_coin_source();
  Explorer.Market.Source.CoinGecko = Explorer.Market.Source.native_coin_price_history_source();
  nil = Explorer.Market.Source.market_cap_history_source();
  "ethereum" = Application.get_env(:explorer, Explorer.Market.Source.CoinGecko)[:coin_id];
  true = Application.get_env(:explorer, Explorer.Market.Fetcher.Coin)[:enabled];
  false = Application.get_env(:explorer, Explorer.Market.Fetcher.Token)[:enabled];
  true = Application.get_env(:explorer, Explorer.Market.Fetcher.History)[:enabled];
  2 = Application.get_env(:explorer, Explorer.Market.Fetcher.History)[:first_fetch_day_count];
  coin = %{Explorer.Market.Token.null() | fiat_value: Decimal.new("123"),
    total_supply: Decimal.new("100"), available_supply: Decimal.new("99"),
    market_cap: Decimal.new("12300"), volume_24h: Decimal.new("10")};
  %{fiat_value: price, total_supply: nil, available_supply: nil, market_cap: nil,
    volume_24h: nil} = Explorer.Market.apply_native_coin_policy(coin);
  true = Decimal.equal?(price, Decimal.new("123"));
end;
'''
