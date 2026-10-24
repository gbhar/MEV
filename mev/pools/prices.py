"""USD reference prices per block, derived from the fetched pool states themselves: dollar tokens are 1,
every other token is priced through the deepest pool that links it to an already-priced token."""
from decimal import Decimal

from mev.dex.base import DexAdapter
from mev.pools.registry import STABLE_SYMBOLS
from mev.pools.state import PoolState


def depth_score(st: PoolState) -> int:
    """Rough size used only to pick the reference pool: in-range liquidity for V3, reserve product for x*y=k."""
    if st.liquidity is not None:
        return st.liquidity
    return (st.reserve0 or 0) * (st.reserve1 or 0)


def usd_prices(states: dict[str, PoolState], adapters: dict[str, DexAdapter]) -> dict[str, Decimal]:
    """token address (lowercase) -> USD price. Tokens with no path to a dollar token are absent."""
    prices: dict[str, Decimal] = {}
    for st in states.values():
        for t in (st.pool.token0, st.pool.token1):
            if t.symbol in STABLE_SYMBOLS:
                prices[t.key] = Decimal(1)
    pools = sorted(states.values(), key=depth_score, reverse=True)
    for _ in range(4):   # a few hops cover everything we list (e.g. VIRTUAL -> WETH -> USDC)
        changed = False
        for st in pools:
            t0, t1 = st.pool.token0, st.pool.token1
            p = adapters[st.pool.kind].marginal_price(st)   # token1 per token0
            if p <= 0:
                continue
            if t0.key in prices and t1.key not in prices:
                prices[t1.key] = prices[t0.key] / p
                changed = True
            elif t1.key in prices and t0.key not in prices:
                prices[t0.key] = prices[t1.key] * p
                changed = True
        if not changed:
            break
    return prices
