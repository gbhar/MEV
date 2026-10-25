"""Virtual FlashArb for shadow mode without a deployment (Phase 6 at zero cost).

Geth-style state overrides let eth_call / eth_estimateGas run against a modified world: here the FlashArb runtime
bytecode is placed at an address nobody uses and the owner slot is set to the engine's `from` address. Aave, the
pools and the tokens are the real ones, so the simulation answers exactly what a deployed contract would, minus the
deployment. Alchemy honours overrides on both calls but returns revert data only from eth_estimateGas."""
from web3 import Web3

from mev.chain.addresses import AAVE_V3_POOL, MORPHO_BLUE
from mev.chain.flash_arb_code import DEPLOYED_CODE, IMMUTABLE_OFFSETS, OWNER_SLOT

VIRTUAL_ADDRESS = Web3.to_checksum_address("0x" + "f1a5" * 10)   # no code on Base; ours is injected per call


def runtime_code(aave: str = AAVE_V3_POOL, morpho: str = MORPHO_BLUE) -> bytes:
    """The compiled runtime with the two immutables written where the constructor would have put them."""
    code = bytearray(bytes.fromhex(DEPLOYED_CODE[2:]))
    for name, addr in (("aave", aave), ("morpho", morpho)):
        word = bytes(12) + bytes.fromhex(addr[2:])
        for off in IMMUTABLE_OFFSETS[name]:
            code[off:off + 32] = word
    return bytes(code)


def state_override(owner: str, address: str = VIRTUAL_ADDRESS) -> dict:
    """eth_call/eth_estimateGas state override: our code at `address`, `owner` in slot 0 (paused = false)."""
    slot = "0x" + OWNER_SLOT.to_bytes(32, "big").hex()
    return {address: {"code": "0x" + runtime_code().hex(), "stateDiff": {slot: "0x" + bytes(12).hex() + owner[2:].lower()}}}
