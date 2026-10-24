# MEV platform — V1 (paper trading on Base)

Simulation-first cross-DEX arbitrage scanner and paper trader. It reads Base mainnet, finds WETH/USDC
price gaps between Uniswap V3 and constant-product pools (Aerodrome, Uniswap V2), simulates a
flash-loan round trip at several sizes with exact AMM math, applies deterministic risk rules, records
paper trades and what happened one block later, and stores a feature vector per opportunity for the
models that come next. Nothing is ever signed or broadcast and no private key exists anywhere.

Spec: `~/Documents/MEV.docx`. This implements sections 36 steps 1–8 (connectivity through the feature
pipeline) plus the read API, historical replay and the monitoring stack.

## Do I need any coins?

No. V1 only reads the chain through a free RPC. Coins are needed only in Phase 7 (restricted live
execution): a little ETH on Base for gas. The trade capital comes from the flash loan and is repaid in
the same transaction.

## Run

```bash
make install          # venv + deps
cp .env.example .env  # optional; defaults: publicnode RPC, SQLite at ./mev.db
make test             # offline unit tests (36)
make live-tests       # also the tests that hit the RPC (QuoterV2 cross-check, replay determinism)
make check-rpc        # live price on each pool
make run-once         # process 3 blocks then exit
make run              # forever; Prometheus metrics on :9100
make api              # read-only API on http://127.0.0.1:8000 (docs at /docs)
make monitoring       # Prometheus :9090 + Grafana :3000 (admin/admin), dashboard "MEV V1 paper trading"
make replay FROM=<a> TO=<b>   # reproduce a block range into ./replay.db
make export           # dataset.csv: opportunities + features + simulations + paper outcomes
```

RPC: the default is `https://base-rpc.publicnode.com`. `https://mainnet.base.org` also works but allows
5 requests/s and refuses `eth_getLogs` under load; the loop needs 3 requests per block (header, logs,
one Multicall3 for every pool's state). A free Alchemy/QuickNode key is better still (`MEV_RPC_HTTP`).

## What happens on each block

1. `mev/main.py` processes blocks in order (at most `max_catchup_blocks` behind head), retries a failed
   block, never processes a stored block twice, and flags a parent-hash mismatch (reorg) in `blocks`.
2. `mev/pools/state.py` fetches every pool's state in one Multicall3 call: `slot0`/`liquidity` plus the
   tick bitmap and `liquidityNet` of each initialized tick within ±300 ticks for V3, `getReserves` for
   x*y=k pools. The previous block's bitmap is the hint, so a warm block is a single `eth_call`.
3. `mev/indexer/events.py` fetches the block's pool logs (V3 Swap/Mint/Burn, V2/Aerodrome Swap/Sync),
   stores them in `swaps`, and cross-checks the event-derived end-of-block state against the fetched
   state (`state_divergence_total`). A missing x*y=k state is rebuilt from its last Sync.
4. `mev/scanner/scanner.py` compares marginal prices; a candidate exists only when the spread exceeds
   both pool fees.
5. `mev/simulator/simulate.py` borrows USDC, buys WETH on the cheap pool and sells on the dear pool for
   each size in the grid using exact AMM math (`mev/dex/uniswap_v3.py` is an integer port of the V3
   swap loop that crosses ticks and matches QuoterV2 to the wei), then nets out flash-loan fee, next
   block's EIP-1559 gas and a buffer. Pool fees and price impact are inside the swap outputs and are
   never subtracted twice; `dex_fee_usd`/`slippage_usd` are diagnostics only.
6. `mev/risk/rules.py`: min profit, max size, max gas, stale state, inexact quote, daily loss, failure
   streak, one trade per pool per block, no re-trading a pool whose spread persists. Deterministic, no
   model input.
7. `mev/features/compute.py` stores the spec's feature vector (spread, depth, impact, 1m/5m
   volatility, volumes from `swaps`, gas, latency, ages, historical outcomes, calendar) per opportunity.
8. `mev/paper/executor.py` opens a paper trade and on the next block re-simulates it: `actual_net_usd`
   (same formula), `realized_net_usd` (atomic rule: profit guard passes → gross − flash − gas, else
   −gas), `execution_loss_usd`. Unresolved trades survive restarts.
9. Everything lands in SQLite (`pools`, `blocks`, `pool_snapshots`, `swaps`, `opportunities`,
   `simulations`, `features`, `paper_trades`), every row pinned to a block.

## Agent (`mev/agent`)

`make agent` runs a second process on the laptop that watches the database and acts when the loop
finds something: a risk-passed opportunity, or a blocked candidate whose best net clears
`MEV_AGENT_ALERT_MIN_NET_USD`. For each one it

