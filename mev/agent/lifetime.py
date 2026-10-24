"""Follow an opportunity block by block until it no longer clears the minimum profit, then record how many
blocks it lived and which swaps closed it. This is the competition label (who took the spread, and how fast)."""
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from web3 import AsyncWeb3

from mev.db.session import load_active_pools
from mev.config import Settings
from mev.db.models import Swap
from mev.db.session import session
from mev.dex.base import DexAdapter
from mev.pools.prices import usd_prices
from mev.pools.state import fetch_states
from mev.scanner.scanner import Candidate
from mev.simulator.simulate import simulate_size



@dataclass
class Tracker:
    opportunity_id: int
    candidate: Candidate
    size_usd: int
    gas_usd: Decimal
    last_block: int
    nets: list[tuple[int, float]] = field(default_factory=list)   # (block, net_usd) after the opening block
    closed_block: int | None = None
    closing_swaps: list[dict] = field(default_factory=list)

    @property
    def done(self) -> bool:
        return self.closed_block is not None

    @property
    def survived_blocks(self) -> int:
        return (self.closed_block - self.candidate.block_number) if self.closed_block else len(self.nets)

    async def step(self, w3: AsyncWeb3, block_number: int, adapters: dict[str, DexAdapter], s: Settings) -> None:
        if self.done or block_number <= self.last_block:
            return
        c = self.candidate
        states = await fetch_states(w3, load_active_pools(), block_number)
        if c.buy_pool_id not in states or c.sell_pool_id not in states:
            return
        net = simulate_size(c, states, adapters, self.size_usd, self.gas_usd, s, usd_prices(states, adapters)).net_usd
        self.last_block = block_number
        self.nets.append((block_number, float(net)))
        if net < s.min_net_profit_usd:
            self.closed_block = block_number
            self.closing_swaps = closing_swaps(c, block_number)
        elif len(self.nets) >= s.agent_max_track_blocks:
            self.closed_block = block_number   # still open; recorded as right-censored in the payload

    def payload(self) -> dict:
        still_open = bool(self.nets) and self.nets[-1][1] >= 0 and not self.closing_swaps
        return {"opened_block": self.candidate.block_number, "closed_block": self.closed_block,
                "survived_blocks": self.survived_blocks, "censored": still_open and self.done,
                "nets": self.nets, "closing_swaps": self.closing_swaps}


def closing_swaps(c: Candidate, block_number: int) -> list[dict]:
    """Swaps stored by the loop's indexer for either pool in the block that closed the spread, largest first."""
    with session() as db:
        rows = db.scalars(select(Swap).where(Swap.block_number == block_number, Swap.event_type == "swap",
                                             Swap.pool_id.in_([c.buy_pool_id, c.sell_pool_id]))).all()
    rows.sort(key=lambda r: -(r.volume_usd or 0))
    return [{"tx": r.transaction_hash, "log_index": r.log_index, "pool_id": r.pool_id, "volume_usd": r.volume_usd,
             "amount0": r.amount0, "amount1": r.amount1} for r in rows[:5]]
