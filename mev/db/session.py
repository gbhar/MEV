from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from mev.chain.addresses import CHAIN_ID, POOLS
from mev.config import settings
from mev.db.models import Base, PoolRow

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
            for ix in table.indexes:
                ix.create(conn, checkfirst=True)
    with session() as db:
        for p in POOLS:
            db.merge(PoolRow(pool_id=p.pool_id, chain_id=CHAIN_ID, dex=p.dex, kind=p.kind, address=p.address,
                             token0_address=p.token0.address, token0_symbol=p.token0.symbol, token0_decimals=p.token0.decimals,
                             token1_address=p.token1.address, token1_symbol=p.token1.symbol, token1_decimals=p.token1.decimals,
                             fee_ppm=p.fee_ppm, tick_spacing=p.tick_spacing))
        db.commit()


def session() -> Session:
    return SessionLocal()
