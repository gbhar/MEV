"""The laptop agent: a second process that watches the paper-trading database and acts on what the loop
finds. Nothing here signs or sends a transaction.

    python -m mev.agent            # follow new opportunities
    python -m mev.agent --from-id 0 --once   # process everything already stored, then exit
"""
import argparse
import asyncio
import json
import logging
from decimal import Decimal

from sqlalchemy import func, select

from mev.agent import llm, notify
from mev.agent.lifetime import Tracker
from mev.agent.verify import verify
from mev.chain.addresses import POOLS
from mev.chain.rpc import make_web3
from mev.config import settings
from mev.db.models import Action, Block, Opportunity, PaperTrade
from mev.db.session import init_db, session
from mev.dex.base import get_adapter
from mev.scanner.scanner import Candidate

log = logging.getLogger("mev.agent")


def record(opp_id: int, block: int, kind: str, status: str, payload: dict) -> None:
    with session() as db:
        db.add(Action(opportunity_id=opp_id, block_number=block, kind=kind, status=status, payload=json.dumps(payload, default=str)))
        db.commit()


def candidate_of(o: Opportunity) -> Candidate:
    return Candidate(o.buy_pool_id, o.sell_pool_id, Decimal(str(o.buy_price)), Decimal(str(o.sell_price)),
                     Decimal(str(o.spread_bps)), Decimal(str(o.fee_floor_bps)), o.block_number)


def describe(o: Opportunity) -> str:
    size, net = (o.chosen_trade_size_usd, o.chosen_net_usd) if o.risk_passed else (o.best_trade_size_usd, o.best_net_usd)
    verdict = "PAPER TRADE" if o.risk_passed else f"blocked: {o.risk_violations[:80]}"
    return f"{o.buy_pool_id} -> {o.sell_pool_id} spread {o.spread_bps:.1f} bps, ${size:,} nets ${net:.2f} at block {o.block_number} ({verdict})"


class Agent:
    def __init__(self, w3, last_id: int):
        self.w3 = w3
        self.last_id = last_id
        self.adapters = {k: get_adapter(k) for k in {p.kind for p in POOLS}}
        self.trackers: list[Tracker] = []

    def new_opportunities(self) -> list[Opportunity]:
        with session() as db:
            rows = db.scalars(select(Opportunity).where(Opportunity.id > self.last_id).order_by(Opportunity.id)).all()
        if rows:
            self.last_id = rows[-1].id
        return [o for o in rows if o.risk_passed or o.best_net_usd >= settings.agent_alert_min_net_usd]

    async def handle(self, o: Opportunity) -> None:
        text = describe(o)
        log.info("opportunity %s: %s", o.id, text)
        with session() as db:
            blk = db.get(Block, o.block_number)
        gas_usd = Decimal(str(blk.gas_usd)) if blk else Decimal("0.05")
        c = candidate_of(o)
        size = o.chosen_trade_size_usd if o.risk_passed else o.best_trade_size_usd
        stored_net = o.chosen_net_usd if o.risk_passed else o.best_net_usd

        if settings.agent_macos_notify:
            ok = notify.macos_notify("MEV opportunity", text)
            record(o.id, o.block_number, "alert", "ok" if ok else "skipped", {"text": text})
        if settings.alert_webhook:
            try:
                ok, info = await notify.webhook(settings.alert_webhook, text)
                record(o.id, o.block_number, "webhook", "ok" if ok else "failed", {"response": info})
            except Exception as e:
                record(o.id, o.block_number, "webhook", "failed", {"error": repr(e)})

        verification = None
        try:
            verification = await verify(self.w3, c, size, stored_net, gas_usd, self.adapters, settings)
            record(o.id, o.block_number, "verify", "ok" if verification["all_match"] else "failed", verification)
            log.info("verify %s: fresh net %.2f vs stored %.2f, quoter legs %s", o.id, verification["fresh_net_usd"],
                     stored_net, {k: v["match"] for k, v in verification["legs"].items()})
        except Exception as e:
            record(o.id, o.block_number, "verify", "failed", {"error": repr(e)})
            log.warning("verify %s failed: %r", o.id, e)

        if llm.enabled(settings):
            try:
                verdict = await llm.assess(settings, {"opportunity": text, "risk_violations": o.risk_violations,
                                                      "verification": verification})
                record(o.id, o.block_number, "llm", "ok", verdict)
                log.info("llm %s: %s", o.id, verdict)
            except Exception as e:
                record(o.id, o.block_number, "llm", "failed", {"error": repr(e)})

        self.trackers.append(Tracker(o.id, c, size, gas_usd, last_block=o.block_number))

    async def track(self, head: int) -> None:
        for t in list(self.trackers):
            try:
                await t.step(self.w3, head, self.adapters, settings)
            except Exception as e:
                log.warning("tracker %s failed at %s: %r", t.opportunity_id, head, e)
                continue
            if t.done:
                p = t.payload()
                record(t.opportunity_id, t.closed_block, "lifetime", "ok", p)
                log.info("opportunity %s survived %d block(s); closed at %s by %d swap(s)", t.opportunity_id,
                         p["survived_blocks"], t.closed_block, len(t.closing_swaps))
                self.trackers.remove(t)

    async def tick(self) -> int:
        new = self.new_opportunities()
        for o in new:
            await self.handle(o)
        if self.trackers:
            head = await self.w3.eth.block_number
            await self.track(head)
        return len(new)


async def run(from_id: int | None, once: bool) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    init_db()
    with session() as db:
        latest = db.scalar(select(func.max(Opportunity.id))) or 0
        opened = db.scalar(select(func.count(PaperTrade.id))) or 0
    agent = Agent(make_web3(), latest if from_id is None else from_id)
    log.info("agent watching %s from opportunity id %s (%d paper trades so far); alerts: macos=%s webhook=%s llm=%s",
             settings.database_url, agent.last_id, opened, settings.agent_macos_notify, bool(settings.alert_webhook), llm.enabled(settings))
    while True:
        try:
            n = await agent.tick()
        except Exception as e:
            log.warning("tick failed: %r", e)
            n = 0
        if once and n == 0 and not agent.trackers:
            return
        await asyncio.sleep(settings.agent_poll_s)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-id", type=int, default=None, help="start from this opportunity id (default: only new ones)")
    ap.add_argument("--once", action="store_true", help="process what is there, finish tracking, then exit")
    a = ap.parse_args()
    asyncio.run(run(a.from_id, a.once))
