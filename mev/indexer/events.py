"""Pool event ingestion over HTTP: one eth_getLogs per block for every registered pool.

Events are the spec's raw swap feed (section 6) and a cross-check on the state fetched by eth_call:
at the end of a block the last V3 Swap's sqrtPriceX96/liquidity/tick and the last Sync's reserves
must equal what slot0()/liquidity()/getReserves() return for that block. A mismatch is counted in
state_divergence_total and logged; the fetched state stays authoritative. When a constant-product
pool's state fetch failed, its last Sync of the block is a complete substitute and is used.

WebSocket subscriptions are not used: the public Base endpoint refuses wss:// connections (tested
2026-10-24) and the per-block path needs no extra round trip because logs are fetched concurrently
with the state multicalls. Set MEV_RPC_WS and extend run() if a provider with WS is available."""
import logging
from dataclasses import dataclass

from eth_abi import decode
from web3 import AsyncWeb3

from mev import metrics
from mev.db.models import Swap
from mev.pools.registry import Pool
from mev.pools.state import PoolState

log = logging.getLogger("mev.indexer")


def _topic(sig: str) -> str:
    return "0x" + AsyncWeb3.keccak(text=sig).hex()


TOPIC_V3_SWAP = _topic("Swap(address,address,int256,int256,uint160,uint128,int24)")
TOPIC_V3_MINT = _topic("Mint(address,address,int24,int24,uint128,uint256,uint256)")
TOPIC_V3_BURN = _topic("Burn(address,int24,int24,uint128,uint256,uint256)")
TOPIC_V2_SWAP = _topic("Swap(address,uint256,uint256,uint256,uint256,address)")
TOPIC_V2_SYNC = _topic("Sync(uint112,uint112)")
TOPIC_AERO_SWAP = _topic("Swap(address,address,uint256,uint256,uint256,uint256)")
TOPIC_AERO_SYNC = _topic("Sync(uint256,uint256)")


@dataclass(frozen=True)
class PoolEvent:
    pool_id: str
    block_number: int
    tx_hash: str
    log_index: int
    event_type: str                  # swap | sync | mint | burn
    amount0: int | None = None       # pool's perspective: + into the pool, - out
    amount1: int | None = None
    sqrt_price_x96: int | None = None
    liquidity: int | None = None
    tick: int | None = None
    reserve0: int | None = None
    reserve1: int | None = None


def _hex(x) -> str:
    h = x.hex() if hasattr(x, "hex") else str(x)
    return h if h.startswith("0x") else "0x" + h


def decode_log(log_: dict, pools_by_address: dict[str, Pool]) -> PoolEvent | None:
    pool = pools_by_address.get(log_["address"].lower())
    if pool is None or not log_["topics"]:
        return None
    topic = _hex(log_["topics"][0])
    data = bytes(log_["data"])
    base = dict(pool_id=pool.pool_id, block_number=log_["blockNumber"], tx_hash=_hex(log_["transactionHash"]), log_index=log_["logIndex"])
    if pool.kind == "uniswap_v3":
        if topic == TOPIC_V3_SWAP:
            a0, a1, sqrt_p, liq, tick = decode(["int256", "int256", "uint160", "uint128", "int24"], data)
            return PoolEvent(event_type="swap", amount0=a0, amount1=a1, sqrt_price_x96=sqrt_p, liquidity=liq, tick=tick, **base)
        if topic == TOPIC_V3_MINT:
            _, _, a0, a1 = decode(["address", "uint128", "uint256", "uint256"], data)
            return PoolEvent(event_type="mint", amount0=a0, amount1=a1, **base)
        if topic == TOPIC_V3_BURN:
            _, a0, a1 = decode(["uint128", "uint256", "uint256"], data)
            return PoolEvent(event_type="burn", amount0=-a0, amount1=-a1, **base)
        return None
    if topic in (TOPIC_V2_SYNC, TOPIC_AERO_SYNC):
        r0, r1 = decode(["uint256", "uint256"], data)   # uint112 pairs decode identically (left-padded words)
        return PoolEvent(event_type="sync", reserve0=r0, reserve1=r1, **base)
    if topic in (TOPIC_V2_SWAP, TOPIC_AERO_SWAP):
        in0, in1, out0, out1 = decode(["uint256", "uint256", "uint256", "uint256"], data)
        return PoolEvent(event_type="swap", amount0=in0 - out0, amount1=in1 - out1, **base)
    return None


