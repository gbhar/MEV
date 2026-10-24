"""Deterministic candidate detection. Emits candidates; never executes."""
from dataclasses import dataclass
from decimal import Decimal
from itertools import combinations

from mev.dex.base import DexAdapter
from mev.pools.state import PoolState


@dataclass(frozen=True)
class Candidate:
    buy_pool_id: str     # where token0 (WETH) is cheaper in token1 terms
    sell_pool_id: str
    buy_price: Decimal
    sell_price: Decimal
    spread_bps: Decimal
    fee_floor_bps: Decimal   # both pool fees; the spread must clear this before anything else
    block_number: int


def scan(states: dict[str, PoolState], adapters: dict[str, DexAdapter]) -> list[Candidate]:
    out: list[Candidate] = []
    for a, b in combinations(states.values(), 2):
        if (a.pool.token0.address.lower(), a.pool.token1.address.lower()) != (b.pool.token0.address.lower(), b.pool.token1.address.lower()):
            continue  # V1 only compares pools with identical token ordering
        pa, pb = adapters[a.pool.kind].marginal_price(a), adapters[b.pool.kind].marginal_price(b)
        if pa <= 0 or pb <= 0:
            continue
        lo, hi = (a, b) if pa < pb else (b, a)
        lo_p, hi_p = min(pa, pb), max(pa, pb)
        spread_bps = (hi_p - lo_p) / lo_p * 10_000
        fee_floor = Decimal(a.pool.fee_bps + b.pool.fee_bps)
        if spread_bps > fee_floor:
            out.append(Candidate(lo.pool.pool_id, hi.pool.pool_id, lo_p, hi_p, spread_bps, fee_floor, a.block_number))
    return out
