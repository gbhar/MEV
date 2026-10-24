"""Static pool registry. In V1 this is a hard-coded list; later it becomes a DB-backed registry
maintained by the pool indexer and fed by the protocol-research agent."""
from dataclasses import dataclass
from typing import Literal

PoolKind = Literal["uniswap_v3", "constant_product"]


@dataclass(frozen=True)
class Token:
    address: str
    symbol: str
    decimals: int


@dataclass(frozen=True)
class Pool:
    pool_id: str
    dex: str
    kind: PoolKind
    address: str
    token0: Token
    token1: Token
    fee_ppm: int                 # parts per million: 500 = 0.05%, 3000 = 0.30%
    tick_spacing: int | None = None  # uniswap_v3 only

    @property
    def fee_bps(self) -> float:
        return self.fee_ppm / 100

    @property
    def pair(self) -> str:
        return f"{self.token0.symbol}/{self.token1.symbol}"
