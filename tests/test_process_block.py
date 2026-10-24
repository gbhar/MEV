"""Drives main.process_block offline with synthetic pool states: writes, one-trade-per-pool, repeat
suppression, N+1 resolution and idempotent re-processing."""
from decimal import Decimal

import pytest
from sqlalchemy import select

from mev import main
from mev.db.models import Feature, Swap, Block, Opportunity, PaperTrade, PoolSnapshot
from mev.db.session import session
from mev.dex.base import get_adapter
from mev.paper.executor import PaperExecutor
from mev.pools.registry import Pool, Token
from mev.pools.state import PoolState

WETH = Token("0x4200000000000000000000000000000000000006", "WETH", 18)
USDC = Token("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "USDC", 6)
A = Pool("cp_a", "dex_a", "constant_product", "0x" + "a" * 40, WETH, USDC, fee_ppm=3000)
B = Pool("cp_b", "dex_b", "constant_product", "0x" + "b" * 40, WETH, USDC, fee_ppm=3000)
C = Pool("cp_c", "dex_c", "constant_product", "0x" + "c" * 40, WETH, USDC, fee_ppm=3000)
ADAPTERS = {"constant_product": get_adapter("constant_product")}
EXTRA = bytes.fromhex("01000000640000000500000000004c4b40")


def cp_state(pool, usdc_per_eth, block):
    return PoolState(pool, block, reserve0=1000 * 10**18, reserve1=1000 * usdc_per_eth * 10**6)


class FakeEth:
    def __init__(self):
        self.head = 0

    @property
    async def block_number(self):
        return self.head


class FakeW3:
    def __init__(self):
        self.eth = FakeEth()


@pytest.fixture
def world(monkeypatch):
    with session() as db:
        for m in (PaperTrade, Feature, Swap, Opportunity, PoolSnapshot, Block):
            db.query(m).delete()
        db.commit()
    prices = {}

    async def fetch_all(w3, block_number, partial_ok):
        block = {"baseFeePerGas": 5_000_000, "gasUsed": 20_000_000, "gasLimit": 400_000_000, "timestamp": 1_700_000_000 + 2 * block_number,
                 "hash": bytes(32), "parentHash": bytes(32), "extraData": EXTRA}
        return block, {p.pool_id: cp_state(p, prices[p.pool_id], block_number) for p in (A, B, C)}, []

    monkeypatch.setattr(main, "fetch_all", fetch_all)
    monkeypatch.setattr(main.settings, "trade_sizes_usd", [500, 1000, 5000])
    w3 = FakeW3()
    return w3, prices, PaperExecutor(main.settings, ADAPTERS)


async def test_one_trade_per_pool_then_repeat_then_resolve(world):
    w3, prices, paper = world
    # cp_c is 4% dear versus cp_a and cp_b: two candidates both selling into cp_c.
    prices.update(cp_a=2400, cp_b=2410, cp_c=2500)
    w3.eth.head = 10
    await main.process_block(w3, 10, ADAPTERS, paper)
    with session() as db:
        opps = db.scalars(select(Opportunity).where(Opportunity.block_number == 10).order_by(Opportunity.id)).all()
        trades = db.scalars(select(PaperTrade)).all()
        assert db.get(Block, 10).pools_fetched == 3
        assert len(db.scalars(select(PoolSnapshot).where(PoolSnapshot.block_number == 10)).all()) == 3
    assert [o.buy_pool_id for o in opps] == ["cp_a", "cp_b"]      # best net first
    assert opps[0].risk_passed and opps[0].head_block == 10 and opps[0].chosen_trade_size_usd == opps[0].best_trade_size_usd
    assert not opps[1].risk_passed and opps[1].risk_violations.startswith("pool_busy")
    assert len(trades) == 1 and trades[0].resolved_block is None

    # Same spread persists: resolved at N+1 as still profitable; no new trade (repeat), the pair is already open.
    w3.eth.head = 11
    await main.process_block(w3, 11, ADAPTERS, paper)
    with session() as db:
        t = db.get(PaperTrade, trades[0].id)
        opps11 = db.scalars(select(Opportunity).where(Opportunity.block_number == 11).order_by(Opportunity.id)).all()
    assert t.resolved_block == 11 and t.resolve_lag_blocks == 1 and t.still_profitable is True
    assert t.realized_net_usd == pytest.approx(t.actual_net_usd + 5000 * 2 / 10_000)   # buffer not paid
    assert all(o.risk_violations.startswith("repeat") for o in opps11) and paper.pending == []   # cp_c already sold into

    # Re-processing a stored block is a no-op (restart / lagging replica).
    await main.process_block(w3, 11, ADAPTERS, paper)
    with session() as db:
        assert len(db.scalars(select(PoolSnapshot).where(PoolSnapshot.block_number == 11)).all()) == 3

    # Spread gone for a block, then back: it is a fresh opportunity and opens again.
    prices.update(cp_c=2400)
    w3.eth.head = 12
    await main.process_block(w3, 12, ADAPTERS, paper)
    prices.update(cp_c=2500)
    w3.eth.head = 13
    await main.process_block(w3, 13, ADAPTERS, paper)
    with session() as db:
        assert len(db.scalars(select(PaperTrade)).all()) == 2


async def test_stale_state_blocks_when_head_moved_on(world):
    w3, prices, paper = world
    prices.update(cp_a=2400, cp_b=2400, cp_c=2500)
    w3.eth.head = 23   # processing block 20 while the chain is already at 23 (catch-up)
    await main.process_block(w3, 20, ADAPTERS, paper)
    with session() as db:
        opps = db.scalars(select(Opportunity).where(Opportunity.block_number == 20)).all()
    assert opps and all(not o.risk_passed and "stale state: head 23, state 20" in o.risk_violations for o in opps)
    assert paper.pending == []
