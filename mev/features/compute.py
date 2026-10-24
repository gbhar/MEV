"""Feature vector per opportunity (spec section 11), computed at decision time from stored rows and the
current states only, so the same inputs always give the same vector (feature_version bumps on any change)."""
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mev.db.models import Feature, Opportunity, PaperTrade, PoolSnapshot, Swap
from mev.dex.base import DexAdapter
from mev.dex.uniswap_v3 import Q96
from mev.pools.state import PoolState
from mev.scanner.scanner import Candidate
from mev.simulator.simulate import SimResult

FEATURE_VERSION = 1
BLOCKS_1M, BLOCKS_5M = 30, 150          # Base: one block every 2 s
BLOCK_MS = 2000

_pair_first_seen: dict[tuple[str, str], int] = {}


@dataclass
class BlockContext:
    block_number: int
    timestamp: int
    gas_usd: Decimal
    rpc_latency_ms: float
    wall_time: float = 0.0              # time.time() when the block was processed; 0 in replay


def note_candidates(block_number: int, pairs: set[tuple[str, str]]) -> None:
    """Track since when each pair has been a candidate on consecutive processed blocks."""
    for pair in list(_pair_first_seen):
        if pair not in pairs:
            del _pair_first_seen[pair]
    for pair in pairs:
        _pair_first_seen.setdefault(pair, block_number)


def depth_usd(st: PoolState, adapters: dict[str, DexAdapter]) -> float:
    """USD value of the reserves the swap math sees: real reserves for x*y=k, virtual reserves at the
    current tick for V3 (what the within-tick formula uses; far above what a 1% move can absorb)."""
    p = st.pool
    price = float(adapters[p.kind].marginal_price(st))
    if st.reserve0 is not None and st.reserve1 is not None:
        return st.reserve0 / 10**p.token0.decimals * price + st.reserve1 / 10**p.token1.decimals
    assert st.liquidity is not None and st.sqrt_price_x96 is not None
    virt1 = st.liquidity * st.sqrt_price_x96 / Q96 / 10**p.token1.decimals
    virt0 = st.liquidity * Q96 / st.sqrt_price_x96 / 10**p.token0.decimals * price
    return virt0 + virt1


def _volatility_bps(prices: list[float]) -> float | None:
    rets = [math.log(b / a) for a, b in zip(prices, prices[1:]) if a > 0 and b > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    return math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) * 10_000


def compute(db: Session, opportunity_id: int, c: Candidate, sim: SimResult, states: dict[str, PoolState],
            adapters: dict[str, DexAdapter], ctx: BlockContext) -> Feature:
    buy, sell = states[c.buy_pool_id], states[c.sell_pool_id]
    n = ctx.block_number
    prices = db.scalars(select(PoolSnapshot.price).where(PoolSnapshot.pool_id == c.buy_pool_id, PoolSnapshot.block_number > n - BLOCKS_5M,
                                                         PoolSnapshot.block_number <= n).order_by(PoolSnapshot.block_number)).all()
    vol_1m = _volatility_bps(prices[-(BLOCKS_1M + 1):])
    vol_5m = _volatility_bps(prices)
    pools = [c.buy_pool_id, c.sell_pool_id]

    def volume(blocks: int) -> float:
        return float(db.scalar(select(func.coalesce(func.sum(Swap.volume_usd), 0.0)).where(
            Swap.pool_id.in_(pools), Swap.event_type == "swap", Swap.block_number > n - blocks, Swap.block_number <= n)) or 0.0)

    v1, v5 = volume(BLOCKS_1M), volume(BLOCKS_5M)
    outcomes = db.execute(select(PaperTrade.still_profitable, PaperTrade.resolve_lag_blocks)
                          .join(Opportunity, PaperTrade.opportunity_id == Opportunity.id)
                          .where(Opportunity.buy_pool_id == c.buy_pool_id, Opportunity.sell_pool_id == c.sell_pool_id,
                                 PaperTrade.resolved_block.is_not(None))).all()
    count = len(outcomes)
    success_rate = sum(1 for ok, _ in outcomes if ok) / count if count else None
    lags = [lag for _, lag in outcomes if lag is not None]
    lag = sum(lags) / len(lags) if lags else None
    buy_depth, sell_depth = depth_usd(buy, adapters), depth_usd(sell, adapters)
    first_seen = _pair_first_seen.get((c.buy_pool_id, c.sell_pool_id), n)
    ts = datetime.fromtimestamp(ctx.timestamp, tz=timezone.utc)
    return Feature(
        opportunity_id=opportunity_id, feature_version=FEATURE_VERSION, block_number=n, timestamp=ctx.timestamp,
        spread_bps=float(c.spread_bps), trade_size_usd=sim.trade_size_usd,
        liquidity_usd=buy_depth + sell_depth, buy_liquidity_usd=buy_depth, sell_liquidity_usd=sell_depth,
        price_impact_bps=float(sim.slippage_usd / sim.trade_size_usd * 10_000) if sim.trade_size_usd else 0.0,
        volatility_1m=vol_1m, volatility_5m=vol_5m, volume_1m=v1, volume_5m=v5,
        volume_velocity=(v1 / (v5 / 5)) if v5 > 0 else None,
        pool_imbalance=buy_depth / (buy_depth + sell_depth) if buy_depth + sell_depth > 0 else 0.5,
        gas_usd=float(ctx.gas_usd), rpc_latency_ms=ctx.rpc_latency_ms,
        block_age_ms=max(0.0, (ctx.wall_time - ctx.timestamp) * 1000) if ctx.wall_time else 0.0,
        opportunity_age_ms=float((n - first_seen) * BLOCK_MS),
        historical_success_rate=success_rate if count else None,
        historical_capture_time_ms=float(lag) * BLOCK_MS if count and lag is not None else None,
        dex_id=f"{buy.pool.dex}>{sell.pool.dex}", token_pair_id=buy.pool.pair, hour_of_day=ts.hour, day_of_week=ts.weekday())
