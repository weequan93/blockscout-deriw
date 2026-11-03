INSERT INTO addresses(hash, inserted_at, updated_at)
SELECT decode(lpad(to_hex(n), 40, '0'), 'hex'), NOW(), NOW()
FROM generate_series(1, 7) AS n;

INSERT INTO tokens(contract_address_hash, type, cataloged, name, symbol, decimals, skip_metadata, inserted_at, updated_at)
VALUES
  (decode(lpad('1',40,'0'),'hex'), 'ERC-20', true, NULL, NULL, NULL, true, NOW(), NOW()),
  (decode(lpad('2',40,'0'),'hex'), 'ERC-20', true, 'Synthetic', 'SYN', 18, true, NOW(), NOW()),
  (decode(lpad('3',40,'0'),'hex'), 'ERC-20', true, 'Partial', NULL, NULL, true, NOW(), NOW()),
  (decode(lpad('4',40,'0'),'hex'), 'ERC-20', true, NULL, NULL, NULL, false, NOW(), NOW());

INSERT INTO transactions(hash, gas, gas_price, input, nonce, r, s, v, value, from_address_hash, inserted_at, updated_at)
VALUES (decode(repeat('a',64),'hex'), 21000, 1, decode('a9059cbb','hex'), 0, 1, 1, 27, 0,
  decode(lpad('1',40,'0'),'hex'), NOW(), NOW());

INSERT INTO signed_authorizations(transaction_hash, "index", chain_id, address, nonce, v, r, s, inserted_at, updated_at)
VALUES (decode(repeat('a',64),'hex'), 0, 18417507517, decode(lpad('1',40,'0'),'hex'), 0, 0, 1, 1, NOW(), NOW());

INSERT INTO contract_methods(identifier, abi, type, inserted_at, updated_at)
VALUES (16909060, '{"type":"function","name":"syntheticFixture","inputs":[],"outputs":[]}'::jsonb,
  'function', NOW(), NOW());

INSERT INTO proxy_implementations(proxy_address_hash, address_hashes, names, proxy_type, inserted_at, updated_at)
VALUES
  (decode(lpad('5',40,'0'),'hex'), ARRAY[decode(lpad('1',40,'0'),'hex')], ARRAY['Synthetic'], 'eip930', NOW(), NOW()),
  (decode(lpad('6',40,'0'),'hex'), ARRAY[decode(lpad('1',40,'0'),'hex')], ARRAY['Synthetic'], 'unknown', NOW(), NOW()),
  (decode(lpad('7',40,'0'),'hex'), ARRAY[decode(lpad('1',40,'0'),'hex')], ARRAY['Synthetic'], 'eip1967', NOW(), NOW());

INSERT INTO migrations_status(migration_name, status, inserted_at, updated_at)
VALUES
  ('sanitize_duplicated_log_index_logs', 'completed', NOW(), NOW()),
  ('sanitize_incorrect_nft', 'completed', NOW(), NOW()),
  ('token_transfers_block_consensus', 'completed', NOW(), NOW());

INSERT INTO blocks(hash, number, consensus, miner_hash, nonce, parent_hash, gas_limit, gas_used, size, timestamp, inserted_at, updated_at)
SELECT decode(lpad(to_hex(n),64,'0'),'hex'), CASE WHEN n=21 THEN 20 ELSE n END, n!=21,
  decode(lpad('1',40,'0'),'hex'), decode('0000000000000000','hex'), decode(lpad(to_hex(1000+n),64,'0'),'hex'),
  30000000, 100, 1, NOW(), NOW(), NOW()
FROM unnest(ARRAY[10,11,12,20,21]) AS n;

INSERT INTO transactions(hash, gas, gas_price, input, nonce, r, s, v, value, from_address_hash,
  block_hash, block_number, block_consensus, block_timestamp, "index", cumulative_gas_used, gas_used, status,
  inserted_at, updated_at)
SELECT decode(lpad(to_hex(100+n),64,'0'),'hex'), 21000, 1, decode('a9059cbb','hex'), n, 1, 1, 27, 0,
  decode(lpad('1',40,'0'),'hex'), decode(lpad(to_hex(n),64,'0'),'hex'), CASE WHEN n=21 THEN 20 ELSE n END,
  n!=21, NOW(), 0, 100, 100, 1, NOW(), NOW()
FROM unnest(ARRAY[10,11,12,20,21]) AS n;

INSERT INTO internal_transactions(block_hash, block_index, block_number, transaction_index, transaction_hash, "index",
  type, call_type, trace_address, value, gas, gas_used, input, output, error, init,
  created_contract_address_hash, created_contract_code, from_address_hash, to_address_hash, inserted_at, updated_at)
SELECT decode(lpad(to_hex(n),64,'0'),'hex'), 0, CASE WHEN n=21 THEN 20 ELSE n END, 0,
  decode(lpad(to_hex(100+n),64,'0'),'hex'), 1,
  CASE WHEN n=12 THEN 'create' ELSE 'call' END,
  CASE WHEN n=12 THEN NULL ELSE 'call' END,
  ARRAY[0], CASE WHEN n=11 THEN 7 ELSE 0 END, 21000, CASE WHEN n=11 THEN NULL ELSE 100 END,
  decode('abcd','hex'), CASE WHEN n=11 THEN NULL ELSE decode('','hex') END,
  CASE WHEN n=11 THEN 'synthetic revert' ELSE NULL END, CASE WHEN n=12 THEN decode('00','hex') ELSE NULL END,
  CASE WHEN n=12 THEN decode(lpad('4',40,'0'),'hex') ELSE NULL END,
  CASE WHEN n=12 THEN decode('6000','hex') ELSE NULL END,
  decode(lpad('1',40,'0'),'hex'), CASE WHEN n=12 THEN NULL ELSE decode(lpad('2',40,'0'),'hex') END,
  NOW(), NOW()
FROM unnest(ARRAY[10,11,12,20,21]) AS n;
