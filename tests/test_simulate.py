from decimal import Decimal

from mev.config import Settings
from mev.dex.base import get_adapter
from mev.pools.registry import Pool, Token
from mev.pools.state import PoolState
from mev.risk import rules
from mev.scanner.scanner import scan
from mev.simulator.simulate import best, fee_params, gas_cost_usd, gas_price_wei, next_base_fee, simulate

WETH = Token("0x4200000000000000000000000000000000000006", "WETH", 18)
USDC = Token("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "USDC", 6)
A = Pool("cp_a", "dex_a", "constant_product", "0x" + "a" * 40, WETH, USDC, fee_ppm=3000)
B = Pool("cp_b", "dex_b", "constant_product", "0x" + "b" * 40, WETH, USDC, fee_ppm=3000)
ADAPTERS = {"constant_product": get_adapter("constant_product")}
S = Settings(_env_file=None, trade_sizes_usd=[500, 1000, 5000, 20000, 100000], min_net_profit_usd=1.0)


def cp_state(pool, eth_reserve, usdc_per_eth, block=1):
    return PoolState(pool, block, reserve0=eth_reserve * 10**18, reserve1=eth_reserve * usdc_per_eth * 10**6)


def test_no_candidate_when_spread_is_below_fee_floor():
    states = {"cp_a": cp_state(A, 1000, 2500), "cp_b": cp_state(B, 1000, 2510)}  # 40 bps < 60 bps floor
    assert scan(states, ADAPTERS) == []


def test_candidate_and_optimal_size():
    states = {"cp_a": cp_state(A, 1000, 2400), "cp_b": cp_state(B, 1000, 2500)}  # ~417 bps spread
    (c,) = scan(states, ADAPTERS)
    assert c.buy_pool_id == "cp_a" and c.sell_pool_id == "cp_b"
    results = simulate(c, states, ADAPTERS, gas_usd=Decimal("0.05"), s=S)
    b = best(results)
    assert b.net_usd > 0
    # profit must be concave in size: the biggest size is not the best one
    assert b.trade_size_usd < max(S.trade_sizes_usd)
    assert results[-1].net_usd < b.net_usd
    assert rules.check(b, head_block=1, state_block=1, s=S) == []


def test_risk_blocks_thin_profit_and_stale_state():
    states = {"cp_a": cp_state(A, 1000, 2500), "cp_b": cp_state(B, 1000, 2520)}  # 80 bps, barely above floor
    (c,) = scan(states, ADAPTERS)
    b = best(simulate(c, states, ADAPTERS, gas_usd=Decimal("0.05"), s=S))
    v = rules.check(b, head_block=5, state_block=1, s=S)
    assert any("stale" in x for x in v)


def test_best_allowed_size_is_chosen_when_unconstrained_best_breaks_a_limit():
    # Deep pools, 125 bps spread: the 50k row has the largest net but exceeds max_trade_size_usd.
    s = Settings(_env_file=None, trade_sizes_usd=[500, 1_000, 2_500, 5_000, 10_000, 20_000, 50_000], max_trade_size_usd=25_000)
    states = {"cp_a": cp_state(A, 40_000, 2500), "cp_b": cp_state(B, 40_000, Decimal("2531.25"))}
    (c,) = scan(states, ADAPTERS)
    results = simulate(c, states, ADAPTERS, gas_usd=Decimal("0.05"), s=s)
    unconstrained = best(results)
    assert unconstrained.trade_size_usd == 50_000
    assert rules.check(unconstrained, 1, 1, s) == ["trade_size 50000 > max 25000"]
    allowed = rules.allowed(results, 1, 1, s)
    chosen = best(allowed)
    assert chosen.trade_size_usd == 20_000 and chosen.net_usd > 100
    assert rules.check(chosen, 1, 1, s) == []


def test_diagnostics_decompose_gross_without_changing_net():
    states = {"cp_a": cp_state(A, 1000, 2400), "cp_b": cp_state(B, 1000, 2500)}
    (c,) = scan(states, ADAPTERS)
    r = best(simulate(c, states, ADAPTERS, gas_usd=Decimal("0.05"), s=S))
    assert r.net_usd == r.gross_usd - r.flash_fee_usd - r.gas_usd - r.buffer_usd   # diagnostics are not subtracted
    assert r.dex_fee_usd > 0 and r.slippage_usd > 0
    spread_gross = Decimal(r.trade_size_usd) * (c.sell_price / c.buy_price - 1)
    assert abs(spread_gross - r.dex_fee_usd - r.slippage_usd - r.gross_usd) < Decimal("0.01")


def test_next_base_fee_follows_eip1559():
    base, limit, el, den = 10_000_000, 120_000_000, 6, 250
    target = limit // el
    assert next_base_fee(base, target, limit, el, den) == base
    assert next_base_fee(base, limit, limit, el, den) == base + base * 5 // den
    assert next_base_fee(base, 0, limit, el, den) == base - base // den
    assert next_base_fee(base, target + 1, limit, el, den) == base + 1   # at least +1 wei when above target
    assert next_base_fee(base, 0, limit, el, den, min_base_fee=base) == base   # floored


def test_fee_params_from_base_header():
    s = Settings(_env_file=None)
    # Base mainnet block 53030320 extraData: version 1, denominator 100, elasticity 5, min base fee 5e6 wei
    extra = bytes.fromhex("01000000640000000500000000004c4b40")
    assert fee_params(extra, s) == (5, 100, 5_000_000)
    assert fee_params(b"", s) == (s.base_fee_elasticity, s.base_fee_change_denominator, s.min_base_fee_wei)
    # Base mainnet blocks 52965331 -> 52965332 (base fee above the floor): 5024035 -> 5005303
    assert next_base_fee(5_024_035, 50_171_574, 400_000_000, *fee_params(extra, s)) == 5_005_303
    # and on the floor, 53030316 -> 53030317 stays at 5e6
    assert next_base_fee(5_000_000, 33_704_753, 400_000_000, *fee_params(extra, s)) == 5_000_000


def test_gas_includes_priority_fee():
    s = Settings(_env_file=None, priority_fee_wei=1_000_000_000, l1_data_fee_usd=0.0, gas_units=100_000)
    assert gas_price_wei(0, s) == 1_000_000_000
    assert gas_cost_usd(gas_price_wei(0, s), Decimal(3000), s) == Decimal("0.3")


def test_morpho_lender_charges_no_flash_fee():
    from decimal import Decimal
    from mev.config import Settings
    aave, morpho = Settings(_env_file=None, flash_lender="aave_v3"), Settings(_env_file=None, flash_lender="morpho_blue")
    assert aave.flash_fee_bps == 5 and morpho.flash_fee_bps == 0
