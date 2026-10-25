"""Check the loop's stored simulation against the chain at the same block: fresh state through the exact
local math, and the Uniswap V3 legs through the on-chain QuoterV2."""
from decimal import Decimal

from web3 import AsyncWeb3

from mev.chain.addresses import QUOTERS
from mev.config import Settings
from mev.dex.base import DexAdapter
from mev.dex.uniswap_v3 import quote_onchain, quoter_tier
from mev.pools.prices import usd_prices
from mev.pools.state import fetch_states
from mev.db.session import load_active_pools
from mev.scanner.scanner import Candidate
from mev.simulator.simulate import simulate_size



async def verify(w3: AsyncWeb3, c: Candidate, size_usd: int, stored_net_usd: float, gas_usd: Decimal,
                 adapters: dict[str, DexAdapter], s: Settings) -> dict:
    by_id = {p.pool_id: p for p in load_active_pools()}
    states = await fetch_states(w3, list(by_id.values()), c.block_number)   # all pools: USD prices need the reference pools
    prices = usd_prices(states, adapters)
    r = simulate_size(c, states, adapters, size_usd, gas_usd, s, prices)
    out: dict = {"block_number": c.block_number, "size_usd": size_usd, "stored_net_usd": stored_net_usd,
                 "fresh_net_usd": float(r.net_usd), "exact": r.exact, "legs": {}}
    if c.route:
        # Routes: re-quote every concentrated-liquidity leg on its quoter, chaining the local outputs.
        amount = r.amount_in
        for i, pid in enumerate(c.route):
            pool = by_id[pid]
            t_in = pool.token0 if pool.token0.key == c.path[i] else pool.token1
            t_out = pool.token1 if t_in is pool.token0 else pool.token0
            local = adapters[pool.kind].quote_exact_in(states[pid], t_in.address, amount).amount_out
            if pool.kind == "uniswap_v3":
                chain = await quote_onchain(w3, QUOTERS[pool.variant], t_in.address, t_out.address, amount, quoter_tier(pool), c.block_number, pool.variant)
                out["legs"][f"leg{i}"] = {"pool": pid, "local_out": local, "quoter_out": chain, "match": chain == local}
            amount = local
        out["all_match"] = all(l["match"] for l in out["legs"].values()) and abs(float(r.net_usd) - stored_net_usd) < 0.01
        return out
    buy, sell = by_id[c.buy_pool_id], by_id[c.sell_pool_id]
    quote, base = buy.quote, buy.base
    if buy.kind == "uniswap_v3":
        chain = await quote_onchain(w3, QUOTERS[buy.variant], quote.address, base.address, r.amount_in, quoter_tier(buy), c.block_number, buy.variant)
        out["legs"]["buy"] = {"local_out": r.token0_out, "quoter_out": chain, "match": chain == r.token0_out}
    if sell.kind == "uniswap_v3":
        chain = await quote_onchain(w3, QUOTERS[sell.variant], base.address, quote.address, r.token0_out, quoter_tier(sell), c.block_number, sell.variant)
        out["legs"]["sell"] = {"local_out": r.amount_back, "quoter_out": chain, "match": chain == r.amount_back}
    out["all_match"] = all(l["match"] for l in out["legs"].values()) and abs(float(r.net_usd) - stored_net_usd) < 0.01
    return out
