"""Runtime settings. Every value can be overridden with an MEV_-prefixed env var or a .env file."""
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="MEV_", extra="ignore")

    # Chain access. Read-only unless execution_mode is live (see the Execution block below).
    rpc_http: str = "https://base-rpc.publicnode.com"   # mainnet.base.org allows 5 req/s and 429s eth_getLogs under load
    rpc_http_fallbacks: str = "https://base.drpc.org,https://1rpc.io/base,https://mainnet.base.org"   # rotated to on 403/429
    rpc_ws: str | None = None
    poll_interval_s: float = 0.5
    rpc_timeout_s: float = 5.0           # one stuck request must not cost tens of 2 s blocks
    max_catchup_blocks: int = 5          # process every block in order up to this many behind head (chase_head off)
    chase_head: bool = True              # when behind by more than one block, jump to the newest block; the skipped
                                         # blocks are counted in blocks_skipped_total. A backlog block is stale by the
                                         # time it is processed, so working through it only produces blocked candidates.
    block_retries: int = 3               # attempts per block before it is skipped (lagging replica, 429, ...)
    resolve_retries: int = 3             # attempts per pending paper trade before it is dropped from memory

    # Uniswap V3 tick window (mev/pools/state.py). Every initialized tick within v3_tick_window ticks of the
    # current tick is fetched each block (one Multicall3 eth_call) so quotes cross ticks exactly; 300 ticks is
    # about 3% in price, far beyond what the largest size in trade_sizes_usd moves either pool. A quote that
    # would leave the window is marked inexact and the risk engine blocks it.
    v3_tick_window: int = 300
    v3_tick_margin: int = 50             # extra ticks fetched speculatively from the previous block's bitmap
    v3_max_ticks: int = 64               # cap per pool per block: the initialized ticks nearest the price (a $50k
                                         # trade crosses a handful; 1 bp pools have hundreds within the window)
    v3_max_ticks_spacing1: int = 160     # cap for 1-tick-spacing (1 bp) pools: a $20k trade crosses ~30 of them
    multicall_chunk: int = 500           # calls per Multicall3 eth_call; chunks run concurrently
    tick_refresh_pools_per_block: int = 2   # round-robin tick-window refresh (all pools every ~25 blocks with 47 pools)
    min_pool_tvl_usd: float = 200_000    # discovery marks pools below this inactive
    snapshot_all_every_blocks: int = 300  # full pool_snapshots row set every ~10 min; otherwise only changed pools
    pool_reload_blocks: int = 1800       # re-read the active pool set from the DB about hourly

    fetch_logs: bool = True              # decode pool events per block (swaps table, state cross-check)
    api_port: int = 8000

    # Storage / metrics
    database_url: str = "sqlite:///mev.db"
    metrics_port: int = 9100

    # Economics of a flash-loan arbitrage on Base
    trade_sizes_usd: list[int] = [500, 1_000, 2_500, 5_000, 10_000, 20_000, 50_000]
    flash_loan_fee_bps: int = 5          # Aave V3 flashLoanSimple premium
    gas_units: int = 350_000             # flash loan + two swaps, conservative
    l1_data_fee_usd: float = 0.02        # Base posts calldata to L1; small but not zero
    safety_buffer_bps: int = 2           # applied to trade size
    priority_fee_wei: int = 50_000_000   # 0.05 gwei tip, a deterministic constant (never from a model)
    base_fee_elasticity: int = 5         # EIP-1559 fallbacks; Base headers carry the live values in extraData
    base_fee_change_denominator: int = 100
    min_base_fee_wei: int = 5_000_000    # Base floors the base fee at 0.005 gwei

    # Laptop agent (mev/agent): reacts to stored opportunities; never trades
    agent_poll_s: float = 1.0
    agent_alert_min_net_usd: float = 5.0     # also alert on blocked candidates whose best net clears this
    agent_max_track_blocks: int = 30         # how long to follow an opportunity before giving up
    agent_macos_notify: bool = True
    alert_webhook: str | None = None         # Slack/Discord/Telegram URL; see mev/agent/notify.py
    llm_api_key: str | None = None           # enables the optional assessment step (mev/agent/llm.py)
    llm_base_url: str = "https://integrate.api.nvidia.com/v1"   # any OpenAI-compatible endpoint
    llm_model: str = "nvidia/llama-3.1-nemotron-70b-instruct"

    # Deterministic risk rules (paper mode still enforces them so the dataset is realistic)
    min_net_profit_usd: float = 1.0      # the doc's $25 is a mainnet number; Base margins are smaller
    max_trade_size_usd: int = 25_000
    max_gas_usd: float = 5.0
    max_state_age_blocks: int = 2        # a transaction sent at head lands in head+1 or head+2 anyway
    max_daily_loss_usd: float = 500.0    # realized paper loss in the current UTC day (block time)
    max_failed_trades: int = 5           # consecutive resolutions that were no longer profitable

    # Execution (spec Phases 5-7). paper: no transaction exists. shadow: the exact FlashArb transaction is built
    # and eth_call-simulated at head, never sent (Phase 6). live: signed and sent under the caps below (Phase 7).
    # Live needs flash_arb_address and a signer key (Kubernetes secret mev-signer) and stops while the kill switch
    # is set: the halt file exists, execution_halt is true, or meta.execution_halt = "1".
    execution_mode: Literal["paper", "shadow", "live"] = "paper"
    flash_lender: Literal["aave_v3", "morpho_blue"] = "aave_v3"   # morpho_blue is fee-free; the simulator still charges flash_loan_fee_bps
    flash_arb_address: str | None = None       # scripts/deploy_contract.py prints it
    signer_key: str | None = None              # hex private key; never logged, never written to the database
    signer_key_file: str | None = None         # alternative: path of a file holding the hex key
    execution_deadline_blocks: int = 2         # the transaction reverts if included later than head + this
    execution_gas_limit: int = 450_000
    execution_halt: bool = False
    execution_halt_file: str = "HALT"
    live_max_trade_size_usd: int = 1_000
    live_max_daily_loss_usd: float = 20.0      # realized live loss (gas included) in the current UTC day
    live_pair_allowlist: str = "WETH/USDC,VIRTUAL/WETH,AERO/WETH"
    live_receipt_timeout_s: float = 90.0


settings = Settings()
