from decimal import Decimal

from mev.dex.uniswap_v3 import Q96, price_from_sqrt_x96, sqrt_x96_at_tick, swap_within_tick

# Live Base WETH/USDC 0.05% state captured 2026-10-24 (block 53028880)
SQRT = 3856509629693946940415200
LIQ = 985235654424548596
TICK = -198617


def test_price_from_sqrt_is_plausible():
    p = price_from_sqrt_x96(SQRT, 18, 6)
    assert Decimal(2000) < p < Decimal(3000)


def test_sqrt_at_tick_brackets_live_sqrt():
    assert sqrt_x96_at_tick(TICK) <= SQRT <= sqrt_x96_at_tick(TICK + 1)


def test_small_swap_token1_in_matches_spot_less_fee():
    price = price_from_sqrt_x96(SQRT, 18, 6)
    amount_in = 100 * 10**6  # 100 USDC
    out, new_sqrt = swap_within_tick(SQRT, LIQ, 500, zero_for_one=False, amount_in=amount_in)
    expected = Decimal(amount_in) / Decimal(10**6) / price * Decimal("0.9995") * Decimal(10**18)
    assert abs(Decimal(out) / expected - 1) < Decimal("1e-4")
    assert new_sqrt > SQRT  # buying token0 pushes price up


def test_small_swap_token0_in_matches_spot_less_fee():
    price = price_from_sqrt_x96(SQRT, 18, 6)
    amount_in = 10**17  # 0.1 WETH
    out, new_sqrt = swap_within_tick(SQRT, LIQ, 500, zero_for_one=True, amount_in=amount_in)
    expected = Decimal(amount_in) / Decimal(10**18) * price * Decimal("0.9995") * Decimal(10**6)
    assert abs(Decimal(out) / expected - 1) < Decimal("1e-4")
    assert new_sqrt < SQRT


def test_round_trip_loses_only_fees_and_impact():
    usdc_in = 1_000 * 10**6
    weth, s1 = swap_within_tick(SQRT, LIQ, 500, False, usdc_in)
    usdc_back, _ = swap_within_tick(s1, LIQ, 500, True, weth)
    assert usdc_back < usdc_in
    assert usdc_back > usdc_in * 0.998  # two 5 bps fees plus tiny impact


def test_q96_constant():
    assert Q96 == 2**96
