"""Tick-crossing swap math against QuoterV2 answers captured on-chain (tests/fixtures/*.json).
Set MEV_LIVE_TESTS=1 to also compare against the live quoter at the current head."""
import asyncio
import json
import os
from pathlib import Path

import pytest

from mev.chain.addresses import POOLS, UNISWAP_V3_QUOTER_V2, USDC, WETH
from mev.dex.uniswap_v3 import UniswapV3Adapter, swap_exact_in
from mev.pools.state import PoolState

FIXTURE = Path(__file__).parent / "fixtures" / "univ3_weth_usdc_5_state.json"


def load_state() -> tuple[PoolState, list[dict]]:
    fx = json.loads(FIXTURE.read_text())
    pool = next(p for p in POOLS if p.pool_id == fx["pool_id"])
    st = PoolState(pool, fx["block_number"], sqrt_price_x96=fx["sqrt_price_x96"], tick=fx["tick"], liquidity=fx["liquidity"],
                   tick_bitmap={int(k): v for k, v in fx["tick_bitmap"].items()},
                   tick_nets={int(k): v for k, v in fx["tick_nets"].items()}, tick_lo=fx["tick_lo"], tick_hi=fx["tick_hi"])
    return st, fx["quoter_cases"]


def test_matches_quoter_v2_to_the_wei_across_ticks():
    st, cases = load_state()
    crossed_any = False
    for c in cases:
        r = swap_exact_in(st, c["zero_for_one"], c["amount_in"])
        assert r.exact, c
        assert r.amount_out == c["amount_out"], c
        assert r.sqrt_price_x96 == c["sqrt_after"], c
        crossed_any |= r.ticks_crossed > 0
    assert crossed_any, "fixture should include at least one swap that crosses initialized ticks"


def test_leaving_the_window_is_flagged_inexact():
    st, _ = load_state()
    r = swap_exact_in(st, True, 50_000 * 10**18)   # 50k ETH into a pool with a few thousand ETH in range
    assert not r.exact
    assert r.amount_out > 0


def test_adapter_wraps_swap():
    st, cases = load_state()
    ad = UniswapV3Adapter()
    c = cases[0]
    q = ad.quote_exact_in(st, USDC.address if not c["zero_for_one"] else WETH.address, c["amount_in"])
    assert q.amount_out == c["amount_out"] and q.exact


@pytest.mark.skipif(not os.environ.get("MEV_LIVE_TESTS"), reason="set MEV_LIVE_TESTS=1 to hit the public RPC")
def test_live_quotes_match_quoter_v2():
    from mev.chain.rpc import make_web3
    from mev.dex.uniswap_v3 import quote_onchain_detail
    from mev.pools.state import fetch_state

    async def run():
        w3 = make_web3()
        head = await w3.eth.block_number
        for pool in POOLS[:2]:
            st = await fetch_state(w3, pool, head)
            for usd in (100, 1_000, 10_000, 50_000, 200_000):
                amt = usd * 10**6
                r = swap_exact_in(st, False, amt)
                out, sqrt_after, _ = await quote_onchain_detail(w3, UNISWAP_V3_QUOTER_V2, USDC.address, WETH.address, amt, pool.fee_ppm, head)
                assert r.exact and r.amount_out == out and r.sqrt_price_x96 == sqrt_after, (pool.pool_id, usd)
    asyncio.run(run())
