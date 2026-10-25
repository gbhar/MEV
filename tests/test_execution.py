"""Execution layer offline: calldata matches the Solidity struct, the signer never leaks its key, the engine in
shadow mode stores the eth_call result (profit or decoded revert) without sending, live mode honours every cap and
the kill switch, and a successful live send is tracked to its receipt."""
import asyncio
import itertools
import time
from decimal import Decimal

import pytest
from sqlalchemy import select
from web3 import Web3

from mev.config import Settings
from mev.db.models import Execution, Opportunity
from mev.db.session import session
from mev.execution import calldata, killswitch
from mev.execution.engine import ExecutionEngine
from mev.execution.signer import Signer, generate
from mev.pools.registry import Pool, Token
from mev.scanner.scanner import Candidate
from mev.simulator.simulate import SimResult

WETH = Token("0x4200000000000000000000000000000000000006", "WETH", 18)
VIRTUAL = Token("0x0b3e328455c4059EEb9e3f84b5543F74E24e7E1b", "VIRTUAL", 18)
AERO = Pool("aero_virtual_weth_v", "aerodrome", "constant_product", "0x21594b992F68495dD28d605834b58889d0a727c7", VIRTUAL, WETH, 10000, variant="aerodrome")
UNI = Pool("univ3_virtual_weth_5", "uniswap_v3", "uniswap_v3", "0x9c087Eb773291e50CF6c6a90ef0F4500e349B903", VIRTUAL, WETH, 500, 10, "uniswap")
PCS = Pool("pcs", "pancake_v3", "uniswap_v3", "0x" + "1" * 40, VIRTUAL, WETH, 500, 10, "pancake")
V2 = Pool("v2", "uniswap_v2", "constant_product", "0x" + "2" * 40, VIRTUAL, WETH, 3000, variant="v2")
CONTRACT = Web3.to_checksum_address("0x" + "c" * 40)
TEST_KEY = "0x" + "11" * 32   # a well-known throwaway key, never funded
POOLS = {p.pool_id: p for p in (AERO, UNI, PCS, V2)}
CAND = Candidate("aero_virtual_weth_v", "univ3_virtual_weth_5", Decimal("0.00041"), Decimal("0.00042"), Decimal(70), Decimal(105), 100, "VIRTUAL/WETH")
SIM = SimResult(1000, 429501687933749293, 1606635843626486163868, 431000000000000000, Decimal("3.4"), Decimal("0.5"), Decimal("0.065"),
                Decimal("0.2"), Decimal("2.6"), True, quote_symbol="WETH")


def settings(**kw) -> Settings:
    return Settings(_env_file=None, flash_arb_address=CONTRACT, signer_key=TEST_KEY, execution_halt_file="/nonexistent/HALT", **kw)


# ---------------------------------------------------------------- calldata

def test_params_mirror_solidity_struct():
    p = calldata.build_params(AERO, UNI, 429501687933749293, 1, 53038943, "aave_v3")
    assert p.quote == WETH.address and p.amount == 429501687933749293
    assert p.buy == (AERO.address, calldata.KIND_PAIR_AERODROME, WETH.address, VIRTUAL.address, 0)
    assert p.sell == (UNI.address, calldata.KIND_CL_UNISWAP, VIRTUAL.address, WETH.address, 0)
    data = calldata.encode_execute(p)
    assert data[:4].hex() == "3ff2fc5e"     # cast sig "execute((uint8,address,uint256,(address,uint8,address,address,uint24),(...),uint256,uint256))"
    assert len(data) == 4 + 15 * 32


def test_leg_kinds_and_v2_fee():
    assert calldata.leg(PCS, VIRTUAL, WETH)[1] == calldata.KIND_CL_PANCAKE
    assert calldata.leg(V2, WETH, VIRTUAL) == (V2.address, calldata.KIND_PAIR_V2, WETH.address, VIRTUAL.address, 3000)
    assert calldata.leg(AERO, WETH, VIRTUAL)[4] == 0   # Aerodrome quotes its own fee on-chain


