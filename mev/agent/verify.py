"""Check the loop's stored simulation against the chain at the same block: fresh state through the exact
local math, and the Uniswap V3 legs through the on-chain QuoterV2."""
from decimal import Decimal

from web3 import AsyncWeb3

from mev.chain.addresses import POOLS, UNISWAP_V3_QUOTER_V2
from mev.config import Settings
from mev.dex.base import DexAdapter
from mev.dex.uniswap_v3 import quote_onchain
from mev.pools.state import fetch_states
from mev.scanner.scanner import Candidate
from mev.simulator.simulate import simulate_size

BY_ID = {p.pool_id: p for p in POOLS}


async def verify(w3: AsyncWeb3, c: Candidate, size_usd: int, stored_net_usd: float, gas_usd: Decimal,
                 adapters: dict[str, DexAdapter], s: Settings) -> dict:
    states = await fetch_states(w3, [BY_ID[c.buy_pool_id], BY_ID[c.sell_pool_id]], c.block_number)
    r = simulate_size(c, states, adapters, size_usd, gas_usd, s)
    buy, sell = BY_ID[c.buy_pool_id], BY_ID[c.sell_pool_id]
    out: dict = {"block_number": c.block_number, "size_usd": size_usd, "stored_net_usd": stored_net_usd,
                 "fresh_net_usd": float(r.net_usd), "exact": r.exact, "legs": {}}
    if buy.kind == "uniswap_v3":
        chain = await quote_onchain(w3, UNISWAP_V3_QUOTER_V2, buy.token1.address, buy.token0.address, r.amount_in, buy.fee_ppm, c.block_number)
        out["legs"]["buy"] = {"local_out": r.token0_out, "quoter_out": chain, "match": chain == r.token0_out}
    if sell.kind == "uniswap_v3":
        chain = await quote_onchain(w3, UNISWAP_V3_QUOTER_V2, sell.token0.address, sell.token1.address, r.token0_out, sell.fee_ppm, c.block_number)
        out["legs"]["sell"] = {"local_out": r.amount_back, "quoter_out": chain, "match": chain == r.amount_back}
    out["all_match"] = all(l["match"] for l in out["legs"].values()) and abs(float(r.net_usd) - stored_net_usd) < 0.01
    return out
