"""Paper execution. Nothing is signed or broadcast.

A paper trade is opened at block N with the chosen size and expected net. On the next processed
block (N+1 while the loop keeps up; resolve_lag_blocks records the gap otherwise) the same trade is
re-simulated against the new pool states and two numbers are stored:

- actual_net_usd: the re-simulated net with the same formula as at decision time (buffer included).
  still_profitable = actual_net_usd >= min_net_profit_usd, i.e. would the same rule set still open it.
- realized_net_usd: what an atomic flash-loan arbitrage would have booked. If the sell leg repays the
  loan plus its fee (gross - flash_fee > 0) the contract's profit guard passes and the trade earns
  gross - flash_fee - gas; otherwise the transaction reverts and only gas is lost. The safety buffer
  is a decision-time haircut, never a paid cost, so it is not part of realized P&L.

cumulative_net (the paper_profit_usd gauge) is the sum of realized_net_usd and is re-read from the
table on startup; unresolved rows from a previous run are reloaded and resolved on the next block.
All of this still ignores the race (whether we would have been first), which README documents."""
import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mev.config import Settings
from mev.db.models import Opportunity, PaperTrade
from mev.db.session import session
from mev.dex.base import DexAdapter
from mev.metrics import paper_profit_usd, paper_resolve_lag, paper_trades
from mev.pools.state import PoolState
from mev.scanner.scanner import Candidate
from mev.simulator.simulate import SimResult, simulate_size

log = logging.getLogger("mev.paper")


@dataclass
class Pending:
    trade_id: int
    candidate: Candidate
    size_usd: int
    expected_net: Decimal
    attempts: int = 0


def realized_net(r: SimResult) -> Decimal:
    """Atomic-execution rule: profit guard passes -> gross - flash - gas; otherwise the revert still pays gas."""
    if r.gross_usd - r.flash_fee_usd > 0:
        return r.gross_usd - r.flash_fee_usd - r.gas_usd
    return -r.gas_usd


