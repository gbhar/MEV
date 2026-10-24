import asyncio
import json
from decimal import Decimal

from sqlalchemy import select

from mev.agent import lifetime, notify, watcher
from mev.agent.lifetime import Tracker
from mev.config import Settings
from mev.db.models import Action, Block, Opportunity
from mev.db.session import session
from mev.dex.base import get_adapter
from mev.pools.registry import Pool, Token
from mev.pools.state import PoolState
from mev.scanner.scanner import Candidate

WETH = Token("0x4200000000000000000000000000000000000006", "WETH", 18)
USDC = Token("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "USDC", 6)
A = Pool("cp_a", "dex_a", "constant_product", "0x" + "a" * 40, WETH, USDC, fee_ppm=3000)
B = Pool("cp_b", "dex_b", "constant_product", "0x" + "b" * 40, WETH, USDC, fee_ppm=3000)
ADAPTERS = {"constant_product": get_adapter("constant_product")}
S = Settings(_env_file=None, min_net_profit_usd=1.0, agent_max_track_blocks=5)


def cp_state(pool, usdc_per_eth, block, eth=1000):
    return PoolState(pool, block, reserve0=eth * 10**18, reserve1=eth * usdc_per_eth * 10**6)


def test_webhook_payload_shapes():
    assert notify.webhook_payload("https://discord.com/api/webhooks/1/x", "hi") == {"content": "hi"}
    assert notify.webhook_payload("https://api.telegram.org/bot1:x/sendMessage?chat_id=2", "hi") == {"text": "hi"}
    assert notify.webhook_payload("https://hooks.slack.com/services/x", "hi") == {"text": "hi"}


def test_tracker_closes_when_spread_disappears(monkeypatch):
    prices = {100: (2400, 2500), 101: (2400, 2500), 102: (2495, 2500)}   # spread gone at 102

    async def fake_fetch_states(w3, pools, block):
        a, b = prices[block]
        return {"cp_a": cp_state(A, a, block), "cp_b": cp_state(B, b, block)}

    monkeypatch.setattr(lifetime, "fetch_states", fake_fetch_states)
    monkeypatch.setattr(lifetime, "load_active_pools", lambda *a, **k: [A, B])
    monkeypatch.setattr(lifetime, "closing_swaps", lambda c, b: [{"tx": "0xdead", "pool_id": "cp_b"}])
    c = Candidate("cp_a", "cp_b", Decimal(2400), Decimal(2500), Decimal(400), Decimal(60), 99, "WETH/USDC")
    t = Tracker(1, c, 5000, Decimal("0.05"), last_block=99)
    for b in (100, 101, 102):
        asyncio.run(t.step(None, b, ADAPTERS, S))
    assert t.done and t.closed_block == 102 and t.survived_blocks == 3
    p = t.payload()
    assert p["closing_swaps"][0]["tx"] == "0xdead" and p["censored"] is False
    assert [n for _, n in p["nets"]][:2] == sorted([n for _, n in p["nets"]][:2], reverse=True)


def test_tracker_is_censored_after_max_blocks(monkeypatch):
    async def fake_fetch_states(w3, pools, block):
        return {"cp_a": cp_state(A, 2400, block), "cp_b": cp_state(B, 2500, block)}

    monkeypatch.setattr(lifetime, "fetch_states", fake_fetch_states)
    monkeypatch.setattr(lifetime, "load_active_pools", lambda *a, **k: [A, B])
    c = Candidate("cp_a", "cp_b", Decimal(2400), Decimal(2500), Decimal(400), Decimal(60), 0, "WETH/USDC")
    t = Tracker(1, c, 5000, Decimal("0.05"), last_block=0)
    for b in range(1, 10):
        asyncio.run(t.step(None, b, ADAPTERS, S))
    assert t.done and t.survived_blocks == 5 and t.payload()["censored"] is True


def test_watcher_records_and_describes():
    with session() as db:
        db.query(Action).delete()
        db.merge(Block(block_number=7, timestamp=1, block_hash="0x1", parent_hash="0x0", base_fee_per_gas="1", gas_used=1,
                       gas_limit=2, next_base_fee_per_gas="1", gas_price_wei="1", eth_price_usd=2400.0, gas_usd=0.05,
                       rpc_latency_ms=1.0, pools_fetched=2))
        opp = Opportunity(block_number=7, buy_pool_id="cp_a", sell_pool_id="cp_b", buy_price=2400.0, sell_price=2500.0,
                          spread_bps=416.7, fee_floor_bps=60.0, best_trade_size_usd=5000, best_net_usd=12.5,
                          chosen_trade_size_usd=5000, chosen_net_usd=12.5, risk_passed=True, risk_violations="")
        db.add(opp)
        db.commit()
        opp_id = opp.id
    text = watcher.describe(opp)
    assert "cp_a -> cp_b" in text and "PAPER TRADE" in text and "$5,000" in text
    assert watcher.candidate_of(opp).block_number == 7
    watcher.record(opp_id, 7, "verify", "ok", {"all_match": True})
    with session() as db:
        a = db.scalar(select(Action).where(Action.opportunity_id == opp_id))
    assert a.kind == "verify" and json.loads(a.payload)["all_match"] is True


def test_redact_hides_password():
    assert watcher.redact("postgresql+psycopg://mev:s3cret@mev-postgres-rw:5432/mev") == "postgresql+psycopg://mev:***@mev-postgres-rw:5432/mev"
    assert watcher.redact("sqlite:///mev.db") == "sqlite:///mev.db"


def test_rpc_throttle_detection():
    from mev.chain.rpc import is_throttle
    class Http(Exception):
        status = 429
    assert is_throttle(Http())
    assert is_throttle(RuntimeError("{'code': -32001, 'message': \"You've reached the usage limit for your current plan\"}"))
    assert not is_throttle(RuntimeError("execution reverted"))
    assert not is_throttle(None)
