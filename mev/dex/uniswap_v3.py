"""Uniswap V3 concentrated-liquidity math.

Integer ports of TickMath.getSqrtRatioAtTick, SqrtPriceMath, SwapMath.computeSwapStep,
TickBitmap.nextInitializedTickWithinOneWord and the exact-input Pool.swap loop, with the
protocol's rounding, so a local quote equals QuoterV2 to the wei for as long as the swap stays
inside the tick window the pool indexer fetched (PoolState.tick_bitmap / tick_nets). A quote that
would cross a tick whose liquidityNet was not fetched is returned with exact=False and the risk
engine blocks it. quote_onchain() asks QuoterV2 itself and is the ground truth the tests use.
"""
from dataclasses import dataclass
from decimal import Decimal, getcontext

from web3 import AsyncWeb3

from mev.chain import abi
from mev.dex.base import Quote
from mev.pools.state import PoolState

getcontext().prec = 60
Q96 = 1 << 96
PPM = 1_000_000
UINT256_MAX = (1 << 256) - 1
MIN_TICK, MAX_TICK = -887272, 887272
MIN_SQRT_RATIO = 4295128739
MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342

# TickMath: (bit of |tick|, 1.0001^(-bit/2) as a Q128.128)
_TICK_RATIOS = (
    (0x2, 0xfff97272373d413259a46990580e213a), (0x4, 0xfff2e50f5f656932ef12357cf3c7fdcc),
    (0x8, 0xffe5caca7e10e4e61c3624eaa0941cd0), (0x10, 0xffcb9843d60f6159c9db58835c926644),
    (0x20, 0xff973b41fa98c081472e6896dfb254c0), (0x40, 0xff2ea16466c96a3843ec78b326b52861),
    (0x80, 0xfe5dee046a99a2a811c461f1969c3053), (0x100, 0xfcbe86c7900a88aedcffc83b479aa3a4),
    (0x200, 0xf987a7253ac413176f2b074cf7815e54), (0x400, 0xf3392b0822b70005940c7a398e4b70f3),
    (0x800, 0xe7159475a2c29b7443b29c7fa6e889d9), (0x1000, 0xd097f3bdfd2022b8845ad8f792aa5825),
    (0x2000, 0xa9f746462d870fdf8a65dc1f90e061e5), (0x4000, 0x70d869a156d2a1b890bb3df62baf32f7),
    (0x8000, 0x31be135f97d08fd981231505542fcfa6), (0x10000, 0x9aa508b5b7a84e1c677de54f3e99bc9),
    (0x20000, 0x5d6af8dedb81196699c329225ee604), (0x40000, 0x2216e584f5fa1ea926041bedfe98),
    (0x80000, 0x48a170391f7dc42444e8fa2),
)


def price_from_sqrt_x96(sqrt_price_x96: int, dec0: int, dec1: int) -> Decimal:
    sqrt_p = Decimal(sqrt_price_x96) / Decimal(Q96)
    return sqrt_p * sqrt_p * Decimal(10) ** (dec0 - dec1)


def sqrt_x96_at_tick(tick: int) -> int:
    """TickMath.getSqrtRatioAtTick: sqrt(1.0001^tick) as a Q64.96, bit for bit."""
    if not MIN_TICK <= tick <= MAX_TICK:
        raise ValueError(f"tick {tick} out of range")
    abs_tick = -tick if tick < 0 else tick
    ratio = 0xfffcb933bd6fad37aa2d162d1a594001 if abs_tick & 1 else 1 << 128
    for bit, r in _TICK_RATIOS:
        if abs_tick & bit:
            ratio = (ratio * r) >> 128
    if tick > 0:
        ratio = UINT256_MAX // ratio
    return (ratio >> 32) + (1 if ratio & 0xffffffff else 0)


# --- FullMath / UnsafeMath -------------------------------------------------------------------

def mul_div(a: int, b: int, d: int) -> int:
    return a * b // d


def mul_div_up(a: int, b: int, d: int) -> int:
    q, r = divmod(a * b, d)
    return q + 1 if r else q


def div_up(x: int, y: int) -> int:
    q, r = divmod(x, y)
    return q + 1 if r else q


# --- SqrtPriceMath ---------------------------------------------------------------------------

