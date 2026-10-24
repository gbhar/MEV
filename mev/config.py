"""Runtime settings. Every value can be overridden with an MEV_-prefixed env var or a .env file."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="MEV_", extra="ignore")

    # Chain access (read-only in V1; no signer anywhere)
    rpc_http: str = "https://base-rpc.publicnode.com"   # mainnet.base.org allows 5 req/s and 429s eth_getLogs under load
    rpc_ws: str | None = None
    poll_interval_s: float = 0.5
    rpc_timeout_s: float = 5.0           # one stuck request must not cost tens of 2 s blocks
    max_catchup_blocks: int = 5          # process every block in order up to this many behind head
    block_retries: int = 3               # attempts per block before it is skipped (lagging replica, 429, ...)
    resolve_retries: int = 3             # attempts per pending paper trade before it is dropped from memory

    # Uniswap V3 tick window (mev/pools/state.py). Every initialized tick within v3_tick_window ticks of the
    # current tick is fetched each block (one Multicall3 eth_call) so quotes cross ticks exactly; 300 ticks is
    # about 3% in price, far beyond what the largest size in trade_sizes_usd moves either pool. A quote that
    # would leave the window is marked inexact and the risk engine blocks it.
    v3_tick_window: int = 300
    v3_tick_margin: int = 50             # extra ticks fetched speculatively from the previous block's bitmap
    v3_max_ticks: int = 400              # hard cap per pool per block; the ticks nearest the price are kept

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
    max_state_age_blocks: int = 1
    max_daily_loss_usd: float = 500.0    # realized paper loss in the current UTC day (block time)
    max_failed_trades: int = 5           # consecutive resolutions that were no longer profitable


settings = Settings()
