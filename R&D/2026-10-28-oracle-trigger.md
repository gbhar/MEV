# Oracle-update triggered actions on Base — 2026-10-28

## What Aave on Base prices with
AaveOracle 0x2Cc0Fc26eD4563A5ce5e8bdcfe1A2878676Ae156. Sources: cbBTC → Chainlink "BTC / USD"
0x3A932b286715abc4A86a4ACAF68A6cdD89E0d446 (aggregator 0x1372…7b1B); WETH → "ETH / USD" 0x9dA00D23465282005DB222a441a663eE7B9dfCc8
(aggregator 0xD772…7BF9); USDC, cbETH, wstETH → capped adapters (CAPO) on top of Chainlink.

## Measured cadence, 24 h to block ~53171650
| feed | updates / 24 h | median move per update | smallest move |
|---|---|---|---|
| BTC/USD | 164 | 11.0 bps | 1.5 bps |
| ETH/USD | 150 | 16.6 bps | 0.1 bps |

So a feed moves roughly every 9 minutes on average, driven by the deviation threshold (ETH/USD on Base is listed at
0.15%; BTC/USD looks like ~0.1% from the data), with heartbeat fallbacks when quiet. Health factors only change at
these updates (and on interest accrual), which is why liquidations cluster on update blocks.

## Base ordering facts that matter
- No public mempool: the sequencer holds a private queue; nothing can be seen before inclusion.
- Flashblocks: 200 ms sub-blocks; within a Flashblock the builder selects by fee, committed order is never reordered.
  A later high-fee transaction lands in the next Flashblock, not ahead. `eth_call` against pre-confirmed state is
  available up to ~1.8 s before the block seals (Base RPC docs).
- Practical consequence: the only way to "trigger on the oracle" is to be the first transaction after the
  `AnswerUpdated` log, i.e. subscribe to Flashblocks, keep the at-risk position list warm, and submit within the same
  200 ms window. From Ashburn over Alchemy (p50 140–500 ms) that window is already gone.

## Verdict
The trigger is cheap to build (the loop already processes every block and can watch two aggregators), but it has no
value without a liquidation or arbitrage edge to fire. Given liquidations.md, park it. If revisited: Flashblocks
WebSocket + a co-located node, not polling.

Sources: docs.base.org/specifications/flashblocks, docs.base.org RPC overview, data.chain.link Base feeds (ETH/USD
0.15% deviation), on-chain scan of AnswerUpdated events.
