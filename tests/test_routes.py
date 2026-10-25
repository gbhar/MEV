"""Triangular routes: scanner finds the cycle through the best venue per hop, the simulator chains the legs,
the calldata matches FlashArb.executeRoute, the engine builds it, and the paper book treats a route's pools as busy."""
from decimal import Decimal

from web3 import Web3

from mev.config import Settings
from mev.dex.base import get_adapter
from mev.execution import calldata
from mev.execution.engine import ExecutionEngine
from mev.execution.lenders import LenderBook
from mev.paper.executor import PaperExecutor
from mev.pools.registry import Pool, Token
from mev.pools.state import PoolState
from mev.scanner.scanner import Candidate, scan, scan_routes
from mev.simulator.simulate import best, simulate

USDC = Token("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "USDC", 6)
WETH = Token("0x4200000000000000000000000000000000000006", "WETH", 18)
CBBTC = Token("0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf", "cbBTC", 8)
# token0 is the lower address: WETH < USDC < cbBTC
P_WETH_USDC = Pool("weth_usdc", "dex_a", "constant_product", "0x" + "a" * 40, WETH, USDC, 3000)
P_WETH_USDC_B = Pool("weth_usdc_b", "dex_b", "constant_product", "0x" + "b" * 40, WETH, USDC, 3000)
P_WETH_CBBTC = Pool("weth_cbbtc", "dex_a", "constant_product", "0x" + "c" * 40, WETH, CBBTC, 3000)
P_USDC_CBBTC = Pool("usdc_cbbtc", "dex_a", "constant_product", "0x" + "d" * 40, USDC, CBBTC, 3000)
AD = {"constant_product": get_adapter("constant_product")}
S = Settings(_env_file=None, trade_sizes_usd=[500, 1000, 5000], flash_lender="aave_v3")


def cp(pool, r0, r1, block=10):
    return PoolState(pool, block, reserve0=r0, reserve1=r1)


def world(btc_usdc=100_000, btc_in_weth=40, eth_usdc=2500):
    """WETH/USDC and WETH/cbBTC consistent; the USDC/cbBTC pool is priced independently so a cycle may open."""
    return {
        "weth_usdc": cp(P_WETH_USDC, 2000 * 10**18, 2000 * eth_usdc * 10**6),
        "weth_usdc_b": cp(P_WETH_USDC_B, 2000 * 10**18, 2000 * eth_usdc * 10**6),
        "weth_cbbtc": cp(P_WETH_CBBTC, 4000 * 10**18, int(4000 / btc_in_weth) * 10**8),
        "usdc_cbbtc": cp(P_USDC_CBBTC, 50 * btc_usdc * 10**6, 50 * 10**8),
    }


def test_no_route_when_prices_are_consistent():
    st = world()   # 1 cbBTC = 40 WETH = 100,000 USDC everywhere: no free cycle beyond fees
    assert scan(st, AD) == []
    assert scan_routes(st, AD) == []


def test_route_found_when_a_hop_is_mispriced():
    st = world(btc_usdc=103_000)   # cbBTC is 3% dearer in USDC terms on the USDC/cbBTC pool than via WETH
    routes = scan_routes(st, AD, quotes=("USDC",))
    assert routes, "a USDC -> WETH -> cbBTC -> USDC cycle should clear 90 bp of fees"
    c = routes[0]
    assert c.route == ("weth_usdc", "weth_cbbtc", "usdc_cbbtc") or c.route == ("weth_usdc_b", "weth_cbbtc", "usdc_cbbtc")
    assert c.path == (USDC.key, WETH.key, CBBTC.key, USDC.key) and c.pair == "USDC>WETH>cbBTC>USDC"
    assert c.legs == 3 and c.pools == c.route and c.key == c.route
    assert Decimal("290") < c.spread_bps < Decimal("310") and c.fee_floor_bps == 90
    # the same cycle from WETH is also a candidate; two-leg scan sees nothing (no pool pair shares a token pair)
    assert any(r.path[0] == WETH.key for r in scan_routes(st, AD, quotes=("WETH",)))
    assert scan(st, AD) == []


def test_route_simulation_chains_the_legs():
    st = world(btc_usdc=103_000)
    (c, *_) = scan_routes(st, AD, quotes=("USDC",))
    results = simulate(c, st, AD, Decimal("0.05"), S, {USDC.key: Decimal(1)})
    b = best(results)
    assert b.quote_symbol == "USDC" and b.amount_in == b.trade_size_usd * 10**6
    assert b.amount_back > b.amount_in and b.gross_usd > 0 and b.exact
    # 3% edge minus 90 bp of fees minus impact leaves most of 2%: on $1,000 roughly $20 before flash fee and gas
    r1000 = next(r for r in results if r.trade_size_usd == 1000)
    assert Decimal("15") < r1000.gross_usd < Decimal("21")
    assert r1000.flash_fee_usd == Decimal("0.5") and r1000.net_usd == r1000.gross_usd - r1000.flash_fee_usd - Decimal("0.05") - r1000.buffer_usd


def test_route_calldata_matches_contract():
    pools = {p.pool_id: p for p in (P_WETH_USDC, P_WETH_CBBTC, P_USDC_CBBTC)}
    route, path = ("weth_usdc", "weth_cbbtc", "usdc_cbbtc"), (USDC.key, WETH.key, CBBTC.key, USDC.key)
    p = calldata.build_route_params(pools, route, path, 1000 * 10**6, 7, 123, "morpho_blue")
    assert p.lender == 1 and p.quote == USDC.address and len(p.legs) == 3
    assert p.legs[0] == (Web3.to_checksum_address(P_WETH_USDC.address), calldata.KIND_PAIR_V2, USDC.address, WETH.address, 3000)
    assert p.legs[1][2:4] == (WETH.address, CBBTC.address) and p.legs[2][2:4] == (CBBTC.address, USDC.address)
    data = calldata.encode_execute_route(p)
    assert data[:4].hex() == "0e696068"   # cast sig "executeRoute((uint8,address,uint256,(address,uint8,address,address,uint24)[],uint256,uint256))"
    import pytest
    with pytest.raises(ValueError):
        calldata.build_route_params(pools, route, (USDC.key, WETH.key, CBBTC.key, WETH.key), 1, 1, 1, "aave_v3")


def test_engine_builds_route_transactions():
    st = world(btc_usdc=103_000)
    (c, *_) = scan_routes(st, AD, quotes=("USDC",))
    r = best(simulate(c, st, AD, Decimal("0.05"), S, {USDC.key: Decimal(1)}))
    pools = {p.pool_id: p for p in (P_WETH_USDC, P_WETH_USDC_B, P_WETH_CBBTC, P_USDC_CBBTC)}
    s = Settings(_env_file=None, execution_mode="shadow", flash_lender="auto", execution_gas_limit=450_000, execution_gas_per_extra_leg=120_000)
    book = LenderBook(s)
    book.morpho_idle[USDC.key] = 10**15
    e = ExecutionEngine(s, None, None, lambda: pools, book)
    b = e.build(c, r, 100, 55_000_000, Decimal(1))
    assert isinstance(b.params, calldata.RouteParams) and b.lender == "morpho_blue" and b.quote_decimals == 6
    assert b.tx["data"][:4].hex() == "0e696068" and b.tx["gas"] == 570_000
    assert b.expected_profit == r.amount_back - r.amount_in   # Morpho: no premium
    assert b.params.min_profit == int((r.gas_usd + Decimal("1.0")) * 10**6)


def test_paper_book_treats_route_pools_as_busy_and_repeating():
    paper = PaperExecutor(S, AD)
    c = Candidate("weth_usdc", "usdc_cbbtc", Decimal(1), Decimal("1.03"), Decimal(300), Decimal(90), 10, "USDC>WETH>cbBTC>USDC",
                  ("weth_usdc", "weth_cbbtc", "usdc_cbbtc"), (USDC.key, WETH.key, CBBTC.key, USDC.key))
    paper.active_pairs[c.key] = 10
    two_leg = Candidate("weth_cbbtc", "weth_usdc_b", Decimal(1), Decimal(2), Decimal(50), Decimal(60), 10, "x")
    assert paper.is_repeat(two_leg)             # shares weth_cbbtc with the open route
    paper.note_candidates({two_leg.key})        # the route is no longer a candidate -> released
    assert not paper.is_repeat(two_leg)