1. sends a macOS notification and, if `MEV_ALERT_WEBHOOK` is set, posts to Slack, Discord or Telegram;
2. re-quotes the Uniswap V3 legs on the on-chain QuoterV2 at that block and re-simulates with fresh
   state, so the local math is checked against the chain (`actions.kind = verify`);
3. follows the opportunity block by block until the spread no longer clears the minimum profit, and
   records how many blocks it survived and the swaps in the block that closed it
   (`actions.kind = lifetime`). That is the competition label the spec's MEV model needs;
4. writes every step to the `actions` table (`GET /actions`, and inside `GET /opportunities/{id}`).

It never signs or sends anything. Live execution (spec Phase 7) needs the flash-loan contract, a
deployment, ETH for gas and a signer, none of which exist here.

## API (`mev/api/app.py`)

`GET /health`, `GET /opportunities` (filters: dex, pair, min_profit, risk_passed, from_block, to_block),
`GET /opportunities/{id}` (candidate, market state, simulations, features, risk decision, paper trade),
`GET /pnl` (gross, fees, gas, net, win rate, max drawdown, execution loss), `GET /actions`, `POST /simulate`
(`pool_buy`, `pool_sell`, `trade_size_usd`, optional `block_number`; runs against stored snapshots,
which hold no V3 tick window, so a tick-crossing V3 leg is reported `exact=false`).

## Monitoring

`infrastructure/monitoring/` holds the Prometheus scrape config and a provisioned Grafana dashboard
(paper P&L, block rate, RPC latency, opportunity funnel, spreads, prices, risk blocks by rule, events,
state divergence, stage latencies, errors, resolve lag). The YAML/JSON are validated by `make test`
tooling but the stack itself was not brought up on this machine (Docker was not running).

## Running it on a free VM (Oracle Cloud Always Free)

The loop, agent and API together need well under 1 GB of RAM, no GPU and no coins, so an always-free
VM is enough. Oracle's Ampere A1 shape (up to 4 cores, 24 GB) is free indefinitely.

1. Create the tenancy yourself at cloud.oracle.com (card needed for identity verification; the free
   shapes are never charged). Compute → Instances → Create: image **Ubuntu 24.04**, shape
   **VM.Standard.A1.Flex** (1 OCPU, 6 GB is plenty), add your SSH public key, create.
2. Copy the repo to the VM and run the setup script:
   ```bash
   rsync -a --exclude .venv --exclude mev.db --exclude data ./ ubuntu@<vm-ip>:mev/
   ssh ubuntu@<vm-ip> 'cd mev && bash deploy/setup.sh'
   ```
   (or `git clone` your remote on the VM instead of rsync).
3. Set alerts: follow `deploy/telegram.md`, put the URL in `.env` on the VM, restart the agent.
4. Watch: `docker compose -f docker-compose.app.yml logs -f loop agent`. The API stays bound to
   localhost on the VM; reach it with `ssh -L 8000:127.0.0.1:8000 ubuntu@<vm-ip>` and open
   http://localhost:8000/pnl. Nothing needs to be opened in the Oracle security list.

The same `Dockerfile` and `docker-compose.app.yml` work on any Ubuntu VM (GCP e2-micro included).

## Known limitations (deliberate for V1)

- **"Actual result" ignores the race.** Re-simulating on the next block says whether the spread
  survived, not whether this bot would have been first; on Base that is a latency contest to the
  sequencer. The MEV-competition model in the spec is meant to correct for it.
- A V3 quote that would leave the fetched ±300-tick window is marked inexact and blocked (far larger
  than any size in the grid moves these pools).
- Pools are compared only when their token0/token1 ordering matches (true for the V1 set).
- Logs are fetched per block over HTTP; the public Base WebSocket endpoint refused connections
  (2026-10-24). With a provider that offers WSS, set `MEV_RPC_WS` and add a subscription path.
- Reorgs are detected and flagged (`blocks.parent_mismatch`, `reorgs_total`) but rows written for the
  orphaned block are kept.
- Replay needs the RPC to serve state at those blocks (recent only on public endpoints).
- No ML models or LLM agents yet: they need the dataset this version produces.

## Next steps

1. Run for a few days (`make run`) and watch `GET /pnl` and the dashboard.
2. Train the first model (realized profit) from `make export` once a few thousand opportunities exist;
   run it in shadow mode, logging predictions next to the deterministic decision.
3. Add the MEV-competition label: find the transaction that captured each spread in the next block.
4. Second token pair and a third DEX adapter via the strategy interface.
