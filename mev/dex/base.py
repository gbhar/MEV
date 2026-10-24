"""Common adapter interface. Every DEX adapter answers two questions from a PoolState alone:
what is the marginal price, and how much comes out for a given input."""
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from mev.pools.state import PoolState


@dataclass(frozen=True)
class Quote:
    amount_out: int
    exact: bool          # False when the local math is an approximation (e.g. V3 tick crossing)
    note: str = ""


class DexAdapter(Protocol):
    def marginal_price(self, state: PoolState) -> Decimal:
        """token1 per token0, decimal-adjusted (e.g. USDC per WETH)."""

    def quote_exact_in(self, state: PoolState, token_in: str, amount_in: int) -> Quote:
        """Output amount in raw token units for amount_in raw units of token_in."""


def get_adapter(kind: str) -> DexAdapter:
    if kind == "uniswap_v3":
        from mev.dex.uniswap_v3 import UniswapV3Adapter
        return UniswapV3Adapter()
    if kind == "constant_product":
        from mev.dex.constant_product import ConstantProductAdapter
        return ConstantProductAdapter()
    raise ValueError(f"unknown pool kind {kind}")
