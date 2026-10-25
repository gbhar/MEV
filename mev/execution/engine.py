"""The execution engine (spec Phases 6 and 7).

For every risk-passed opportunity the loop hands over, the engine builds the exact FlashArb.execute()
transaction the contract would run and then, depending on MEV_EXECUTION_MODE:

- shadow: eth_call()s it at head and estimates gas. The result (profit the contract would have booked, or the
  decoded revert) is stored in `executions` with mode=shadow. Nothing is signed. This answers the spec's
  shadow-mode questions: would it have succeeded, with what profit, at what gas, after how much latency.
- live: runs the same eth_call first (a revert costs nothing off-chain), then signs and sends. The receipt is
  awaited in a background task so the block loop is never held up; the Executed event gives the booked
  profit and the receipt the gas, both stored with mode=live. Live sends additionally require every cap in
  live_blocked() to pass and the kill switch (mev.execution.killswitch) to be clear.

The contract itself enforces the outcome: it reverts unless the owner receives at least min_profit, which the
engine sets to the gas estimate plus min_net_profit_usd in quote units, so a mined transaction always nets
at least the configured minimum or costs only its gas."""
import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from sqlalchemy import func, select
from web3 import AsyncWeb3, Web3

from mev import metrics
from mev.chain import abi
from mev.config import Settings
from mev.db.models import Execution
from mev.db.session import session
from mev.execution import calldata, killswitch, virtual
from mev.execution.lenders import LenderBook
from mev.execution.signer import Signer
from mev.pools.registry import Pool
from mev.scanner.scanner import Candidate
from mev.simulator.simulate import SimResult

log = logging.getLogger("mev.execution")
VIRTUAL_OWNER = "0x000000000000000000000000000000000000F1A5"   # shadow `from` when there is no signer


@dataclass(frozen=True)
class Built:
    params: calldata.Params | calldata.RouteParams
    tx: dict            # EIP-1559 fields without nonce/from
    expected_profit: int
    quote_decimals: int
    lender: str


