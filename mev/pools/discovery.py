"""Pool discovery: build and refresh the database of pools on Base (spec section 6 / pool indexer).

    python -m mev.pools.discovery            # allowlisted tokens x every factory x every tier (a few multicalls)
    python -m mev.pools.discovery --all      # also every pool of the Aerodrome factories (allPools) and PoolCreated
                                             # logs of the V3 forks from --from-block (resumable; stored in meta).
                                             # Heavy on public RPCs: run by hand, not from the CronJob.

Every pool gets token metadata, fee, tick spacing and a TVL from real token balances priced in USD through
pools that link each token to a dollar token. Rows are upserted by address; bootstrap rows keep their ids.
Pools with tvl >= settings.min_pool_tvl_usd are active and picked up by the loop within an hour."""
import argparse
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from eth_abi import decode
from sqlalchemy import select
from web3 import AsyncWeb3

from mev.chain import abi
from mev.chain.addresses import (AERODROME_CL_FACTORY, AERODROME_FACTORY, PANCAKE_V3_FACTORY, SUSHI_V3_FACTORY, TOKENS,
                                 UNISWAP_V2_FACTORY, UNISWAP_V3_FACTORY, CHAIN_ID)
from mev.config import settings
from mev.db.models import Meta, PoolRow, TokenRow
from mev.db.session import init_db, session
from mev.dex.uniswap_v3 import Q96
from mev.pools.registry import STABLE_SYMBOLS, Pool, Token
from mev.pools.state import multicall

log = logging.getLogger("mev.discovery")
cs = AsyncWeb3.to_checksum_address
ZERO = "0x" + "00" * 20


@dataclass(frozen=True)
class Factory:
    dex: str
    address: str
    kind: str          # uniswap_v3 | constant_product
    variant: str       # uniswap | pancake | slipstream | v2 | aerodrome
    tiers: tuple       # fee ppm (V3), tick spacings (Slipstream), stable flags (Aerodrome), or (None,) for V2
    enumerable: bool   # allPools()/allPairs() exist


FACTORIES = [
    Factory("uniswap_v3", UNISWAP_V3_FACTORY, "uniswap_v3", "uniswap", (100, 500, 3000, 10000), False),
    Factory("pancake_v3", PANCAKE_V3_FACTORY, "uniswap_v3", "pancake", (100, 500, 2500, 10000), False),
    Factory("sushi_v3", SUSHI_V3_FACTORY, "uniswap_v3", "uniswap", (100, 500, 3000, 10000), False),
    Factory("aerodrome_cl", AERODROME_CL_FACTORY, "uniswap_v3", "slipstream", (1, 50, 100, 200, 2000), True),
    Factory("aerodrome", AERODROME_FACTORY, "constant_product", "aerodrome", (False, True), True),
    Factory("uniswap_v2", UNISWAP_V2_FACTORY, "constant_product", "v2", (None,), False),   # 3M+ spam pairs: allowlist only
]

