"""V1 hot loop in one process: new block -> pool states -> scan -> simulate -> risk -> paper trade.

Blocks are processed in order (last+1 .. head, at most max_catchup_blocks behind) so a paper trade
opened at N is normally resolved at N+1; a block that fails is retried before it is skipped, and a
block already in the blocks table is never processed twice. Per block the writes are: the block row
and pool snapshots first (ground truth, one transaction), then each paper-trade resolution, then one
transaction per candidate holding its opportunity, its simulations and, if risk passed, its paper
trade. Candidates are handled best-net first and at most one paper trade is opened per pool per block.

Run:  python -m mev.main            (forever)
      python -m mev.main --blocks 3 (process three new blocks, resolve what is left, then exit)
"""
import argparse
import asyncio
import logging
import time
from decimal import Decimal

from prometheus_client import start_http_server

from mev import metrics
from mev.chain.addresses import POOLS
from mev.chain.rpc import make_web3
from mev.config import settings
from mev.db.models import Block, Opportunity, PoolSnapshot, Simulation
from mev.features import compute as features
from mev.indexer import events as indexer
from mev.db.session import init_db, session
from mev.dex.base import get_adapter
from mev.paper.executor import PaperExecutor
from mev.pools import state as pool_state
from mev.pools.state import PoolState, fetch_state
from mev.risk import rules
from mev.scanner.scanner import scan
from mev.simulator.simulate import best, fee_params, gas_cost_usd, gas_price_wei, next_base_fee, simulate

log = logging.getLogger("mev")
ETH_PRICE_REF = "univ3_weth_usdc_5"   # deepest pool; any other fetched pool is the fallback


def snapshot_rows(states: dict[str, PoolState], adapters) -> list[PoolSnapshot]:
    rows = []
    for st in states.values():
        price = adapters[st.pool.kind].marginal_price(st)
        metrics.pool_price.labels(st.pool.pool_id).set(float(price))
        rows.append(PoolSnapshot(
            pool_id=st.pool.pool_id, block_number=st.block_number, price=float(price),
            sqrt_price_x96=str(st.sqrt_price_x96) if st.sqrt_price_x96 is not None else None,
            tick=st.tick, liquidity=str(st.liquidity) if st.liquidity is not None else None,
            reserve0=str(st.reserve0) if st.reserve0 is not None else None,
            reserve1=str(st.reserve1) if st.reserve1 is not None else None))
    return rows


async def fetch_logs_safe(w3, block_number: int) -> list[indexer.PoolEvent]:
    if not settings.fetch_logs:
        return []
    try:
        return await indexer.fetch_events(w3, POOLS, block_number)
    except Exception as e:
        metrics.log_fetch_errors.inc()
        log.warning("block=%s eth_getLogs failed, continuing without events: %s", block_number, short_error(e))
        return []


def short_error(e: BaseException) -> str:
    status = getattr(e, "status", None)
    return f"{type(e).__name__}({status})" if status else f"{type(e).__name__}: {str(e)[:120]}"


async def fetch_all(w3, block_number: int, partial_ok: bool) -> tuple[dict, dict[str, PoolState], list[indexer.PoolEvent]]:
    """Header, every pool state (one Multicall3 eth_call) and the block's pool logs: three RPC requests,
    concurrently. A missing pool raises (so the block is retried) unless partial_ok; a failed log fetch
    only loses the events."""
    block, events, states = await asyncio.gather(w3.eth.get_block(block_number), fetch_logs_safe(w3, block_number),
                                                 pool_state.fetch_states(w3, POOLS, block_number), return_exceptions=True)
    if isinstance(block, BaseException):
        raise block
    if isinstance(states, BaseException):
        metrics.rpc_errors.inc()
        raise states
    if isinstance(events, BaseException):
        events = []
    for p in POOLS:
        if p.pool_id not in states:
            metrics.rpc_errors.inc()
            if not partial_ok:
                raise RuntimeError(f"block {block_number}: pool {p.pool_id} state call failed")
            log.warning("block=%s pool %s fetch failed, continuing without it", block_number, p.pool_id)
    indexer.reconcile(states, events, POOLS, block_number)   # cross-check, and rebuild a missing x*y=k state from its Sync
    if not states:
        raise RuntimeError(f"block {block_number}: no pool state fetched")
    return block, states, events