def test_build_params_rejects_mismatched_pairs():
    other = Pool("x", "uniswap_v3", "uniswap_v3", "0x" + "3" * 40, Token("0x" + "4" * 40, "USDC", 6), WETH, 500, 10)
    with pytest.raises(ValueError):
        calldata.build_params(AERO, other, 1, 1, 1, "aave_v3")


def test_decode_revert_and_profit():
    sel = Web3.keccak(text="InsufficientProfit(uint256,uint256)")[:4]
    assert calldata.decode_revert(sel + (7).to_bytes(32, "big") + (9).to_bytes(32, "big")) == "InsufficientProfit(7, 9)"
    assert calldata.decode_revert(Web3.keccak(text="IsPaused()")[:4]) == "IsPaused()"
    err = Web3.keccak(text="Error(string)")[:4] + Web3().codec.encode(["string"], ["nope"])
    assert calldata.decode_revert(err) == "Error(nope)"
    assert calldata.decode_revert(None) == "reverted without data"
    assert calldata.decode_profit((123).to_bytes(32, "big")) == 123


# ---------------------------------------------------------------- signer

def test_signer_exposes_address_only():
    s = Signer(TEST_KEY)
    assert s.address == "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
    assert "11" * 8 not in repr(s)
    raw = s.sign({"chainId": 8453, "nonce": 0, "to": CONTRACT, "value": 0, "data": b"", "gas": 21000, "type": 2,
                  "maxFeePerGas": 10, "maxPriorityFeePerGas": 1})
    assert isinstance(raw, bytes) and raw[0] == 2   # typed EIP-1559 envelope


def test_generate_gives_fresh_keys():
    a1, k1 = generate()
    a2, k2 = generate()
    assert a1 != a2 and k1 != k2 and Signer(k1).address == a1


# ---------------------------------------------------------------- engine

class FakeEth:
    def __init__(self, profit=5 * 10**14, revert=None, receipt_status=1):
        self.profit, self.revert, self.receipt_status = profit, revert, receipt_status
        self.sent: list[bytes] = []
        self.calls: list[dict] = []

    call_has_data = True   # Alchemy strips revert data from eth_call under a state override; estimate_gas keeps it

    async def call(self, tx, block_identifier=None, state_override=None):
        self.calls.append(tx)
        self.overrides = getattr(self, "overrides", []) + [state_override]
        if self.revert is not None:
            e = Exception("execution reverted")
            if self.call_has_data:
                e.data = self.revert
            raise e
        return self.profit.to_bytes(32, "big")

    async def estimate_gas(self, tx, block_identifier=None, state_override=None):
        if self.revert is not None:
            e = Exception("execution reverted")
            e.data = self.revert
            raise e
        return 300_000

    async def get_transaction_count(self, addr, block="pending"):
        return 7

    async def send_raw_transaction(self, raw):
        self.sent.append(raw)
        return bytes.fromhex("ab" * 32)

    async def wait_for_transaction_receipt(self, h, timeout=None):
        return {"status": self.receipt_status, "gasUsed": 280_000, "effectiveGasPrice": 10_000_000, "l1Fee": 0, "logs": []}

    def contract(self, address=None, abi=None):
        class Ev:
            def process_receipt(self_, receipt):
                return [{"args": {"profit": 5 * 10**14}}] if receipt["status"] == 1 else []
        class Events:
            def Executed(self_):
                return Ev()
        class C:
            events = Events()
        return C()


class FakeW3:
    def __init__(self, **kw):
        self.eth = FakeEth(**kw)


@pytest.fixture(autouse=True)
def clean():
    with session() as db:
        db.query(Execution).delete()
        db.commit()


_blocks = itertools.count(100)


def opp_id() -> int:
    with session() as db:
        o = Opportunity(block_number=next(_blocks), buy_pool_id="aero_virtual_weth_v", sell_pool_id="univ3_virtual_weth_5", pair="VIRTUAL/WETH",
                        buy_price=1, sell_price=1, spread_bps=70, fee_floor_bps=105, best_trade_size_usd=1000, best_net_usd=2.6, risk_passed=True)
        db.add(o)
        db.commit()
        return o.id


def submit(engine, **kw):
    return asyncio.run(engine.submit(opp_id(), CAND, SIM, head_block=101, block_ts=1_700_000_000, gas_price_wei=55_000_000,
                                     eth_price=Decimal(2300), quote_usd=Decimal(2300), **kw))


