# Proposed build: pool expansion (2026-10-24)

## Why

After the first hours of cluster data on four WETH/USDC pools, the largest spread was 31.7 bps against a
35 bps fee floor, and the average gap sat in a 14 bps dead band that nobody can close profitably at
5 + 30 bps fees. A factory probe found 224 pools on 8 Base factories; 60 hold more than $100k of depth,
and several of the deepest charge 1 to 1.5 bps (PancakeSwap V3, Uniswap V3 0.01%, Aerodrome Slipstream).
Two 1 bp venues put the floor at 2 bps. Today's gaps would have cleared that dozens of times.

## What

1. **Pool database.** Enumerate every pool from each factory (`PoolCreated` logs for the V3 forks,
   `allPools` for Aerodrome and V2 forks), store them in `pools` with real TVL from token balances,
   refresh daily (Kubernetes CronJob), and let the loop watch everything above `min_pool_tvl_usd`.
2. **Pair generalization.** The quote (borrowed) token may be token0 or token1; non-dollar quotes
   (WETH, cbBTC) are priced in USD through the deepest reference pools each block; sizes stay in USD.
3. **Fork variants.** PancakeSwap V3 (7-word `slot0`) and Aerodrome Slipstream (6-word `slot0`,
   per-pool dynamic fee) through the existing verified tick-crossing math, checked against each fork's
   QuoterV2.
4. **All-combination scanner.** Every pool of a pair against every other, across DEXs, and a bootstrap
   set of 47 pools over 13 pairs.
5. **Cheaper per-block fetch.** Chunked Multicall3, at most 64 ticks per pool, and pools without an
   event in the block carried forward (full refresh every 50 blocks).

## Not in this build

- Aave liquidations (next: a health-factor watcher and paper liquidator as a second strategy).
- Triangular / multi-hop routes.
- Any signing or broadcasting; the namespace keeps no wallet secret (spec section 28).

## Acceptance

- Unit tests pass; PancakeSwap and Slipstream quotes match their quoters to the wei within the window.
- The loop keeps up with 2 s blocks on the cluster with 47 pools.
- Replay of a block range stays reproducible.
- `GET /opportunities` shows candidates with `pair` and the chosen size; first real opportunities expected
  on the 1 bp WETH/USDC and stable pairs within days.
