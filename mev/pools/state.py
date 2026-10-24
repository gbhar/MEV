"""Fetch the on-chain state a swap simulation needs, pinned to a block number.

Uniswap V3 pools also carry a tick window: the tickBitmap words around the current tick and the
liquidityNet of every initialized tick within settings.v3_tick_window ticks of it, so the swap math
can cross ticks exactly. All of a pool's view calls go through Multicall3 in one eth_call per block.
The previous block's state is the hint for which bitmap words and ticks to ask for; only when the
price moved by more than v3_tick_margin or a position changed inside the window is a second
eth_call needed for the ticks that were not fetched speculatively."""
from dataclasses import dataclass

from web3 import AsyncWeb3

from mev.chain import abi
from mev.chain.addresses import MULTICALL3
from mev.config import settings
from mev.pools.registry import Pool


@dataclass
class PoolState:
    pool: Pool
    block_number: int
    # uniswap_v3
    sqrt_price_x96: int | None = None
    tick: int | None = None
    liquidity: int | None = None
    tick_bitmap: dict[int, int] | None = None   # tickBitmap word position -> uint256 word, as fetched
    tick_nets: dict[int, int] | None = None     # liquidityNet of every initialized tick in [tick_lo, tick_hi]
    tick_lo: int | None = None                  # the window the quote math may cross ticks in
    tick_hi: int | None = None
    # constant_product
    reserve0: int | None = None
    reserve1: int | None = None


_last_v3: dict[str, PoolState] = {}   # pool_id -> last fetched state, the hint for the speculative fetch


def reset_hints() -> None:
    """Forget the speculative tick windows (after a reorg or replica switch the hint may describe another chain)."""
    _last_v3.clear()

_SLOT0 = ["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"]
_TICKS = ["uint128", "int128", "uint256", "uint256", "int56", "uint160", "uint32", "bool"]