class ExecutionEngine:
    def __init__(self, s: Settings, w3: AsyncWeb3, signer: Signer | None, pools: Callable[[], dict[str, Pool]], lenders: LenderBook | None = None):
        self.s, self.w3, self.signer, self.pools = s, w3, signer, pools
        self.lenders = lenders or LenderBook(s)
        self.mode = s.execution_mode
        self.contract = Web3.to_checksum_address(s.flash_arb_address) if s.flash_arb_address else None
        self.tracking: set[asyncio.Task] = set()
        self.live_total = Decimal(0)
        self.owner: str | None = None   # read at startup; shadow mode eth_calls as the owner when there is no signer
        # Shadow without a deployment: FlashArb's bytecode is injected into every eth_call (mev/execution/virtual.py)
        self.virtual = self.mode == "shadow" and self.contract is None
        if self.virtual:
            self.contract = virtual.VIRTUAL_ADDRESS
        self.override: dict | None = None
        self.route_override: dict | None = None   # set when the deployed contract lacks executeRoute (shadow only)

    # ------------------------------------------------------------------ lifecycle

    @property
    def enabled(self) -> bool:
        return self.mode != "paper"

    async def startup(self) -> None:
        if not self.enabled:
            return
        if self.virtual:
            self.owner = self.signer.address if self.signer else VIRTUAL_OWNER
            self.override = virtual.state_override(self.owner)
            log.info("shadow execution against a VIRTUAL FlashArb at %s (state override, nothing deployed); owner=%s", self.contract, self.owner)
            return
        if self.contract is None:
            raise RuntimeError(f"execution_mode={self.mode} needs MEV_FLASH_ARB_ADDRESS")
        c = self.w3.eth.contract(address=self.contract, abi=abi.FLASH_ARB)
        owner, paused = await asyncio.gather(c.functions.owner().call(), c.functions.paused().call())
        self.owner = owner
        if self.mode == "live":
            if self.signer is None:
                raise RuntimeError("execution_mode=live needs a signer (MEV_SIGNER_KEY from secret mev-signer)")
            if owner.lower() != self.signer.address.lower():
                raise RuntimeError(f"FlashArb owner {owner} is not the signer {self.signer.address}")
            bal = await self.w3.eth.get_balance(self.signer.address)
            log.warning("LIVE EXECUTION ENABLED contract=%s signer=%s balance=%.5f ETH caps: size<=$%s daily_loss<=$%s pairs=%s",
                        self.contract, self.signer.address, bal / 1e18, self.s.live_max_trade_size_usd,
                        self.s.live_max_daily_loss_usd, self.s.live_pair_allowlist)
            if bal == 0:
                log.error("signer has no ETH for gas; every send will fail")
        else:
            log.info("shadow execution: contract=%s owner=%s (transactions are built and simulated, never sent)", self.contract, owner)
        if paused:
            log.warning("FlashArb is paused on-chain; every execution will revert with IsPaused")
        code = await self.w3.eth.get_code(self.contract)
        if calldata.ROUTE_SELECTOR not in bytes(code):
            if self.mode == "shadow":
                self.route_override = virtual.state_override(owner, self.contract)
                log.warning("deployed FlashArb predates executeRoute: route candidates are shadowed against the current bytecode "
                            "(state override); redeploy (make contract-deploy) before going live with routes")
            else:
                log.error("deployed FlashArb has no executeRoute: route candidates will be skipped in live mode until it is redeployed")
        with session() as db:
            total = db.scalar(select(func.sum(Execution.realized_usd)).where(Execution.mode == "live"))
        self.live_total = Decimal(str(total or 0))
        metrics.live_profit_usd.set(float(self.live_total))

    # ------------------------------------------------------------------ building

    def build(self, c: Candidate, r: SimResult, head_block: int, gas_price_wei: int, quote_usd: Decimal) -> Built:
        pools = self.pools()
        first = pools[c.route[0]] if c.route else pools[c.buy_pool_id]
        if c.route:
            quote = first.token0 if first.token0.key == c.path[0] else first.token1
        else:
            quote = first.quote
        scale = Decimal(10 ** quote.decimals)
        min_profit = int((r.gas_usd + Decimal(str(self.s.min_net_profit_usd))) / quote_usd * scale)
        lender = self.lenders.choose(quote.address, r.amount_in)
        premium = r.amount_in * self.lenders.fee_bps(quote.address, r.amount_in) // 10_000
        deadline = head_block + self.s.execution_deadline_blocks
        if c.route:
            params = calldata.build_route_params(pools, c.route, c.path, r.amount_in, min_profit, deadline, lender)
            data = calldata.encode_execute_route(params)
        else:
            params = calldata.build_params(first, pools[c.sell_pool_id], r.amount_in, min_profit, deadline, lender)
            data = calldata.encode_execute(params)
        priority = self.s.priority_fee_wei
        gas_limit = self.s.execution_gas_limit + (len(c.route) - 2) * self.s.execution_gas_per_extra_leg if c.route else self.s.execution_gas_limit
        tx = {"chainId": 8453, "to": self.contract, "value": 0, "data": data, "type": 2,
              "gas": gas_limit, "maxPriorityFeePerGas": priority,
              "maxFeePerGas": 2 * max(gas_price_wei - priority, 0) + priority}   # room for two base-fee increases
        return Built(params, tx, r.amount_back - r.amount_in - premium, quote.decimals, lender)

    def live_blocked(self, c: Candidate, r: SimResult, block_ts: int, db) -> str | None:
        """The live cap that stops this send, or None. Shadow mode ignores these."""
        allow = {p.strip() for p in self.s.live_pair_allowlist.split(",") if p.strip()}
        if allow and c.pair not in allow:
            return f"pair {c.pair} not in live allowlist"
        if r.trade_size_usd > self.s.live_max_trade_size_usd:
            return f"size ${r.trade_size_usd} > live cap ${self.s.live_max_trade_size_usd}"
        day0 = datetime.fromtimestamp(block_ts - block_ts % 86400, tz=timezone.utc).replace(tzinfo=None)
        loss = db.scalar(select(func.sum(Execution.realized_usd)).where(Execution.mode == "live", Execution.created_at >= day0)) or 0.0
        if -loss > self.s.live_max_daily_loss_usd:
            return f"live daily loss ${-loss:.2f} > cap ${self.s.live_max_daily_loss_usd}"
        reason = killswitch.halted(self.s, db)
        return f"kill switch: {reason}" if reason else None

    # ------------------------------------------------------------------ the hand-over from the loop

    async def submit(self, opportunity_id: int, c: Candidate, r: SimResult, *, head_block: int, block_ts: int,
                     gas_price_wei: int, eth_price: Decimal, quote_usd: Decimal) -> Execution | None:
        if not self.enabled:
            return None
        t0 = time.perf_counter()
        try:
            built = self.build(c, r, head_block, gas_price_wei, quote_usd)
        except Exception as e:
            log.exception("execution build failed for opportunity %s", opportunity_id)
            return self._record(opportunity_id, c, r, head_block, None, status="error", revert_reason=f"build: {e}")
        row = self._record(opportunity_id, c, r, head_block, built, status="built")
        call = {**built.tx, "from": self.signer.address if self.signer else (self.owner or self.contract)}
        if c.route and self.route_override is not None and self.mode == "shadow":
            override, self.override = self.override, self.route_override
            try:
                profit, gas, revert = await self._simulate(call)
            finally:
                self.override = override
        elif c.route and self.route_override is None and not self.virtual and self.mode == "live" and not await self._has_route_selector():
            profit, gas, revert = None, None, "contract lacks executeRoute (redeploy)"
        else:
            profit, gas, revert = await self._simulate(call)
        latency = (time.perf_counter() - t0) * 1000
        with session() as db:
            row = db.get(Execution, row.id)
            row.simulated_profit, row.gas_estimate, row.revert_reason, row.latency_ms = (str(profit) if profit is not None else None), gas, revert, latency
            row.status = "ok" if revert is None else "revert"
            if self.mode == "live" and revert is None:
                blocked = self.live_blocked(c, r, block_ts, db)
                if blocked:
                    row.status, row.blocked_reason = "blocked", blocked
            db.commit()
            status, row_id = row.status, row.id
        metrics.executions.labels(self.mode, status).inc()
        metrics.execution_latency.observe(latency / 1000)
        log.info("%s execution opp=%s %s size=$%s expected=%s simulated=%s gas=%s %s", self.mode, opportunity_id, c.pair, r.trade_size_usd,
                 built.expected_profit, profit, gas, revert or (row.blocked_reason if status == "blocked" else "ok"))
        if self.mode == "live" and status == "ok":
            await self._send(row_id, built, gas, eth_price, quote_usd, c)
        return row

    # ------------------------------------------------------------------ helpers

    async def _simulate(self, call: dict) -> tuple[int | None, int | None, str | None]:
        """(profit, gas, revert). Both calls run; a revert reason is taken from whichever carries data (Alchemy strips
        it from eth_call under a state override but not from eth_estimateGas). Fee fields are left out: with them the
        node checks the sender's balance first and an unfunded sender gets "insufficient funds" instead of the revert."""
        sim = {k: v for k, v in call.items() if k in ("from", "to", "data", "value", "gas")}
        ret, gas = await asyncio.gather(self.w3.eth.call(sim, "latest", self.override), self.w3.eth.estimate_gas(sim, "latest", self.override),
                                        return_exceptions=True)
        errors = [e for e in (ret, gas) if isinstance(e, BaseException)]
        if not errors:
            return calldata.decode_profit(ret), int(gas), None
        reason = None
        for e in errors:
            data = getattr(e, "data", None)
            if isinstance(data, dict):
                data = data.get("data")
            if data and str(data).startswith("0x") and len(str(data)) > 2:
                reason = calldata.decode_revert(data)
                break
        if reason is None:
            e = errors[0]
            reason = f"{type(e).__name__}: {str(e)[:160]}"
        return None, None, reason

    async def _send(self, row_id: int, built: Built, gas: int | None, eth_price: Decimal, quote_usd: Decimal, c: Candidate) -> None:
        assert self.signer is not None
        try:
            nonce = await self.w3.eth.get_transaction_count(self.signer.address, "pending")
            tx = {**built.tx, "nonce": nonce, "gas": min(self.s.execution_gas_limit, int((gas or self.s.execution_gas_limit) * 1.25))}
            tx_hash = await self.w3.eth.send_raw_transaction(self.signer.sign(tx))
        except Exception as e:
            log.exception("live send failed for execution %s", row_id)
            with session() as db:
                row = db.get(Execution, row_id)
                row.status, row.revert_reason = "error", f"send: {str(e)[:200]}"
                db.commit()
            metrics.executions.labels("live", "error").inc()
            return
        h = tx_hash.hex() if not isinstance(tx_hash, str) else tx_hash
        h = h if h.startswith("0x") else "0x" + h
        with session() as db:
            row = db.get(Execution, row_id)
            row.status, row.tx_hash, row.nonce = "sent", h, nonce
            db.commit()
        log.warning("LIVE SENT execution=%s %s tx=%s nonce=%s", row_id, c.pair, h, nonce)
        task = asyncio.create_task(self._track(row_id, h, eth_price, quote_usd, built))
        self.tracking.add(task)
        task.add_done_callback(self.tracking.discard)

    async def _track(self, row_id: int, tx_hash: str, eth_price: Decimal, quote_usd: Decimal, built: Built) -> None:
        try:
            receipt = await self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=self.s.live_receipt_timeout_s)
        except Exception as e:
            log.error("execution %s: no receipt for %s (%s)", row_id, tx_hash, str(e)[:120])
            with session() as db:
                row = db.get(Execution, row_id)
                row.status, row.revert_reason = "error", f"receipt: {str(e)[:200]}"
                db.commit()
            return
        quote_decimals = built.quote_decimals
        gas_used, price = int(receipt["gasUsed"]), int(receipt.get("effectiveGasPrice", 0))
        l1_fee = int(receipt.get("l1Fee", 0) or 0)
        gas_usd = Decimal(gas_used * price + l1_fee) / Decimal(10**18) * eth_price
        profit = None
        if receipt["status"] == 1:
            c = self.w3.eth.contract(address=self.contract, abi=abi.FLASH_ARB)
            for ev in self._executed_logs(c, receipt):
                profit = int(ev["args"]["profit"])
        profit_usd = Decimal(profit) / Decimal(10**quote_decimals) * quote_usd if profit is not None else Decimal(0)
        realized = profit_usd - gas_usd
        with session() as db:
            row = db.get(Execution, row_id)
            row.status = "mined" if receipt["status"] == 1 else "failed"
            row.gas_used, row.effective_gas_price = gas_used, str(price)
            row.profit = str(profit) if profit is not None else None
            row.profit_usd, row.gas_usd, row.realized_usd = float(profit_usd), float(gas_usd), float(realized)
            row.resolved_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db.commit()
        self.live_total += realized
        metrics.live_profit_usd.set(float(self.live_total))
        metrics.executions.labels("live", "mined" if receipt["status"] == 1 else "failed").inc()
        log.warning("LIVE %s execution=%s tx=%s profit=%s (%.4f USD) gas=%.4f USD realized=%.4f USD total=%.4f",
                    "MINED" if receipt["status"] == 1 else "FAILED", row_id, tx_hash, profit, profit_usd, gas_usd, realized, self.live_total)

    async def _has_route_selector(self) -> bool:
        if not hasattr(self, "_route_ok"):
            code = await self.w3.eth.get_code(self.contract)
            self._route_ok = calldata.ROUTE_SELECTOR in bytes(code)
        return self._route_ok

    @staticmethod
    def _executed_logs(contract, receipt) -> list:
        out = []
        for name in ("Executed", "RouteExecuted"):
            try:
                out += list(getattr(contract.events, name)().process_receipt(receipt))
            except Exception:
                pass
        return out

    def _record(self, opportunity_id: int, c: Candidate, r: SimResult, head_block: int, built: Built | None, *, status: str,
                revert_reason: str | None = None) -> Execution:
        row = Execution(opportunity_id=opportunity_id, block_number=c.block_number, head_block=head_block, mode=self.mode,
                        lender=built.lender if built else self.s.flash_lender, status=status, contract=self.contract,
                        signer=self.signer.address if self.signer else None, quote_symbol=r.quote_symbol,
                        trade_size_usd=r.trade_size_usd, amount_in=str(r.amount_in),
                        min_profit=str(built.params.min_profit) if built else "0",
                        expected_profit=str(built.expected_profit) if built else str(r.amount_back - r.amount_in),
                        revert_reason=revert_reason)
        with session() as db:
            db.add(row)
            db.commit()
            db.refresh(row)
        return row

    async def drain(self, timeout: float = 120.0) -> None:
        """Wait for in-flight receipt trackers (shutdown / --blocks runs)."""
        if self.tracking:
            await asyncio.wait(self.tracking, timeout=timeout)
