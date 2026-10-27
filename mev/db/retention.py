"""Retention: keep rolling windows of stored data and delete the rest (owner's decision of 2026-10-28).

Two windows:
  raw per-block state (pool_snapshots, swaps, blocks)        settings.retention_hours          24 h
  decisions (opportunities, simulations, features)           settings.retention_decision_hours 7 days
Decisions whose best net was ever >= 0, and rows an execution, paper trade or action points at, are kept whatever
their age so the interesting events and the P&L record survive; pools, tokens and meta are never touched.

    python -m mev.db.retention                              # settings defaults
    python -m mev.db.retention --hours 48 --decision-hours 720 --vacuum

Deletes run in batches of --batch rows per transaction so the loop's own writes interleave instead of waiting on
one long lock. --vacuum returns the freed pages to the filesystem (SQLite only; Postgres autovacuums)."""
import argparse
import logging
import time

from sqlalchemy import text

from mev.config import settings
from mev.db.session import engine

log = logging.getLogger("mev.retention")

KEPT = ("select opportunity_id from executions union select opportunity_id from paper_trades "
        "union select opportunity_id from actions")
STALE_OPPS = f"select id from opportunities where block_number <= :bd and best_net_usd < 0 and id not in ({KEPT})"
# (table, primary key, condition) — children before parents; :bd = decision cutoff block, :br = raw cutoff block
STEPS = [
    ("features", "id", f"opportunity_id in ({STALE_OPPS})"),
    ("simulations", "id", f"opportunity_id in ({STALE_OPPS})"),
    ("opportunities", "id", f"id in ({STALE_OPPS})"),
    ("pool_snapshots", "id", "block_number <= :br"),
    ("swaps", "id", "block_number <= :br"),
    ("blocks", "block_number", "block_number <= :br and block_number not in (select block_number from opportunities)"),
]


def cutoff_block(hours: float, now: float | None = None) -> int | None:
    """The newest stored block older than `hours`; None when nothing is that old yet."""
    cutoff_ts = int((now or time.time()) - hours * 3600)
    with engine.begin() as conn:
        return conn.execute(text("select max(block_number) from blocks where timestamp < :t"), {"t": cutoff_ts}).scalar()


def purge(hours: float | None = None, decision_hours: float | None = None, batch: int = 20_000, vacuum: bool = False,
          now: float | None = None) -> dict[str, int]:
    """Delete raw state older than `hours` and losing decisions older than `decision_hours`. Rows deleted per table."""
    hours = settings.retention_hours if hours is None else hours
    decision_hours = settings.retention_decision_hours if decision_hours is None else decision_hours
    br, bd = cutoff_block(hours, now), cutoff_block(decision_hours, now)
    params = {"br": br if br is not None else -1, "bd": bd if bd is not None else -1}
    if br is None and bd is None:
        log.info("nothing older than %s h", min(hours, decision_hours))
        return {}
    deleted: dict[str, int] = {}
    for table, pk, cond in STEPS:
        n = 0
        while True:
            with engine.begin() as conn:
                r = conn.execute(text(f"delete from {table} where {pk} in (select {pk} from {table} where {cond} limit :n)"), {**params, "n": batch})
            n += r.rowcount
            if r.rowcount < batch:
                break
        deleted[table] = n
    log.info("purged raw state at blocks <= %s (older than %s h), losing decisions at blocks <= %s (older than %s h): %s",
             br, hours, bd, decision_hours, deleted)
    if vacuum and engine.dialect.name == "sqlite":
        t0 = time.monotonic()
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text("VACUUM"))
        log.info("vacuum done in %.1f s", time.monotonic() - t0)
    return deleted


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--hours", type=float, default=settings.retention_hours, help="raw per-block state window")
    ap.add_argument("--decision-hours", type=float, default=settings.retention_decision_hours, help="opportunities/simulations/features window")
    ap.add_argument("--batch", type=int, default=20_000)
    ap.add_argument("--vacuum", action="store_true", help="reclaim file space afterwards (SQLite)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    purge(a.hours, a.decision_hours, a.batch, a.vacuum)


if __name__ == "__main__":
    main()
