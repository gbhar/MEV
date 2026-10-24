"""Deterministic candidate detection: every pool of a token pair against every other, across all DEXs.
Emits candidates; never executes."""
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from itertools import combinations

from mev.dex.base import DexAdapter
from mev.pools.state import PoolState


@dataclass(frozen=True)
class Candidate:
    buy_pool_id: str     # where the base token is cheaper, in quote terms
    sell_pool_id: str
    buy_price: Decimal   # quote per base
    sell_price: Decimal
    spread_bps: Decimal
    fee_floor_bps: Decimal   # both pool fees; the spread must clear this before anything else
    block_number: int
    pair: str = ""           # e.g. WETH/USDC (base/quote)


def fee_bps(st: PoolState) -> float:
    return (st.fee_ppm if st.fee_ppm is not None else st.pool.fee_ppm) / 100


def quote_per_base(st: PoolState, adapters: dict[str, DexAdapter]) -> Decimal:
    p = adapters[st.pool.kind].marginal_price(st)   # token1 per token0
    if p <= 0:
        return Decimal(0)
    return p if st.pool.quote_is_token1 else 1 / p


def scan(states: dict[str, PoolState], adapters: dict[str, DexAdapter]) -> list[Candidate]:
    groups: dict[tuple[str, str], list[PoolState]] = defaultdict(list)
    for st in states.values():
        groups[st.pool.pair_key].append(st)
    out: list[Candidate] = []
    for members in groups.values():
        priced = [(st, quote_per_base(st, adapters)) for st in members]
        priced = [(st, p) for st, p in priced if p > 0]
        for (a, pa), (b, pb) in combinations(priced, 2):
            lo, hi = (a, b) if pa < pb else (b, a)
            lo_p, hi_p = min(pa, pb), max(pa, pb)
            spread_bps = (hi_p - lo_p) / lo_p * 10_000
            fee_floor = Decimal(fee_bps(a) + fee_bps(b))
            if spread_bps > fee_floor:
                out.append(Candidate(lo.pool.pool_id, hi.pool.pool_id, lo_p, hi_p, spread_bps, fee_floor, a.block_number, a.pool.pair))
    out.sort(key=lambda c: c.spread_bps - c.fee_floor_bps, reverse=True)
    return out
