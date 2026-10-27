from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from mev.chain.addresses import CHAIN_ID, POOLS
from mev.config import settings
from mev.db.models import Base, PoolRow
from mev.pools.registry import Pool

engine = create_engine(settings.database_url, future=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def init_db() -> None:
    """Create tables, add columns/indexes missing from an older mev.db, and seed the pools table."""
    Base.metadata.create_all(engine)
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in have:
                    conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(engine.dialect)}"))
            if table.name == "opportunities":   # 2026-10-25: uniqueness now includes the route (triangular candidates)
                conn.execute(text("DROP INDEX IF EXISTS ux_opportunities_block_pair"))
            for ix in table.indexes:
                ix.create(conn, checkfirst=True)
    with session() as db:
        for p in POOLS:
            row = db.get(PoolRow, p.pool_id)
            if row is None:
                row = PoolRow(pool_id=p.pool_id, active=True, source="bootstrap")
                db.add(row)
            row.chain_id, row.dex, row.kind, row.address, row.variant, row.pair = CHAIN_ID, p.dex, p.kind, p.address, p.variant, p.pair
            row.token0_address, row.token0_symbol, row.token0_decimals = p.token0.address, p.token0.symbol, p.token0.decimals
            row.token1_address, row.token1_symbol, row.token1_decimals = p.token1.address, p.token1.symbol, p.token1.decimals
            if row.updated_at is None:   # discovery (make discover) refreshes fees from the factories; never undo that
                row.fee_ppm, row.tick_spacing = p.fee_ppm, p.tick_spacing
            if row.source is None:
                row.source = "bootstrap"
            if row.active is None:   # rows created before the column existed
                row.active = True
        db.commit()


def load_active_pools(min_tvl_usd: float | None = None) -> list[Pool]:
    """The pool set the loop watches: active rows with tvl >= min_tvl_usd (rows discovery has not valued yet, i.e.
    tvl NULL, are included). Falls back to the bootstrap list if the table is empty."""
    from mev.pools.registry import Token
    with session() as db:
        rows = db.query(PoolRow).filter(PoolRow.active.is_(True)).all()
    pools = []
    for r in rows:
        if min_tvl_usd is not None and r.tvl_usd is not None and r.tvl_usd < min_tvl_usd:
            continue
        pools.append(Pool(r.pool_id, r.dex, r.kind, r.address, Token(r.token0_address, r.token0_symbol, r.token0_decimals),
                          Token(r.token1_address, r.token1_symbol, r.token1_decimals), r.fee_ppm, r.tick_spacing, r.variant or "uniswap"))
    return pools or list(POOLS)


def session() -> Session:
    return SessionLocal()
