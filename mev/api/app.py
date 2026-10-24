"""Read-only HTTP API over the paper-trading database (spec section 26). Nothing here can trade.
Run: make api  (uvicorn mev.api.app:app --port 8000)"""
from decimal import Decimal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from mev.chain.addresses import POOLS
from mev.config import settings
from mev.db.models import Feature, Opportunity, PaperTrade, PoolSnapshot, Simulation
from mev.db.session import session
from mev.dex.base import get_adapter
from mev.pools.state import PoolState
from mev.scanner.scanner import Candidate
from mev.simulator.simulate import simulate_size

app = FastAPI(title="MEV platform V1 (paper)", version="0.1.0")
ADAPTERS = {k: get_adapter(k) for k in {p.kind for p in POOLS}}
BY_ID = {p.pool_id: p for p in POOLS}
GAS_USD_ASSUMED = Decimal("0.05")   # typical Base cost of the flash loan + two swaps; the live loop prices it per block


def row(obj) -> dict:
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


@app.get("/health")
def health() -> dict:
    with session() as db:
        last = db.scalar(select(func.max(Opportunity.block_number)))
        snap = db.scalar(select(func.max(PoolSnapshot.block_number)))
    return {"status": "healthy", "last_snapshot_block": snap, "last_opportunity_block": last, "execution": "paper-only"}


@app.get("/opportunities")
def opportunities(dex: str | None = None, pair: str | None = None, min_profit: float | None = None,
                  risk_passed: bool | None = None, from_block: int | None = None, to_block: int | None = None,
                  limit: int = Query(100, le=1000)) -> list[dict]:
    q = select(Opportunity).order_by(Opportunity.id.desc()).limit(limit)
    if dex:
        ids = [p.pool_id for p in POOLS if p.dex == dex]
        q = q.where((Opportunity.buy_pool_id.in_(ids)) | (Opportunity.sell_pool_id.in_(ids)))
    if pair:
        ids = [p.pool_id for p in POOLS if p.pair == pair]
        q = q.where(Opportunity.buy_pool_id.in_(ids))
    if min_profit is not None:
        q = q.where(Opportunity.best_net_usd >= min_profit)
    if risk_passed is not None:
        q = q.where(Opportunity.risk_passed == risk_passed)
    if from_block is not None:
        q = q.where(Opportunity.block_number >= from_block)
    if to_block is not None:
        q = q.where(Opportunity.block_number <= to_block)
    with session() as db:
        return [row(o) for o in db.scalars(q)]


@app.get("/opportunities/{opportunity_id}")
def opportunity(opportunity_id: int) -> dict:
    with session() as db:
        o = db.get(Opportunity, opportunity_id)
        if o is None:
            raise HTTPException(404, "no such opportunity")
        sims = db.scalars(select(Simulation).where(Simulation.opportunity_id == o.id).order_by(Simulation.trade_size_usd)).all()
        feat = db.scalar(select(Feature).where(Feature.opportunity_id == o.id))
        trade = db.scalar(select(PaperTrade).where(PaperTrade.opportunity_id == o.id))
        snaps = db.scalars(select(PoolSnapshot).where(PoolSnapshot.block_number == o.block_number,
                                                      PoolSnapshot.pool_id.in_([o.buy_pool_id, o.sell_pool_id]))).all()
    return {"candidate": row(o), "market_state": [row(s) for s in snaps], "simulations": [row(s) for s in sims],
            "features": row(feat) if feat else None, "ai_predictions": None,   # models run in shadow mode later
            "risk_decision": {"passed": o.risk_passed, "violations": o.risk_violations},
            "paper_trade": row(trade) if trade else None}


@app.get("/pnl")
def pnl() -> dict:
    with session() as db:
        trades = db.scalars(select(PaperTrade).where(PaperTrade.resolved_block.is_not(None)).order_by(PaperTrade.resolved_block, PaperTrade.id)).all()
        sims = {s.opportunity_id: s for s in db.scalars(select(Simulation))} if trades else {}
    gross = sum(t.actual_gross_usd or 0.0 for t in trades)
    gas = sum(t.actual_gas_usd or 0.0 for t in trades)
    net = sum(t.realized_net_usd or 0.0 for t in trades)
    wins = sum(1 for t in trades if (t.realized_net_usd or 0.0) > 0)
    peak = running = drawdown = 0.0
    for t in trades:
        running += t.realized_net_usd or 0.0
        peak = max(peak, running)
        drawdown = max(drawdown, peak - running)
    flash = sum((sims.get(t.opportunity_id).flash_fee_usd if sims.get(t.opportunity_id) else 0.0) for t in trades)
    return {"trades": len(trades), "gross_profit": gross, "fees": flash, "gas": gas, "net_profit": net,
            "win_rate": wins / len(trades) if trades else None, "max_drawdown": drawdown,
            "expected_net_total": sum(t.expected_net_usd for t in trades),
            "execution_loss_total": sum(t.execution_loss_usd or 0.0 for t in trades)}


class SimulateRequest(BaseModel):
    pool_buy: str = Field(description="pool_id of the pool to buy WETH on")
    pool_sell: str
    trade_size_usd: int = Field(gt=0)
    block_number: int | None = Field(None, description="defaults to the latest stored snapshot")


def _state_from_snapshot(s: PoolSnapshot) -> PoolState:
    p = BY_ID[s.pool_id]
    return PoolState(p, s.block_number, sqrt_price_x96=int(s.sqrt_price_x96) if s.sqrt_price_x96 else None, tick=s.tick,
                     liquidity=int(s.liquidity) if s.liquidity else None,
                     reserve0=int(s.reserve0) if s.reserve0 else None, reserve1=int(s.reserve1) if s.reserve1 else None)


@app.post("/simulate")
def simulate_endpoint(req: SimulateRequest) -> dict:
    if req.pool_buy not in BY_ID or req.pool_sell not in BY_ID:
        raise HTTPException(400, f"unknown pool; known: {sorted(BY_ID)}")
    with session() as db:
        block = req.block_number or db.scalar(select(func.max(PoolSnapshot.block_number)))
        snaps = {s.pool_id: s for s in db.scalars(select(PoolSnapshot).where(PoolSnapshot.block_number == block,
                                                                              PoolSnapshot.pool_id.in_([req.pool_buy, req.pool_sell])))}
    if len(snaps) < 2:
        raise HTTPException(404, f"no snapshot for both pools at block {block}")
    states = {k: _state_from_snapshot(v) for k, v in snaps.items()}
    buy, sell = states[req.pool_buy], states[req.pool_sell]
    bp, sp = ADAPTERS[buy.pool.kind].marginal_price(buy), ADAPTERS[sell.pool.kind].marginal_price(sell)
    c = Candidate(req.pool_buy, req.pool_sell, bp, sp, (sp - bp) / bp * 10_000 if bp else Decimal(0),
                  Decimal(buy.pool.fee_bps + sell.pool.fee_bps), block)
    r = simulate_size(c, states, ADAPTERS, req.trade_size_usd, GAS_USD_ASSUMED, settings)
    note = "snapshots hold no V3 tick window, so a V3 leg that crosses a tick is reported exact=false" if not r.exact else ""
    return {"block_number": block, "gas_usd_assumed": float(GAS_USD_ASSUMED), "note": note,
            **{k: (float(v) if isinstance(v, Decimal) else v) for k, v in r.__dict__.items()}}
