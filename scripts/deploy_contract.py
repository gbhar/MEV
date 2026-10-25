"""Deploy FlashArb to Base with the platform signer (spec Phase 5).

    make contract-build                      # forge build -> contracts/out/FlashArb.sol/FlashArb.json
    python scripts/deploy_contract.py        # signer from MEV_SIGNER_KEY / MEV_SIGNER_KEY_FILE / secret mev-signer
    python scripts/deploy_contract.py --dry-run   # estimate gas only

Prints the contract address; put it in .env / the mev-env secret as MEV_FLASH_ARB_ADDRESS. The deployer becomes
the owner, so it must be the signer the loop will use. The Aave V3 pool and Morpho Blue addresses are the Base
mainnet ones from mev.chain.addresses."""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mev.chain.addresses import AAVE_V3_POOL, CHAIN_ID, MORPHO_BLUE  # noqa: E402
from mev.chain.rpc import make_web3  # noqa: E402
from mev.config import settings  # noqa: E402

ARTIFACT = Path(__file__).resolve().parents[1] / "contracts/out/FlashArb.sol/FlashArb.json"


async def main(dry_run: bool) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from wallet import resolve_signer
    signer = resolve_signer()
    if signer is None:
        print("no signer; run scripts/wallet.py create first")
        return 1
    if not ARTIFACT.exists():
        print(f"{ARTIFACT} missing; run `make contract-build`")
        return 1
    art = json.loads(ARTIFACT.read_text())
    w3 = make_web3()
    chain_id = await w3.eth.chain_id
    if chain_id != CHAIN_ID:
        print(f"RPC is chain {chain_id}, expected Base ({CHAIN_ID})")
        return 1
    contract = w3.eth.contract(abi=art["abi"], bytecode=art["bytecode"]["object"])
    data = contract.constructor(w3.to_checksum_address(AAVE_V3_POOL), w3.to_checksum_address(MORPHO_BLUE)).data_in_transaction
    balance, nonce, block = await asyncio.gather(w3.eth.get_balance(signer.address), w3.eth.get_transaction_count(signer.address), w3.eth.get_block("latest"))
    gas = await w3.eth.estimate_gas({"from": signer.address, "data": data})
    base_fee = block["baseFeePerGas"]
    max_fee = 2 * base_fee + settings.priority_fee_wei
    print(f"signer {signer.address} balance {balance / 1e18:.6f} ETH, nonce {nonce}; deploy gas {gas} (~{gas * max_fee / 1e18:.6f} ETH max)")
    if dry_run:
        return 0
    if balance < gas * max_fee:
        print("insufficient ETH for the deployment")
        return 1
    tx = {"chainId": CHAIN_ID, "nonce": nonce, "data": data, "value": 0, "gas": int(gas * 1.2), "type": 2,
          "maxFeePerGas": max_fee, "maxPriorityFeePerGas": settings.priority_fee_wei}
    tx_hash = await w3.eth.send_raw_transaction(signer.sign(tx))
    print(f"sent {tx_hash.hex()}")
    receipt = await w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt["status"] != 1:
        print("deployment reverted")
        return 1
    addr = receipt["contractAddress"]
    print(f"FlashArb deployed at {addr} (block {receipt['blockNumber']}, gas {receipt['gasUsed']})")
    print(f"next: MEV_FLASH_ARB_ADDRESS={addr} in .env, then `make k8s-secret`; MEV_EXECUTION_MODE=shadow first")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    sys.exit(asyncio.run(main(ap.parse_args().dry_run)))
