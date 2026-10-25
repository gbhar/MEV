from eth_abi import encode
from hexbytes import HexBytes

from mev.chain.addresses import POOLS
from mev.indexer.events import (TOPIC_AERO_SYNC, TOPIC_PCS_SWAP, TOPIC_V2_SWAP, TOPIC_V3_SWAP, decode_log, end_of_block, reconcile, swap_rows)
from mev.pools.state import PoolState

BY_ADDR = {p.address.lower(): p for p in POOLS}
V3 = next(p for p in POOLS if p.pool_id == "univ3_weth_usdc_5")
AERO = next(p for p in POOLS if p.pool_id == "aero_weth_usdc_v")
V2 = next(p for p in POOLS if p.pool_id == "univ2_weth_usdc")
PCS = next(p for p in POOLS if p.variant == "pancake")


def mk(pool, topic, data, idx=0, block=100):
    return {"address": pool.address, "topics": [HexBytes(topic)], "data": HexBytes(data),
            "blockNumber": block, "transactionHash": HexBytes("0x" + "ab" * 32), "logIndex": idx}


def test_decode_v3_swap():
    data = encode(["int256", "int256", "uint160", "uint128", "int24"], [-10**18, 2_365_000_000, 3856509629693946940415200, 985235654424548596, -198617])
    e = decode_log(mk(V3, TOPIC_V3_SWAP, data), BY_ADDR)
    assert e.event_type == "swap" and e.amount0 == -10**18 and e.amount1 == 2_365_000_000
    assert e.sqrt_price_x96 == 3856509629693946940415200 and e.tick == -198617
    (row,) = swap_rows([e], POOLS)
    assert row.volume_usd == 2365.0 and row.transaction_hash.startswith("0x")


def test_decode_sync_and_v2_swap():
    sync = decode_log(mk(AERO, TOPIC_AERO_SYNC, encode(["uint256", "uint256"], [800 * 10**18, 1_900_000 * 10**6])), BY_ADDR)
    assert sync.event_type == "sync" and sync.reserve0 == 800 * 10**18
    swap = decode_log(mk(V2, TOPIC_V2_SWAP, encode(["uint256"] * 4, [0, 1000 * 10**6, 4 * 10**17, 0])), BY_ADDR)
    assert swap.event_type == "swap" and swap.amount0 == -4 * 10**17 and swap.amount1 == 1000 * 10**6


def test_unknown_pool_or_topic_is_ignored():
    assert decode_log(mk(V3, "0x" + "11" * 32, b""), BY_ADDR) is None
    other = mk(V3, TOPIC_V3_SWAP, b""); other["address"] = "0x" + "00" * 20
    assert decode_log(other, BY_ADDR) is None


def test_reconcile_agrees_disagrees_and_rebuilds():
    sync_data = encode(["uint256", "uint256"], [800 * 10**18, 1_900_000 * 10**6])
    events = [decode_log(mk(AERO, TOPIC_AERO_SYNC, encode(["uint256", "uint256"], [1, 1]), idx=0), BY_ADDR),
              decode_log(mk(AERO, TOPIC_AERO_SYNC, sync_data, idx=1), BY_ADDR),
              decode_log(mk(V2, TOPIC_AERO_SYNC, sync_data, idx=2), BY_ADDR)]
    assert end_of_block(events)[AERO.pool_id].reserve0 == 800 * 10**18   # last one wins
    states = {AERO.pool_id: PoolState(AERO, 100, reserve0=800 * 10**18, reserve1=1_900_000 * 10**6)}
    agreed = reconcile(states, events, POOLS, 100)
    assert agreed == {AERO.pool_id: True}
    assert V2.pool_id in states and states[V2.pool_id].reserve1 == 1_900_000 * 10**6   # rebuilt from Sync
    states[AERO.pool_id].reserve0 += 1
    assert reconcile(states, events, POOLS, 100)[AERO.pool_id] is False


def test_burn_after_swap_relaxes_liquidity_check():
    from mev.indexer.events import TOPIC_V3_BURN
    swap = decode_log(mk(V3, TOPIC_V3_SWAP, encode(["int256", "int256", "uint160", "uint128", "int24"], [-1, 1, 3856509629693946940415200, 1000, -198617]), idx=1), BY_ADDR)
    burn = decode_log(mk(V3, TOPIC_V3_BURN, encode(["uint128", "uint256", "uint256"], [5, 1, 1]), idx=2), BY_ADDR)
    states = {V3.pool_id: PoolState(V3, 100, sqrt_price_x96=3856509629693946940415200, tick=-198617, liquidity=990)}
    assert reconcile(states, [swap, burn], POOLS, 100)[V3.pool_id] is True      # liquidity moved by the burn: ignored
    assert reconcile(states, [swap], POOLS, 100)[V3.pool_id] is False            # no burn: liquidity must match


def test_decode_pancake_v3_swap_with_protocol_fee_fields():
    """PancakeSwap V3's Swap has two trailing uint128 fields and therefore a different topic; until 2026-10-25 it was
    never decoded, so Pancake pools were only refreshed by the round-robin and their price went stale for ~20 blocks."""
    data = encode(["int256", "int256", "uint160", "uint128", "int24", "uint128", "uint128"],
                  [-10**18, 2_365_000_000, 3856509629693946940415200, 985235654424548596, -198617, 123, 456])
    assert TOPIC_PCS_SWAP != TOPIC_V3_SWAP
    e = decode_log(mk(PCS, TOPIC_PCS_SWAP, data), BY_ADDR)
    assert e is not None and e.event_type == "swap" and e.amount0 == -10**18 and e.tick == -198617 and e.liquidity == 985235654424548596
    assert decode_log(mk(PCS, TOPIC_V3_SWAP, data[:5 * 32]), BY_ADDR).event_type == "swap"   # a Uniswap-shaped log still decodes
