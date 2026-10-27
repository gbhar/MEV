# Delta-neutral yield (funding / basis, hedged LP) — 2026-10-28

## Funding today
BTC perpetual funding is positive on every tracked venue (longs pay shorts): Hyperliquid ≈ 0.0013%/h ≈ 11%/yr,
Binance ≈ 0.0081%/8h ≈ 8.9%/yr; cross-venue averages quoted at 9.6–13.3%/yr in mid/late October 2026. Binance's
fixed interest floor is 0.01%/8h ≈ 11% simple. These flip negative in sell-offs; the 2025 average was far lower.

## Two shapes
1. **Basis trade:** hold spot BTC (or cbBTC on Base), short the same notional on a perp. Earns funding, no BTC
   direction. Risks: funding goes negative, exchange/venue risk, margin top-ups on the short in a rally.
2. **Hedged LP:** the yield track's cbBTC/USDC LP position plus a short perp equal to the LP's BTC delta. Earns pool
   fees (measured 12–19% on depth, see hand-off 2026-10-28 §5) minus funding paid/earned, with impermanent loss as
   the remaining risk. Delta drifts as price moves; rebalance when the LP delta is >10% off the hedge.

## Numbers on $10k (90 days)
| shape | income | cost | net before IL |
|---|---|---|---|
| basis at 10% funding | $250 | trading fees ~$20, nothing else | ≈ $230 |
| hedged LP at 15% fee APR, hedge 50% notional, funding earned on the short | $375 + $62 | rebalance gas, IL ~$50–100 | ≈ $350–400 |
Compare the unhedged loop from the hand-off: +$72 to +$368 at 0% BTC move, −$350 to −$830 at −20%/−40%.

## Venues
Perp on Base: none deep enough; use Hyperliquid (hourly funding, on-chain) or a CEX. Spot/LP stays on Base.

## Verdict
The right home for the capital-based track: it keeps the carry the owner wanted and deletes the BTC direction that
produced every losing row. Next step: the same read-only monitor planned for the yield track, extended with the perp
funding rate and the hedge ratio; paper for 2–4 weeks.

Sources: defirate.com/perp, convextrade.com/metrics/btc-funding, app.hyperliquid.xyz/fundingComparison.
