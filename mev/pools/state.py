"""Fetch the on-chain state a swap simulation needs, pinned to a block number.

All pools go through Multicall3 in a few chunked eth_calls per block. Uniswap V3 kinds (Uniswap, Sushi,
PancakeSwap, Aerodrome Slipstream) also carry a tick window: tickBitmap words around the current tick and
the liquidityNet of the initialized ticks nearest the price (at most settings.v3_max_ticks per pool), so
the swap math can cross ticks exactly. Forks differ in slot0/ticks layout, but the leading fields are the
same, so decoding the leading types works for all of them. The previous block's state is the hint for
which words and ticks to ask for, and pools that emitted no event in a block are carried forward."""
import asyncio
from dataclasses import dataclass, replace

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
    fee_ppm: int | None = None                  # live fee for pools with dynamic fees (Slipstream); else the registry fee
    # constant_product
    reserve0: int | None = None
    reserve1: int | None = None


_last_v3: dict[str, PoolState] = {}   # pool_id -> last fetched state, the hint for the speculative fetch

_SLOT0 = ["uint160", "int24"]          # leading fields of every fork's slot0
_TICKS = ["uint128", "int128"]         # (liquidityGross, liquidityNet) lead every fork's ticks()


def reset_hints() -> None:
    """Forget the speculative tick windows (after a reorg or replica switch the hint may describe another chain)."""
    _last_v3.clear()


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
    """(target, calldata) pairs -> return data (None where the call failed), all at block_number. Chunked into
    several eth_calls that run concurrently so one block never needs one enormous call."""
    if not calls:
        return []
    mc = w3.eth.contract(address=AsyncWeb3.to_checksum_address(MULTICALL3), abi=abi.MULTICALL3)
    n = settings.multicall_chunk
    chunks = [calls[i:i + n] for i in range(0, len(calls), n)]
    results = await asyncio.gather(*(mc.functions.aggregate3([(t, True, d) for t, d in ch]).call(block_identifier=block_number) for ch in chunks))
    return [bytes(r[1]) if r[0] else None for res in results for r in res]


