# MEV platform — V1 (paper, shadow and restricted live execution on Base)

Simulation-first cross-DEX arbitrage scanner and paper trader. It reads Base mainnet, finds WETH/USDC
price gaps between Uniswap V3 and constant-product pools (Aerodrome, Uniswap V2), simulates a
flash-loan round trip at several sizes with exact AMM math, applies deterministic risk rules, records
paper trades and what happened one block later, and stores a feature vector per opportunity for the
models that come next. By default nothing is signed or broadcast and no private key exists anywhere; the
execution layer (`contracts/`, `mev/execution/`, spec Phases 5-7) is off until `MEV_EXECUTION_MODE` is set.

Spec: `~/Documents/MEV.docx`. This implements sections 36 steps 1–8 (connectivity through the feature
pipeline) plus the read API, historical replay and the monitoring stack.

## Do I need any coins?

Not for paper or shadow mode: both only read the chain. Live execution needs a little ETH on Base for gas in
the signer wallet (`make wallet`) and one deployment transaction. The trade capital comes from the flash loan
and is repaid in the same transaction.

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

## Pools and pairs

47 bootstrap pools over 13 pairs (`mev/chain/addresses.py`) across Uniswap V3, PancakeSwap V3, SushiSwap V3,
Aerodrome Slipstream, Aerodrome V2 and Uniswap V2, including the 1 bp WETH/USDC, cbBTC and stable pools.
`make discover` (daily CronJob on the cluster) refreshes every pool's fee and TVL from real token balances
and adds any pool of the allowlisted tokens above `MEV_MIN_POOL_TVL_USD` (default $200k); `GET /pools`
lists them with `min_tvl` / `max_tvl` / `pair` / `dex` filters. `make discover ARGS=--all` also walks the
Aerodrome factories and PoolCreated logs; it is RPC-heavy and meant to be run by hand.

Each pair has a quote token (USDC, then USDbC, USDT, DAI, WETH, cbBTC by priority) that is borrowed and
measured in; non-dollar quotes are priced in USD per block through the deepest reference pools.

## What happens on each block

1. `mev/main.py` processes blocks in order (at most `max_catchup_blocks` behind head), retries a failed
   block, never processes a stored block twice, and flags a parent-hash mismatch (reorg) in `blocks`.
2. `mev/pools/state.py` fetches pool state through chunked Multicall3 calls: `slot0`/`liquidity` (and the
   live `fee` for Slipstream) plus the tick bitmap and `liquidityNet` of the initialized ticks nearest the
   price for V3 kinds, `getReserves` for x*y=k pools. Only pools that emitted an event are refetched; tick
   data is cached and rebuilt after a Mint/Burn or on a round-robin schedule. RPC endpoints rotate on 403/429.
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

It never signs or sends anything; execution lives in the loop process (next section).

## Execution (`contracts/`, `mev/execution/`) — spec Phases 5-7

`contracts/src/FlashArb.sol` is the spec's intentionally simple contract: owner-only `execute()` borrows the
quote token (Aave V3 `flashLoanSimple`, 5 bp; Morpho Blue, fee-free, selectable per call), buys the base on
pool A, sells it on pool B and reverts unless the owner receives at least `minProfit`; the surplus is swept
in the same transaction. Legs cover every venue the scanner watches: Uniswap V3 / SushiSwap V3 / Aerodrome
Slipstream (`uniswapV3SwapCallback`), PancakeSwap V3 (`pancakeV3SwapCallback`), Uniswap V2 pairs and
Aerodrome volatile pairs. Pausable, with `rescue()` and a per-call deadline block. `make contract-test` runs
the Foundry fork tests against Base at the blocks of the first paper trades: the on-chain leg outputs equal
the Python simulator's to the wei (`test_trade4_legs_match_python_simulator_to_the_wei`).

`MEV_EXECUTION_MODE` selects what the loop does with a risk-passed opportunity:

- `paper` (default): the paper trade only. No contract address or key is needed or read.
- `shadow` (Phase 6): `mev/execution/engine.py` builds the exact `execute()` transaction, `eth_call`s it at
  head and estimates gas; profit or the decoded revert (`InsufficientProfit(got, want)`, `Expired`, ...) lands
  in `executions` (`GET /executions`, `GET /executions/summary`). Nothing is signed. Needs `MEV_FLASH_ARB_ADDRESS`.
