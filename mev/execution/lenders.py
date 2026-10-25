"""Per-trade flash-loan lender choice. Morpho Blue is fee-free but can only lend what sits idle in its singleton
contract per token; Aave V3 charges 5 bp and is the fallback. The book caches Morpho's idle balance per quote token
(one balanceOf per token every `refresh_blocks`) so the simulator can charge the right fee and the engine can put
the right lender into FlashArb.execute(). With flash_lender fixed to one lender the book just returns it."""
import logging

from web3 import AsyncWeb3

from mev.chain.addresses import MORPHO_BLUE
from mev.config import Settings

log = logging.getLogger("mev.lenders")
_ERC20_BALANCE = [{"name": "balanceOf", "type": "function", "stateMutability": "view",
                   "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]}]
LENDER_FEE_BPS = {"aave_v3": None, "morpho_blue": 0}   # None = settings.flash_loan_fee_bps


class LenderBook:
    def __init__(self, s: Settings):
        self.s = s
        self.morpho_idle: dict[str, int] = {}   # token address (lower) -> raw idle balance
        self.refreshed_at: int | None = None

    async def refresh(self, w3: AsyncWeb3, tokens: list[str], block_number: int) -> None:
        if self.s.flash_lender != "auto":
            return
        if self.refreshed_at is not None and block_number - self.refreshed_at < self.s.lender_refresh_blocks:
            return
        for t in {t.lower() for t in tokens}:
            try:
                c = w3.eth.contract(address=AsyncWeb3.to_checksum_address(t), abi=_ERC20_BALANCE)
                self.morpho_idle[t] = await c.functions.balanceOf(AsyncWeb3.to_checksum_address(MORPHO_BLUE)).call(block_identifier=block_number)
            except Exception as e:   # keep the previous value; Aave remains the fallback
                log.warning("morpho idle balance for %s not refreshed: %s", t, str(e)[:80])
        self.refreshed_at = block_number

    def choose(self, token: str, amount: int) -> str:
        """Lender for borrowing `amount` raw units of `token`."""
        if self.s.flash_lender != "auto":
            return self.s.flash_lender
        idle = self.morpho_idle.get(token.lower(), 0)
        return "morpho_blue" if idle >= amount * self.s.lender_morpho_headroom // 100 else "aave_v3"

    def fee_bps(self, token: str, amount: int) -> int:
        fee = LENDER_FEE_BPS[self.choose(token, amount)]
        return self.s.flash_loan_fee_bps if fee is None else fee