def test_paper_mode_builds_nothing():
    e = ExecutionEngine(Settings(_env_file=None), FakeW3(), None, lambda: POOLS)
    assert not e.enabled and submit(e) is None
    with session() as db:
        assert db.scalar(select(Execution)) is None


def test_shadow_stores_simulation_and_never_sends():
    w3 = FakeW3(profit=5 * 10**14)
    e = ExecutionEngine(settings(execution_mode="shadow"), w3, None, lambda: POOLS)
    row = submit(e)
    assert row.mode == "shadow" and row.status == "ok" and row.simulated_profit == str(5 * 10**14) and row.gas_estimate == 300_000
    assert row.expected_profit == str(SIM.amount_back - SIM.amount_in - SIM.amount_in * 5 // 10_000)
    # min_profit covers gas plus the configured minimum net, in quote units
    assert int(row.min_profit) == int((SIM.gas_usd + Decimal("1.0")) / Decimal(2300) * Decimal(10**18))
    assert w3.eth.sent == [] and row.tx_hash is None
    call = w3.eth.calls[0]
    assert call["to"] == CONTRACT and call["data"][:4].hex() == "3ff2fc5e"
    assert "maxFeePerGas" not in call and "nonce" not in call   # simulation runs without fee fields (unfunded sender must still see the revert)
    tx = e.build(CAND, SIM, 101, 55_000_000, Decimal(2300)).tx
    assert tx["maxPriorityFeePerGas"] == 50_000_000 and tx["maxFeePerGas"] == 2 * (55_000_000 - 50_000_000) + 50_000_000
    assert tx["chainId"] == 8453 and tx["type"] == 2


def test_shadow_decodes_contract_revert():
    sel = Web3.keccak(text="InsufficientProfit(uint256,uint256)")[:4]
    w3 = FakeW3(revert="0x" + (sel + (0).to_bytes(32, "big") + (1).to_bytes(32, "big")).hex())
    row = submit(ExecutionEngine(settings(execution_mode="shadow"), w3, None, lambda: POOLS))
    assert row.status == "revert" and row.revert_reason == "InsufficientProfit(0, 1)" and row.simulated_profit is None


def test_live_sends_and_tracks_receipt():
    w3 = FakeW3()
    s = settings(execution_mode="live", live_pair_allowlist="VIRTUAL/WETH")
    e = ExecutionEngine(s, w3, Signer(TEST_KEY), lambda: POOLS)
    row = submit(e)
    asyncio.run(e.drain())
    with session() as db:
        row = db.get(Execution, row.id)
    assert len(w3.eth.sent) == 1 and row.tx_hash == "0x" + "ab" * 32 and row.nonce == 7
    assert row.status == "mined" and row.profit == str(5 * 10**14) and row.gas_used == 280_000
    assert row.profit_usd == pytest.approx(5e14 / 1e18 * 2300) and row.gas_usd == pytest.approx(280_000 * 1e7 / 1e18 * 2300)
    assert row.realized_usd == pytest.approx(row.profit_usd - row.gas_usd) and float(e.live_total) == pytest.approx(row.realized_usd)


def test_live_failed_receipt_books_gas_loss():
    w3 = FakeW3(receipt_status=0)
    e = ExecutionEngine(settings(execution_mode="live", live_pair_allowlist=""), w3, Signer(TEST_KEY), lambda: POOLS)
    row = submit(e)
    asyncio.run(e.drain())
    with session() as db:
        row = db.get(Execution, row.id)
    assert row.status == "failed" and row.profit is None and row.realized_usd == pytest.approx(-row.gas_usd)


@pytest.mark.parametrize("kw,reason", [
    ({"live_pair_allowlist": "WETH/USDC"}, "not in live allowlist"),
    ({"live_max_trade_size_usd": 500}, "live cap"),
    ({"execution_halt": True}, "kill switch"),
])
def test_live_caps_block_the_send(kw, reason):
    w3 = FakeW3()
    e = ExecutionEngine(settings(execution_mode="live", **kw), w3, Signer(TEST_KEY), lambda: POOLS)
    row = submit(e)
    assert row.status == "blocked" and reason in row.blocked_reason and w3.eth.sent == []


def test_live_revert_is_not_sent():
    w3 = FakeW3(revert="0x" + Web3.keccak(text="IsPaused()")[:4].hex())
    row = submit(ExecutionEngine(settings(execution_mode="live", live_pair_allowlist=""), w3, Signer(TEST_KEY), lambda: POOLS))
    assert row.status == "revert" and row.revert_reason == "IsPaused()" and w3.eth.sent == []


def test_daily_loss_cap_and_db_kill_switch():
    s = settings(execution_mode="live", live_pair_allowlist="", live_max_daily_loss_usd=1.0)
    e = ExecutionEngine(s, FakeW3(), Signer(TEST_KEY), lambda: POOLS)
    with session() as db:
        db.add(Execution(opportunity_id=opp_id(), block_number=1, mode="live", lender="aave_v3", status="failed", trade_size_usd=1000,
                         amount_in="1", min_profit="0", expected_profit="0", realized_usd=-1.5))
        db.commit()
        assert "daily loss" in e.live_blocked(CAND, SIM, int(time.time()), db)   # the row was created today (UTC)
    with session() as db:
        db.query(Execution).delete()
        db.commit()
        killswitch.set_halt(db, True, "manual")
        assert "kill switch" in e.live_blocked(CAND, SIM, 1_700_000_000, db)
        killswitch.set_halt(db, False)
        assert e.live_blocked(CAND, SIM, 1_700_000_000, db) is None


# ---------------------------------------------------------------- virtual contract (shadow without a deployment)

def test_virtual_override_patches_immutables_and_owner():
    from mev.chain.addresses import AAVE_V3_POOL, MORPHO_BLUE
    from mev.chain.flash_arb_code import IMMUTABLE_OFFSETS
    from mev.execution import virtual
    code = virtual.runtime_code()
    for off in IMMUTABLE_OFFSETS["aave"]:
        assert code[off:off + 32] == bytes(12) + bytes.fromhex(AAVE_V3_POOL[2:])
    for off in IMMUTABLE_OFFSETS["morpho"]:
        assert code[off:off + 32] == bytes(12) + bytes.fromhex(MORPHO_BLUE[2:])
    o = virtual.state_override(OWNER := "0x05094Ba9f83D86ec5F0d8AFf8e032A0F57E36537")
    (entry,) = o.values()
    assert entry["code"] == "0x" + code.hex()
    assert entry["stateDiff"]["0x" + "00" * 32] == "0x" + "00" * 12 + OWNER[2:].lower()


def test_shadow_without_address_uses_virtual_contract_and_estimate_gas_revert_data():
    from mev.execution import virtual
    sel = Web3.keccak(text="InsufficientProfit(uint256,uint256)")[:4]
    w3 = FakeW3(revert="0x" + (sel + (0).to_bytes(32, "big") + (5).to_bytes(32, "big")).hex())
    w3.eth.call_has_data = False
    s = Settings(_env_file=None, execution_mode="shadow", signer_key=TEST_KEY, execution_halt_file="/nonexistent/HALT")   # no flash_arb_address
    e = ExecutionEngine(s, w3, Signer(TEST_KEY), lambda: POOLS)
    assert e.virtual and e.contract == virtual.VIRTUAL_ADDRESS
    asyncio.run(e.startup())
    assert e.owner == Signer(TEST_KEY).address and e.override is not None
    row = submit(e)
    assert row.status == "revert" and row.revert_reason == "InsufficientProfit(0, 5)" and row.contract == virtual.VIRTUAL_ADDRESS
    assert w3.eth.overrides[-1] is e.override and w3.eth.calls[-1]["from"] == e.owner and w3.eth.sent == []


def test_live_never_uses_a_virtual_contract():
    s = Settings(_env_file=None, execution_mode="live", signer_key=TEST_KEY)
    e = ExecutionEngine(s, FakeW3(), Signer(TEST_KEY), lambda: POOLS)
    assert not e.virtual and e.contract is None
    with pytest.raises(RuntimeError):
        asyncio.run(e.startup())