- `live` (Phase 7): the same simulation first, then sign and send if every cap passes: pair allowlist
  (`MEV_LIVE_PAIR_ALLOWLIST`), `MEV_LIVE_MAX_TRADE_SIZE_USD` ($1,000), `MEV_LIVE_MAX_DAILY_LOSS_USD` ($20, gas
  included) and the kill switch (`make halt` / a `HALT` file / `MEV_EXECUTION_HALT=true`). The receipt is awaited
  in the background; the `Executed` event gives the booked profit, the receipt the gas, `realized_usd` the
  difference. `minProfit` is set to the gas estimate plus `min_net_profit_usd`, so a mined transaction nets at
  least the configured minimum or costs only its gas.

Setup, in order (each step is a Makefile target):

```bash
brew install foundry && make contract-build && make contract-test   # compile + fork tests
make wallet            # new key -> Kubernetes secret mev-signer; prints the address. Fund it with ~0.01 ETH on Base.
make contract-deploy   # deploys FlashArb from that key (it becomes the owner); prints MEV_FLASH_ARB_ADDRESS
# .env: MEV_FLASH_ARB_ADDRESS=0x..., MEV_EXECUTION_MODE=shadow  ->  make k8s-secret && make k8s-apply
# after days of shadow data (/executions/summary): MEV_EXECUTION_MODE=live, make k8s-secret, restart the loop
```

The key is generated on the laptop and piped straight into the secret; it is never printed, logged, committed
or stored in the database (`mev/execution/signer.py` exposes the address only). The loop mounts the secret as
`MEV_SIGNER_KEY`; the API and the agent never see it.

## API (`mev/api/app.py`)

`GET /health`, `GET /opportunities` (filters: dex, pair, min_profit, risk_passed, from_block, to_block),
`GET /opportunities/{id}` (candidate, market state, simulations, features, risk decision, paper trade),
`GET /pnl` (gross, fees, gas, net, win rate, max drawdown, execution loss, plus a `live` block), `GET /actions`,
`GET /executions` (filters: mode, status, pair, opportunity_id), `GET /executions/summary`, `POST /simulate`
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

## Running it on Kubernetes (`infrastructure/kubernetes`)

Manifests for an existing cluster with the CloudNativePG operator and kube-prometheus-stack (the OKE
cluster `prodaiq-oke-test` has both). Everything lives in the `mev-platform` namespace: a 1-instance
Postgres cluster, and one Deployment each for the loop, the agent and the API, all pulling
`bom.ocir.io/bm9gugcjiuw7/ane:mev` through the `ocir-secret` pull secret (push with `docker build -t
bom.ocir.io/bm9gugcjiuw7/ane:mev . && docker push bom.ocir.io/bm9gugcjiuw7/ane:mev`). The GitHub Actions
workflow also publishes `ghcr.io/gbhar/mev` on every push to `main`; switch the image back to it if that
package is made public.

```bash
make k8s-apply     # namespace, Postgres, deployments, ServiceMonitor, dashboard ConfigMap
make k8s-secret    # optional: MEV_ALERT_WEBHOOK / MEV_LLM_API_KEY / MEV_RPC_HTTP from .env
make k8s-status
make k8s-logs
make k8s-api       # port-forward, then http://localhost:8000/pnl
```

The API is also published through the cluster's Traefik at http://mev.141.148.211.129.nip.io behind basic
auth (`infrastructure/kubernetes/ingress.yaml`; the users secret `mev-api-basicauth` is created by hand).

The loop's metrics are scraped by the cluster Prometheus through the ServiceMonitor; the dashboard
JSON is shipped as a sidecar ConfigMap and can also be imported by hand. The signer secret `mev-signer`
(`make wallet`) is mounted into the loop only, and only matters when `MEV_EXECUTION_MODE` is shadow or live.

## Known limitations (deliberate for V1)

- **"Actual result" ignores the race.** Re-simulating on the next block says whether the spread
  survived, not whether this bot would have been first; on Base that is a latency contest to the
  sequencer. The MEV-competition model in the spec is meant to correct for it.
- A V3 quote that would leave the fetched ±300-tick window is marked inexact and blocked (far larger
  than any size in the grid moves these pools).
- Pool fees come from the registry. Aerodrome volatile pools have per-pool fees (AERO/WETH and VIRTUAL/WETH
  charge 1%, the others 0.3%); `make discover` reads them from the factory and startup seeding no longer
  overwrites them. Before 2026-10-25 those two were registered at 0.3%, which made every VIRTUAL/WETH and
  AERO/WETH candidate look ~0.7% better than it was; the six paper trades of 2026-10-24 were phantom.
- The loop needs several RPC round trips per block (~250-400 ms each from Mumbai to Alchemy). When it falls
  behind it jumps to head (`chase_head`) and counts the skipped blocks instead of processing a stale backlog.
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