F_V3 = [{"name": "getPool", "type": "function", "stateMutability": "view", "inputs": [{"type": "address"}, {"type": "address"}, {"type": "uint24"}], "outputs": [{"type": "address"}]}]
F_CL = [{"name": "getPool", "type": "function", "stateMutability": "view", "inputs": [{"type": "address"}, {"type": "address"}, {"type": "int24"}], "outputs": [{"type": "address"}]},
        {"name": "allPoolsLength", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
        {"name": "allPools", "type": "function", "stateMutability": "view", "inputs": [{"type": "uint256"}], "outputs": [{"type": "address"}]}]
F_AERO = [{"name": "getPool", "type": "function", "stateMutability": "view", "inputs": [{"type": "address"}, {"type": "address"}, {"type": "bool"}], "outputs": [{"type": "address"}]},
          {"name": "allPoolsLength", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
          {"name": "allPools", "type": "function", "stateMutability": "view", "inputs": [{"type": "uint256"}], "outputs": [{"type": "address"}]},
          {"name": "getFee", "type": "function", "stateMutability": "view", "inputs": [{"type": "address"}, {"type": "bool"}], "outputs": [{"type": "uint256"}]}]
F_V2 = [{"name": "getPair", "type": "function", "stateMutability": "view", "inputs": [{"type": "address"}, {"type": "address"}], "outputs": [{"type": "address"}]},
        {"name": "allPairsLength", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
        {"name": "allPairs", "type": "function", "stateMutability": "view", "inputs": [{"type": "uint256"}], "outputs": [{"type": "address"}]}]
P_COMMON = [{"name": "token0", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "address"}]},
            {"name": "token1", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "address"}]},
            {"name": "fee", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint24"}]},
            {"name": "tickSpacing", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "int24"}]},
            {"name": "slot0", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint160"}, {"type": "int24"}]},
            {"name": "getReserves", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}, {"type": "uint256"}, {"type": "uint256"}]},
            {"name": "stable", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "bool"}]},
            {"name": "balanceOf", "type": "function", "stateMutability": "view", "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]},
            {"name": "symbol", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "string"}]},
            {"name": "decimals", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint8"}]}]
TOPIC_POOL_CREATED = "0x" + AsyncWeb3.keccak(text="PoolCreated(address,address,uint24,int24,address)").hex()


@dataclass
class Found:
    factory: Factory
    address: str
    tier: object = None   # what the factory lookup used (fee/tickSpacing/stable)


def _c(w3, addr, abi_):
    return w3.eth.contract(address=cs(addr), abi=abi_)


async def lookup_allowlist(w3: AsyncWeb3, block: int) -> list[Found]:
    """Every factory x every tier x every pair of allowlisted tokens, through Multicall3."""
    toks = list(TOKENS.values())
    calls, meta = [], []
    for f in FACTORIES:
        for i, a in enumerate(toks):
            for b in toks[i + 1:]:
                for tier in f.tiers:
                    if f.variant in ("uniswap", "pancake"):
                        data = _c(w3, f.address, F_V3).encode_abi("getPool", args=[cs(a.address), cs(b.address), tier])
                    elif f.variant == "slipstream":
                        data = _c(w3, f.address, F_CL).encode_abi("getPool", args=[cs(a.address), cs(b.address), tier])
                    elif f.variant == "aerodrome":
                        data = _c(w3, f.address, F_AERO).encode_abi("getPool", args=[cs(a.address), cs(b.address), tier])
                    else:
                        data = _c(w3, f.address, F_V2).encode_abi("getPair", args=[cs(a.address), cs(b.address)])
                    calls.append((cs(f.address), data))
                    meta.append((f, tier))
    raw = await multicall(w3, calls, block)
    found = []
    for (f, tier), r in zip(meta, raw):
        if r is None:
            continue
        addr = decode(["address"], r)[0]
        if addr.lower() != ZERO:
            found.append(Found(f, addr, tier))
    return found


async def enumerate_all(w3: AsyncWeb3, block: int) -> list[Found]:
    """allPools()/allPairs() of the enumerable factories, in multicall batches."""
    found = []
    for f in FACTORIES:
        if not f.enumerable:
            continue
        fabi = F_CL if f.variant == "slipstream" else F_AERO if f.variant == "aerodrome" else F_V2
        length_fn, item_fn = ("allPairsLength", "allPairs") if f.variant == "v2" else ("allPoolsLength", "allPools")
        c = _c(w3, f.address, fabi)
        n = await getattr(c.functions, length_fn)().call(block_identifier=block)
        log.info("%s: %d pools", f.dex, n)
        for start in range(0, n, 2000):
            idx = list(range(start, min(start + 2000, n)))
            raw = await multicall(w3, [(cs(f.address), c.encode_abi(item_fn, args=[i])) for i in idx], block)
            found += [Found(f, decode(["address"], r)[0]) for r in raw if r is not None]
    return found


async def scan_pool_created(w3: AsyncWeb3, from_block: int, to_block: int, step: int = 20_000) -> list[Found]:
    """PoolCreated logs of the V3 forks (Uniswap, PancakeSwap, Sushi share the event signature)."""
    by_factory = {f.address.lower(): f for f in FACTORIES if f.variant in ("uniswap", "pancake")}
    found = []
    for a in range(from_block, to_block + 1, step):
        b = min(a + step - 1, to_block)
        logs = await w3.eth.get_logs({"fromBlock": a, "toBlock": b, "address": [cs(x) for x in by_factory], "topics": [TOPIC_POOL_CREATED]})
        for lg in logs:
            pool = decode(["int24", "address"], bytes(lg["data"]))[1]
            found.append(Found(by_factory[lg["address"].lower()], pool, int.from_bytes(bytes(lg["topics"][3])[-3:], "big")))
        with session() as db:
            db.merge(Meta(key="discovery_v3_scanned_to", value=str(b)))
            db.commit()
        log.info("PoolCreated scan %d..%d: %d pools so far", a, b, len(found))
    return found


async def describe(w3: AsyncWeb3, found: list[Found], block: int) -> list[dict]:
    """token0/token1, fee, tick spacing, price and balances for every pool, plus token metadata (cached in tokens)."""
    uniq = {}
    for f in found:
        uniq.setdefault(f.address.lower(), f)
    found = list(uniq.values())
    calls = []
    for f in found:
        c = _c(w3, f.address, P_COMMON)
        calls += [(cs(f.address), c.encode_abi("token0", args=[])), (cs(f.address), c.encode_abi("token1", args=[]))]
        if f.factory.kind == "uniswap_v3":
            calls += [(cs(f.address), c.encode_abi("fee", args=[])), (cs(f.address), c.encode_abi("tickSpacing", args=[])), (cs(f.address), c.encode_abi("slot0", args=[]))]
        else:
            calls += [(cs(f.address), c.encode_abi("getReserves", args=[])), (cs(f.address), c.encode_abi("stable", args=[]))]
    raw = await multicall(w3, calls, block)
    pools, i = [], 0
    for f in found:
        t0, t1 = raw[i], raw[i + 1]
        if t0 is None or t1 is None:
            i += 5 if f.factory.kind == "uniswap_v3" else 4
            continue
        d = {"found": f, "token0": decode(["address"], t0)[0].lower(), "token1": decode(["address"], t1)[0].lower()}
        if f.factory.kind == "uniswap_v3":
            fee, ts, s0 = raw[i + 2], raw[i + 3], raw[i + 4]
            i += 5
            if fee is None or ts is None or s0 is None:
                continue
            d.update(fee_ppm=decode(["uint24"], fee)[0], tick_spacing=decode(["int24"], ts)[0], sqrt_price_x96=decode(["uint160", "int24"], s0)[0])
        else:
            res, stable = raw[i + 2], raw[i + 3]
            i += 4
            if res is None:
                continue
            r0, r1, _ = decode(["uint256", "uint256", "uint256"], res)
            is_stable = bool(decode(["bool"], stable)[0]) if stable else False
            if is_stable:
                continue   # Aerodrome stable pools use a different curve; not simulated
            d.update(reserve0=r0, reserve1=r1, fee_ppm=3000, tick_spacing=None)
        pools.append(d)
    # Aerodrome volatile fee from the factory (per pool)
    aero = [d for d in pools if d["found"].factory.variant == "aerodrome"]
    if aero:
        fc = _c(w3, AERODROME_FACTORY, F_AERO)
        raw = await multicall(w3, [(cs(AERODROME_FACTORY), fc.encode_abi("getFee", args=[cs(d["found"].address), False])) for d in aero], block)
        for d, r in zip(aero, raw):
            if r is not None:
                d["fee_ppm"] = decode(["uint256"], r)[0] * 100   # bps -> ppm
    # token metadata
    addrs = sorted({d["token0"] for d in pools} | {d["token1"] for d in pools})
    tokens = await token_metadata(w3, addrs, block)
    # balances
    calls = []
    for d in pools:
        for t in (d["token0"], d["token1"]):
            calls.append((cs(t), _c(w3, t, P_COMMON).encode_abi("balanceOf", args=[cs(d["found"].address)])))
    raw = await multicall(w3, calls, block)
    for k, d in enumerate(pools):
        b0, b1 = raw[2 * k], raw[2 * k + 1]
        d["balance0"] = decode(["uint256"], b0)[0] if b0 else 0
        d["balance1"] = decode(["uint256"], b1)[0] if b1 else 0
        d["tok0"], d["tok1"] = tokens.get(d["token0"]), tokens.get(d["token1"])
    return [d for d in pools if d["tok0"] and d["tok1"]]


async def token_metadata(w3: AsyncWeb3, addrs: list[str], block: int) -> dict[str, Token]:
    out: dict[str, Token] = {}
    with session() as db:
        for r in db.scalars(select(TokenRow).where(TokenRow.address.in_(addrs))):
            out[r.address] = Token(r.address, r.symbol, r.decimals)
    for k, t in TOKENS.items():
        out[k] = t
    missing = [a for a in addrs if a not in out]
    if missing:
        calls = []
        for a in missing:
            c = _c(w3, a, P_COMMON)
            calls += [(cs(a), c.encode_abi("symbol", args=[])), (cs(a), c.encode_abi("decimals", args=[]))]
        raw = await multicall(w3, calls, block)
        with session() as db:
            for j, a in enumerate(missing):
                sym, dec = raw[2 * j], raw[2 * j + 1]
                if sym is None or dec is None:
                    continue
                try:
                    symbol = decode(["string"], sym)[0] if len(sym) > 32 else sym.rstrip(b"\\x00").decode(errors="ignore")
                    decimals = decode(["uint8"], dec)[0]
                except Exception:
                    continue
                symbol = symbol[:16] or a[:8]
                out[a] = Token(a, symbol, decimals)
                db.merge(TokenRow(address=a, symbol=symbol, decimals=decimals, chain_id=CHAIN_ID))
            db.commit()
    return out


def price_tokens(pools: list[dict]) -> dict[str, Decimal]:
    """USD per token via spot prices, hopping from dollar tokens through the largest pools first."""
    prices: dict[str, Decimal] = {}
    for d in pools:
        for t in (d["tok0"], d["tok1"]):
            if t.symbol in STABLE_SYMBOLS:
                prices[t.key] = Decimal(1)

    def spot(d) -> Decimal | None:   # token1 per token0, decimal-adjusted
        t0, t1 = d["tok0"], d["tok1"]
        if "sqrt_price_x96" in d and d["sqrt_price_x96"]:
            sp = Decimal(d["sqrt_price_x96"]) / Decimal(Q96)
            return sp * sp * Decimal(10) ** (t0.decimals - t1.decimals)
        if d.get("reserve0") and d.get("reserve1"):
            return Decimal(d["reserve1"]) / Decimal(d["reserve0"]) * Decimal(10) ** (t0.decimals - t1.decimals)
        return None

    def size(d) -> int:   # raw balance product as a rough size for ordering
        return d["balance0"] * d["balance1"]

    ordered = sorted(pools, key=size, reverse=True)
    for _ in range(4):
        changed = False
        for d in ordered:
            p = spot(d)
            if not p or p <= 0:
                continue
            k0, k1 = d["tok0"].key, d["tok1"].key
            if k0 in prices and k1 not in prices and size(d) > 0:
                prices[k1] = prices[k0] / p
                changed = True
            elif k1 in prices and k0 not in prices and size(d) > 0:
                prices[k0] = prices[k1] * p
                changed = True
        if not changed:
            break
    return prices


def upsert(pools: list[dict], prices: dict[str, Decimal]) -> tuple[int, int]:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    added = active = 0
    with session() as db:
        by_addr = {r.address.lower(): r for r in db.scalars(select(PoolRow))}
        for d in pools:
            f, t0, t1 = d["found"], d["tok0"], d["tok1"]
            p0, p1 = prices.get(t0.key), prices.get(t1.key)
            tvl = None
            if p0 is not None and p1 is not None:
                tvl = float(Decimal(d["balance0"]) / 10**t0.decimals * p0 + Decimal(d["balance1"]) / 10**t1.decimals * p1)
            pool = Pool("x", f.factory.dex, f.factory.kind, f.address, t0, t1, d["fee_ppm"], d.get("tick_spacing"), f.factory.variant)
            row = by_addr.get(f.address.lower())
            if row is None:
                tier = d.get("tick_spacing") if f.factory.variant == "slipstream" else d["fee_ppm"]
                pid = f"{f.factory.dex}_{t0.symbol}_{t1.symbol}_{tier}_{f.address[2:8]}".lower()
                row = PoolRow(pool_id=pid, chain_id=CHAIN_ID, dex=f.factory.dex, kind=f.factory.kind, address=f.address,
                              token0_address=t0.address, token0_symbol=t0.symbol, token0_decimals=t0.decimals,
                              token1_address=t1.address, token1_symbol=t1.symbol, token1_decimals=t1.decimals,
                              fee_ppm=d["fee_ppm"], tick_spacing=d.get("tick_spacing"), variant=f.factory.variant, source="discovery")
                db.add(row)
                by_addr[f.address.lower()] = row
                added += 1
            row.fee_ppm, row.tick_spacing, row.pair, row.tvl_usd, row.updated_at = d["fee_ppm"], d.get("tick_spacing"), pool.pair, tvl, now
            row.active = tvl is not None and tvl >= settings.min_pool_tvl_usd   # bootstrap rows too (2026-10-28: thin bootstrap pools were the losing routes)
            active += bool(row.active)
        db.commit()
    return added, active


async def run(all_pools: bool, from_block: int | None) -> None:
    from mev.chain.rpc import make_web3
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    init_db()
    w3 = make_web3()
    block = await w3.eth.block_number - 2
    found = await lookup_allowlist(w3, block)
    log.info("allowlist lookup: %d pools at block %d", len(found), block)
    if all_pools:
        found += await enumerate_all(w3, block)
        start = from_block
        if start is None:
            with session() as db:
                m = db.get(Meta, "discovery_v3_scanned_to")
            start = int(m.value) + 1 if m else block - 200_000   # default: the last ~5 days; pass --from-block for history
        if start <= block:
            found += await scan_pool_created(w3, start, block)
    pools = await describe(w3, found, block)
    prices = price_tokens(pools)
    added, active = upsert(pools, prices)
    log.info("described %d pools, priced %d tokens, added %d new rows, %d active (tvl >= $%s)", len(pools), len(prices), added, active, settings.min_pool_tvl_usd)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="enumerate every pool of the enumerable factories and scan PoolCreated logs")
    ap.add_argument("--from-block", type=int, default=None, help="start of the PoolCreated scan (default: resume, else last ~5 days)")
    a = ap.parse_args()
    asyncio.run(run(a.all, a.from_block))


if __name__ == "__main__":
    main()