def word_range(tick_lo: int, tick_hi: int, spacing: int) -> range:
    """tickBitmap word positions covering every tick in [tick_lo, tick_hi]."""
    return range((tick_lo // spacing) >> 8, ((tick_hi // spacing) >> 8) + 1)


def initialized_ticks(bitmap: dict[int, int], spacing: int, tick_lo: int, tick_hi: int) -> list[int]:
    """Initialized ticks in [tick_lo, tick_hi] according to the fetched bitmap words, ascending."""
    out = []
    for word_pos in sorted(bitmap):
        word = bitmap[word_pos]
        if word == 0:
            continue
        for bit in range(256):
            if word >> bit & 1:
                tick = (word_pos * 256 + bit) * spacing
                if tick_lo <= tick <= tick_hi:
                    out.append(tick)
    return out


async def multicall(w3: AsyncWeb3, calls: list[tuple[str, bytes]], block_number: int) -> list[bytes | None]:
    """(target, calldata) pairs -> return data (None where the call failed), all at block_number in one eth_call."""
    if not calls:
        return []
    mc = w3.eth.contract(address=AsyncWeb3.to_checksum_address(MULTICALL3), abi=abi.MULTICALL3)
    results = await mc.functions.aggregate3([(target, True, data) for target, data in calls]).call(block_identifier=block_number)
    return [bytes(r[1]) if r[0] else None for r in results]


class _V3Plan:
    """The calls one V3 pool needs this block, and how to turn the answers into a PoolState."""

    def __init__(self, w3: AsyncWeb3, pool: Pool):
        self.pool, self.addr = pool, AsyncWeb3.to_checksum_address(pool.address)
        self.c = w3.eth.contract(address=self.addr, abi=abi.UNISWAP_V3_POOL)
        self.spacing = pool.tick_spacing or 1
        reach = settings.v3_tick_window + settings.v3_tick_margin
        prev = _last_v3.get(pool.pool_id)
        self.words: list[int] = []
        self.spec: list[int] = []
        if prev is not None and prev.tick is not None and prev.tick_bitmap is not None:
            self.words = list(word_range(prev.tick - reach, prev.tick + reach, self.spacing))
            self.spec = initialized_ticks(prev.tick_bitmap, self.spacing, prev.tick - reach, prev.tick + reach)

    def calls(self) -> list[tuple[str, bytes]]:
        return ([(self.addr, self.c.encode_abi("slot0", args=[])), (self.addr, self.c.encode_abi("liquidity", args=[]))]
                + [(self.addr, self.c.encode_abi("tickBitmap", args=[w])) for w in self.words]
                + [(self.addr, self.c.encode_abi("ticks", args=[t])) for t in self.spec])

    def parse(self, w3: AsyncWeb3, raw: list[bytes | None], block_number: int) -> tuple[PoolState, list[int], list[int]]:
        """State from the first round of answers, plus the bitmap words and ticks still missing for the window."""
        if raw[0] is None or raw[1] is None:
            raise RuntimeError(f"{self.pool.pool_id}: slot0/liquidity call failed")
        slot0 = w3.codec.decode(_SLOT0, raw[0])
        liquidity = w3.codec.decode(["uint256"], raw[1])[0]
        tick = slot0[1]
        bitmap = {w: w3.codec.decode(["uint256"], r)[0] for w, r in zip(self.words, raw[2:2 + len(self.words)]) if r is not None}
        nets = {t: w3.codec.decode(_TICKS, r)[1] for t, r in zip(self.spec, raw[2 + len(self.words):]) if r is not None}
        lo, hi = tick - settings.v3_tick_window, tick + settings.v3_tick_window
        st = PoolState(self.pool, block_number, sqrt_price_x96=slot0[0], tick=tick, liquidity=liquidity,
                       tick_bitmap=bitmap, tick_nets=nets, tick_lo=lo, tick_hi=hi)
        missing_words = [w for w in word_range(lo, hi, self.spacing) if w not in bitmap]
        return st, missing_words, []

    def finish(self, st: PoolState) -> PoolState:
        """Trim the window to the ticks actually known and publish the hint for next block."""
        assert st.tick is not None and st.tick_bitmap is not None and st.tick_nets is not None
        needed = initialized_ticks(st.tick_bitmap, self.spacing, st.tick_lo, st.tick_hi)
        if len(needed) > settings.v3_max_ticks:   # keep the band nearest the price; the window shrinks to it
            needed = sorted(sorted(needed, key=lambda t: abs(t - st.tick))[:settings.v3_max_ticks])
            st.tick_lo, st.tick_hi = needed[0], needed[-1]
        st.tick_nets = {t: st.tick_nets[t] for t in needed if t in st.tick_nets}
        missing = [t for t in needed if t not in st.tick_nets]
        if missing:   # a tick inside the window with no liquidityNet: shrink the window to exclude it
            below = [t for t in missing if t < st.tick]
            above = [t for t in missing if t > st.tick]
            if below:
                st.tick_lo = max(below) + 1
            if above:
                st.tick_hi = min(above) - 1
            st.tick_nets = {t: n for t, n in st.tick_nets.items() if st.tick_lo <= t <= st.tick_hi}
        _last_v3[self.pool.pool_id] = st
        return st


async def fetch_states(w3: AsyncWeb3, pools: list[Pool], block_number: int) -> dict[str, PoolState]:
    """Every pool's state at block_number in ONE eth_call (two when a V3 tick window grew past its hint).
    A pool whose calls failed is left out; the caller decides whether that is fatal."""
    plans = {p.pool_id: _V3Plan(w3, p) for p in pools if p.kind == "uniswap_v3"}
    calls: list[tuple[str, bytes]] = []
    spans: dict[str, tuple[int, int]] = {}
    for p in pools:
        if p.kind == "uniswap_v3":
            cs = plans[p.pool_id].calls()
        else:
            c = w3.eth.contract(address=AsyncWeb3.to_checksum_address(p.address), abi=abi.CONSTANT_PRODUCT_POOL)
            cs = [(AsyncWeb3.to_checksum_address(p.address), c.encode_abi("getReserves", args=[]))]
        spans[p.pool_id] = (len(calls), len(calls) + len(cs))
        calls += cs
    raw = await multicall(w3, calls, block_number)

    states: dict[str, PoolState] = {}
    second: list[tuple[str, bytes]] = []
    second_meta: list[tuple[str, str, int]] = []   # (pool_id, "word"|"tick", key)
    for p in pools:
        a, b = spans[p.pool_id]
        chunk = raw[a:b]
        if p.kind != "uniswap_v3":
            if chunk[0] is None:
                continue
            r0, r1, _ = w3.codec.decode(["uint256", "uint256", "uint256"], chunk[0])
            states[p.pool_id] = PoolState(p, block_number, reserve0=r0, reserve1=r1)
            continue
        try:
            st, missing_words, _ = plans[p.pool_id].parse(w3, chunk, block_number)
        except RuntimeError:
            continue
        states[p.pool_id] = st
        for wpos in missing_words:
            second.append((plans[p.pool_id].addr, plans[p.pool_id].c.encode_abi("tickBitmap", args=[wpos])))
            second_meta.append((p.pool_id, "word", wpos))
    if second:   # bitmap words outside the hint (first block, or the price moved past the margin)
        raw2 = await multicall(w3, second, block_number)
        for (pid, _, wpos), r in zip(second_meta, raw2):
            if r is not None:
                states[pid].tick_bitmap[wpos] = w3.codec.decode(["uint256"], r)[0]
        third: list[tuple[str, bytes]] = []
        third_meta: list[tuple[str, int]] = []
        for pid, plan in plans.items():
            st = states.get(pid)
            if st is None:
                continue
            for t in initialized_ticks(st.tick_bitmap, plan.spacing, st.tick_lo, st.tick_hi):
                if t not in st.tick_nets:
                    third.append((plan.addr, plan.c.encode_abi("ticks", args=[t])))
                    third_meta.append((pid, t))
        if third:
            raw3 = await multicall(w3, third, block_number)
            for (pid, t), r in zip(third_meta, raw3):
                if r is not None:
                    states[pid].tick_nets[t] = w3.codec.decode(_TICKS, r)[1]
    for pid, plan in plans.items():
        if pid in states:
            plan.finish(states[pid])
    return states


async def fetch_state(w3: AsyncWeb3, pool: Pool, block_number: int) -> PoolState:
    """Single-pool convenience for scripts and tests; the loop uses fetch_states()."""
    states = await fetch_states(w3, [pool], block_number)
    if pool.pool_id not in states:
        raise RuntimeError(f"{pool.pool_id}: state fetch failed at block {block_number}")
    return states[pool.pool_id]
