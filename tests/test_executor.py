from decimal import Decimal

import pytest
from sqlalchemy import select

from mev.config import Settings
from mev.db.models import Opportunity, PaperTrade
from mev.db.session import session
from mev.dex.base import get_adapter
from mev.paper import executor as ex
from mev.paper.executor import PaperExecutor, realized_net
from mev.pools.registry import Pool, Token
from mev.pools.state import PoolState
from mev.scanner.scanner import scan
from mev.simulator.simulate import SimResult, best, simulate

WETH = Token("0x4200000000000000000000000000000000000006", "WETH", 18)
USDC = Token("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "USDC", 6)
A = Pool("cp_a", "dex_a", "constant_product", "0x" + "a" * 40, WETH, USDC, fee_ppm=3000)
B = Pool("cp_b", "dex_b", "constant_product", "0x" + "b" * 40, WETH, USDC, fee_ppm=3000)
ADAPTERS = {"constant_product": get_adapter("constant_product")}
S = Settings(_env_file=None, trade_sizes_usd=[500, 1000, 5000], min_net_profit_usd=1.0, resolve_retries=2)
GAS = Decimal("0.05")


def cp_state(pool, eth_reserve, usdc_per_eth, block):
    return PoolState(pool, block, reserve0=eth_reserve * 10**18, reserve1=int(eth_reserve * usdc_per_eth * 10**6))


def states_at(block, spread_price=2500):
    return {"cp_a": cp_state(A, 1000, 2400, block), "cp_b": cp_state(B, 1000, spread_price, block)}


def open_trade(paper, block):
    states = states_at(block)
    (c,) = scan(states, ADAPTERS)
    b = best(simulate(c, states, ADAPTERS, GAS, S))
    with session() as db:
        opp = Opportunity(block_number=block, buy_pool_id=c.buy_pool_id, sell_pool_id=c.sell_pool_id,
                          buy_price=float(c.buy_price), sell_price=float(c.sell_price), spread_bps=float(c.spread_bps),
                          fee_floor_bps=float(c.fee_floor_bps), best_trade_size_usd=b.trade_size_usd,
                          best_net_usd=float(b.net_usd), risk_passed=True)
        db.add(opp)
        db.flush()
        tid = paper.open(db, opp.id, c, b.trade_size_usd, b.net_usd)
        db.commit()
    return tid, b


@pytest.fixture(autouse=True)
def clean_tables():
    with session() as db:
        db.query(PaperTrade).delete()
        db.query(Opportunity).delete()
        db.commit()


def test_realized_net_follows_the_atomic_profit_guard():
    ok = SimResult(1000, 0, 0, 0, Decimal("3"), Decimal("0.5"), Decimal("0.1"), Decimal("0.2"), Decimal("2.2"), True)
    assert realized_net(ok) == Decimal("2.4")          # gross - flash - gas; buffer is never paid
    revert = SimResult(1000, 0, 0, 0, Decimal("-35"), Decimal("0.5"), Decimal("0.1"), Decimal("0.2"), Decimal("-35.8"), True)
    assert realized_net(revert) == Decimal("-0.1")     # reverted tx: only gas


def test_open_resolve_books_realized_and_breakdown():
    paper = PaperExecutor(S, ADAPTERS)
    tid, b = open_trade(paper, 100)
    assert len(paper.pending) == 1 and paper.is_repeat(paper.pending[0].candidate)
    out = paper.resolve(101, 1_700_000_000, states_at(101, spread_price=2402), GAS)   # spread collapsed -> revert
    (p, r, realized) = out[0]
    assert p.trade_id == tid and realized == -GAS and paper.pending == []
    assert paper.cumulative_net == -GAS
    with session() as db:
        row = db.get(PaperTrade, tid)
    assert row.resolved_block == 101 and row.resolved_ts == 1_700_000_000 and row.resolve_lag_blocks == 1
    assert row.realized_net_usd == float(-GAS) and row.actual_net_usd == float(r.net_usd)
    assert row.still_profitable is False and row.execution_loss_usd == pytest.approx(float(b.net_usd + GAS))
    # same spread again -> still profitable by the same threshold that opened it, and lag is recorded
    tid2, _ = open_trade(paper, 102)
    (p2, r2, realized2) = paper.resolve(104, 1_700_000_004, states_at(104), GAS)[0]
    with session() as db:
        row2 = db.get(PaperTrade, tid2)
    assert row2.still_profitable is True and row2.resolve_lag_blocks == 2 and realized2 == r2.net_usd + r2.buffer_usd


def test_failed_resolve_keeps_trade_pending_and_does_not_double_count(monkeypatch):
    paper = PaperExecutor(S, ADAPTERS)
    t1, _ = open_trade(paper, 200)
    with session() as db:   # a second opportunity on the same block/pair would violate the unique index
        db.query(Opportunity).delete()
        db.commit()
    t2, _ = open_trade(paper, 201)
    calls = {"n": 0}
    real = ex.simulate_size

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("database is locked")
        return real(*a, **k)

    monkeypatch.setattr(ex, "simulate_size", flaky)
    out = paper.resolve(202, 1_700_000_000, states_at(202), GAS)
    assert [p.trade_id for p, _, _ in out] == [t1]
    assert [p.trade_id for p in paper.pending] == [t2] and paper.pending[0].attempts == 1
    total = paper.cumulative_net
    out = paper.resolve(203, 1_700_000_002, states_at(203), GAS)
    assert [p.trade_id for p, _, _ in out] == [t2] and paper.pending == []
    assert paper.cumulative_net == total + out[0][2]   # t1 was not resolved or counted a second time
    with session() as db:
        assert db.get(PaperTrade, t1).resolved_block == 202


def test_unresolved_trades_and_total_are_reloaded_on_restart():
    paper = PaperExecutor(S, ADAPTERS)
    t1, _ = open_trade(paper, 300)
    paper.resolve(301, 1_700_000_000, states_at(301), GAS)
    with session() as db:
        db.query(Opportunity).delete()
        db.commit()
    t2, _ = open_trade(paper, 302)
    fresh = PaperExecutor(S, ADAPTERS)   # "restart"
    assert [p.trade_id for p in fresh.pending] == [t2]
    assert fresh.cumulative_net == paper.cumulative_net
    assert fresh.is_repeat(fresh.pending[0].candidate)
    fresh.note_candidates(set())
    assert not fresh.is_repeat(fresh.pending[0].candidate)
    fresh.resolve(305, 1_700_000_006, states_at(305), GAS)
    with session() as db:
        assert db.get(PaperTrade, t2).resolve_lag_blocks == 3
        assert db.scalar(select(PaperTrade).where(PaperTrade.resolved_block.is_(None))) is None


def test_book_reports_daily_loss_and_failure_streak():
    paper = PaperExecutor(S, ADAPTERS)
    day = 1_700_000_000 - 1_700_000_000 % 86400
    for i in range(3):
        with session() as db:
            db.query(Opportunity).delete()
            db.commit()
        open_trade(paper, 400 + 2 * i)
        paper.resolve(401 + 2 * i, day + 10 + i, states_at(401 + 2 * i, spread_price=2402), GAS)   # three reverts
    loss, failed = paper.book(day + 3600)
    assert loss == pytest.approx(3 * GAS) and failed == 3
    assert paper.book(day + 86400)[0] == 0   # next UTC day starts clean
    with session() as db:
        db.query(Opportunity).delete()
        db.commit()
    open_trade(paper, 410)
    paper.resolve(411, day + 20, states_at(411), GAS)   # profitable one resets the streak
    assert paper.book(day + 3600)[1] == 0