class PaperExecutor:
    def __init__(self, settings: Settings, adapters: dict[str, DexAdapter], lenders=None):
        self.s = settings
        self.adapters = adapters
        self.lenders = lenders
        self.pending: list[Pending] = []
        self.active_pairs: dict[tuple[str, str], int] = {}   # (buy, sell) -> block of the open trade on that pair
        self.cumulative_net = Decimal(0)
        self._load()

    def _load(self) -> None:
        """Rebuild state from the table: realized total and any trades a previous run left unresolved."""
        with session() as db:
            total = db.scalar(select(func.sum(PaperTrade.realized_net_usd)))
            rows = db.execute(select(PaperTrade, Opportunity).join(Opportunity, PaperTrade.opportunity_id == Opportunity.id)
                              .where(PaperTrade.resolved_block.is_(None)).order_by(PaperTrade.id)).all()
        self.cumulative_net = Decimal(str(total or 0))
        for t, o in rows:
            c = Candidate(o.buy_pool_id, o.sell_pool_id, Decimal(str(o.buy_price)), Decimal(str(o.sell_price)),
                          Decimal(str(o.spread_bps)), Decimal(str(o.fee_floor_bps)), o.block_number, o.pair or "",
                          tuple(o.route.split(">")) if o.route else (), tuple(o.path.split(">")) if o.path else ())
            self.pending.append(Pending(t.id, c, t.trade_size_usd, Decimal(str(t.expected_net_usd))))
            self.active_pairs[c.key] = t.opened_block
        paper_profit_usd.set(float(self.cumulative_net))
        if rows:
            log.info("reloaded %d unresolved paper trades: %s", len(rows), [t.id for t, _ in rows])

    def open(self, db: Session, opportunity_id: int, c: Candidate, size_usd: int, expected_net: Decimal) -> int:
        """Adds the row to the caller's session (committed together with its opportunity) and tracks it."""
        row = PaperTrade(opportunity_id=opportunity_id, opened_block=c.block_number,
                         trade_size_usd=size_usd, expected_net_usd=float(expected_net))
        db.add(row)
        db.flush()
        self.pending.append(Pending(row.id, c, size_usd, expected_net))
        self.active_pairs[c.key] = c.block_number
        paper_trades.inc()
        return row.id

    def note_candidates(self, pairs: set[tuple[str, ...]]) -> None:
        """A pair stays active only while it is a candidate on every processed block."""
        for pair in list(self.active_pairs):
            if pair not in pairs:
                del self.active_pairs[pair]

    def note_gap(self) -> None:
        self.active_pairs.clear()

    def is_repeat(self, c: Candidate) -> bool:
        """True while an open trade's pair is still in spread and shares a pool with this candidate: the
        mispriced liquidity was already sold once, so it must not be booked again."""
        return any(pid in key for key in self.active_pairs for pid in c.pools)

    def book(self, block_ts: int) -> tuple[Decimal, int]:
        """Deterministic risk inputs from the table: realized loss in the UTC day of block_ts (positive =
        loss) and the current run of resolutions that were no longer profitable."""
        day0 = block_ts - block_ts % 86400
        with session() as db:
            total = db.scalar(select(func.sum(PaperTrade.realized_net_usd))
                              .where(PaperTrade.resolved_ts >= day0, PaperTrade.resolved_ts < day0 + 86400))
            flags = db.scalars(select(PaperTrade.still_profitable).where(PaperTrade.resolved_block.is_not(None))
                               .order_by(PaperTrade.resolved_block.desc(), PaperTrade.id.desc())
                               .limit(self.s.max_failed_trades + 1)).all()
        failed = 0
        for f in flags:
            if f:
                break
            failed += 1
        return max(Decimal(0), -Decimal(str(total or 0))), failed

    def resolve(self, block_number: int, block_ts: int, states: dict[str, PoolState],
                gas_usd: Decimal, prices: dict[str, Decimal] | None = None) -> list[tuple[Pending, SimResult, Decimal]]:
        """gas_usd is what a transaction included in this block pays (this block's base fee + tip)."""
        resolved: list[tuple[Pending, SimResult, Decimal]] = []
        for p in list(self.pending):
            if any(pid not in states for pid in p.candidate.pools):
                continue  # a pool fetch failed this block; try again on the next one
            try:
                r = simulate_size(p.candidate, states, self.adapters, p.size_usd, gas_usd, self.s, prices, self.lenders)
                realized = realized_net(r)
                with session() as db:
                    row = db.get(PaperTrade, p.trade_id)
                    if row is None:   # its opening transaction never committed
                        self.pending.remove(p)
                        log.warning("paper trade %s has no row, dropping", p.trade_id)
                        continue
                    row.resolved_block = block_number
                    row.resolved_ts = block_ts
                    row.resolve_lag_blocks = block_number - p.candidate.block_number
                    row.actual_net_usd = float(r.net_usd)
                    row.actual_gross_usd = float(r.gross_usd)
                    row.actual_gas_usd = float(r.gas_usd)
                    row.realized_net_usd = float(realized)
                    row.execution_loss_usd = float(p.expected_net - realized)
                    row.still_profitable = r.net_usd >= self.s.min_net_profit_usd
                    db.commit()
            except Exception:
                p.attempts += 1
                log.exception("paper trade %s resolve failed (attempt %d)", p.trade_id, p.attempts)
                if p.attempts >= self.s.resolve_retries:
                    self.pending.remove(p)
                    log.error("paper trade %s left unresolved; it will be retried on the next start", p.trade_id)
                continue
            self.pending.remove(p)   # only after the commit, so a failure cannot resolve it twice
            self.cumulative_net += realized
            paper_resolve_lag.observe(block_number - p.candidate.block_number)
            resolved.append((p, r, realized))
        paper_profit_usd.set(float(self.cumulative_net))
        return resolved
