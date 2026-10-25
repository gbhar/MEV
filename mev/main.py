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
from mev.chain import rpc
from mev.chain.rpc import make_web3
from mev.config import settings
from mev.db.models import Block, Opportunity, PoolSnapshot, Simulation
from mev.features import compute as features
from mev.indexer import events as indexer
from mev.db.session import init_db, load_active_pools, session
from mev.dex.base import get_adapter
from mev.execution.engine import ExecutionEngine
from mev.execution.signer import load_signer
from mev.paper.executor import PaperExecutor
from mev.pools import state as pool_state
from mev.pools.prices import usd_prices
from mev.pools.state import PoolState, fetch_state
from mev.risk import rules
from mev.scanner.scanner import scan
from mev.simulator.simulate import best, fee_params, gas_cost_usd, gas_price_wei, next_base_fee, simulate

log = logging.getLogger("mev")
WETH = "0x4200000000000000000000000000000000000006"

POOLS: list = []                    # the active set, loaded from the DB (see reload_pools)
_last_states: dict[str, PoolState] = {}
_fetched_now: set[str] = set()      # pools whose state was fetched (not carried forward) in the current block
_last_full_snapshot: int = 0
_rr: int = 0                        # round-robin cursor for tick-window refreshes
_behind: bool = False               # the loop had to skip blocks to reach this one: no optional RPC work
_head_at_fetch: int | None = None   # chain head read concurrently with the pool-state fetch (saves a round trip)
ENGINE: ExecutionEngine | None = None   # shadow/live execution; None in paper mode


def reload_pools() -> None:
    global POOLS
    POOLS = load_active_pools(settings.min_pool_tvl_usd)
    _last_states.clear()


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


async def fetch_logs_safe(w3, block_number: int) -> list[indexer.PoolEvent] | None:
    """None means the fetch failed (so every pool must be refetched); [] means no events."""
    if not settings.fetch_logs:
        return None
    try:
        return await indexer.fetch_events(w3, POOLS, block_number)
    except Exception as e:
        metrics.log_fetch_errors.inc()
        log.warning("block=%s eth_getLogs failed, continuing without events: %s", block_number, short_error(e))
        if rpc.is_throttle(e):
            rpc.rotate(w3)
        return None


def short_error(e: BaseException) -> str:
    status = getattr(e, "status", None)
    return f"{type(e).__name__}({status})" if status else f"{type(e).__name__}: {str(e)[:120]}"


async def fetch_all(w3, block_number: int, partial_ok: bool) -> tuple[dict, dict[str, PoolState], list[indexer.PoolEvent]]:
    """Header and the block's pool logs first, then the state of every pool that emitted an event (pools
    without one are carried forward from the previous block; everything is refetched when the log fetch
    failed, on the first block, or every full_refetch_every_blocks). A missing pool raises (so the block
    is retried) unless partial_ok."""
    global _rr
    block, events = await asyncio.gather(w3.eth.get_block(block_number), fetch_logs_safe(w3, block_number), return_exceptions=True)
    if isinstance(block, BaseException):
        raise block
    if isinstance(events, BaseException):
        events = None
    carried = {}
    full = (events is None or not _last_states or any(p.pool_id not in _last_states for p in POOLS)
            or _last_states_block() != block_number - 1)
    if full:
        targets = list(POOLS)
        refresh = None if not _last_states else set()   # first block: everything incl. ticks; later: prices only, ticks cached
    else:
        changed = {e.pool_id for e in events}
        # tick windows change only on Mint/Burn; refresh those plus a round-robin slice so every pool is rebuilt regularly
        n = 0 if _behind else settings.tick_refresh_pools_per_block   # optional work waits until we are caught up
        rr = [POOLS[(_rr + i) % len(POOLS)].pool_id for i in range(min(n, len(POOLS)))] if POOLS else []
        _rr = (_rr + n) % max(len(POOLS), 1)
        refresh = {e.pool_id for e in events if e.event_type in ("mint", "burn")} | set(rr)
        targets = [p for p in POOLS if p.pool_id in changed or p.pool_id in refresh]
        carried = pool_state.carry_forward({k: v for k, v in _last_states.items() if k not in changed and k not in refresh}, block_number)
    global _head_at_fetch
    head_task = asyncio.ensure_future(w3.eth.block_number)
    try:
        fetched = await pool_state.fetch_states(w3, targets, block_number, refresh) if targets else {}
    except Exception:
        metrics.rpc_errors.inc()
        head_task.cancel()
        raise
    try:
        _head_at_fetch = await head_task
    except Exception:
        _head_at_fetch = None
    states = {**carried, **fetched}
    for p in POOLS:
        if p.pool_id not in states:
            metrics.rpc_errors.inc()
            if not partial_ok:
                raise RuntimeError(f"block {block_number}: pool {p.pool_id} state call failed")
            log.warning("block=%s pool %s fetch failed, continuing without it", block_number, p.pool_id)
    events = events or []
    indexer.reconcile(states, events, POOLS, block_number)   # cross-check, and rebuild a missing x*y=k state from its Sync
    if not states:
        raise RuntimeError(f"block {block_number}: no pool state fetched")
    _last_states.clear()
    _last_states.update(states)
    _fetched_now.clear()
    _fetched_now.update(p.pool_id for p in targets)
    metrics.pools_refetched.observe(len(targets))
    return block, states, events