async def fetch_events(w3: AsyncWeb3, pools: list[Pool], block_number: int) -> list[PoolEvent]:
    by_addr = {p.address.lower(): p for p in pools}
    logs = await w3.eth.get_logs({"fromBlock": block_number, "toBlock": block_number,
                                  "address": [AsyncWeb3.to_checksum_address(p.address) for p in pools]})
    events = [e for e in (decode_log(l, by_addr) for l in logs) if e is not None]
    events.sort(key=lambda e: e.log_index)
    for e in events:
        metrics.events_fetched.labels(e.event_type).inc()
    return events


def end_of_block(events: list[PoolEvent]) -> dict[str, PoolEvent]:
    """Last state-carrying event per pool (V3 swap, or x*y=k sync) in log order."""
    last: dict[str, PoolEvent] = {}
    for e in events:
        if e.event_type == "swap" and e.sqrt_price_x96 is not None or e.event_type == "sync":
            last[e.pool_id] = e
    return last


def reconcile(states: dict[str, PoolState], events: list[PoolEvent], pools: list[Pool], block_number: int) -> dict[str, bool]:
    """Compare event-derived end-of-block state with the fetched state. Returns pool_id -> agreed.
    Fills in a missing constant-product state from its last Sync (the fetch failed for that pool)."""
    agreed: dict[str, bool] = {}
    by_id = {p.pool_id: p for p in pools}
    for pool_id, e in end_of_block(events).items():
        st = states.get(pool_id)
        if st is None:
            if e.event_type == "sync":
                states[pool_id] = PoolState(by_id[pool_id], block_number, reserve0=e.reserve0, reserve1=e.reserve1)
                log.warning("block=%s %s state rebuilt from its Sync event (fetch failed)", block_number, pool_id)
            continue
        if e.event_type == "sync":
            ok = (st.reserve0, st.reserve1) == (e.reserve0, e.reserve1)
        else:
            ok = (st.sqrt_price_x96, st.liquidity, st.tick) == (e.sqrt_price_x96, e.liquidity, e.tick)
        agreed[pool_id] = ok
        if ok:
            metrics.events_applied.labels(pool_id).inc()
        else:
            metrics.state_divergence.labels(pool_id).inc()
            log.warning("block=%s %s event-derived state differs from fetched state: event=%s fetched=(%s,%s,%s,%s,%s)",
                        block_number, pool_id, e, st.sqrt_price_x96, st.liquidity, st.tick, st.reserve0, st.reserve1)
    return agreed


def swap_rows(events: list[PoolEvent], pools: list[Pool]) -> list[Swap]:
    by_id = {p.pool_id: p for p in pools}
    rows = []
    for e in events:
        vol = None
        if e.event_type == "swap" and e.amount1 is not None:
            vol = abs(e.amount1) / 10 ** by_id[e.pool_id].token1.decimals
        rows.append(Swap(pool_id=e.pool_id, block_number=e.block_number, transaction_hash=e.tx_hash, log_index=e.log_index,
                         event_type=e.event_type, amount0=None if e.amount0 is None else str(e.amount0),
                         amount1=None if e.amount1 is None else str(e.amount1),
                         sqrt_price_x96=None if e.sqrt_price_x96 is None else str(e.sqrt_price_x96),
                         liquidity=None if e.liquidity is None else str(e.liquidity), tick=e.tick,
                         reserve0=None if e.reserve0 is None else str(e.reserve0),
                         reserve1=None if e.reserve1 is None else str(e.reserve1), volume_usd=vol))
    return rows
