"""Local key signer. The key arrives through MEV_SIGNER_KEY (Kubernetes secret `mev-signer`, written by
scripts/wallet.py) or MEV_SIGNER_KEY_FILE and lives only inside this process. Nothing here logs, stores or
returns it; repr() shows the address only."""
from pathlib import Path

from eth_account import Account
from eth_account.signers.local import LocalAccount

from mev.config import Settings


class Signer:
    def __init__(self, key_hex: str):
        self._acct: LocalAccount = Account.from_key(key_hex.strip())

    @property
    def address(self) -> str:
        return self._acct.address

    def sign(self, tx: dict) -> bytes:
        """tx is an EIP-1559 dict without `from`; returns the raw transaction bytes."""
        return bytes(self._acct.sign_transaction(tx).raw_transaction)

    def __repr__(self) -> str:
        return f"Signer({self.address})"


def load_signer(s: Settings) -> Signer | None:
    key = s.signer_key
    if not key and s.signer_key_file:
        key = Path(s.signer_key_file).read_text()
    return Signer(key) if key else None


def generate() -> tuple[str, str]:
    """A fresh (address, hex key). The caller decides where the key goes; it is never written here."""
    acct = Account.create()
    return acct.address, acct.key.hex()
