from mev.dex.constant_product import get_amount_out


def test_matches_uniswap_v2_reference():
    # Uniswap V2 getAmountOut(1e18, 100e18, 100e18) with 0.3% fee = 987158034397061298
    assert get_amount_out(10**18, 100 * 10**18, 100 * 10**18, 3000) == 987158034397061298


def test_small_trade_approaches_spot_less_fee():
    out = get_amount_out(10**12, 10**24, 2_000 * 10**24, 3000)
    assert abs(out / (2_000 * 10**12 * 0.997) - 1) < 1e-6


def test_zero_or_negative():
    assert get_amount_out(0, 10, 10, 3000) == 0
    assert get_amount_out(10, 0, 10, 3000) == 0
