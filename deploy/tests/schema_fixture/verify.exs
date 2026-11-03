[[1]] =
  q.(
    "SELECT count(*) FROM transactions WHERE hash = decode(repeat('a',64),'hex') AND method_id = decode('a9059cbb','hex')"
  ).rows

# Both metadata reset migrations execute, with unrelated partial/false rows retained.
[[1, nil], [2, nil], [3, true], [4, false]] =
  q.("SELECT get_byte(contract_address_hash,19), skip_metadata FROM tokens ORDER BY contract_address_hash").rows

[[5, nil, 0, 0], [6, nil, 0, 0], [7, "eip1967", 1, 1]] =
  q.(
    "SELECT get_byte(proxy_address_hash,19), proxy_type::text, cardinality(address_hashes), cardinality(names) FROM proxy_implementations ORDER BY proxy_address_hash"
  ).rows

[[true, true, false, false]] =
  q.(
    "SELECT EXISTS(SELECT 1 FROM pg_enum WHERE enumlabel='eip1967_oz'), EXISTS(SELECT 1 FROM pg_enum WHERE enumlabel='eip1967_beacon'), EXISTS(SELECT 1 FROM pg_enum WHERE enumlabel='eip930'), EXISTS(SELECT 1 FROM pg_enum WHERE enumlabel='unknown')"
  ).rows

[["numeric", "78", "0"]] =
  q.(
    "SELECT data_type, numeric_precision::text, numeric_scale::text FROM information_schema.columns WHERE table_schema='public' AND table_name='signed_authorizations' AND column_name='chain_id'"
  ).rows

q.("UPDATE signed_authorizations SET chain_id=1000000000000000000000000000001")
[["1000000000000000000000000000001"]] = q.("SELECT chain_id::text FROM signed_authorizations").rows

# The replacement uniqueness index must retain and protect existing ABIs.
[[1]] = q.("SELECT count(*) FROM contract_methods WHERE identifier=16909060").rows

[[true, true]] =
  q.(
    "SELECT indisvalid, indisunique FROM pg_index WHERE indexrelid='contract_methods_identifier_md5_abi_index'::regclass"
  ).rows

try do
  q.(
    "INSERT INTO contract_methods(identifier, abi, type, inserted_at, updated_at) SELECT identifier, abi, type, NOW(), NOW() FROM contract_methods WHERE identifier=16909060"
  )

  raise "Duplicate ABI unexpectedly accepted"
rescue
  error in Postgrex.Error -> :unique_violation = error.postgres.code
end

# Deliberate data/schema removals and background resets match the upstream files.
[[nil]] = q.("SELECT to_regclass('public.account_public_tags_requests')::text").rows

[[nil, "internal_transactions_delete_queue"]] =
  q.(
    "SELECT to_regclass('public.internal_transactions_zero_value_delete_queue')::text, to_regclass('public.internal_transactions_delete_queue')::text"
  ).rows

[["sanitize_incorrect_nft", "started", "delete_erc_1155"]] =
  q.(
    "SELECT migration_name, status, meta->>'step' FROM migrations_status WHERE migration_name IN ('sanitize_duplicated_log_index_logs','sanitize_incorrect_nft','token_transfers_block_consensus')"
  ).rows

[[false], [false]] =
  q.(
    "SELECT convalidated FROM pg_constraint WHERE conname IN ('internal_transactions_block_number_not_null','internal_transactions_transaction_index_not_null') ORDER BY conname"
  ).rows

[[true]] =
  q.(
    "SELECT EXISTS(SELECT 1 FROM pg_enum e JOIN pg_type t ON t.oid=e.enumtypid WHERE t.typname='arbitrum_da_containers_types' AND e.enumlabel='in_eigenda')"
  ).rows

expected_custom_indexes = if defer_indexes?, do: 0, else: 6

[[^expected_custom_indexes]] =
  q.(
    "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid WHERE i.indisvalid AND i.indisready AND c.relname IN ('blocks_consensus_number_timestamp_hash_idx','blocks_empty_sanitizer_hash_number_idx','transactions_status_created_contract_code_indexed_at_index','transactions_consensus_timestamp_stats_idx','transactions_consensus_block_number_gas_used_idx','logs_token_transfer_topics_block_number_cover_idx')"
  ).rows
