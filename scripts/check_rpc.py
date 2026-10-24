"""Connectivity smoke test: prints chain id, head block, and the live price on each V1 pool."""
import asyncio

from mev.chain.addresses import POOLS
from mev.chain.rpc import make_web3
from mev.dex.base import get_adapter
from mev.pools.state import fetch_state


async def main() -> None:
    w3 = make_web3()
    head = await w3.eth.block_number
    print("chain_id", await w3.eth.chain_id, "head", head)
    for p in POOLS:
        st = await fetch_state(w3, p, head)
        print(f"{p.pool_id:22s} {p.dex:12s} fee={p.fee_bps:>4.1f}bps price={get_adapter(p.kind).marginal_price(st):.2f} {p.pair}")


asyncio.run(main())
