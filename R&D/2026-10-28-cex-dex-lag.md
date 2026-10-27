# CEX→DEX lag (non-atomic) arbitrage — 2026-10-28

## What the research says
- Cross-Rollup MEV (arXiv 2406.02172, CAAW 2026 version): >500k non-atomic arbitrage opportunities across Arbitrum,
  Base, Optimism, ZKsync; opportunity size ≈ 0.03–0.05% of trading volume on Arbitrum/Base/Optimism; opportunities
  persist ~10–20 blocks (seconds on an L2), i.e. they are not closed instantly.
- Princeton thesis on CEX-DEX gap survival: moving from L1 to Arbitrum cut the mean ETH CEX-DEX spread 70%
  (69 → 21 bps). Friction ranking: CEX fee structure > chain architecture > pool fee > block time > gas.
- "Darkest of the MEV dark forest" (arXiv 2507.13023): on Ethereum, 19 labeled searchers moved $241.7B of CEX-DEX
  volume Aug 2023–Mar 2025; profitability tracks access to exclusive order flow, not just speed.

## What it means for us
Our two-leg scanner is looking for DEX-DEX spreads that other bots have already closed against the CEX price; the
measured −$0.16 best round trip is the residue after they trade. The spread we are missing is DEX-vs-CEX: when
Binance moves 20 bps, Base pools lag for a few seconds and the 1–5 bp pools are briefly mispriced by more than their
fee. Capturing it is not atomic: buy on the DEX, hedge or sell on the CEX, carry inventory in between.

## What it takes
1. A CEX price feed (Binance/Coinbase WebSocket book ticker) alongside the per-block pool prices already stored.
2. Inventory on both sides (USDC + the asset on the CEX, and on Base) and a rebalance policy.
3. A signal: DEX mid vs CEX mid gap > pool fee + gas + expected slippage, filtered by gap age.
4. Backtest first with stored pool snapshots vs recorded CEX ticks (the swaps/snapshots tables keep 24 h now; the
   backtest needs the CEX tick history recorded in parallel for a week).

## Verdict
This is where the L2 spread actually lives, and it fits a "tiny steady profits" goal better than atomic arbitrage
because the counterparty is a slow DEX, not a faster bot. Cost: exchange account, capital on two venues, directional
risk measured in seconds. Next step if pursued: record CEX ticks + pool mids for 7 days and measure gap size and
survival on WETH/USDC and cbBTC/USDC before any execution work.

Sources: arxiv.org/pdf/2406.02172, caaw.io/2026/papers/CAAW26_paper_6.pdf, arxiv.org/pdf/2507.13023, Princeton
theses "Latency Arbitrage Across Centralized and Decentralized Exchanges".
