"""Read-only HTTP API over the platform database (spec section 26). Nothing here can trade: the execution engine
lives in the loop process and this API only reports what it did (`/executions`).
Run: make api  (uvicorn mev.api.app:app --port 8000)"""
from decimal import Decimal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from mev.config import settings
import json

from mev.db.models import Action, Execution, Feature, Opportunity, PaperTrade, PoolRow, PoolSnapshot, Simulation
from mev.db.session import init_db, load_active_pools, session
from mev.dex.base import get_adapter
from mev.pools.prices import usd_prices
from mev.pools.state import PoolState
from mev.scanner.scanner import Candidate, quote_per_base
from mev.simulator.simulate import simulate_size

app = FastAPI(title="MEV platform V1 (paper)", version="0.1.0")
ADAPTERS = {k: get_adapter(k) for k in ("uniswap_v3", "constant_product")}


def pools_by_id() -> dict:
    return {p.pool_id: p for p in load_active_pools()}


init_db()
GAS_USD_ASSUMED = Decimal("0.05")   # typical Base cost of the flash loan + two swaps; the live loop prices it per block


def row(obj) -> dict:
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


@app.get("/health")
def health() -> dict:
    with session() as db:
        last = db.scalar(select(func.max(Opportunity.block_number)))
        snap = db.scalar(select(func.max(PoolSnapshot.block_number)))
    return {"status": "healthy", "last_snapshot_block": snap, "last_opportunity_block": last, "execution": settings.execution_mode,
            "flash_arb": settings.flash_arb_address}


@app.get("/pools")
def pools(active: bool | None = True, min_tvl: float | None = None, max_tvl: float | None = None, pair: str | None = None,
          dex: str | None = None, limit: int = Query(200, le=2000)) -> list[dict]:
    """The pool database (bootstrap + discovery). Default: active pools only, largest TVL first."""
    q = select(PoolRow).order_by(PoolRow.tvl_usd.desc().nulls_last()).limit(limit)
    if active is not None:
        q = q.where(PoolRow.active.is_(active))
    if min_tvl is not None:
        q = q.where(PoolRow.tvl_usd >= min_tvl)
    if max_tvl is not None:
        q = q.where(PoolRow.tvl_usd <= max_tvl)
    if pair:
        q = q.where(PoolRow.pair == pair)
    if dex:
        q = q.where(PoolRow.dex == dex)
    with session() as db:
        return [row(p) for p in db.scalars(q)]


@app.get("/opportunities")
def opportunities(dex: str | None = None, pair: str | None = None, min_profit: float | None = None,
                  risk_passed: bool | None = None, from_block: int | None = None, to_block: int | None = None,
                  limit: int = Query(100, le=1000)) -> list[dict]:
    q = select(Opportunity).order_by(Opportunity.id.desc()).limit(limit)
    if dex:
        ids = [p.pool_id for p in load_active_pools() if p.dex == dex]
        q = q.where((Opportunity.buy_pool_id.in_(ids)) | (Opportunity.sell_pool_id.in_(ids)))
    if pair:
        q = q.where(Opportunity.pair == pair)
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
        execs = db.scalars(select(Execution).where(Execution.opportunity_id == o.id).order_by(Execution.id)).all()
        actions = db.scalars(select(Action).where(Action.opportunity_id == o.id).order_by(Action.id)).all()
        snaps = db.scalars(select(PoolSnapshot).where(PoolSnapshot.block_number == o.block_number,
                                                      PoolSnapshot.pool_id.in_([o.buy_pool_id, o.sell_pool_id]))).all()
    return {"candidate": row(o), "market_state": [row(s) for s in snaps], "simulations": [row(s) for s in sims],
            "features": row(feat) if feat else None, "ai_predictions": None,   # models run in shadow mode later
            "risk_decision": {"passed": o.risk_passed, "violations": o.risk_violations},
            "paper_trade": row(trade) if trade else None,
            "executions": [row(e) for e in execs],
            "actions": [action_row(a) for a in actions]}


def action_row(a: Action) -> dict:
    d = row(a)
    try:
        d["payload"] = json.loads(a.payload)
    except ValueError:
        pass
    return d


@app.get("/actions")
def actions(kind: str | None = None, opportunity_id: int | None = None, limit: int = Query(100, le=1000)) -> list[dict]:
    """What the laptop agent did: alert | webhook | verify | lifetime | llm."""
    q = select(Action).order_by(Action.id.desc()).limit(limit)
    if kind:
        q = q.where(Action.kind == kind)
    if opportunity_id is not None:
        q = q.where(Action.opportunity_id == opportunity_id)
    with session() as db:
        return [action_row(a) for a in db.scalars(q)]


