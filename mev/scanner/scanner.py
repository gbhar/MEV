"""Deterministic candidate detection. scan(): every pool of a token pair against every other, across all DEXs
(two legs, quote -> base -> quote). scan_routes(): triangular routes quote -> A -> B -> quote through the best
venue per hop. Both emit Candidates; nothing here executes."""
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from itertools import combinations, permutations

from mev.dex.base import DexAdapter
from mev.pools.state import PoolState


@dataclass(frozen=True)
class Candidate:
    buy_pool_id: str     # two legs: where the base token is cheaper, in quote terms. Routes: the first leg's pool
    sell_pool_id: str    # two legs: where it is dearer. Routes: the last leg's pool
    buy_price: Decimal   # quote per base (routes: 1)
    sell_price: Decimal  # quote per base (routes: the gross round-trip multiple, e.g. 1.0042)
    spread_bps: Decimal  # before fees; must clear fee_floor_bps
    fee_floor_bps: Decimal   # the sum of the legs' pool fees
    block_number: int
    pair: str = ""           # e.g. WETH/USDC (base/quote), or USDC>WETH>cbBTC>USDC for a route
    route: tuple[str, ...] = ()    # pool ids leg by leg (empty for the classic two-leg candidate)
    path: tuple[str, ...] = ()     # token addresses (lowercase) the route passes through, len(route) + 1

    @property
    def legs(self) -> int:
        return len(self.route) or 2

    @property
    def pools(self) -> tuple[str, ...]:
        return self.route or (self.buy_pool_id, self.sell_pool_id)

    @property
    def key(self) -> tuple[str, ...]:
        """What identifies the trade for repeat/busy bookkeeping and the DB uniqueness index."""
        return self.route or (self.buy_pool_id, self.sell_pool_id)

    @property
    def route_str(self) -> str:
        return ">".join(self.route)

    @property
    def path_str(self) -> str:
        return ">".join(self.path)


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


def hop_rate(st: PoolState, token_in: str, adapters: dict[str, DexAdapter]) -> Decimal:
    """Marginal units of the other token per unit of token_in (decimal-adjusted), before the pool fee."""
    p = adapters[st.pool.kind].marginal_price(st)   # token1 per token0
    if p <= 0:
        return Decimal(0)
    return p if token_in == st.pool.token0.key else 1 / p


def scan_routes(states: dict[str, PoolState], adapters: dict[str, DexAdapter], quotes: tuple[str, ...] = ("USDC", "WETH"),
                max_candidates: int = 50) -> list[Candidate]:
    """Triangular candidates quote -> A -> B -> quote. For every hop the venue with the best marginal rate (after its fee)
    is used, so each triangle yields at most one candidate per quote token. A candidate exists when the gross
    round-trip multiple clears the sum of the three fees, the same rule the two-leg scanner applies."""
    by_pair: dict[tuple[str, str], list[PoolState]] = defaultdict(list)
    symbols: dict[str, str] = {}
    for st in states.values():
        t0, t1 = st.pool.token0, st.pool.token1
        by_pair[(t0.key, t1.key)].append(st)
        by_pair[(t1.key, t0.key)].append(st)
        symbols[t0.key], symbols[t1.key] = t0.symbol, t1.symbol

    def best_hop(a: str, b: str) -> tuple[PoolState, Decimal, Decimal] | None:
        """(pool, gross rate, fee bps) of the best venue for a -> b."""
        best = None
        for st in by_pair.get((a, b), ()):
            r = hop_rate(st, a, adapters)
            if r <= 0:
                continue
            fee = Decimal(fee_bps(st))
            net = r * (1 - fee / 10_000)
            if best is None or net > best[3]:
                best = (st, r, fee, net)
        return best[:3] if best else None

    tokens = sorted(symbols)
    out: list[Candidate] = []
    seen: set[tuple[str, ...]] = set()
    for q in tokens:
        if symbols[q] not in quotes:
            continue
        others = [t for t in tokens if t != q]
        for a, b in permutations(others, 2):
            h1, h2, h3 = best_hop(q, a), best_hop(a, b), best_hop(b, q)
            if h1 is None or h2 is None or h3 is None:
                continue
            pools = (h1[0].pool.pool_id, h2[0].pool.pool_id, h3[0].pool.pool_id)
            if len(set(pools)) < 3 or pools in seen:
                continue
            gross = h1[1] * h2[1] * h3[1]
            spread_bps = (gross - 1) * 10_000
            fee_floor = h1[2] + h2[2] + h3[2]
            if spread_bps <= fee_floor:
                continue
            seen.add(pools)
            block = h1[0].block_number
            out.append(Candidate(pools[0], pools[-1], Decimal(1), gross, spread_bps, fee_floor, block,
                                 ">".join(symbols[t] for t in (q, a, b, q)), pools, (q, a, b, q)))
    out.sort(key=lambda c: c.spread_bps - c.fee_floor_bps, reverse=True)
    return out[:max_candidates]
