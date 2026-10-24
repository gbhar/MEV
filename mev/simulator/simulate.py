"""Deterministic economics. Borrow token1 (USDC) via flash loan, buy token0 (WETH) on the cheap
pool, sell it on the expensive pool, repay. Net profit is computed from the two swap outputs,
which already include pool fees and price impact, so those are NOT subtracted again.

dex_fee_usd and slippage_usd are diagnostics only (which layer eats the spread). They decompose
what is already inside gross_usd and must never be subtracted from it.

Gas: a trade decided on block N can only be included in N+1, so gas is priced at the EIP-1559
base fee of N+1 (derived from N's header) plus a constant priority fee from Settings."""
from dataclasses import dataclass
from decimal import Decimal

from mev.config import Settings
from mev.dex.base import DexAdapter
from mev.pools.state import PoolState
from mev.scanner.scanner import Candidate


@dataclass(frozen=True)
class SimResult:
    trade_size_usd: int
    amount_in: int           # raw token1 units borrowed
    token0_out: int          # raw WETH from the buy leg
    amount_back: int         # raw token1 from the sell leg
    gross_usd: Decimal
    flash_fee_usd: Decimal
    gas_usd: Decimal
    buffer_usd: Decimal
    net_usd: Decimal
    exact: bool
    dex_fee_usd: Decimal = Decimal(0)    # informational: pool fees on both legs, already inside gross_usd
    slippage_usd: Decimal = Decimal(0)   # informational: price impact vs. marginal prices, already inside gross_usd


def fee_params(extra_data: bytes, s: Settings) -> tuple[int, int, int]:
    """(elasticity, denominator, min_base_fee) for the next block. OP-stack Holocene/Jovian headers carry them in
    extraData (version 1: u32 denominator, u32 elasticity, optional u64 min base fee); Settings are the fallback."""
    if len(extra_data) >= 9 and extra_data[0] == 1:
        denominator, elasticity = int.from_bytes(extra_data[1:5], "big"), int.from_bytes(extra_data[5:9], "big")
        min_fee = int.from_bytes(extra_data[9:17], "big") if len(extra_data) >= 17 else s.min_base_fee_wei
        if denominator > 0 and elasticity > 0:
            return elasticity, denominator, min_fee
    return s.base_fee_elasticity, s.base_fee_change_denominator, s.min_base_fee_wei


def next_base_fee(base_fee: int, gas_used: int, gas_limit: int, elasticity: int, denominator: int, min_base_fee: int = 0) -> int:
    """EIP-1559 base fee of block N+1 from block N's header."""
    target = gas_limit // elasticity
    if target <= 0 or gas_used == target:
        return max(base_fee, min_base_fee)
    if gas_used > target:
        return max(base_fee + max(base_fee * (gas_used - target) // target // denominator, 1), min_base_fee)
    return max(base_fee - base_fee * (target - gas_used) // target // denominator, min_base_fee)


def gas_price_wei(base_fee: int, s: Settings) -> int:
    return base_fee + s.priority_fee_wei


def gas_cost_usd(gas_price_wei: int, eth_price_usd: Decimal, s: Settings) -> Decimal:
    """gas_price_wei must already include the priority fee (see gas_price_wei())."""
    l2 = Decimal(s.gas_units * gas_price_wei) / Decimal(10**18) * eth_price_usd
    return l2 + Decimal(str(s.l1_data_fee_usd))


def simulate_size(c: Candidate, states: dict[str, PoolState], adapters: dict[str, DexAdapter],
                  size_usd: int, gas_usd: Decimal, s: Settings) -> SimResult:
    buy, sell = states[c.buy_pool_id], states[c.sell_pool_id]
    t0, t1 = buy.pool.token0, buy.pool.token1
    amount_in = size_usd * 10**t1.decimals
    q1 = adapters[buy.pool.kind].quote_exact_in(buy, t1.address, amount_in)
    q2 = adapters[sell.pool.kind].quote_exact_in(sell, t0.address, q1.amount_out)
    scale = Decimal(10**t1.decimals)
    gross = (Decimal(q2.amount_out) - Decimal(amount_in)) / scale
    flash = Decimal(size_usd) * s.flash_loan_fee_bps / 10_000
    buffer = Decimal(size_usd) * s.safety_buffer_bps / 10_000
    net = gross - flash - gas_usd - buffer
    # Diagnostics at the marginal prices of the states being simulated (already inside gross):
    # size * (sell/buy - 1) == dex_fee + slippage + gross, with fees measured on the sell-side value.
    size = Decimal(size_usd)
    buy_fee, sell_fee = Decimal(buy.pool.fee_ppm) / 1_000_000, Decimal(sell.pool.fee_ppm) / 1_000_000
    buy_p, sell_p = adapters[buy.pool.kind].marginal_price(buy), adapters[sell.pool.kind].marginal_price(sell)
    ratio = sell_p / buy_p if buy_p > 0 else Decimal(0)
    dex_fee = size * ratio * (1 - (1 - buy_fee) * (1 - sell_fee))
    slippage = size * (1 - buy_fee) * (1 - sell_fee) * ratio - size - gross
    return SimResult(size_usd, amount_in, q1.amount_out, q2.amount_out, gross, flash, gas_usd, buffer, net,
                     q1.exact and q2.exact, dex_fee, slippage)


def simulate(c: Candidate, states: dict[str, PoolState], adapters: dict[str, DexAdapter],
             gas_usd: Decimal, s: Settings) -> list[SimResult]:
    """Grid search over trade sizes. The caller picks the best size that passes the risk rules."""
    return [simulate_size(c, states, adapters, size, gas_usd, s) for size in s.trade_sizes_usd]


def best(results: list[SimResult]) -> SimResult:
    return max(results, key=lambda r: r.net_usd)
