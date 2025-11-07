defmodule BlockScoutWeb.API.V2.AddressController do
  use BlockScoutWeb, :controller
  use Utils.CompileTimeEnvHelper, chain_type: [:explorer, :chain_type]
  use Utils.RuntimeEnvHelper, chain_type: [:explorer, :chain_type]
  use OpenApiSpex.ControllerSpecs

  import BlockScoutWeb.Chain,
    only: [
      next_page_params: 3,
      next_page_params: 4,
      token_transfers_next_page_params: 3,
      paging_options: 1,
      split_list_by_page: 1,
      current_filter: 1,
      paging_params_with_fiat_value: 1,
      fetch_scam_token_toggle: 2
    ]

  import BlockScoutWeb.PagingHelper,
    only: [
      addresses_sorting: 1,
      delete_parameters_from_next_page_params: 1,
      token_transfers_types_options: 1,
      address_transactions_sorting: 1,
      nft_types_options: 1
    ]

  import Explorer.Helper, only: [safe_parse_non_negative_integer: 1]

  import Explorer.MicroserviceInterfaces.BENS, only: [maybe_preload_ens: 1, maybe_preload_ens_to_address: 1]
  import Explorer.MicroserviceInterfaces.Metadata, only: [maybe_preload_metadata: 1]

  alias BlockScoutWeb.AccessHelper
  alias BlockScoutWeb.API.V2.{BlockView, TransactionView, WithdrawalView}
  alias Explorer.{Chain, Market, PagingOptions}
  alias Explorer.Chain.{Address, Hash, InternalTransaction, Transaction}
  alias Explorer.Chain.Address.{CoinBalance, Counters}

  alias Explorer.Chain.Token.Instance
  alias Explorer.SmartContract.Helper, as: SmartContractHelper

  alias BlockScoutWeb.API.V2.CeloView
  alias Explorer.Chain.Celo.ElectionReward, as: CeloElectionReward

  alias Indexer.Fetcher.OnDemand.CoinBalance, as: CoinBalanceOnDemand
  alias Indexer.Fetcher.OnDemand.ContractCode, as: ContractCodeOnDemand
  alias Indexer.Fetcher.OnDemand.TokenBalance, as: TokenBalanceOnDemand

  case @chain_type do
    :celo ->
      @chain_type_transaction_necessity_by_association %{
        :gas_token => :optional
      }

    _ ->
      @chain_type_transaction_necessity_by_association %{}
  end

  @transaction_necessity_by_association [
    necessity_by_association:
      %{
        [created_contract_address: [:scam_badge, :names, :smart_contract, proxy_implementations_association()]] =>
          :optional,
        [from_address: [:scam_badge, :names, :smart_contract, proxy_implementations_association()]] => :optional,
        [to_address: [:scam_badge, :names, :smart_contract, proxy_implementations_association()]] => :optional,
        :block => :optional
      }
      |> Map.merge(@chain_type_transaction_necessity_by_association),
    api?: true
  ]

  @token_transfer_necessity_by_association [
    necessity_by_association: %{
      [to_address: [:scam_badge, :names, :smart_contract, proxy_implementations_association()]] => :optional,
      [from_address: [:scam_badge, :names, :smart_contract, proxy_implementations_association()]] => :optional,
      :block => :optional,
      :transaction => :optional,
      :token => :optional
    },
    api?: true
  ]

  @address_options [
    necessity_by_association: %{
      :names => :optional,
      :scam_badge => :optional,
      :token => :optional,
      :signed_authorization => :optional,
      :smart_contract => :optional,
      # Add comprehensive proxy implementations preloading - remove invalid nested associations
      :proxy_implementations => :optional,
      # Preload contract creation transaction associations
      [contract_creation_transaction: [:from_address, :to_address, :created_contract_address]] => :optional
    },
    api?: true
  ]

  @nft_necessity_by_association [
    necessity_by_association: %{
      :token => :optional
    }
  ]

  @api_true [api?: true]

  @celo_election_rewards_options [
    necessity_by_association: %{
      [
        account_address: [
          :names,
          :smart_contract,
          proxy_implementations_association()
        ]
      ] => :optional,
      [
        associated_account_address: [
          :names,
          :smart_contract,
          proxy_implementations_association()
        ]
      ] => :optional,
      [epoch: [:end_processing_block]] => :optional
    },
    api?: true
  ]

  @spec contract_address_preloads() :: [keyword()]
  defp contract_address_preloads do
    chain_type_associations =
      case chain_type() do
        :filecoin -> Address.contract_creation_transaction_with_from_address_associations()
        _ -> Address.contract_creation_transaction_associations()
      end

    [:smart_contract | chain_type_associations]
  end

  action_fallback(BlockScoutWeb.API.V2.FallbackController)

  plug(OpenApiSpex.Plug.CastAndValidate, json_render_error_v2: true)

  tags ["addresses"]

  operation :address,
    summary: "Retrieve detailed information about a specific address or contract",
    description:
      "Retrieves detailed information for a specific address, including balance, transaction count, and metadata.",
    parameters: [address_hash_param() | base_params()],
    responses: [
      ok: {"Detailed information about the specified address.", "application/json", Schemas.Address.Response},
      unprocessable_entity: JsonErrorResponse.response(),
      forbidden: ForbiddenResponse.response()
    ]

  @doc """
  Function to handle GET requests to `/api/v2/addresses/:address_hash_param` endpoint.
  Returns 200 on any valid address_hash, even if the address is not found in the database.
  """
  @spec address(Plug.Conn.t(), map()) :: {:format, :error} | {:restricted_access, true} | Plug.Conn.t()
  def address(conn, %{address_hash_param: address_hash_string} = params) do
    require Logger
    Logger.error("=== ADDRESS ENDPOINT START: #{address_hash_string} ===")

    ip = AccessHelper.conn_to_ip_string(conn)

    with {:ok, address_hash} <- validate_address_hash(address_hash_string, params) do
      # Start comprehensive query monitoring with process-specific handler
      start_time = System.monotonic_time(:millisecond)
      current_pid = self()  # Capture current process PID

      # Create ETS tables with process-specific names to avoid conflicts
      pid_string = inspect(current_pid)
      query_count_name = String.to_atom("query_counter_#{pid_string}")
      slow_queries_name = String.to_atom("slow_queries_#{pid_string}")

      query_count = :ets.new(query_count_name, [:set, :public, :named_table])
      :ets.insert(query_count, {:count, 0})
      slow_queries = :ets.new(slow_queries_name, [:bag, :public, :named_table])

      # Use process PID for truly unique handler ID
      handler_id = "query_monitor_#{pid_string}_#{System.unique_integer()}"

      # Store table references and process info in process dictionary for cleanup
      Process.put(:query_monitoring_tables, {query_count, slow_queries})
      Process.put(:query_monitoring_active, true)
      Process.put(:monitoring_pid, current_pid)

      :telemetry.attach(
        handler_id,
        [:explorer, :repo, :query],
        fn _event, measurements, metadata, _config ->
          # ONLY monitor queries from the original address request process
          query_pid = metadata[:pid] || self()

          if query_pid == current_pid and Process.get(:query_monitoring_active) == true do
            case Process.get(:query_monitoring_tables) do
              {qc_table, sq_table} ->
                try do
                  # Safely access ETS tables with existence check
                  case :ets.whereis(qc_table) do
                    :undefined -> :ok
                    _ ->
                      :ets.update_counter(qc_table, :count, 1)

                      case :ets.lookup(qc_table, :count) do
                        [{:count, current_count}] ->
                          query_time_ms = measurements.total_time / 1_000_000
                          elapsed_ms = System.monotonic_time(:millisecond) - start_time

                          # Log ALL queries during render phase (after query 100) and slow queries
                          is_in_render_phase = current_count > 100
                          is_slow = query_time_ms > 100  # Lowered threshold to catch more

                          if is_in_render_phase or is_slow or rem(current_count, 25) == 0 do
                            # Get more detail about the query
                            query_source = metadata[:source] || "unknown"
                            query_full = metadata.query |> to_string()

                            # Extract table names from the query
                            table_pattern = ~r/FROM\s+"?(\w+)"?/i
                            tables = Regex.scan(table_pattern, query_full) |> Enum.map(fn [_, table] -> table end)

                            query_preview = query_full
                                          |> String.replace(~r/\s+/, " ")
                                          |> String.slice(0, 200)

                            phase = cond do
                              current_count <= 20 -> "LOOKUP"
                              current_count <= 50 -> "PRELOAD"
                              current_count <= 80 -> "PROXY"
                              current_count <= 100 -> "ENS"
                              true -> "RENDER"
                            end

                            Logger.error("[#{phase}-#{current_count}] +#{elapsed_ms}ms (#{Float.round(query_time_ms, 1)}ms) Tables: #{inspect(tables)} | #{query_preview}...")
                          end

                          # Store ALL queries in render phase for detailed analysis
                          if is_in_render_phase and :ets.whereis(sq_table) != :undefined do
                            query_source = metadata[:source] || "unknown"
                            :ets.insert(sq_table, {current_count, query_time_ms, metadata.query, elapsed_ms, query_source})
                          end

                        [] -> :ok
                      end
                  end
                rescue
                  _ -> :ok
                catch
                  _ -> :ok
                end

              _ -> :ok
            end
          end
        end,
        nil
      )

      try do
        Logger.error("Step 1: Looking up address in database...")
        step_start = System.monotonic_time(:millisecond)

        result = case Chain.hash_to_address(address_hash, @address_options) do
          {:ok, address} ->
            step_time = System.monotonic_time(:millisecond) - step_start
            queries_after_lookup = safe_get_query_count_by_name(query_count_name)
            Logger.error("Step 1 completed in #{step_time}ms with #{queries_after_lookup} queries")

            Logger.error("Step 2: Preloading smart contract associations...")
            step_start = System.monotonic_time(:millisecond)

            fully_preloaded_address = try do
              Logger.error("Step 2a: Starting Address.maybe_preload_smart_contract_associations...")
              preload_result = Address.maybe_preload_smart_contract_associations(address, contract_address_preloads(), @api_true)
              Logger.error("Step 2a: Completed Address.maybe_preload_smart_contract_associations")

              Logger.error("Step 2b: Starting preload_proxy_implementations_efficiently...")
              final_result = preload_proxy_implementations_efficiently(preload_result)
              Logger.error("Step 2b: Completed preload_proxy_implementations_efficiently")

              final_result
            rescue
              error ->
                Logger.error("ERROR in Step 2: #{inspect(error)}")
                Logger.error("ERROR stacktrace: #{inspect(__STACKTRACE__)}")

                # Return the original address with minimal processing to continue
                %{address | proxy_implementations: []}
            catch
              :exit, reason ->
                Logger.error("EXIT in Step 2: #{inspect(reason)}")
                %{address | proxy_implementations: []}

              :throw, value ->
                Logger.error("THROW in Step 2: #{inspect(value)}")
                %{address | proxy_implementations: []}
            end

            step_time = System.monotonic_time(:millisecond) - step_start
            queries_after_preload = safe_get_query_count_by_name(query_count_name)
            Logger.error("Step 2 completed in #{step_time}ms with #{queries_after_preload - queries_after_lookup} new queries")

            Logger.error("Step 3: Getting proxy implementations...")
            step_start = System.monotonic_time(:millisecond)

            _implementations = fully_preloaded_address.proxy_implementations || []

            step_time = System.monotonic_time(:millisecond) - step_start
            queries_after_impl = safe_get_query_count_by_name(query_count_name)
            Logger.error("Step 3 completed in #{step_time}ms with #{queries_after_impl - queries_after_preload} new queries")

            Logger.error("Step 4: Starting background fetchers...")
            spawn_link(fn ->
              CoinBalanceOnDemand.trigger_fetch(ip, address)
              ContractCodeOnDemand.trigger_fetch(ip, fully_preloaded_address)
            end)

            Logger.error("Step 5: ENS preloading...")
            step_start = System.monotonic_time(:millisecond)

            final_address = case Application.get_env(:block_scout_web, :ens_enabled, false) do
              true -> maybe_preload_ens_to_address(fully_preloaded_address)
              false -> fully_preloaded_address
            end

            step_time = System.monotonic_time(:millisecond) - step_start
            queries_after_ens = safe_get_query_count_by_name(query_count_name)
            Logger.error("Step 5 completed in #{step_time}ms with #{queries_after_ens - queries_after_impl} new queries")

            Logger.error("Step 6: Starting view rendering...")
            step_start = System.monotonic_time(:millisecond)

            # Try a more aggressive approach - render with minimal preloading first
            render_task = Task.async(fn ->
              # Inherit monitoring context for the render task
              Process.put(:query_monitoring_active, true)
              Process.put(:monitoring_pid, current_pid)
              Process.put(:query_monitoring_tables, {query_count, slow_queries})

              # Create a minimal address version for faster rendering
              # Ensure proxy_implementations is properly structured or empty
              safe_proxy_implementations = case final_address.proxy_implementations do
                nil -> []
                [] -> []
                implementations when is_list(implementations) ->
                  # Take only the first 3 and ensure they have the required fields
                  implementations
                  |> Enum.take(3)
                  |> Enum.filter(fn impl ->
                    is_map(impl) and Map.has_key?(impl, :proxy_type)
                  end)
                _ -> []
              end

              Logger.error("render_task: Using #{length(safe_proxy_implementations)} proxy implementations")

              minimal_address = %{final_address |
                proxy_implementations: safe_proxy_implementations,
                contract_creation_transaction: nil  # Skip this expensive association
              }

              conn
              |> put_status(200)
              |> render(:address, %{address: minimal_address})
            end)

            render_result = case Task.yield(render_task, 8_000) do  # Reduced timeout to 8 seconds
              {:ok, result} ->
                Logger.error("View rendering succeeded")
                result
              nil ->
                Task.shutdown(render_task, :brutal_kill)  # Force kill the task
                Logger.error("View rendering timed out after 8 seconds - returning minimal response")

                queries_at_timeout = safe_get_query_count_by_name(query_count_name)
                Logger.error("Queries executed before timeout: #{queries_at_timeout}")

                # Get the render-phase queries for analysis
                render_queries = safe_get_slow_queries_by_name(slow_queries_name)
                render_phase_queries = Enum.filter(render_queries, fn {query_num, _, _, _, _} -> query_num > 100 end)

                Logger.error("=== RENDER PHASE QUERIES (causing timeout) ===")
                Logger.error("Total render queries: #{length(render_phase_queries)}")

                # Group queries by pattern to find the worst offenders
                query_patterns = render_phase_queries
                |> Enum.map(fn {query_num, time_ms, query, elapsed_ms, _source} ->
                  # Extract table and operation pattern
                  query_str = to_string(query)
                  pattern = cond do
                    String.contains?(query_str, "proxy_implementations") -> "PROXY_IMPL_QUERY"
                    String.contains?(query_str, "smart_contracts") -> "SMART_CONTRACT_QUERY"
                    String.contains?(query_str, "addresses") and String.contains?(query_str, "JOIN") -> "ADDRESS_JOIN_QUERY"
                    String.contains?(query_str, "contract_creation_transaction") -> "CONTRACT_CREATION_QUERY"
                    String.contains?(query_str, "SELECT") and String.contains?(query_str, "WHERE") -> "BASIC_SELECT"
                    true -> "OTHER_QUERY"
                  end
                  {pattern, query_num, time_ms, elapsed_ms, String.slice(query_str, 0, 300)}
                end)
                |> Enum.group_by(fn {pattern, _, _, _, _} -> pattern end)

                # Log summary of query patterns
                Enum.each(query_patterns, fn {pattern, queries} ->
                  count = length(queries)
                  total_time = Enum.sum(Enum.map(queries, fn {_, _, time_ms, _, _} -> time_ms end))
                  Logger.error("#{pattern}: #{count} queries, #{Float.round(total_time, 1)}ms total")

                  # Show worst offenders for each pattern
                  worst_queries = Enum.sort_by(queries, fn {_, _, time_ms, _, _} -> time_ms end, :desc) |> Enum.take(3)
                  Enum.each(worst_queries, fn {_, query_num, time_ms, elapsed_ms, query_preview} ->
                    Logger.error("  #{query_num}: #{Float.round(time_ms, 1)}ms at +#{elapsed_ms}ms: #{query_preview}...")
                  end)
                end)

                # Return a more comprehensive minimal response
                conn
                |> put_status(200)
                |> json(%{
                  hash: to_string(address_hash),
                  fetched_coin_balance: final_address.fetched_coin_balance || "0",
                  fetched_coin_balance_block_number: final_address.fetched_coin_balance_block_number,
                  is_contract: !is_nil(final_address.smart_contract),
                  is_verified: case final_address.smart_contract do
                    nil -> nil
                    sc -> !is_nil(sc.abi)
                  end,
                  name: case final_address.names do
                    [] -> nil
                    [first | _] -> first.name
                    _ -> nil
                  end,
                  implementation_name: case final_address.smart_contract do
                    nil -> nil
                    sc -> sc.name
                  end,
                  proxy_type: case final_address.proxy_implementations do
                    [] -> nil
                    [first | _] -> first.proxy_type
                    _ -> nil
                  end,
                  creator_address_hash: case final_address.contract_creation_transaction do
                    nil -> nil
                    tx -> tx.from_address_hash
                  end,
                  creation_tx_hash: case final_address.contract_creation_transaction do
                    nil -> nil
                    tx -> tx.hash
                  end,
                  token: final_address.token,
                  coin_balance: final_address.fetched_coin_balance,
                  exchange_rate: nil,
                  message: "Partial address data - view rendering timeout after 8s, #{queries_at_timeout} queries executed"
                })
            end

            step_time = System.monotonic_time(:millisecond) - step_start
            total_queries = safe_get_query_count_by_name(query_count_name)
            queries_during_render = total_queries - queries_after_ens
            Logger.error("Step 6 completed in #{step_time}ms with #{queries_during_render} new queries")

            # Summary - more concise
            total_time = System.monotonic_time(:millisecond) - start_time
            Logger.error("=== SUMMARY for #{address_hash_string} ===")
            Logger.error("Total: #{total_time}ms, #{total_queries} queries")
            Logger.error("Breakdown: lookup(#{queries_after_lookup}) + preload(#{queries_after_preload - queries_after_lookup}) + impl(#{queries_after_impl - queries_after_preload}) + ens(#{queries_after_ens - queries_after_impl}) + render(#{queries_during_render})")

            # Log slow queries - only if any exist
            slow_query_list = safe_get_slow_queries_by_name(slow_queries_name)
            if length(slow_query_list) > 0 do
              Logger.error("=== #{length(slow_query_list)} SLOW QUERIES (>500ms) ===")
              Enum.take(slow_query_list, 5)  # Only show first 5 slow queries
              |> Enum.each(fn {query_num, time_ms, query} ->
                query_preview = query |> inspect() |> String.slice(0, 150) |> String.replace(~r/\s+/, " ")
                Logger.error("Slow ##{query_num}: #{Float.round(time_ms, 1)}ms - #{query_preview}...")
              end)
              if length(slow_query_list) > 5 do
                Logger.error("... and #{length(slow_query_list) - 5} more slow queries")
              end
            end

            render_result

          _ ->
            address =
              %Address{
                hash: address_hash,
                names: [],
                scam_badge: nil,
                token: nil,
                signed_authorization: nil,
                smart_contract: nil
              }
              |> maybe_preload_ens_to_address()

            CoinBalanceOnDemand.trigger_fetch(ip, address)
            ContractCodeOnDemand.trigger_fetch(ip, address)

            conn
            |> put_status(200)
            |> render(:address, %{address: address})
        end

        result
      after
        # Disable monitoring first
        Process.put(:query_monitoring_active, false)

        # Detach telemetry handler
        try do
          :telemetry.detach(handler_id)
        catch
          _ -> :ok
        end

        # Clean up ETS tables
        safe_delete_ets_by_name(query_count_name)
        safe_delete_ets_by_name(slow_queries_name)

        # Clean up process dictionary
        Process.delete(:query_monitoring_tables)
        Process.delete(:query_monitoring_active)
        Process.delete(:monitoring_pid)
      end
    end
  end

  operation :counters,
    summary: "Get activity count stats for a specific address",
    description:
      "Retrieves count statistics for an address, including transactions, token transfers, gas usage, and validations.",
    parameters: [address_hash_param() | base_params()],
    responses: [
      ok: {"Count statistics for the specified address", "application/json", Schemas.Address.Counters},
      unprocessable_entity: JsonErrorResponse.response(),
      forbidden: ForbiddenResponse.response()
    ]

  @doc """
  Handles GET requests to `/api/v2/addresses/:address_hash_param/counters` endpoint.

  ## Parameters
  - conn: The connection struct (Plug.Conn.t()).
  - params: A map of parameters.

  ## Returns
  - `{:format, :error}` if provided address_hash is invalid.
  - `{:restricted_access, true}` if access is restricted.
  - `Plug.Conn.t()` if the operation is successful.
  """
  @spec counters(Plug.Conn.t(), map()) :: {:format, :error} | {:restricted_access, true} | Plug.Conn.t()
  def counters(conn, %{address_hash_param: address_hash_string} = params) do
    with {:ok, address_hash} <- validate_address_hash(address_hash_string, params) do
      case Chain.hash_to_address(address_hash, @address_options) do
        {:ok, address} ->
          {validation_count} = Counters.address_counters(address, @api_true)

          transactions_from_db = address.transactions_count || 0
          token_transfers_from_db = address.token_transfers_count || 0
          address_gas_usage_from_db = address.gas_used || 0

          json(conn, %{
            transactions_count: to_string(transactions_from_db),
            token_transfers_count: to_string(token_transfers_from_db),
            gas_usage_count: to_string(address_gas_usage_from_db),
            validations_count: to_string(validation_count)
          })

        _ ->
          json(conn, %{
            transactions_count: to_string(0),
            token_transfers_count: to_string(0),
            gas_usage_count: to_string(0),
            validations_count: to_string(0)
          })
      end
    end
  end

  operation :token_balances,
    summary: "List all token balances held by a specific address",
    description:
      "Retrieves all token balances held by a specific address, including ERC-20, ERC-721, ERC-1155 and ERC-404 tokens.",
    parameters: [address_hash_param() | base_params()],
    responses: [
      ok:
        {"All token balances for the specified address.", "application/json",
         %Schema{title: "AddressTokenBalances", type: :array, items: Schemas.Address.TokenBalance}},
      unprocessable_entity: JsonErrorResponse.response(),
      forbidden: ForbiddenResponse.response()
    ]

  @doc """
  Handles GET requests to `/api/v2/addresses/:address_hash_param/token-balances` endpoint (retrieves the token balances for a given address)

  ## Parameters

    - conn: The connection struct.
    - params: A map containing the request parameters.

  ## Returns

    - `{:format, :error}` if provided address_hash is invalid.
    - `{:restricted_access, true}` if access is restricted.
    - `Plug.Conn.t()` if the request is successful.
  """
  @spec token_balances(Plug.Conn.t(), map()) :: {:format, :error} | {:restricted_access, true} | Plug.Conn.t()
  def token_balances(conn, %{address_hash_param: address_hash_string} = params) do
    ip = AccessHelper.conn_to_ip_string(conn)

    with {:ok, address_hash} <- validate_address_hash(address_hash_string, params) do
      case Chain.hash_to_address(address_hash, @address_options) do
        {:ok, _address} ->
          token_balances =
            address_hash
            |> Chain.fetch_last_token_balances(@api_true |> fetch_scam_token_toggle(conn))

          TokenBalanceOnDemand.trigger_fetch(ip, address_hash)

          conn
          |> put_status(200)
          |> render(:token_balances, %{token_balances: token_balances})

        _ ->
          conn
          |> put_status(200)
          |> render(:token_balances, %{token_balances: []})
      end
    end
  end

  operation :transactions,
    summary: "List transactions involving a specific address with to-from filtering",
    description:
      "Retrieves transactions involving a specific address, with optional filtering for transactions sent from or to the address.",
    parameters:
      base_params() ++
        [
          address_hash_param(),
          direction_filter_param(),
          sort_param(["block_number", "value", "fee"]),
          order_param()
        ] ++
        define_paging_params([
          "block_number_nullable",
          "index_nullable",
          "inserted_at",
          "hash",
          "value",
          "fee",
          "items_count"
        ]),
    responses: [
      ok:
        {"All transactions for the specified address.", "application/json",
         paginated_response(
           items: Schemas.Transaction,
           next_page_params_example: %{
             "block_number" => 22_566_361,
             "fee" => "19206937428000",
             "hash" => "0xe38d616dade747097354b0731b5560f581536dacf22121feb4bb4a0b776018aa",
             "index" => 103,
             "inserted_at" => "2025-05-26T10:26:51.474448Z",
             "items_count" => 50,
             "value" => "24741049597737"
           },
           title_prefix: "AddressTransactions"
         )},
      unprocessable_entity: JsonErrorResponse.response(),
      forbidden: ForbiddenResponse.response()
    ]

  @doc """
  Handles GET requests to `/api/v2/addresses/:address_hash_param/transactions` endpoint (retrieves transactions for a given address)

  ## Parameters

    - conn: The connection struct.
    - params: A map containing the parameters for the request.

  ## Returns

    - `{:format, :error}` if provided address_hash is invalid.
    - `{:restricted_access, true}` if access is restricted.
    - `Plug.Conn.t()` if the request is successful.
  """
  @spec transactions(Plug.Conn.t(), map()) :: {:format, :error} | {:restricted_access, true} | Plug.Conn.t()
  def transactions(conn, %{address_hash_param: address_hash_string} = params) do
    with {:ok, address_hash} <- validate_address_hash(address_hash_string, params) do
      case Chain.hash_to_address(address_hash, @address_options) do
        {:ok, _address} ->
          options =
            @transaction_necessity_by_association
            |> Keyword.merge(paging_options(params))
            |> Keyword.merge(current_filter(params))
            |> Keyword.merge(address_transactions_sorting(params))

          results_plus_one = Transaction.address_to_transactions_without_rewards(address_hash, options, false)
          {transactions, next_page} = split_list_by_page(results_plus_one)

          next_page_params =
            next_page
            |> next_page_params(
              transactions,
              delete_parameters_from_next_page_params(params),
              &Transaction.address_transactions_next_page_params/1
            )

          conn
          |> put_status(200)
          |> put_view(TransactionView)
          |> render(:transactions, %{
            transactions: transactions |> maybe_preload_ens() |> maybe_preload_metadata(),
            next_page_params: next_page_params
          })

        _ ->
          conn
          |> put_status(200)
          |> put_view(TransactionView)
          |> render(:transactions, %{
            transactions: [],
            next_page_params: nil
          })
      end
    end
  end

  operation :token_transfers,
    summary: "List token transfers involving a specific address with filtering options",
    description:
      "Retrieves token transfers involving a specific address, with optional filtering by token type, direction, and specific token.",
    parameters:
      base_params() ++
        [address_hash_param(), direction_filter_param(), token_type_param(), token_filter_param()] ++
        define_paging_params([
          "block_number",
          "index",
          "items_count",
          "batch_log_index",
          "batch_block_hash",
          "batch_transaction_hash",
          "index_in_batch"
        ]),
    responses: [
      ok:
        {"All token transfers for the specified address.", "application/json",
         paginated_response(
           items: Schemas.TokenTransfer,
           next_page_params_example: %{
             "block_number" => 12_345_678,
             "index" => 0,
             "items_count" => 50
           },
           title_prefix: "AddressTokenTransfers"
         )},
      unprocessable_entity: JsonErrorResponse.response(),
      forbidden: ForbiddenResponse.response()
    ]

  @doc """
  Handles GET requests to `/api/v2/addresses/:address_hash_param/token-transfers` endpoint (retrieves token transfers for a given address)

  ## Parameters

    - conn: The connection struct.
    - params: A map containing the parameters for the request.

  ## Returns

    - `{:format, :error}` if provided address_hash is invalid.
    - `{:restricted_access, true}` if access is restricted.
    - `{:not_found, {:error, :not_found}}` if token with provided address hash is not found.
    - `Plug.Conn.t()` if the request is successful.
  """
  @spec token_transfers(Plug.Conn.t(), map()) ::
          {:format, :error}
          | {:not_found, {:error, :not_found}}
          | {:restricted_access, true}
          | Plug.Conn.t()
  def token_transfers(conn, %{address_hash_param: address_hash_string} = params) do
    with {:ok, address_hash} <- validate_address_hash(address_hash_string, params),
         {:ok, token_address_hash} <- validate_optional_address_hash(params[:token], params),
         token_address_exists <- (token_address_hash && Chain.check_token_exists(token_address_hash)) || :ok do
      case {Chain.hash_to_address(address_hash, @address_options), token_address_exists} do
        {{:ok, _address}, :ok} ->
          paging_options = paging_options(params)

          options =
            @token_transfer_necessity_by_association
            |> Keyword.merge(paging_options)
            |> Keyword.merge(current_filter(params))
            |> Keyword.merge(token_transfers_types_options(params))
            |> Keyword.merge(token_address_hash: token_address_hash)
            |> fetch_scam_token_toggle(conn)

          results =
            address_hash
            |> Chain.address_hash_to_token_transfers_new(options)
            |> Chain.flat_1155_batch_token_transfers()
            |> Chain.paginate_1155_batch_token_transfers(paging_options)

          {token_transfers, next_page} = split_list_by_page(results)

          next_page_params =
            next_page
            |> token_transfers_next_page_params(token_transfers, delete_parameters_from_next_page_params(params))

          conn
          |> put_status(200)
          |> put_view(TransactionView)
          |> render(:token_transfers, %{
            token_transfers:
              token_transfers |> Instance.preload_nft(@api_true) |> maybe_preload_ens() |> maybe_preload_metadata(),
            next_page_params: next_page_params
          })

        _ ->
          conn
          |> put_status(200)
          |> put_view(TransactionView)
          |> render(:token_transfers, %{
            token_transfers: [],
            next_page_params: nil
          })
      end
    end
  end

  operation :internal_transactions,
    summary: "List all internal transactions involving a specific address",
    description:
      "Retrieves all internal transactions involving a specific address, with optional filtering for internal transactions sent from or to the address.",
    parameters:
      base_params() ++
        [address_hash_param(), direction_filter_param()] ++
        define_paging_params(["block_number", "index", "items_count", "transaction_index"]),
    responses: [
      ok:
        {"All internal transactions for the specified address.", "application/json",
         paginated_response(
           items: Schemas.InternalTransaction,
           next_page_params_example: %{
             "block_number" => 22_530_770,
             "index" => 8,
             "items_count" => 50,
             "transaction_index" => 8
           },
           title_prefix: "AddressInternalTransactions"
         )},
      unprocessable_entity: JsonErrorResponse.response(),
      forbidden: ForbiddenResponse.response()
    ]

  @doc """
  Handles GET requests to `/api/v2/addresses/:address_hash_param/internal-transactions` endpoint (retrieves internal transactions for a given address)

  ## Parameters

    - conn: The connection struct.
    - params: A map containing the parameters for the request.

  ## Returns

    - `{:format, :error}` if provided address_hash is invalid.
    - `{:restricted_access, true}` if access is restricted.
    - `Plug.Conn.t()` if the request is successful.
  """
  @spec internal_transactions(Plug.Conn.t(), map()) :: {:format, :error} | {:restricted_access, true} | Plug.Conn.t()
  def internal_transactions(conn, %{address_hash_param: address_hash_string} = params) do
    with {:ok, address_hash} <- validate_address_hash(address_hash_string, params) do
      case Chain.hash_to_address(address_hash, @address_options) do
        {:ok, _address} ->
          full_options =
            [
              necessity_by_association: %{
                [created_contract_address: [:scam_badge, :names, :smart_contract, proxy_implementations_association()]] =>
                  :optional,
                [from_address: [:scam_badge, :names, :smart_contract, proxy_implementations_association()]] =>
                  :optional,
                [to_address: [:scam_badge, :names, :smart_contract, proxy_implementations_association()]] => :optional
              }
            ]
            |> Keyword.merge(paging_options(params))
            |> Keyword.merge(current_filter(params))
            |> Keyword.merge(@api_true)

          results_plus_one = InternalTransaction.address_to_internal_transactions(address_hash, full_options)
          {internal_transactions, next_page} = split_list_by_page(results_plus_one)

          next_page_params =
            next_page |> next_page_params(internal_transactions, delete_parameters_from_next_page_params(params))

          conn
          |> put_status(200)
          |> put_view(TransactionView)
          |> render(:internal_transactions, %{
            internal_transactions: internal_transactions |> maybe_preload_ens() |> maybe_preload_metadata(),
            next_page_params: next_page_params
          })

        _ ->
          conn
          |> put_status(200)
          |> put_view(TransactionView)
          |> render(:internal_transactions, %{
            internal_transactions: [],
            next_page_params: nil
          })
      end
    end
  end

  operation :logs,
    summary: "List event logs emitted by or involving a specific address",
    description: "Retrieves event logs emitted by or involving a specific address.",
    parameters:
      base_params() ++
        [address_hash_param(), topic_param()] ++ define_paging_params(["block_number", "index", "items_count"]),
    responses: [
      ok:
        {"Event logs for the specified address, with pagination.", "application/json",
         paginated_response(
           items: Schemas.Log,
           next_page_params_example: %{"block_number" => 22_546_398, "index" => 268, "items_count" => 50},
           title_prefix: "AddressLogs"
         )},
      unprocessable_entity: JsonErrorResponse.response(),
      forbidden: ForbiddenResponse.response()
    ]

  @doc """
  Handles GET requests to `/api/v2/addresses/:address_hash_param/logs` endpoint (retrieves logs for a given address)

  ## Parameters

    - conn: The connection struct.
    - params: A map containing the parameters for the request.

  ## Returns

    - `{:format, :error}` if provided address_hash is invalid.
    - `{:restricted_access, true}` if access is restricted.
    - `Plug.Conn.t()` if the request is successful.
  """
  @spec logs(Plug.Conn.t(), map()) :: {:format, :error} | {:restricted_access, true} | Plug.Conn.t()
  def logs(conn, %{address_hash_param: address_hash_string} = params) do
    with {:ok, address_hash} <- validate_address_hash(address_hash_string, params),
         {:ok, topic} <- validate_optional_topic(params[:topic]) do
      case Chain.hash_to_address(address_hash, @api_true) do
        {:ok, _address} ->
          options =
            params
            |> paging_options()
            |> Keyword.merge(
              necessity_by_association: %{
                [address: [:names, :smart_contract, proxy_implementations_smart_contracts_association()]] => :optional
              }
            )
            |> Keyword.merge(@api_true)
            |> Keyword.put(:topic, topic)

          results_plus_one = Chain.address_to_logs(address_hash, false, options)

          {logs, next_page} = split_list_by_page(results_plus_one)

          next_page_params = next_page |> next_page_params(logs, delete_parameters_from_next_page_params(params))

          conn
          |> put_status(200)
          |> put_view(TransactionView)
          |> render(:logs, %{
            logs: logs |> maybe_preload_ens() |> maybe_preload_metadata(),
            next_page_params: next_page_params
          })

        _ ->
          conn
          |> put_status(200)
          |> put_view(TransactionView)
          |> render(:logs, %{
            logs: [],
            next_page_params: nil
          })
      end
    end
  end

  operation :blocks_validated,
    summary: "List blocks validated (mined) by a specific validator/miner address",
    description:
      "Retrieves blocks that were validated (mined) by a specific address. Useful for tracking validator/miner performance.",
    parameters: base_params() ++ [address_hash_param()] ++ define_paging_params(["block_number", "items_count"]),
    responses: [
      ok:
        {"Blocks validated by the specified address, with pagination.", "application/json",
         paginated_response(
           items: Schemas.Block,
           next_page_params_example: %{"block_number" => 22_546_398, "items_count" => 50},
           title_prefix: "AddressBlocksValidated"
         )},
      unprocessable_entity: JsonErrorResponse.response(),
      forbidden: ForbiddenResponse.response()
    ]

  @doc """
  Handles GET requests to `/api/v2/addresses/:address_hash_param/blocks-validated` endpoint (retrieves validated by a given address blocks)

  ## Parameters

    - conn: The connection struct.
    - params: A map containing the parameters for the request.

  ## Returns

    - `{:format, :error}` if provided address_hash is invalid.
    - `{:restricted_access, true}` if access is restricted.
    - `Plug.Conn.t()` if the request is successful.
  """
  @spec blocks_validated(Plug.Conn.t(), map()) :: {:format, :error} | {:restricted_access, true} | Plug.Conn.t()
  def blocks_validated(conn, %{address_hash_param: address_hash_string} = params) do
    with {:ok, address_hash} <- validate_address_hash(address_hash_string, params) do
      case Chain.hash_to_address(address_hash, @address_options) do
        {:ok, _address} ->
          full_options =
            [
              necessity_by_association: %{
                [miner: proxy_implementations_association()] => :optional,
                miner: :required,
                nephews: :optional,
                transactions: :optional,
                rewards: :optional
              }
            ]
            |> Keyword.merge(paging_options(params))
            |> Keyword.merge(@api_true)

          results_plus_one = Chain.get_blocks_validated_by_address(full_options, address_hash)
          {blocks, next_page} = split_list_by_page(results_plus_one)

          next_page_params = next_page |> next_page_params(blocks, delete_parameters_from_next_page_params(params))

          conn
          |> put_status(200)
          |> put_view(BlockView)
          |> render(:blocks, %{blocks: blocks, next_page_params: next_page_params})

        _ ->
          conn
          |> put_status(200)
          |> put_view(BlockView)
          |> render(:blocks, %{blocks: [], next_page_params: nil})
      end
    end
  end

  operation :coin_balance_history,
    summary: "Get native coin balance history for an address showing all balance changes",
    description:
      "Retrieves historical native coin balance changes for a specific address, tracking how an address's balance has changed over time.",
    parameters: base_params() ++ [address_hash_param()] ++ define_paging_params(["block_number", "items_count"]),
    responses: [
      ok:
        {"Historical coin balance changes for the specified address, with pagination.", "application/json",
         paginated_response(
           items: Schemas.CoinBalance,
           next_page_params_example: %{
             "block_number" => 22_546_398,
             "items_count" => 50
           },
           title_prefix: "AddressCoinBalanceHistory"
         )},
      unprocessable_entity: JsonErrorResponse.response(),
      forbidden: ForbiddenResponse.response()
    ]

  @doc """
  Handles GET requests to `/api/v2/addresses/:address_hash_param/coin-balance-history` endpoint (retrieves coin balance history for given address)

  ## Parameters

    - conn: The connection struct.
    - params: A map containing the parameters for the request.

  ## Returns

    - `{:format, :error}` if provided address_hash is invalid.
    - `{:restricted_access, true}` if access is restricted.
    - `Plug.Conn.t()` if the request is successful.
  """
  @spec coin_balance_history(Plug.Conn.t(), map()) :: {:format, :error} | {:restricted_access, true} | Plug.Conn.t()
  def coin_balance_history(conn, %{address_hash_param: address_hash_string} = params) do
    with {:ok, address_hash} <- validate_address_hash(address_hash_string, params) do
      case Chain.hash_to_address(address_hash, @address_options) do
        {:ok, address} ->
          full_options = params |> paging_options() |> Keyword.merge(@api_true)

          results_plus_one = CoinBalance.address_to_coin_balances(address, full_options)

          {coin_balances, next_page} = split_list_by_page(results_plus_one)

          next_page_params =
            next_page |> next_page_params(coin_balances, delete_parameters_from_next_page_params(params))

          conn
          |> put_status(200)
          |> render(:coin_balances, %{coin_balances: coin_balances, next_page_params: next_page_params})

        _ ->
          conn
          |> put_status(200)
          |> render(:coin_balances, %{coin_balances: [], next_page_params: nil})
      end
    end
  end

  operation :coin_balance_history_by_day,
    summary: "Get daily native coin balance snapshots for an address from previous 10 days",
    description:
      "Retrieves daily snapshots of native coin balance for a specific address. Useful for generating balance-over-time charts.",
    parameters: [address_hash_param() | base_params()],
    responses: [
      ok:
        {"Daily coin balance history for the specified address.", "application/json",
         %Schema{
           title: "AddressCoinBalanceHistoryByDay",
           type: :object,
           properties: %{
             days: %Schema{type: :integer, nullable: false},
             items: %Schema{type: :array, items: Schemas.CoinBalanceByDay}
           },
           nullable: false
         }},
      unprocessable_entity: JsonErrorResponse.response(),
      forbidden: ForbiddenResponse.response()
    ]

  @doc """
  Handles GET requests to `/api/v2/addresses/:address_hash_param/coin-balance-history-by-day` endpoint (retrieves coin balance history by day for given address)

  ## Parameters

    - conn: The connection struct.
    - params: A map containing the parameters for the request.

  ## Returns

    - `{:format, :error}` if provided address_hash is invalid.
    - `{:restricted_access, true}` if access is restricted.
    - `Plug.Conn.t()` if the request is successful.
  """
  @spec coin_balance_history_by_day(Plug.Conn.t(), map()) ::
          {:format, :error} | {:restricted_access, true} | Plug.Conn.t()
  def coin_balance_history_by_day(conn, %{address_hash_param: address_hash_string} = params) do
    with {:ok, address_hash} <- validate_address_hash(address_hash_string, params) do
      case Chain.hash_to_address(address_hash, @address_options) do
        {:ok, _address} ->
          balances_by_day =
            address_hash
            |> Chain.address_to_balances_by_day(@api_true)

          conn
          |> put_status(200)
          |> render(:coin_balances_by_day, %{coin_balances_by_day: balances_by_day})

        _ ->
          conn
          |> put_status(200)
          |> render(:coin_balances_by_day, %{coin_balances_by_day: []})
      end
    end
  end

  operation :tokens,
    summary: "List token balances for an address with pagination and type filtering",
    description:
      "Retrieves token balances for a specific address with pagination and filtering by token type. Useful for displaying large token portfolios.",
    parameters:
      base_params() ++
        [address_hash_param(), token_type_param()] ++
        define_paging_params(["fiat_value_nullable", "id", "items_count", "value"]),
    responses: [
      ok:
        {"Token balances for the specified address with pagination.", "application/json",
         paginated_response(
           items: Schemas.Address.TokenBalance,
           next_page_params_example: %{
             "fiat_value" => nil,
             "id" => 12_519_063_346,
             "items_count" => 50,
             "value" => "3750000000000000000000"
           },
           title_prefix: "AddressTokens"
         )},
      unprocessable_entity: JsonErrorResponse.response(),
      forbidden: ForbiddenResponse.response()
    ]

  @doc """
  Handles GET requests to `/api/v2/addresses/:address_hash_param/tokens` endpoint (retrieves token balances for given address)

  ## Parameters

    - conn: The connection struct.
    - params: A map containing the parameters for the request.

  ## Returns

    - `{:format, :error}` if provided address_hash is invalid.
    - `{:restricted_access, true}` if access is restricted.
    - `Plug.Conn.t()` if the request is successful.
  """
  @spec tokens(Plug.Conn.t(), map()) :: {:format, :error} | {:restricted_access, true} | Plug.Conn.t()
  def tokens(conn, %{address_hash_param: address_hash_string} = params) do
    ip = AccessHelper.conn_to_ip_string(conn)

    with {:ok, address_hash} <- validate_address_hash(address_hash_string, params) do
      case Chain.hash_to_address(address_hash, @address_options) do
        {:ok, _address} ->
          results_plus_one =
            address_hash
            |> Chain.fetch_paginated_last_token_balances(
              params
              |> paging_options()
              |> Keyword.merge(token_transfers_types_options(params))
              |> Keyword.merge(@api_true)
              |> fetch_scam_token_toggle(conn)
            )

          TokenBalanceOnDemand.trigger_fetch(ip, address_hash)

          {tokens, next_page} = split_list_by_page(results_plus_one)

          next_page_params =
            next_page
            |> next_page_params(
              tokens,
              delete_parameters_from_next_page_params(params),
              &paging_params_with_fiat_value/1
            )

          conn
          |> put_status(200)
          |> render(:tokens, %{tokens: tokens, next_page_params: next_page_params})

        _ ->
          conn
          |> put_status(200)
          |> render(:tokens, %{tokens: [], next_page_params: nil})
      end
    end
  end

  operation :withdrawals,
    summary: "List validator withdrawals involving a specific address",
    description:
      "Retrieves withdrawals involving a specific address, typically for proof-of-stake networks supporting validator withdrawals.",
    parameters: base_params() ++ [address_hash_param()] ++ define_paging_params(["index", "items_count"]),
    responses: [
      ok:
        {"Withdrawals for the specified address, with pagination. Note that receiver field is not included in this endpoint.",
         "application/json",
         paginated_response(
           items: Schemas.Withdrawal,
           next_page_params_example: %{"index" => 88_192_653, "items_count" => 50},
           title_prefix: "AddressWithdrawals"
         )},
      unprocessable_entity: JsonErrorResponse.response(),
      forbidden: ForbiddenResponse.response()
    ]

  @doc """
  Handles GET requests to `/api/v2/addresses/:address_hash_param/withdrawals` endpoint (retrieves withdrawals for given address)

  ## Parameters

    - conn: The connection struct.
    - params: A map containing the parameters for the request.

  ## Returns

    - `{:format, :error}` if provided address_hash is invalid.
    - `{:restricted_access, true}` if access is restricted.
    - `Plug.Conn.t()` if the request is successful.
  """
  @spec withdrawals(Plug.Conn.t(), map()) :: {:format, :error} | {:restricted_access, true} | Plug.Conn.t()
  def withdrawals(conn, %{address_hash_param: address_hash_string} = params) do
    with {:ok, address_hash} <- validate_address_hash(address_hash_string, params) do
      case Chain.hash_to_address(address_hash, @address_options) do
        {:ok, _address} ->
          options = @api_true |> Keyword.merge(paging_options(params))
          withdrawals_plus_one = address_hash |> Chain.address_hash_to_withdrawals(options)
          {withdrawals, next_page} = split_list_by_page(withdrawals_plus_one)

          next_page_params = next_page |> next_page_params(withdrawals, delete_parameters_from_next_page_params(params))

          conn
          |> put_status(200)
          |> put_view(WithdrawalView)
          |> render(:withdrawals, %{
            withdrawals: withdrawals |> maybe_preload_ens() |> maybe_preload_metadata(),
            next_page_params: next_page_params
          })

        _ ->
          conn
          |> put_status(200)
          |> put_view(WithdrawalView)
          |> render(:withdrawals, %{
            withdrawals: [],
            next_page_params: nil
          })
      end
    end
  end

  operation :addresses_list,
    summary: "List addresses holding native coins sorted by balance - top accounts",
    description: "Retrieves a paginated list of addresses holding the native coin, sorted by balance.",
    parameters:
      base_params() ++
        [sort_param(["balance", "transactions_count"]), order_param()] ++
        define_paging_params(["fetched_coin_balance", "address_hash", "items_count", "transactions_count"]),
    responses: [
      ok:
        {"List of native coin holders with their balances, with pagination.", "application/json",
         %Schema{
           title: "AddressesList",
           allOf: [
             paginated_response(
               items: Schemas.Address,
               next_page_params_example: %{
                 "fetched_coin_balance" => "124355417998347240251800",
                 "hash" => "0x59708733fbbf64378d9293ec56b977c011a08fd2",
                 "items_count" => 50,
                 "transactions_count" => nil
               },
               title_prefix: "AddressList"
             ),
             %Schema{
               properties: %{
                 exchange_rate: Schemas.General.FloatStringNullable,
                 total_supply: Schemas.General.FloatStringNullable
               },
               required: [:exchange_rate, :total_supply]
             }
           ]
         }},
      forbidden: ForbiddenResponse.response()
    ]

  @doc """
  Handles GET requests to `/api/v2/addresses` endpoint (retrieves addresses list)

  ## Parameters

    - conn: The connection struct.
    - params: A map containing the parameters for the request.

  ## Returns

    - `Plug.Conn.t()` if the request is successful.
  """
  @spec addresses_list(Plug.Conn.t(), map()) :: Plug.Conn.t()
  def addresses_list(conn, params) do
    {addresses, next_page} =
      params
      |> paging_options()
      |> Keyword.merge(@api_true)
      |> Keyword.merge(addresses_sorting(params))
      |> Address.list_top_addresses()
      |> split_list_by_page()

    next_page_params = next_page_params(next_page, addresses, delete_parameters_from_next_page_params(params))

    exchange_rate = Market.get_coin_exchange_rate()
    total_supply = Chain.total_supply()

    conn
    |> put_status(200)
    |> render(:addresses, %{
      addresses: addresses |> maybe_preload_ens() |> maybe_preload_metadata(),
      next_page_params: next_page_params,
      exchange_rate: exchange_rate,
      total_supply: total_supply
    })
  end

  operation :tabs_counters,
    summary: "Get counters for address tabs",
    description: "Retrieves counters for various address-related entities (max counter value is 51).",
    parameters: [address_hash_param() | base_params()],
    responses: [
      ok: {"Counters for address tabs.", "application/json", Schemas.Address.TabsCounters},
      unprocessable_entity: JsonErrorResponse.response(),
      forbidden: ForbiddenResponse.response()
    ]

  @doc """
  Handles GET requests to `/api/v2/addresses/:address_hash_param/tabs-counters` endpoint (retrieves counter for each entity (max counter value is 51) for given address)

  ## Parameters

    - conn: The connection struct.
    - params: A map containing the parameters for the request.

  ## Returns

    - `{:format, :error}` if provided address_hash is invalid.
    - `{:restricted_access, true}` if access is restricted.
    - `Plug.Conn.t()` if the request is successful.
  """
  @spec tabs_counters(Plug.Conn.t(), map()) :: {:format, :error} | {:restricted_access, true} | Plug.Conn.t()
  def tabs_counters(conn, %{address_hash_param: address_hash_string} = params) do
    with {:ok, address_hash} <- validate_address_hash(address_hash_string, params) do
      counter_name_to_json_field_name = %{
        validations: :validations_count,
        transactions: :transactions_count,
        token_transfers: :token_transfers_count,
        token_balances: :token_balances_count,
        logs: :logs_count,
        withdrawals: :withdrawals_count,
        internal_transactions: :internal_transactions_count,
        celo_election_rewards: :celo_election_rewards_count
      }

      case Chain.hash_to_address(address_hash, @address_options) do
        {:ok, _address} ->
          counters_json =
            address_hash
            |> Counters.address_limited_counters(@api_true)
            |> Enum.reduce(%{}, fn {counter_name, counter_value}, acc ->
              counter_name_to_json_field_name
              |> Map.fetch(counter_name)
              # credo:disable-for-next-line
              |> case do
                {:ok, json_field_name} ->
                  Map.put(acc, json_field_name, counter_value)

                :error ->
                  acc
              end
            end)

          conn
          |> put_status(200)
          |> json(counters_json)

        _ ->
          counters_json =
            counter_name_to_json_field_name
            |> Enum.reduce(%{}, fn {_counter_type, json_field}, acc ->
              Map.put(acc, json_field, 0)
            end)

          conn
          |> put_status(200)
          |> json(counters_json)
      end
    end
  end

  operation :nft_list,
    summary: "List NFTs owned by a specific address with optional type filtering",
    description:
      "Retrieves a list of NFTs (non-fungible tokens) owned by a specific address, with optional filtering by token type.",
    parameters:
      base_params() ++
        [address_hash_param(), nft_token_type_param()] ++
        define_paging_params(["items_count", "token_contract_address_hash", "token_id", "token_type"]),
    responses: [
      ok:
        {"NFTs owned by the specified address, with pagination.", "application/json",
         paginated_response(
           items: Schemas.TokenInstanceInList,
           next_page_params_example: %{
             "items_count" => 50,
             "token_contract_address_hash" => "0x1ffe11b9fb7f6ff1b153ab8608cf403ecaf9d44a",
             "token_id" => "24950",
             "token_type" => "ERC-721"
           },
           title_prefix: "AddressNFTs"
         )},
      unprocessable_entity: JsonErrorResponse.response(),
      forbidden: ForbiddenResponse.response()
    ]

  @doc """
  Handles GET requests to `/api/v2/addresses/:address_hash_param/nft` endpoint (retrieves NFTs for given address)

  ## Parameters

    - conn: The connection struct.
    - params: A map containing the parameters for the request.

  ## Returns

    - `{:format, :error}` if provided address_hash is invalid.
    - `{:restricted_access, true}` if access is restricted.
    - `Plug.Conn.t()` if the request is successful.
  """
  @spec nft_list(Plug.Conn.t(), map()) :: {:format, :error} | {:restricted_access, true} | Plug.Conn.t()
  def nft_list(conn, %{address_hash_param: address_hash_string} = params) do
    with {:ok, address_hash} <- validate_address_hash(address_hash_string, params) do
      case Chain.hash_to_address(address_hash, @address_options) do
        {:ok, _address} ->
          results_plus_one =
            Instance.nft_list(
              address_hash,
              params
              |> paging_options()
              |> Keyword.merge(nft_types_options(params))
              |> Keyword.merge(@api_true)
              |> Keyword.merge(@nft_necessity_by_association)
              |> fetch_scam_token_toggle(conn)
            )

          {nfts, next_page} = split_list_by_page(results_plus_one)

          next_page_params =
            next_page
            |> next_page_params(
              nfts,
              delete_parameters_from_next_page_params(params),
              &Instance.nft_list_next_page_params/1
            )

          conn
          |> put_status(200)
          |> render(:nft_list, %{token_instances: nfts, next_page_params: next_page_params})

        _ ->
          conn
          |> put_status(200)
          |> render(:nft_list, %{token_instances: [], next_page_params: nil})
      end
    end
  end

  operation :nft_collections,
    summary: "List NFTs owned by an address grouped by collection/project",
    description:
      "Retrieves NFTs owned by a specific address, organized by collection. Useful for displaying an address's NFT portfolio grouped by project.",
    parameters:
      base_params() ++
        [address_hash_param(), nft_token_type_param()] ++
        define_paging_params(["items_count", "token_contract_address_hash", "token_type"]),
    responses: [
      ok:
        {"NFTs owned by the specified address, grouped by collection, with pagination.", "application/json",
         paginated_response(
           items: Schemas.NFTCollection,
           next_page_params_example: %{
             "items_count" => 50,
             "token_contract_address_hash" => "0x1ffe11b9fb7f6ff1b153ab8608cf403ecaf9d44a",
             "token_type" => "ERC-721"
           },
           title_prefix: "AddressNFTCollections"
         )},
      unprocessable_entity: JsonErrorResponse.response(),
      forbidden: ForbiddenResponse.response()
    ]

  @doc """
  Handles GET requests to `/api/v2/addresses/:address_hash_param/nft/collections` endpoint (retrieves NFTs grouped by collections for given address)

  ## Parameters

    - conn: The connection struct.
    - params: A map containing the parameters for the request.

  ## Returns

    - `{:format, :error}` if provided address_hash is invalid.
    - `{:restricted_access, true}` if access is restricted.
    - `Plug.Conn.t()` if the request is successful.
  """
  @spec nft_collections(Plug.Conn.t(), map()) :: {:format, :error} | {:restricted_access, true} | Plug.Conn.t()
  def nft_collections(conn, %{address_hash_param: address_hash_string} = params) do
    with {:ok, address_hash} <- validate_address_hash(address_hash_string, params) do
      case Chain.hash_to_address(address_hash, @address_options) do
        {:ok, _address} ->
          results_plus_one =
            Instance.nft_collections(
              address_hash,
              params
              |> paging_options()
              |> Keyword.merge(nft_types_options(params))
              |> Keyword.merge(@api_true)
              |> Keyword.merge(@nft_necessity_by_association)
              |> fetch_scam_token_toggle(conn)
            )

          {collections, next_page} = split_list_by_page(results_plus_one)

          next_page_params =
            next_page
            |> next_page_params(
              collections,
              delete_parameters_from_next_page_params(params),
              &Instance.nft_collections_next_page_params/1
            )

          conn
          |> put_status(200)
          |> render(:nft_collections, %{collections: collections, next_page_params: next_page_params})

        _ ->
          conn
          |> put_status(200)
          |> render(:nft_collections, %{collections: [], next_page_params: nil})
      end
    end
  end

  operation :celo_election_rewards,
    summary: "List Celo election rewards for a specific address",
    description: "Retrieves Celo election rewards for a specific address.",
    parameters:
      base_params() ++
        [address_hash_param()] ++
        define_paging_params(["epoch_number", "amount", "associated_account_address_hash", "type"]),
    responses: [
      ok:
        {"Celo election rewards for the specified address.", "application/json",
         paginated_response(
           items: Schemas.Celo.ElectionReward,
           next_page_params_example: %{
             "block_number" => 100,
             "amount" => "1000000000000000000",
             "associated_account_address_hash" => "0x1234567890123456789012345678901234567890",
             "type" => "validator"
           },
           title_prefix: "AddressCeloElectionRewards"
         )},
      unprocessable_entity: JsonErrorResponse.response(),
      forbidden: ForbiddenResponse.response()
    ]

  @doc """
  Handles GET requests to `/api/v2/addresses/:address_hash_param/election-rewards` endpoint.
  """
  @spec celo_election_rewards(Plug.Conn.t(), map()) :: {:format, :error} | {:restricted_access, true} | Plug.Conn.t()
  def celo_election_rewards(conn, %{address_hash_param: address_hash_string} = params) do
    with {:ok, address_hash} <- validate_address_hash(address_hash_string, params),
         {:ok, _address} <- Chain.hash_to_address(address_hash, api?: true) do
      full_options =
        @celo_election_rewards_options
        |> Keyword.put(
          :paging_options,
          celo_election_rewards_paging_options(params)
        )

      results_plus_one = CeloElectionReward.address_hash_to_rewards(address_hash, full_options)

      {rewards, next_page} = split_list_by_page(results_plus_one)

      filtered_params =
        params
        |> delete_parameters_from_next_page_params()
        |> Map.drop([
          "epoch_number",
          "amount",
          "associated_account_address_hash",
          "type"
        ])

      next_page_params =
        next_page_params(
          next_page,
          rewards,
          filtered_params,
          &%{
            epoch_number: &1.epoch_number,
            amount: &1.amount,
            associated_account_address_hash: &1.associated_account_address_hash,
            type: &1.type
          }
        )

      conn
      |> put_status(200)
      |> put_view(CeloView)
      |> render(:celo_address_election_rewards, %{
        rewards: rewards,
        next_page_params: next_page_params
      })
    end
  end

  @spec celo_election_rewards_paging_options(map()) :: PagingOptions.t()
  defp celo_election_rewards_paging_options(params) do
    with %{
           epoch_number: epoch_number_string,
           amount: amount_string,
           associated_account_address_hash: associated_account_address_hash_string,
           type: type_string
         }
         when is_binary(epoch_number_string) and
                is_binary(amount_string) and
                is_binary(associated_account_address_hash_string) and
                is_binary(type_string) <- params,
         {:ok, epoch_number} <- safe_parse_non_negative_integer(epoch_number_string),
         {amount, ""} <- Decimal.parse(amount_string),
         {:ok, associated_account_address_hash} <-
           Hash.Address.cast(associated_account_address_hash_string),
         {:ok, type} <- CeloElectionReward.type_from_string(type_string) do
      %{
        PagingOptions.default_paging_options()
        | key: %{
            epoch_number: epoch_number,
            amount: amount,
            associated_account_address_hash: associated_account_address_hash,
            type: type
          }
      }
    else
      _ ->
        PagingOptions.default_paging_options()
    end
  end

  # Checks if this address hash string is valid, and this address is not prohibited.
  # Returns the `{:ok, address_hash}` if address hash passed all the checks.
  # Returns {:ok, _} response even if the address is not present in the database.
  @spec validate_address_hash(String.t(), any()) ::
          {:format, :error}
          | {:restricted_access, true}
          | {:ok, Hash.t()}
  defp validate_address_hash(address_hash_string, params) do
    with {:format, {:ok, address_hash}} <- {:format, Chain.string_to_address_hash(address_hash_string)},
         {:ok, false} <- AccessHelper.restricted_access?(address_hash_string, params) do
      {:ok, address_hash}
    end
  end

  # Checks if this address hash string is valid, and this address is not prohibited.
  # Returns the `{:ok, nil}` if first argument is `nil`.
  # Returns the `{:ok, address_hash}` if address hash passed all the checks.
  # Returns {:ok, _} response even if the address is not present in the database.
  @spec validate_optional_address_hash(nil | String.t(), any()) ::
          {:format, :error}
          | {:restricted_access, true}
          | {:ok, nil | Hash.t()}
  defp validate_optional_address_hash(address_hash_string, params) do
    case address_hash_string do
      nil ->
        {:ok, nil}

      _ ->
        validate_address_hash(address_hash_string, params)
    end
  end

  @spec validate_optional_topic(nil | String.t()) :: {:ok, nil | Hash.Full.t()} | {:format, :error}
  defp validate_optional_topic(topic) do
    topic = if is_binary(topic), do: String.trim(topic), else: topic

    case topic do
      nil ->
        {:ok, nil}

      "" ->
        {:ok, nil}

      "null" ->
        {:ok, nil}

      _ ->
        with {:format, {:ok, topic}} <- {:format, Chain.string_to_full_hash(topic)} do
          {:ok, topic}
        end
    end
  end

  # Simplified proxy implementations preloading to reduce queries
  defp preload_proxy_implementations_efficiently(address) do
    Logger.error("preload_proxy_implementations_efficiently: Starting for address #{inspect(address.hash)}")
    Logger.error("preload_proxy_implementations_efficiently: proxy_implementations loaded? #{Ecto.assoc_loaded?(address.proxy_implementations)}")
    Logger.error("preload_proxy_implementations_efficiently: smart_contract present? #{!is_nil(address.smart_contract)}")

    if Ecto.assoc_loaded?(address.proxy_implementations) do
      # Even if loaded, limit the number to prevent view rendering issues
      limited_implementations = Enum.take(address.proxy_implementations || [], 5)
      Logger.error("preload_proxy_implementations_efficiently: Already loaded, limiting to #{length(limited_implementations)} implementations")
      %{address | proxy_implementations: limited_implementations}
    else
      # Only fetch proxy implementations if smart contract exists
      case address.smart_contract do
        nil ->
          Logger.error("preload_proxy_implementations_efficiently: No smart contract, returning empty implementations")
          %{address | proxy_implementations: []}
        _smart_contract ->
          Logger.error("preload_proxy_implementations_efficiently: Smart contract found, attempting to preload...")
          try do
            # Get only the most basic proxy implementations - no nested preloading
            Logger.error("preload_proxy_implementations_efficiently: Starting Explorer.Repo.preload...")
            preloaded = Explorer.Repo.preload(address, [:proxy_implementations], timeout: 5_000)
            Logger.error("preload_proxy_implementations_efficiently: Repo.preload completed")

            implementations = preloaded
              |> Map.get(:proxy_implementations, [])
              |> Enum.take(3)  # Reduced to only 3 to limit queries

            Logger.error("preload_proxy_implementations_efficiently: Found #{length(implementations)} implementations")

            # Log the structure of implementations to debug the issue
            if length(implementations) > 0 do
              first_impl = List.first(implementations)
              Logger.error("preload_proxy_implementations_efficiently: First implementation keys: #{inspect(Map.keys(first_impl))}")
              Logger.error("preload_proxy_implementations_efficiently: First implementation has proxy_type? #{Map.has_key?(first_impl, :proxy_type)}")
            end

            %{address | proxy_implementations: implementations}
          rescue
            error ->
              Logger.error("preload_proxy_implementations_efficiently: ERROR #{inspect(error)}")
              Logger.error("preload_proxy_implementations_efficiently: ERROR stacktrace: #{inspect(__STACKTRACE__)}")
              %{address | proxy_implementations: []}
          end
      end
    end
  end

  # Helper functions for safe ETS access by table name
  defp safe_get_query_count_by_name(table_name) do
    try do
      case :ets.whereis(table_name) do
        :undefined -> 0
        _tid ->
          case :ets.lookup(table_name, :count) do
            [{:count, count}] -> count
            [] -> 0
          end
      end
    rescue
      _ -> 0
    catch
      _ -> 0
    end
  end

  defp safe_get_slow_queries_by_name(table_name) do
    try do
      case :ets.whereis(table_name) do
        :undefined -> []
        _tid -> :ets.tab2list(table_name)
      end
    rescue
      _ -> []
    catch
      _ -> []
    end
  end

  defp safe_delete_ets_by_name(table_name) do
    try do
      case :ets.whereis(table_name) do
        :undefined -> :ok
        tid -> :ets.delete(tid)
      end
    rescue
      _ -> :ok
    catch
      _ -> :ok
    end
  end
end
