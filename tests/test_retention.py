"""mev.db.retention: raw state older than the short window goes, losing decisions older than the long window go,
newer rows, net-positive decisions and anything a paper trade/action points at stay."""
from sqlalchemy import select, text

from mev.db.models import Action, Block, Feature, Opportunity, PaperTrade, PoolSnapshot, Simulation, Swap
from mev.db.retention import purge
from mev.db.session import session

NOW = 1_800_000_000
H = 3600
TABLES = ("features", "simulations", "actions", "paper_trades", "pool_snapshots", "swaps", "opportunities", "blocks")


def _block(n, ts):
    return Block(block_number=n, timestamp=ts, block_hash=f"0x{n:064x}", parent_hash=f"0x{n-1:064x}", base_fee_per_gas="1", gas_used=1, gas_limit=1,
                 next_base_fee_per_gas="1", gas_price_wei="1", eth_price_usd=2000.0, gas_usd=0.05, rpc_latency_ms=1.0, pools_fetched=1)


def _opp(n, net=-1.0):
    return Opportunity(block_number=n, pair="WETH/USDC", buy_pool_id="univ3_weth_usdc_5", sell_pool_id="pcs3_weth_usdc_1", buy_price=1.0, sell_price=1.0,
                       spread_bps=1.0, fee_floor_bps=1.0, best_trade_size_usd=500, best_net_usd=net, risk_passed=False)


def _feature(o):
    return Feature(opportunity_id=o.id, feature_version=1, block_number=o.block_number, timestamp=0, spread_bps=1.0, trade_size_usd=500, liquidity_usd=1.0,
                   buy_liquidity_usd=1.0, sell_liquidity_usd=1.0, price_impact_bps=1.0, volume_1m=0.0, volume_5m=0.0, pool_imbalance=0.5, gas_usd=0.05,
                   rpc_latency_ms=1.0, block_age_ms=1.0, opportunity_age_ms=1.0, dex_id="x", token_pair_id="WETH/USDC", hour_of_day=0, day_of_week=0)


def _seed(db, n, age_h, net=-1.0):
    db.add(_block(n, NOW - int(age_h * H))); db.flush()
    o = _opp(n, net); db.add(o); db.flush()
    db.add(_feature(o))
    db.add(Simulation(opportunity_id=o.id, trade_size_usd=500, gross_usd=0, flash_fee_usd=0, gas_usd=0, buffer_usd=0, net_usd=net, exact=True))
    db.add(PoolSnapshot(pool_id="univ3_weth_usdc_5", block_number=n, price=1.0))
    db.add(Swap(pool_id="univ3_weth_usdc_5", block_number=n, transaction_hash=f"0x{n:064x}", log_index=0, event_type="swap"))
    return o


def test_split_windows():
    with session() as db:
        for t in TABLES:
            db.execute(text(f"delete from {t}"))
        ancient = _seed(db, 100, age_h=10 * 24)            # losing, 10 days: everything goes
        winner = _seed(db, 101, age_h=10 * 24, net=0.5)     # net-positive, 10 days: decision rows + block stay
        traded = _seed(db, 102, age_h=10 * 24)              # losing but a paper trade points at it: stays
        recent = _seed(db, 200, age_h=2 * 24)               # 2 days: raw state goes, decision stays
        fresh = _seed(db, 300, age_h=1)                     # 1 hour: everything stays
        db.add(PaperTrade(opportunity_id=traded.id, opened_block=102, trade_size_usd=500, expected_net_usd=1.0))
        db.add(Action(opportunity_id=traded.id, block_number=102, kind="alert", status="ok"))
        db.commit()
        ids = {k: o.id for k, o in dict(ancient=ancient, winner=winner, traded=traded, recent=recent, fresh=fresh).items()}

    deleted = purge(hours=24, decision_hours=7 * 24, batch=2, now=NOW)
    assert deleted == {"features": 1, "simulations": 1, "opportunities": 1, "pool_snapshots": 4, "swaps": 4, "blocks": 1}

    with session() as db:
        assert set(db.scalars(select(Opportunity.id))) == {ids["winner"], ids["traded"], ids["recent"], ids["fresh"]}
        assert set(db.scalars(select(Feature.opportunity_id))) == set(db.scalars(select(Simulation.opportunity_id))) == {ids["winner"], ids["traded"], ids["recent"], ids["fresh"]}
        assert sorted(db.scalars(select(Block.block_number))) == [101, 102, 200, 300]   # blocks of surviving decisions stay
        assert db.scalars(select(PoolSnapshot.block_number)).all() == [300]
        assert db.scalars(select(Swap.block_number)).all() == [300]
        assert db.scalar(select(PaperTrade).where(PaperTrade.opportunity_id == ids["traded"])) is not None


def test_purge_is_a_noop_when_nothing_is_old():
    with session() as db:
        for t in TABLES:
            db.execute(text(f"delete from {t}"))
        db.add(_block(400, NOW - 60)); db.commit()
    assert purge(hours=24, decision_hours=7 * 24, now=NOW) == {}
    with session() as db:
        assert db.scalars(select(Block.block_number)).all() == [400]