def next_sqrt_from_amount0_up(sqrt_p: int, liquidity: int, amount: int, add: bool) -> int:
    if amount == 0:
        return sqrt_p
    numerator1 = liquidity << 96
    product = amount * sqrt_p
    if add:
        if product < 1 << 256:                      # the Solidity overflow checks, made explicit
            denominator = numerator1 + product
            if denominator < 1 << 256:
                return mul_div_up(numerator1, sqrt_p, denominator)
        return div_up(numerator1, numerator1 // sqrt_p + amount)
    if product >= 1 << 256 or numerator1 <= product:
        raise ValueError("amount0 exceeds the range's reserves")
    return mul_div_up(numerator1, sqrt_p, numerator1 - product)


def next_sqrt_from_amount1_down(sqrt_p: int, liquidity: int, amount: int, add: bool) -> int:
    if add:
        return sqrt_p + (amount << 96) // liquidity
    quotient = div_up(amount << 96, liquidity)
    if sqrt_p <= quotient:
        raise ValueError("amount1 exceeds the range's reserves")
    return sqrt_p - quotient


def next_sqrt_from_input(sqrt_p: int, liquidity: int, amount_in: int, zero_for_one: bool) -> int:
    if sqrt_p <= 0 or liquidity <= 0:
        raise ValueError("price and liquidity must be positive")
    if zero_for_one:
        return next_sqrt_from_amount0_up(sqrt_p, liquidity, amount_in, True)
    return next_sqrt_from_amount1_down(sqrt_p, liquidity, amount_in, True)


def amount0_delta(sqrt_a: int, sqrt_b: int, liquidity: int, round_up: bool) -> int:
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    numerator1, numerator2 = liquidity << 96, sqrt_b - sqrt_a
    if sqrt_a <= 0:
        raise ValueError("sqrt price must be positive")
    if round_up:
        return div_up(mul_div_up(numerator1, numerator2, sqrt_b), sqrt_a)
    return mul_div(numerator1, numerator2, sqrt_b) // sqrt_a


def amount1_delta(sqrt_a: int, sqrt_b: int, liquidity: int, round_up: bool) -> int:
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    if round_up:
        return mul_div_up(liquidity, sqrt_b - sqrt_a, Q96)
    return mul_div(liquidity, sqrt_b - sqrt_a, Q96)


# --- SwapMath --------------------------------------------------------------------------------

def compute_swap_step(sqrt_current: int, sqrt_target: int, liquidity: int, amount_remaining: int,
                      fee_ppm: int) -> tuple[int, int, int, int]:
    """SwapMath.computeSwapStep for exact input: (sqrt_next, amount_in, amount_out, fee_amount)."""
    zero_for_one = sqrt_current >= sqrt_target
    remaining_less_fee = mul_div(amount_remaining, PPM - fee_ppm, PPM)
    if zero_for_one:
        amount_in = amount0_delta(sqrt_target, sqrt_current, liquidity, True)
    else:
        amount_in = amount1_delta(sqrt_current, sqrt_target, liquidity, True)
    if remaining_less_fee >= amount_in:
        sqrt_next = sqrt_target
    else:
        sqrt_next = next_sqrt_from_input(sqrt_current, liquidity, remaining_less_fee, zero_for_one)
    reached = sqrt_target == sqrt_next
    if zero_for_one:
        if not reached:
            amount_in = amount0_delta(sqrt_next, sqrt_current, liquidity, True)
        amount_out = amount1_delta(sqrt_next, sqrt_current, liquidity, False)
    else:
        if not reached:
            amount_in = amount1_delta(sqrt_current, sqrt_next, liquidity, True)
        amount_out = amount0_delta(sqrt_current, sqrt_next, liquidity, False)
    if not reached:
        fee_amount = amount_remaining - amount_in
    else:
        fee_amount = mul_div_up(amount_in, fee_ppm, PPM - fee_ppm)
    return sqrt_next, amount_in, amount_out, fee_amount


# --- TickBitmap ------------------------------------------------------------------------------

def next_initialized_tick(bitmap: dict[int, int], tick: int, spacing: int, lte: bool) -> tuple[int, bool] | None:
    """TickBitmap.nextInitializedTickWithinOneWord. None when the word was not fetched."""
    compressed = tick // spacing
    if lte:
        word_pos, bit_pos = compressed >> 8, compressed & 255
        word = bitmap.get(word_pos)
        if word is None:
            return None
        masked = word & ((1 << (bit_pos + 1)) - 1)
        if masked:
            return (compressed - (bit_pos - (masked.bit_length() - 1))) * spacing, True
        return (compressed - bit_pos) * spacing, False
    compressed += 1
    word_pos, bit_pos = compressed >> 8, compressed & 255
    word = bitmap.get(word_pos)
    if word is None:
        return None
    masked = word & (UINT256_MAX ^ ((1 << bit_pos) - 1))
    if masked:
        return (compressed + ((masked & -masked).bit_length() - 1 - bit_pos)) * spacing, True
    return (compressed + (255 - bit_pos)) * spacing, False


# --- Pool.swap -------------------------------------------------------------------------------

@dataclass(frozen=True)
class SwapResult:
    amount_in: int           # consumed input including fee; equals the request when exact
    amount_out: int
    sqrt_price_x96: int      # price after the swap
    liquidity: int           # in-range liquidity after the swap
    ticks_crossed: int       # initialized ticks crossed
    exact: bool              # False when the swap would cross a tick outside the fetched window


def swap_exact_in(state: PoolState, zero_for_one: bool, amount_in: int) -> SwapResult:
    """The exact-input Pool.swap loop with the QuoterV2 price limits (no limit in practice)."""
    assert state.sqrt_price_x96 is not None and state.liquidity is not None and state.tick is not None
    sqrt_p, tick, liquidity = state.sqrt_price_x96, state.tick, state.liquidity
    spacing = state.pool.tick_spacing or 1
    fee_ppm = state.pool.fee_ppm
    bitmap, nets = state.tick_bitmap or {}, state.tick_nets or {}
    limit = MIN_SQRT_RATIO + 1 if zero_for_one else MAX_SQRT_RATIO - 1
    remaining, amount_out, crossed = amount_in, 0, 0
    while remaining > 0 and sqrt_p != limit:
        found = next_initialized_tick(bitmap, tick, spacing, zero_for_one)
        if found is None:
            # Word not fetched: the nearest tick a position could start at is the edge of this range.
            tick_next, initialized, known = ((tick // spacing) + (0 if zero_for_one else 1)) * spacing, False, False
        else:
            tick_next, initialized = found
            known = not initialized or tick_next in nets
        tick_next = min(max(tick_next, MIN_TICK), MAX_TICK)
        sqrt_next = sqrt_x96_at_tick(tick_next)
        target = limit if (sqrt_next < limit if zero_for_one else sqrt_next > limit) else sqrt_next
        sqrt_p, step_in, step_out, step_fee = compute_swap_step(sqrt_p, target, liquidity, remaining, fee_ppm)
        remaining -= step_in + step_fee
        amount_out += step_out
        if sqrt_p == sqrt_next:
            if not known:
                break   # the liquidity beyond this tick was not fetched
            if initialized:
                net = nets[tick_next]
                liquidity += -net if zero_for_one else net
                if liquidity < 0:
                    raise ValueError(f"negative liquidity crossing tick {tick_next}")
                crossed += 1
            tick = tick_next - 1 if zero_for_one else tick_next
    return SwapResult(amount_in - remaining, amount_out, sqrt_p, liquidity, crossed, exact=remaining == 0)


def swap_within_tick(sqrt_price_x96: int, liquidity: int, fee_ppm: int, zero_for_one: bool, amount_in: int) -> tuple[int, int]:
    """(amount_out, sqrt_price_x96_after) for a swap that stays in the current liquidity range."""
    if amount_in <= 0 or liquidity <= 0:
        return 0, sqrt_price_x96
    target = MIN_SQRT_RATIO + 1 if zero_for_one else MAX_SQRT_RATIO - 1
    sqrt_next, _, amount_out, _ = compute_swap_step(sqrt_price_x96, target, liquidity, amount_in, fee_ppm)
    return amount_out, sqrt_next


class UniswapV3Adapter:
    def marginal_price(self, state: PoolState) -> Decimal:
        p = state.pool
        assert state.sqrt_price_x96 is not None
        return price_from_sqrt_x96(state.sqrt_price_x96, p.token0.decimals, p.token1.decimals)

    def quote_exact_in(self, state: PoolState, token_in: str, amount_in: int) -> Quote:
        zero_for_one = token_in.lower() == state.pool.token0.address.lower()
        if amount_in <= 0:
            return Quote(0, exact=True)
        r = swap_exact_in(state, zero_for_one, amount_in)
        note = "" if r.exact else f"swap leaves the fetched tick window after {r.ticks_crossed} ticks; local quote is a lower bound"
        return Quote(r.amount_out, exact=r.exact, note=note)


async def quote_onchain_detail(w3: AsyncWeb3, quoter: str, token_in: str, token_out: str, amount_in: int, fee_ppm: int,
                               block_number: int | None = None) -> tuple[int, int, int]:
    """QuoterV2 via eth_call: (amount_out, sqrt_price_x96_after, initialized_ticks_crossed)."""
    c = w3.eth.contract(address=AsyncWeb3.to_checksum_address(quoter), abi=abi.UNISWAP_V3_QUOTER_V2)
    params = (AsyncWeb3.to_checksum_address(token_in), AsyncWeb3.to_checksum_address(token_out), amount_in, fee_ppm, 0)
    out, sqrt_after, crossed, _ = await c.functions.quoteExactInputSingle(params).call(block_identifier=block_number)
    return out, sqrt_after, crossed


async def quote_onchain(w3: AsyncWeb3, quoter: str, token_in: str, token_out: str, amount_in: int, fee_ppm: int,
                        block_number: int | None = None) -> int:
    """Exact quote from QuoterV2 via eth_call. Slower than local math; the ground truth for tests."""
    out, _, _ = await quote_onchain_detail(w3, quoter, token_in, token_out, amount_in, fee_ppm, block_number)
    return out
