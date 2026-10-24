"""Pool registry. The bootstrap set lives in mev.chain.addresses; at runtime the loop loads the active set
from the pools table (filled by mev.pools.discovery) and falls back to the bootstrap set."""
from dataclasses import dataclass
from typing import Literal

PoolKind = Literal["uniswap_v3", "constant_product"]
# ABI/fee differences inside a kind: uniswap (also sushi), pancake (7-word slot0), slipstream (6-word slot0,
# dynamic fee), v2 (Sync uint112), aerodrome (Sync uint256, per-pool fee)
Variant = Literal["uniswap", "pancake", "slipstream", "v2", "aerodrome"]

# The token we borrow and measure in, when a pair contains more than one candidate. Dollar tokens first,
# then the assets with a reliable USD reference pool.
QUOTE_PRIORITY = ["USDC", "USDbC", "USDT", "DAI", "WETH", "cbBTC"]
STABLE_SYMBOLS = {"USDC", "USDbC", "USDT", "DAI"}


@dataclass(frozen=True)
class Token:
    address: str
    symbol: str
    decimals: int

    @property
    def key(self) -> str:
        return self.address.lower()


@dataclass(frozen=True)
class Pool:
    pool_id: str
    dex: str
    kind: PoolKind
    address: str
    token0: Token
    token1: Token
    fee_ppm: int                     # parts per million: 100 = 1 bp, 3000 = 30 bps
    tick_spacing: int | None = None  # uniswap_v3 kinds only
    variant: Variant = "uniswap"

    @property
    def fee_bps(self) -> float:
        return self.fee_ppm / 100

    @property
    def quote(self) -> Token:
        ranks = {s: i for i, s in enumerate(QUOTE_PRIORITY)}
        r0, r1 = ranks.get(self.token0.symbol, 99), ranks.get(self.token1.symbol, 99)
        if r0 == r1:
            return self.token1
        return self.token1 if r1 < r0 else self.token0

    @property
    def base(self) -> Token:
        return self.token0 if self.quote is self.token1 else self.token1

    @property
    def quote_is_token1(self) -> bool:
        return self.quote is self.token1

    @property
    def pair(self) -> str:
        return f"{self.base.symbol}/{self.quote.symbol}"

    @property
    def pair_key(self) -> tuple[str, str]:
        return (self.base.key, self.quote.key)
