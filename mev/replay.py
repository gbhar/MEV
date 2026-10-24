"""Historical replay (spec section 30): run the exact live pipeline over a past block range into a
separate database. State comes from eth_call pinned to each block, so the range must be within what the
RPC serves (the public endpoint keeps recent state; an archive node is needed for old ranges).

    python -m mev.replay --from-block A --to-block B --database-url sqlite:///replay.db

Replaying the same range twice yields identical opportunity/simulation/paper rows (see tests)."""
import argparse
import asyncio
import logging
import os
import sys


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-block", type=int, required=True)
    ap.add_argument("--to-block", type=int, required=True)
    ap.add_argument("--database-url", required=True, help="separate DB; refuses the live default")
    ap.add_argument("--metrics-port", type=int, default=0, help="0 disables the metrics server")
    args = ap.parse_args(argv)
    if args.database_url.rstrip("/") in ("sqlite:///mev.db", os.environ.get("MEV_DATABASE_URL", "")):
        sys.exit("refusing to replay into the live database")
    os.environ["MEV_DATABASE_URL"] = args.database_url
    os.environ["MEV_METRICS_PORT"] = str(args.metrics_port)
    asyncio.run(_run(args.from_block, args.to_block))


async def _run(from_block: int, to_block: int) -> None:
    from mev.chain.addresses import POOLS
    from mev.chain.rpc import make_web3
    from mev.db.session import init_db
    from mev.dex.base import get_adapter
    from mev.main import process_with_retries
    from mev.paper.executor import PaperExecutor

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    init_db()
    w3 = make_web3()
    adapters = {k: get_adapter(k) for k in {p.kind for p in POOLS}}
    paper = PaperExecutor(__import__("mev.config", fromlist=["settings"]).settings, adapters)
    for b in range(from_block, to_block + 1):
        await process_with_retries(w3, b, adapters, paper, replay=True)
    if paper.pending:
        await process_with_retries(w3, to_block + 1, adapters, paper, resolve_only=True, replay=True)


if __name__ == "__main__":
    main()