@app.get("/pnl")
def pnl() -> dict:
    with session() as db:
        trades = db.scalars(select(PaperTrade).where(PaperTrade.resolved_block.is_not(None)).order_by(PaperTrade.resolved_block, PaperTrade.id)).all()
        # only the simulations of the traded size for the traded opportunities (the table holds one row per grid size per candidate)
        sims = {}
        if trades:
            pairs = {(t.opportunity_id, t.trade_size_usd) for t in trades}
            for s_ in db.scalars(select(Simulation).where(Simulation.opportunity_id.in_([o for o, _ in pairs]))):
                if (s_.opportunity_id, s_.trade_size_usd) in pairs:
                    sims[s_.opportunity_id] = s_
        live = db.execute(select(func.count(Execution.id), func.sum(Execution.realized_usd), func.sum(Execution.gas_usd),
                                 func.sum(Execution.profit_usd)).where(Execution.mode == "live", Execution.status.in_(["mined", "failed"]))).one()
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
            "execution_loss_total": sum(t.execution_loss_usd or 0.0 for t in trades),
            "live": {"transactions": live[0], "realized_net_usd": live[1] or 0.0, "gas_usd": live[2] or 0.0, "gross_profit_usd": live[3] or 0.0}}


@app.get("/executions")
def executions(mode: str | None = None, status: str | None = None, pair: str | None = None, opportunity_id: int | None = None,
               limit: int = Query(100, le=1000)) -> list[dict]:
    """What the execution engine did (Phases 6/7): mode shadow = built and eth_call-simulated, never sent;
    mode live = signed and sent. status: ok | revert | blocked | sent | mined | failed | error."""
    q = select(Execution, Opportunity.pair).join(Opportunity, Execution.opportunity_id == Opportunity.id).order_by(Execution.id.desc()).limit(limit)
    if mode:
        q = q.where(Execution.mode == mode)
    if status:
        q = q.where(Execution.status == status)
    if pair:
        q = q.where(Opportunity.pair == pair)
    if opportunity_id is not None:
        q = q.where(Execution.opportunity_id == opportunity_id)
    with session() as db:
        return [{**row(e), "pair": pr} for e, pr in db.execute(q)]


@app.get("/executions/summary")
def executions_summary() -> list[dict]:
    """Counts and totals per mode and status: the shadow-mode scorecard (would it have succeeded, expected vs simulated)."""
    with session() as db:
        rows = db.execute(select(Execution.mode, Execution.status, func.count(Execution.id), func.sum(Execution.realized_usd),
                                 func.avg(Execution.latency_ms), func.avg(Execution.gas_estimate))
                          .group_by(Execution.mode, Execution.status).order_by(Execution.mode, Execution.status)).all()
    return [{"mode": m, "status": st, "count": n, "realized_usd": r or 0.0, "avg_latency_ms": lat, "avg_gas_estimate": g} for m, st, n, r, lat, g in rows]


class SimulateRequest(BaseModel):
    pool_buy: str = Field(description="pool_id of the pool to buy WETH on")
    pool_sell: str
    trade_size_usd: int = Field(gt=0)
    block_number: int | None = Field(None, description="defaults to the latest stored snapshot")


def _state_from_snapshot(s: PoolSnapshot, by_id: dict) -> PoolState:
    p = by_id[s.pool_id]
    return PoolState(p, s.block_number, sqrt_price_x96=int(s.sqrt_price_x96) if s.sqrt_price_x96 else None, tick=s.tick,
                     liquidity=int(s.liquidity) if s.liquidity else None,
                     reserve0=int(s.reserve0) if s.reserve0 else None, reserve1=int(s.reserve1) if s.reserve1 else None)


@app.post("/simulate")
def simulate_endpoint(req: SimulateRequest) -> dict:
    by_id = pools_by_id()
    if req.pool_buy not in by_id or req.pool_sell not in by_id:
        raise HTTPException(400, f"unknown pool; known: {sorted(by_id)}")
    if by_id[req.pool_buy].pair_key != by_id[req.pool_sell].pair_key:
        raise HTTPException(400, "pools are not the same token pair")
    with session() as db:
        block = req.block_number or db.scalar(select(func.max(PoolSnapshot.block_number)))
        snaps = {}
        for s_ in db.scalars(select(PoolSnapshot).where(PoolSnapshot.block_number <= block).order_by(PoolSnapshot.block_number.desc())):
            snaps.setdefault(s_.pool_id, s_)   # latest snapshot per pool at or before the block
    if req.pool_buy not in snaps or req.pool_sell not in snaps:
        raise HTTPException(404, f"no snapshot for both pools at block {block}")
    states = {k: _state_from_snapshot(v, by_id) for k, v in snaps.items() if v.pool_id in by_id}
    prices = usd_prices(states, ADAPTERS)
    buy, sell = states[req.pool_buy], states[req.pool_sell]
    bp, sp = quote_per_base(buy, ADAPTERS), quote_per_base(sell, ADAPTERS)
    c = Candidate(req.pool_buy, req.pool_sell, bp, sp, (sp - bp) / bp * 10_000 if bp else Decimal(0),
                  Decimal(buy.pool.fee_bps + sell.pool.fee_bps), block, buy.pool.pair)
    r = simulate_size(c, states, ADAPTERS, req.trade_size_usd, GAS_USD_ASSUMED, settings, prices)
    note = "snapshots hold no V3 tick window, so a V3 leg that crosses a tick is reported exact=false" if not r.exact else ""
    return {"block_number": block, "gas_usd_assumed": float(GAS_USD_ASSUMED), "note": note,
            **{k: (float(v) if isinstance(v, Decimal) else v) for k, v in r.__dict__.items()}}