def _last_states_block() -> int | None:
    return next(iter(_last_states.values())).block_number if _last_states else None


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

    prices = usd_prices(states, adapters)
    eth_price = prices.get(WETH.lower())
    if eth_price is None:
        raise RuntimeError(f"block {block_number}: no WETH/USD reference price (reference pools missing)")
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
        # snapshots: pools whose state changed this block, plus everything every snapshot_all_every_blocks
        global _last_full_snapshot
        full_snap = block_number - _last_full_snapshot >= settings.snapshot_all_every_blocks or not _fetched_now
        snap_states = states if full_snap else {k: v for k, v in states.items() if k in _fetched_now}
        if full_snap:
            _last_full_snapshot = block_number
        db.add_all(snapshot_rows(snap_states, adapters))
        db.add_all(indexer.swap_rows(events, POOLS))
        db.commit()

    for p, r, realized in paper.resolve(block_number, ts, states, gas_usd_now, prices):
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
        # the head read during the state fetch tells the stale-state rule how old the states really are (in replay: the block itself)
        head_now = block_number if replay else (_head_at_fetch if _head_at_fetch is not None else await w3.eth.block_number)
        daily_loss, failed = paper.book(ts)
    ctx = features.BlockContext(block_number, ts, gas_usd, latency_ms, 0.0 if replay else time.time(), prices)
    sims = []
    for c in candidates:
        t2 = time.perf_counter()
        sims.append((c, simulate(c, states, adapters, gas_usd, settings, prices)))
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
            opp = Opportunity(block_number=block_number, head_block=head_now, pair=c.pair, buy_pool_id=c.buy_pool_id, sell_pool_id=c.sell_pool_id,
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
            opp_id = opp.id
        if not violations:
            opened[c.buy_pool_id] = opened[c.sell_pool_id] = b.trade_size_usd
            if ENGINE is not None and ENGINE.enabled and not replay:
                quote_key = states[c.buy_pool_id].pool.quote.key
                await ENGINE.submit(opp_id, c, b, head_block=head_now, block_ts=ts, gas_price_wei=gas_price_wei(next_fee, settings),
                                    eth_price=eth_price, quote_usd=prices.get(quote_key, Decimal(1)))

        metrics.simulations.inc(len(results))
        metrics.profitable_simulations.inc(sum(1 for r in results if r.net_usd > 0))
        for v in violations:
            metrics.risk_blocks.labels(v.split(" ")[0].rstrip(":")).inc()
        metrics.spread_bps.labels(c.buy_pool_id, c.sell_pool_id).set(float(c.spread_bps))
        log.info("block=%s %s %s -> %s spread=%.1fbps floor=%.1fbps best=$%s/%.2f chosen=$%s/%.2f %s",
                 block_number, c.pair, c.buy_pool_id, c.sell_pool_id, c.spread_bps, c.fee_floor_bps,
                 unconstrained.trade_size_usd, unconstrained.net_usd, b.trade_size_usd, b.net_usd,
                 "PAPER EXECUTE" if not violations else f"BLOCK ({'; '.join(violations)})")

    metrics.blocks_processed.inc()
    if block_number % 30 == 0 or candidates:
        top = max(candidates, key=lambda c: c.spread_bps - c.fee_floor_bps, default=None)
        log.info("block=%s ts=%s gas=$%.4f rpc=%.0fms pools=%d events=%d candidates=%d eth=%.2f%s", block_number, ts, gas_usd,
                 latency_ms, len(states), len(events), len(candidates), eth_price,
                 f" best={top.pair} +{top.spread_bps - top.fee_floor_bps:.1f}bps over floor" if top else "")


async def process_with_retries(w3, block_number: int, adapters, paper: PaperExecutor, *, resolve_only: bool = False, replay: bool = False) -> bool:
    """Retries transient failures (lagging replica 'block not found', 429, timeouts); partial pool data is
    accepted only on the last attempt. Returns False when the block was given up on."""
    for attempt in range(1, settings.block_retries + 1):
        try:
            await process_block(w3, block_number, adapters, paper, partial_ok=attempt == settings.block_retries, resolve_only=resolve_only, replay=replay)
            return True
        except Exception as e:
            if rpc.is_throttle(e) or rpc.is_throttle(getattr(e, "__cause__", None) or e):
                log.warning("block %s attempt %d/%d throttled by %s (%s)", block_number, attempt, settings.block_retries, rpc.redacted(rpc.current(w3)), short_error(e))
                rpc.rotate(w3)
            else:
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
        log.warning("head poll failed (%s), retrying in %.1fs", short_error(e), backoff)
        if rpc.is_throttle(e):
            rpc.rotate(w3)
        await asyncio.sleep(backoff)
        return None, min(backoff * 2, 30.0)


async def run(max_blocks: int | None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    init_db()
    reload_pools()
    if settings.metrics_port:
        start_http_server(settings.metrics_port)
    w3 = make_web3()
    adapters = {k: get_adapter(k) for k in ("uniswap_v3", "constant_product")}
    paper = PaperExecutor(settings, adapters)
    global ENGINE
    ENGINE = ExecutionEngine(settings, w3, load_signer(settings), lambda: {p.pool_id: p for p in POOLS})
    await ENGINE.startup()
    log.info("chain_id=%s rpc=%s (+%d fallbacks) pools=%d pairs=%d metrics=:%d execution=%s", await w3.eth.chain_id, rpc.redacted(rpc.current(w3)), len(rpc.endpoints()) - 1, len(POOLS),
             len({p.pair for p in POOLS}), settings.metrics_port, settings.execution_mode)

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
        global _behind
        _behind = False
        if settings.chase_head and head - last > 1:
            # Every backlog block would be stale by the time it is processed (one block costs several RPC round
            # trips), so go straight to the newest block and let the dataset record the gap.
            skipped = head - 1 - last
            metrics.blocks_skipped.inc(skipped)
            _behind = True
            log.info("behind by %d: jumping to head %s, skipping %s..%s", head - last, head, last + 1, head - 1)
            paper.note_gap()
            last = head - 1
        elif head - last > settings.max_catchup_blocks:
            skipped = head - settings.max_catchup_blocks - last
            metrics.blocks_skipped.inc(skipped)
            log.warning("gap: %d blocks behind head %s, skipping %s..%s", head - last, head, last + 1, head - settings.max_catchup_blocks)
            paper.note_gap()
            last = head - settings.max_catchup_blocks
        for b in range(last + 1, head + 1):
            await process_with_retries(w3, b, adapters, paper)
            last = b
            done += 1
            if b % settings.pool_reload_blocks == 0:
                n = len(POOLS)
                reload_pools()
                if len(POOLS) != n:
                    log.info("active pool set reloaded: %d -> %d pools", n, len(POOLS))
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
    if ENGINE is not None:
        await ENGINE.drain()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--blocks", type=int, default=None, help="process this many new blocks then exit")
    args = ap.parse_args()
    asyncio.run(run(args.blocks))


if __name__ == "__main__":
    main()
