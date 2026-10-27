# Other chains — 2026-10-28

## What could be measured without a paid RPC
- Ethereum mainnet Aave V3, last 24 h (publicnode, non-archive window): 3 liquidations, 2 liquidators, all dust
  (median debt $9). A quiet-market day; the Aave blog says mainnet averages >$50k per liquidation over its history
  and holds 75% of liquidation volume, so this sample says nothing about crash weeks.
- Arbitrum and Optimism: the free endpoints (publicnode, dRPC, Alchemy free tier) refuse log ranges older than a few
  hours or wider than 10 blocks. A 7-day scan like the Base one needs a PAYG key (Alchemy PAYG or dRPC paid; a few
  dollars). Not done yet.

## What the sources say
- Aave (Feb 2026): non-Ethereum networks are 80% of liquidation events but small (avg $4k–$7k); Polygon alone is 46%
  of all Aave liquidation events. Polygon is therefore the busiest "small liquidation" venue by count.
- Solana: liquidations and arbitrage go through Jito bundles (≤5 transactions, atomic, tip auction); Jito runs on
  most of the stake. Kamino and marginfi pay ~5% bonus and are described as crowded keeper markets; a 2026 builder
  reports losing ~60% of races until using a Jito staked connection and targeting smaller protocols. Cumulative
  searcher profit via Jito is quoted at >$480M (vendor figure). The MEV/launch Solana tooling is reusable, but this
  is a latency race with validators co-location, not a free-tier VM job.
- Arbitrum: in the Cross-Rollup MEV paper it has the same 0.03–0.05%-of-volume non-atomic opportunity rate as Base;
  deeper liquidity on some pairs, 250 ms blocks, and a public-ish sequencer feed (no mempool either).

## Verdict
No chain changes the atomic-arbitrage conclusion; every one with volume has resident bots faster than us. The two
things worth a measured look, in order: (1) Polygon and Arbitrum Aave liquidation concentration over 7 days with a
paid RPC (if one bot does not own >70% of events, there is a seat at the table); (2) CEX→DEX lag on Arbitrum vs Base,
same recording approach as cex-dex-lag.md. Solana is out unless the owner wants to fund co-located infra.

Sources: aave.com/blog/historical-liquidations, chainstack.com "Jito explained", rpcfast.com Solana liquidation bot
guide, arxiv.org/pdf/2406.02172, on-chain scans (scripts in session scratchpad).
