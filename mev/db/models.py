"""V1 tables. Every row is pinned to a block number so the whole run can be replayed.

All datetimes are naive UTC (SQLite drops offsets; Postgres would keep them, so neither is used)."""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class PoolRow(Base):
    """Pool definition each row was written under (seeded from mev.chain.addresses.POOLS in init_db)."""
    __tablename__ = "pools"
    pool_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    chain_id: Mapped[int] = mapped_column(Integer)
    dex: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(32))
    address: Mapped[str] = mapped_column(String(42))
    token0_address: Mapped[str] = mapped_column(String(42))
    token0_symbol: Mapped[str] = mapped_column(String(16))
    token0_decimals: Mapped[int] = mapped_column(Integer)
    token1_address: Mapped[str] = mapped_column(String(42))
    token1_symbol: Mapped[str] = mapped_column(String(16))
    token1_decimals: Mapped[int] = mapped_column(Integer)
    fee_ppm: Mapped[int] = mapped_column(Integer)
    tick_spacing: Mapped[int | None] = mapped_column(Integer)
    variant: Mapped[str | None] = mapped_column(String(16))            # uniswap | pancake | slipstream | v2 | aerodrome
    pair: Mapped[str | None] = mapped_column(String(32))               # base/quote
    tvl_usd: Mapped[float | None] = mapped_column(Float)               # real token balances, from discovery
    active: Mapped[bool | None] = mapped_column(Boolean, default=True)  # watched by the loop
    source: Mapped[str | None] = mapped_column(String(16))             # bootstrap | discovery
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class Block(Base):
    """One row per processed block: the authoritative list of blocks plus the raw inputs for gas and time features."""
    __tablename__ = "blocks"
    block_number: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    timestamp: Mapped[int] = mapped_column(Integer)                    # chain time, unix seconds
    block_hash: Mapped[str] = mapped_column(String(66))
    parent_hash: Mapped[str] = mapped_column(String(66))
    base_fee_per_gas: Mapped[str] = mapped_column(String(32))
    gas_used: Mapped[int] = mapped_column(Integer)
    gas_limit: Mapped[int] = mapped_column(Integer)
    next_base_fee_per_gas: Mapped[str] = mapped_column(String(32))     # EIP-1559 base fee of block+1
    gas_price_wei: Mapped[str] = mapped_column(String(32))             # next base fee + priority fee, used for gas_usd
    eth_price_usd: Mapped[float] = mapped_column(Float)
    gas_usd: Mapped[float] = mapped_column(Float)                      # decision-time gas estimate for this block
    rpc_latency_ms: Mapped[float] = mapped_column(Float)
    pools_fetched: Mapped[int] = mapped_column(Integer)
    parent_mismatch: Mapped[bool | None] = mapped_column(Boolean)   # parentHash != stored hash of block-1 (reorg or gap)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow)


class PoolSnapshot(Base):
    __tablename__ = "pool_snapshots"
    __table_args__ = (Index("ux_pool_snapshots_pool_block", "pool_id", "block_number", unique=True),)
    id: Mapped[int] = mapped_column(primary_key=True)
    pool_id: Mapped[str] = mapped_column(ForeignKey("pools.pool_id"), index=True)
    block_number: Mapped[int] = mapped_column(ForeignKey("blocks.block_number"), index=True)
    price: Mapped[float] = mapped_column(Float)
    sqrt_price_x96: Mapped[str | None] = mapped_column(String(80))
    tick: Mapped[int | None] = mapped_column(Integer)
    liquidity: Mapped[str | None] = mapped_column(String(80))
    reserve0: Mapped[str | None] = mapped_column(String(80))
    reserve1: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow)


class Opportunity(Base):
    __tablename__ = "opportunities"
    __table_args__ = (Index("ux_opportunities_block_pair", "block_number", "buy_pool_id", "sell_pool_id", unique=True),)
    id: Mapped[int] = mapped_column(primary_key=True)
    block_number: Mapped[int] = mapped_column(ForeignKey("blocks.block_number"), index=True)
    head_block: Mapped[int | None] = mapped_column(Integer)            # chain head re-read just before the risk check
    strategy: Mapped[str] = mapped_column(String(32), default="cross_dex_arbitrage")
    pair: Mapped[str | None] = mapped_column(String(32), index=True)
    buy_pool_id: Mapped[str] = mapped_column(ForeignKey("pools.pool_id"))
    sell_pool_id: Mapped[str] = mapped_column(ForeignKey("pools.pool_id"))
    buy_price: Mapped[float] = mapped_column(Float)
    sell_price: Mapped[float] = mapped_column(Float)
    spread_bps: Mapped[float] = mapped_column(Float)
    fee_floor_bps: Mapped[float] = mapped_column(Float)
    best_trade_size_usd: Mapped[int] = mapped_column(Integer)          # unconstrained max(net) over the grid
    best_net_usd: Mapped[float] = mapped_column(Float)
    chosen_trade_size_usd: Mapped[int | None] = mapped_column(Integer)  # best size that passes every rule
    chosen_net_usd: Mapped[float | None] = mapped_column(Float)
    risk_passed: Mapped[bool] = mapped_column(Boolean)
    risk_violations: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow)


class Simulation(Base):
    __tablename__ = "simulations"
    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"), index=True)
    trade_size_usd: Mapped[int] = mapped_column(Integer)
    gross_usd: Mapped[float] = mapped_column(Float)
    flash_fee_usd: Mapped[float] = mapped_column(Float)
    gas_usd: Mapped[float] = mapped_column(Float)
    buffer_usd: Mapped[float] = mapped_column(Float)
    net_usd: Mapped[float] = mapped_column(Float)
    exact: Mapped[bool] = mapped_column(Boolean)
    dex_fee_usd: Mapped[float | None] = mapped_column(Float)    # diagnostic: already inside gross_usd
    slippage_usd: Mapped[float | None] = mapped_column(Float)   # diagnostic: already inside gross_usd