async def process_block(w3, block_number: int, adapters, paper: PaperExecutor, *,
                        partial_ok: bool = False, resolve_only: bool = False, replay: bool = False) -> None:
    """replay=True: the chain head is taken to be block_number itself (the stale-state rule and block age
    are judged as they would have been live), so a replay of a past range is reproducible."""
    with session() as db:
        if db.get(Block, block_number) is not None:
            log.warning("block=%s already stored, skipping (restart or lagging replica)", block_number)
            return
        prev = db.get(Block, block_number - 1)
    t0 = time.perf_counter()
    block, states, events = await fetch_all(w3, block_number, partial_ok)
    latency_ms = (time.perf_counter() - t0) * 1000
    metrics.rpc_latency.observe(latency_ms / 1000)
    parent_mismatch = prev is not None and prev.block_hash != block["parentHash"].hex()
    if parent_mismatch:
        metrics.reorgs.inc()
        pool_state.reset_hints()
        log.warning("block=%s parentHash %s != stored hash of %s (%s): reorg or replica switch; tick windows re-fetched",
                    block_number, block["parentHash"].hex(), block_number - 1, prev.block_hash)

    ref = states.get(ETH_PRICE_REF) or next(iter(states.values()))
    eth_price = adapters[ref.pool.kind].marginal_price(ref)
    base_fee, ts = block["baseFeePerGas"], block["timestamp"]
    next_fee = next_base_fee(base_fee, block["gasUsed"], block["gasLimit"], *fee_params(bytes(block["extraData"]), settings))
    gas_usd = gas_cost_usd(gas_price_wei(next_fee, settings), eth_price, settings)       # a decision at N is included at N+1
    gas_usd_now = gas_cost_usd(gas_price_wei(base_fee, settings), eth_price, settings)   # what a tx in this block pays

    with session() as db:
        db.add(Block(block_number=block_number, timestamp=ts, block_hash=block["hash"].hex(), parent_hash=block["parentHash"].hex(),
                     base_fee_per_gas=str(base_fee), gas_used=block["gasUsed"], gas_limit=block["gasLimit"],
                     next_base_fee_per_gas=str(next_fee), gas_price_wei=str(gas_price_wei(next_fee, settings)),
                     eth_price_usd=float(eth_price), gas_usd=float(gas_usd), rpc_latency_ms=latency_ms, pools_fetched=len(states),
                     parent_mismatch=parent_mismatch))
        db.add_all(snapshot_rows(states, adapters))
        db.add_all(indexer.swap_rows(events, POOLS))
        db.commit()

    for p, r, realized in paper.resolve(block_number, ts, states, gas_usd_now):
        metrics.paper_trades_resolved.labels(str(r.net_usd >= settings.min_net_profit_usd)).inc()
        log.info("paper resolved trade=%s size=$%s expected=%.2f actual=%.2f realized=%.2f lag=%d",
                 p.trade_id, p.size_usd, p.expected_net, r.net_usd, realized, block_number - p.candidate.block_number)
    if resolve_only:
        return

    t1 = time.perf_counter()
    candidates = scan(states, adapters)
    metrics.scanner_latency.observe(time.perf_counter() - t1)
    pairs = {(c.buy_pool_id, c.sell_pool_id) for c in candidates}
    paper.note_candidates(pairs)
    features.note_candidates(block_number, pairs)
    metrics.opportunities_detected.inc(len(candidates))
    if candidates:
        # re-read the head so the stale-state rule sees how old the states really are (in replay: the block itself)
        head_now = block_number if replay else await w3.eth.block_number
        daily_loss, failed = paper.book(ts)
    ctx = features.BlockContext(block_number, ts, gas_usd, latency_ms, 0.0 if replay else time.time())
    sims = []
    for c in candidates:
        t2 = time.perf_counter()
        sims.append((c, simulate(c, states, adapters, gas_usd, settings)))
        metrics.simulation_latency.observe(time.perf_counter() - t2)
    sims.sort(key=lambda cr: best(cr[1]).net_usd, reverse=True)
    opened: dict[str, int] = {}
    for c, results in sims:
        unconstrained = best(results)
        book = rules.Book(daily_loss, failed, pool_busy=c.buy_pool_id in opened or c.sell_pool_id in opened,
                          repeat=paper.is_repeat(c))
        allowed = rules.allowed(results, head_now, c.block_number, settings, book)
        b = best(allowed) if allowed else unconstrained
        violations = rules.check(b, head_now, c.block_number, settings, book)

        with session() as db:
            opp = Opportunity(block_number=block_number, head_block=head_now, buy_pool_id=c.buy_pool_id, sell_pool_id=c.sell_pool_id,
                              buy_price=float(c.buy_price), sell_price=float(c.sell_price),
                              spread_bps=float(c.spread_bps), fee_floor_bps=float(c.fee_floor_bps),
                              best_trade_size_usd=unconstrained.trade_size_usd, best_net_usd=float(unconstrained.net_usd),
                              chosen_trade_size_usd=b.trade_size_usd, chosen_net_usd=float(b.net_usd),
                              risk_passed=not violations, risk_violations="; ".join(violations))
            db.add(opp)
            db.flush()
            db.add_all(Simulation(opportunity_id=opp.id, trade_size_usd=r.trade_size_usd, gross_usd=float(r.gross_usd),
                                  flash_fee_usd=float(r.flash_fee_usd), gas_usd=float(r.gas_usd),
                                  buffer_usd=float(r.buffer_usd), net_usd=float(r.net_usd), exact=r.exact,
                                  dex_fee_usd=float(r.dex_fee_usd), slippage_usd=float(r.slippage_usd)) for r in results)
            t3 = time.perf_counter()
            db.add(features.compute(db, opp.id, c, b, states, adapters, ctx))
            metrics.feature_latency.observe(time.perf_counter() - t3)
            if not violations:
                paper.open(db, opp.id, c, b.trade_size_usd, b.net_usd)
            db.commit()
        if not violations:
            opened[c.buy_pool_id] = opened[c.sell_pool_id] = b.trade_size_usd

        metrics.simulations.inc(len(results))
        metrics.profitable_simulations.inc(sum(1 for r in results if r.net_usd > 0))
        for v in violations:
            metrics.risk_blocks.labels(v.split(" ")[0].rstrip(":")).inc()
        metrics.spread_bps.labels(c.buy_pool_id, c.sell_pool_id).set(float(c.spread_bps))
        log.info("block=%s %s -> %s spread=%.1fbps floor=%.1fbps best=$%s/%.2f chosen=$%s/%.2f %s",
                 block_number, c.buy_pool_id, c.sell_pool_id, c.spread_bps, c.fee_floor_bps,
                 unconstrained.trade_size_usd, unconstrained.net_usd, b.trade_size_usd, b.net_usd,
                 "PAPER EXECUTE" if not violations else f"BLOCK ({'; '.join(violations)})")

    metrics.blocks_processed.inc()
    prices = {k: f"{float(adapters[v.pool.kind].marginal_price(v)):.2f}" for k, v in states.items()}
    log.info("block=%s ts=%s gas=$%.4f rpc=%.0fms events=%d candidates=%d prices=%s", block_number, ts, gas_usd, latency_ms, len(events), len(candidates), prices)


