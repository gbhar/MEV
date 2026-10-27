# Liquidations on Aave V3 and Morpho Blue — 2026-10-28

## Mechanics
- Aave v3.3: close factor 50% of the whole position; 100% allowed when the position's debt or collateral is below
  `MIN_BASE_MAX_CLOSE_FACTOR_THRESHOLD` (≈ $1,000 in the governance example) and the remainder must be 0 or ≥
  `MIN_LEFTOVER_BASE` (≈ $500). Both are deployment constants; verify on the Base contracts before relying on them.
  Base bonuses today (read from the Pool): cbBTC 7.5%, USDC 5%, WETH 5%; liquidation thresholds 78% / 78% / 83%.
- Morpho Blue: liquidation incentive factor LIF = min(1.15, 1/(0.3·LLTV + 0.7)); at LLTV 86% ≈ 1.044 (4.4% bonus),
  all of it to the liquidator, no protocol fee. Pre-liquidation contracts let borrowers opt into partial liquidation
  at a lower LTV with a smaller bonus; less competition, less payout, only on opted-in positions.

## Measured: Aave V3 on Base, 7 days to block 53171654
| | |
|---|---|
| liquidation events | 99 |
| distinct liquidators | 10; one address did 81 (82%), next 8 and 3 |
| debt repaid per event | median $0, p90 $70, max $94k; 87 of 99 under $500, 91 under $2k, 3 ≥ $10k |
| gross liquidator take (collateral − debt) | ≈ $8.1k for the whole week, all liquidators |
| pairs | WETH/USDC 42 ($110k debt total), USDC/USDbC 15, USDC/cbBTC 14, cbBTC/USDC 3 ($22k) |

Reading: Base Aave liquidations are mostly v3.3 dust clean-ups of zombie positions, already swept by one dominant bot.
The entire week's bonus pool is about $8k, and the three real events (≥ $10k) are the contested ones. "Small positions
that big bots ignore" does not exist here: the small positions are the dominant bot's main diet.

## Verdict
Not a niche on Base at current activity. It only pays in a crash week (Aave's own data: liquidations cluster in
volatility), and then the race is decided by who lands first in the Flashblock after the oracle update (see
oracle-trigger.md). Keep the FlashArb contract's lender plumbing; do not build a liquidation engine for Base alone.
What would change the verdict: a volatility regime (then re-measure), or a chain where the dominant bot share is low
(see other-chains.md; needs an archive RPC to measure).

Sources: Aave v3.3 features doc (github.com/aave-dao/aave-v3-origin/docs/3.3), Aave governance "Zombie positions"
thread, Morpho docs "Liquidation", github.com/morpho-org/pre-liquidation, Aave blog "How Aave liquidations perform
under volatile conditions" (Feb 2026).
