"""x*y=k pools with fee on input: Uniswap V2, Aerodrome/Velodrome volatile pools, SushiSwap, etc."""
from decimal import Decimal

from mev.dex.base import Quote
from mev.pools.state import PoolState

PPM = 1_000_000


def get_amount_out(amount_in: int, reserve_in: int, reserve_out: int, fee_ppm: int) -> int:
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return 0
    amount_in_with_fee = amount_in * (PPM - fee_ppm)
    return amount_in_with_fee * reserve_out // (reserve_in * PPM + amount_in_with_fee)


class ConstantProductAdapter:
    def marginal_price(self, state: PoolState) -> Decimal:
        p = state.pool
        if not state.reserve0 or not state.reserve1:
            return Decimal(0)
        return (Decimal(state.reserve1) / Decimal(state.reserve0)) * Decimal(10) ** (p.token0.decimals - p.token1.decimals)

    def quote_exact_in(self, state: PoolState, token_in: str, amount_in: int) -> Quote:
        p = state.pool
        assert state.reserve0 is not None and state.reserve1 is not None
        if token_in.lower() == p.token0.address.lower():
            out = get_amount_out(amount_in, state.reserve0, state.reserve1, p.fee_ppm)
        else:
            out = get_amount_out(amount_in, state.reserve1, state.reserve0, p.fee_ppm)
        return Quote(out, exact=True)