async def process_with_retries(w3, block_number: int, adapters, paper: PaperExecutor, *, resolve_only: bool = False, replay: bool = False) -> bool:
    """Retries transient failures (lagging replica 'block not found', 429, timeouts); partial pool data is
    accepted only on the last attempt. Returns False when the block was given up on."""
    for attempt in range(1, settings.block_retries + 1):
        try:
            await process_block(w3, block_number, adapters, paper, partial_ok=attempt == settings.block_retries, resolve_only=resolve_only, replay=replay)
            return True
        except Exception:
            log.exception("block %s attempt %d/%d failed", block_number, attempt, settings.block_retries)
            await asyncio.sleep(settings.poll_interval_s * attempt)
    metrics.blocks_skipped.inc()
    log.error("block %s skipped after %d attempts", block_number, settings.block_retries)
    return False


async def poll_head(w3, backoff: float) -> tuple[int | None, float]:
    """Head block number, or None after a logged failure; the second value is the next backoff to use."""
    try:
        return await w3.eth.block_number, settings.poll_interval_s
    except Exception as e:
        metrics.rpc_errors.inc()
        log.warning("head poll failed (%r), retrying in %.1fs", e, backoff)
        await asyncio.sleep(backoff)
        return None, min(backoff * 2, 30.0)


async def run(max_blocks: int | None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    init_db()
    if settings.metrics_port:
        start_http_server(settings.metrics_port)
    w3 = make_web3()
    adapters = {k: get_adapter(k) for k in {p.kind for p in POOLS}}
    paper = PaperExecutor(settings, adapters)
    log.info("chain_id=%s rpc=%s pools=%d metrics=:%d", await w3.eth.chain_id, settings.rpc_http, len(POOLS), settings.metrics_port)

    last: int | None = None
    done = 0
    backoff = settings.poll_interval_s
    while max_blocks is None or done < max_blocks:
        head, backoff = await poll_head(w3, backoff)
        if head is None:
            continue
        if last is None:
            last = head - 1
        if head <= last:   # nothing new, or a lagging replica / reorg answered with an older head
            await asyncio.sleep(settings.poll_interval_s)
            continue
        if head - last > settings.max_catchup_blocks:
            skipped = head - settings.max_catchup_blocks - last
            metrics.blocks_skipped.inc(skipped)
            log.warning("gap: %d blocks behind head %s, skipping %s..%s", head - last, head, last + 1, head - settings.max_catchup_blocks)
            paper.note_gap()
            last = head - settings.max_catchup_blocks
        for b in range(last + 1, head + 1):
            await process_with_retries(w3, b, adapters, paper)
            last = b
            done += 1
            if max_blocks is not None and done >= max_blocks:
                break

    if max_blocks is not None and paper.pending:
        log.info("waiting for one more block to resolve %d pending paper trades", len(paper.pending))
        while True:
            head, backoff = await poll_head(w3, backoff)
            if head is not None and head > last:
                break
            await asyncio.sleep(settings.poll_interval_s)
        await process_with_retries(w3, last + 1, adapters, paper, resolve_only=True)
        if paper.pending:
            log.warning("paper trades still unresolved (resolved on the next start): %s", [p.trade_id for p in paper.pending])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--blocks", type=int, default=None, help="process this many new blocks then exit")
    args = ap.parse_args()
    asyncio.run(run(args.blocks))


if __name__ == "__main__":
    main()