class PaperTrade(Base):
    __tablename__ = "paper_trades"
    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"), index=True)
    opened_block: Mapped[int] = mapped_column(Integer)
    resolved_block: Mapped[int | None] = mapped_column(Integer)
    resolved_ts: Mapped[int | None] = mapped_column(Integer)           # chain timestamp of resolved_block
    resolve_lag_blocks: Mapped[int | None] = mapped_column(Integer)    # resolved_block - opened_block (1 = N+1)
    trade_size_usd: Mapped[int] = mapped_column(Integer)
    expected_net_usd: Mapped[float] = mapped_column(Float)
    actual_net_usd: Mapped[float | None] = mapped_column(Float)   # same trade re-simulated on the resolve block (buffer included)
    actual_gross_usd: Mapped[float | None] = mapped_column(Float)
    actual_gas_usd: Mapped[float | None] = mapped_column(Float)
    realized_net_usd: Mapped[float | None] = mapped_column(Float)  # atomic-execution P&L, see mev/paper/executor.py
    execution_loss_usd: Mapped[float | None] = mapped_column(Float)  # expected_net_usd - realized_net_usd
    still_profitable: Mapped[bool | None] = mapped_column(Boolean)   # actual_net_usd >= min_net_profit_usd
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow)


class Swap(Base):
    """Decoded pool events (spec section 6): V3 Swap/Mint/Burn and x*y=k Swap/Sync, one row per log."""
    __tablename__ = "swaps"
    __table_args__ = (Index("ux_swaps_tx_log", "transaction_hash", "log_index", unique=True),)
    id: Mapped[int] = mapped_column(primary_key=True)
    pool_id: Mapped[str] = mapped_column(ForeignKey("pools.pool_id"), index=True)
    block_number: Mapped[int] = mapped_column(Integer, index=True)
    transaction_hash: Mapped[str] = mapped_column(String(66))
    log_index: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(8))                 # swap | sync | mint | burn
    amount0: Mapped[str | None] = mapped_column(String(80))            # signed, pool's perspective (+ = into the pool)
    amount1: Mapped[str | None] = mapped_column(String(80))
    sqrt_price_x96: Mapped[str | None] = mapped_column(String(80))     # V3 swap: price after
    liquidity: Mapped[str | None] = mapped_column(String(80))          # V3 swap: in-range liquidity after
    tick: Mapped[int | None] = mapped_column(Integer)
    reserve0: Mapped[str | None] = mapped_column(String(80))           # x*y=k sync: reserves after
    reserve1: Mapped[str | None] = mapped_column(String(80))
    volume_usd: Mapped[float | None] = mapped_column(Float)            # |amount1| in token1 (USDC) terms, swaps only


class Feature(Base):
    """One feature vector per opportunity (spec section 11), computed at decision time from stored rows only."""
    __tablename__ = "features"
    __table_args__ = (Index("ux_features_opportunity", "opportunity_id", unique=True),)
    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"))
    feature_version: Mapped[int] = mapped_column(Integer)
    block_number: Mapped[int] = mapped_column(Integer, index=True)
    timestamp: Mapped[int] = mapped_column(Integer)
    spread_bps: Mapped[float] = mapped_column(Float)
    trade_size_usd: Mapped[int] = mapped_column(Integer)
    liquidity_usd: Mapped[float] = mapped_column(Float)
    buy_liquidity_usd: Mapped[float] = mapped_column(Float)
    sell_liquidity_usd: Mapped[float] = mapped_column(Float)
    price_impact_bps: Mapped[float] = mapped_column(Float)
    volatility_1m: Mapped[float | None] = mapped_column(Float)
    volatility_5m: Mapped[float | None] = mapped_column(Float)
    volume_1m: Mapped[float] = mapped_column(Float)
    volume_5m: Mapped[float] = mapped_column(Float)
    volume_velocity: Mapped[float | None] = mapped_column(Float)
    pool_imbalance: Mapped[float] = mapped_column(Float)
    gas_usd: Mapped[float] = mapped_column(Float)
    rpc_latency_ms: Mapped[float] = mapped_column(Float)
    block_age_ms: Mapped[float] = mapped_column(Float)
    opportunity_age_ms: Mapped[float] = mapped_column(Float)
    historical_success_rate: Mapped[float | None] = mapped_column(Float)
    historical_capture_time_ms: Mapped[float | None] = mapped_column(Float)
    dex_id: Mapped[str] = mapped_column(String(64))
    token_pair_id: Mapped[str] = mapped_column(String(32))
    hour_of_day: Mapped[int] = mapped_column(Integer)
    day_of_week: Mapped[int] = mapped_column(Integer)


class Action(Base):
    """What the laptop agent (mev/agent) did about an opportunity: alert, webhook, on-chain verification,
    lifetime tracking. Advisory records only; nothing here can trade."""
    __tablename__ = "actions"
    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"), index=True)
    block_number: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))          # alert | webhook | verify | lifetime
    status: Mapped[str] = mapped_column(String(16))        # ok | failed | skipped
    payload: Mapped[str] = mapped_column(Text, default="{}")   # JSON
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=utcnow)


class TokenRow(Base):
    __tablename__ = "tokens"
    address: Mapped[str] = mapped_column(String(42), primary_key=True)   # lowercase
    chain_id: Mapped[int] = mapped_column(Integer)
    symbol: Mapped[str] = mapped_column(String(16))
    decimals: Mapped[int] = mapped_column(Integer)


class Meta(Base):
    """Small key/value store for resumable jobs (e.g. discovery scan progress)."""
    __tablename__ = "meta"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