class _V3Plan:
    """The calls one V3-kind pool needs this block, and how to turn the answers into a PoolState.

    Tick data (bitmap words and liquidityNet) only changes when a position is minted or burned, so it is
    reused from the previous block unless refresh_ticks is set (the pool had a Mint/Burn, the price moved
    close to the edge of the cached window, or a periodic full refresh is due)."""

    def __init__(self, w3: AsyncWeb3, pool: Pool, refresh_ticks: bool):
        self.pool, self.addr = pool, AsyncWeb3.to_checksum_address(pool.address)
        self.c = w3.eth.contract(address=self.addr, abi=abi.UNISWAP_V3_POOL)
        self.spacing = pool.tick_spacing or 1
        self.cap = max(settings.v3_max_ticks, settings.v3_max_ticks_spacing1 // self.spacing)
        reach = settings.v3_tick_window + settings.v3_tick_margin
        self.prev = _last_v3.get(pool.pool_id)
        self.reuse = (not refresh_ticks and self.prev is not None and self.prev.tick is not None
                      and self.prev.tick_bitmap is not None and self.prev.tick_nets is not None)
        self.words: list[int] = []
        self.spec: list[int] = []
        if not self.reuse and self.prev is not None and self.prev.tick is not None and self.prev.tick_bitmap is not None:
            self.words = list(word_range(self.prev.tick - reach, self.prev.tick + reach, self.spacing))
            near = initialized_ticks(self.prev.tick_bitmap, self.spacing, self.prev.tick - reach, self.prev.tick + reach)
            self.spec = sorted(sorted(near, key=lambda t: abs(t - self.prev.tick))[:self.cap])

    @property
    def dynamic_fee(self) -> bool:
        return self.pool.variant == "slipstream"

    def calls(self) -> list[tuple[str, bytes]]:
        head = [(self.addr, self.c.encode_abi("slot0", args=[])), (self.addr, self.c.encode_abi("liquidity", args=[]))]
        if self.dynamic_fee:
            head.append((self.addr, self.c.encode_abi("fee", args=[])))
        if self.reuse:
            return head
        return (head + [(self.addr, self.c.encode_abi("tickBitmap", args=[w])) for w in self.words]
                + [(self.addr, self.c.encode_abi("ticks", args=[t])) for t in self.spec])

    def parse(self, w3: AsyncWeb3, raw: list[bytes | None], block_number: int) -> tuple[PoolState, list[int]]:
        """State from the first round of answers, plus the bitmap words still missing for the window."""
        if raw[0] is None or raw[1] is None:
            raise RuntimeError(f"{self.pool.pool_id}: slot0/liquidity call failed")
        sqrt_p, tick = w3.codec.decode(_SLOT0, raw[0])
        liquidity = w3.codec.decode(["uint128"], raw[1])[0]
        fee = None
        off = 2
        if self.dynamic_fee:
            fee = w3.codec.decode(["uint24"], raw[2])[0] if raw[2] is not None else None
            off = 3
        if self.reuse:
            prev = self.prev
            margin = settings.v3_tick_margin
            inside = prev.tick_lo + margin <= tick <= prev.tick_hi - margin
            if inside:   # price still well inside the cached window: keep bitmap and ticks, nothing more to fetch
                st = PoolState(self.pool, block_number, sqrt_price_x96=sqrt_p, tick=tick, liquidity=liquidity,
                               tick_bitmap=dict(prev.tick_bitmap), tick_nets=dict(prev.tick_nets),
                               tick_lo=prev.tick_lo, tick_hi=prev.tick_hi, fee_ppm=fee)
                return st, []
            # drifted near the edge: rebuild the window around the new tick from the cached bitmap/nets
            lo, hi = tick - settings.v3_tick_window, tick + settings.v3_tick_window
            st = PoolState(self.pool, block_number, sqrt_price_x96=sqrt_p, tick=tick, liquidity=liquidity,
                           tick_bitmap=dict(prev.tick_bitmap), tick_nets=dict(prev.tick_nets), tick_lo=lo, tick_hi=hi, fee_ppm=fee)
            return st, [w for w in word_range(lo, hi, self.spacing) if w not in st.tick_bitmap]
        bitmap = {w: w3.codec.decode(["uint256"], r)[0] for w, r in zip(self.words, raw[off:off + len(self.words)]) if r is not None}
        nets = {t: w3.codec.decode(_TICKS, r)[1] for t, r in zip(self.spec, raw[off + len(self.words):]) if r is not None}
        lo, hi = tick - settings.v3_tick_window, tick + settings.v3_tick_window
        st = PoolState(self.pool, block_number, sqrt_price_x96=sqrt_p, tick=tick, liquidity=liquidity,
                       tick_bitmap=bitmap, tick_nets=nets, tick_lo=lo, tick_hi=hi, fee_ppm=fee)
        return st, [w for w in word_range(lo, hi, self.spacing) if w not in bitmap]

    def needed(self, st: PoolState) -> list[int]:
        """The ticks the window needs: the initialized ones nearest the price, capped."""
        assert st.tick is not None and st.tick_bitmap is not None
        ticks = initialized_ticks(st.tick_bitmap, self.spacing, st.tick_lo, st.tick_hi)
        return sorted(sorted(ticks, key=lambda t: abs(t - st.tick))[:self.cap])

    def finish(self, st: PoolState) -> PoolState:
        """Trim the window to the ticks actually known and publish the hint for next block."""
        assert st.tick is not None and st.tick_nets is not None
        needed = self.needed(st)
        if needed:
            st.tick_lo, st.tick_hi = max(st.tick_lo, needed[0]) if needed[0] <= st.tick else st.tick_lo, min(st.tick_hi, needed[-1]) if needed[-1] >= st.tick else st.tick_hi
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


async def fetch_states(w3: AsyncWeb3, pools: list[Pool], block_number: int,
                       refresh_ticks: set[str] | None = None) -> dict[str, PoolState]:
    """Every pool's state at block_number in one round of chunked Multicall3 calls (plus a second and third
    round when a V3 tick window must be (re)built). Tick data is reused from the previous fetch except for
    pools in refresh_ticks (None = refresh all). A pool whose calls failed is left out."""
    plans = {p.pool_id: _V3Plan(w3, p, refresh_ticks is None or p.pool_id in refresh_ticks) for p in pools if p.kind == "uniswap_v3"}
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
    second_meta: list[tuple[str, int]] = []
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
            st, missing_words = plans[p.pool_id].parse(w3, chunk, block_number)
        except RuntimeError:
            continue
        states[p.pool_id] = st
        for wpos in missing_words:
            second.append((plans[p.pool_id].addr, plans[p.pool_id].c.encode_abi("tickBitmap", args=[wpos])))
            second_meta.append((p.pool_id, wpos))
    if second:   # bitmap words outside the hint (first block, or the price moved past the margin)
        for (pid, wpos), r in zip(second_meta, await multicall(w3, second, block_number)):
            if r is not None:
                states[pid].tick_bitmap[wpos] = w3.codec.decode(["uint256"], r)[0]
    third: list[tuple[str, bytes]] = []
    third_meta: list[tuple[str, int]] = []
    for pid, plan in plans.items():
        st = states.get(pid)
        if st is None:
            continue
        for t in plan.needed(st):
            if t not in st.tick_nets:
                third.append((plan.addr, plan.c.encode_abi("ticks", args=[t])))
                third_meta.append((pid, t))
    if third:
        for (pid, t), r in zip(third_meta, await multicall(w3, third, block_number)):
            if r is not None:
                states[pid].tick_nets[t] = w3.codec.decode(_TICKS, r)[1]
    for pid, plan in plans.items():
        if pid in states:
            plan.finish(states[pid])
    return states


def carry_forward(prev: dict[str, PoolState], block_number: int) -> dict[str, PoolState]:
    """Re-pin unchanged states to a new block (pools that emitted no event in that block)."""
    return {pid: replace(st, block_number=block_number) for pid, st in prev.items()}


async def fetch_state(w3: AsyncWeb3, pool: Pool, block_number: int) -> PoolState:
    """Single-pool convenience for scripts and tests; the loop uses fetch_states()."""
    states = await fetch_states(w3, [pool], block_number)
    if pool.pool_id not in states:
        raise RuntimeError(f"{pool.pool_id}: state fetch failed at block {block_number}")
    return states[pool.pool_id]
