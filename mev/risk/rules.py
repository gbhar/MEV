"""Deterministic risk rules. No model output is consulted here, and nothing can loosen a rule at runtime.
Returns the list of violated rules; empty means the trade may proceed (to paper execution in V1).

check() is a pure function of its inputs. Facts about the paper book (daily loss, failure streak,
what was already opened this block) are computed from stored rows outside this module and passed
in as plain numbers in a Book."""
from dataclasses import dataclass
from decimal import Decimal

from mev.config import Settings
from mev.simulator.simulate import SimResult


@dataclass(frozen=True)
class Book:
    daily_loss_usd: Decimal = Decimal(0)   # realized paper loss resolved in the current UTC day (positive = loss)
    failed_trades: int = 0                 # consecutive resolutions that were no longer profitable
    pool_busy: bool = False                # a paper trade was already opened on one of the candidate's pools this block
    repeat: bool = False                   # a pool of this candidate is held by a trade whose spread has persisted since it opened


def check(sim: SimResult, head_block: int, state_block: int, s: Settings, book: Book = Book()) -> list[str]:
    violations: list[str] = []
    if sim.net_usd < s.min_net_profit_usd:
        violations.append(f"net_profit {sim.net_usd:.2f} < min {s.min_net_profit_usd}")
    if sim.trade_size_usd > s.max_trade_size_usd:
        violations.append(f"trade_size {sim.trade_size_usd} > max {s.max_trade_size_usd}")
    if sim.gas_usd > s.max_gas_usd:
        violations.append(f"gas {sim.gas_usd:.2f} > max {s.max_gas_usd}")
    if head_block - state_block > s.max_state_age_blocks:
        violations.append(f"stale state: head {head_block}, state {state_block}")
    if not sim.exact:
        violations.append("simulation not exact (tick crossing)")
    if book.daily_loss_usd > s.max_daily_loss_usd:
        violations.append(f"daily_loss {book.daily_loss_usd:.2f} > max {s.max_daily_loss_usd}")
    if book.failed_trades > s.max_failed_trades:
        violations.append(f"failed_trades {book.failed_trades} > max {s.max_failed_trades}")
    if book.pool_busy:
        violations.append("pool_busy: a paper trade was already opened on this pool in this block")
    if book.repeat:
        violations.append("repeat: pool already traded while its spread persisted")
    return violations


def allowed(results: list[SimResult], head_block: int, state_block: int, s: Settings, book: Book = Book()) -> list[SimResult]:
    """The grid rows that pass every rule. Choosing among these does not weaken any rule: the caller
    still runs check() on whatever it picks."""
    return [r for r in results if not check(r, head_block, state_block, s, book)]
