"""Signer wallet for live execution (spec Phase 7).

    python scripts/wallet.py create            # new key -> Kubernetes secret mev-signer (key MEV_SIGNER_KEY); prints the address
    python scripts/wallet.py create --file ~/.mev-signer.key   # or: write the key to a mode-600 file instead of the cluster
    python scripts/wallet.py address           # the signer address (from the secret or MEV_SIGNER_KEY), never the key
    python scripts/wallet.py balance           # ETH balance of the signer on Base

The key is generated here, handed straight to kubectl's stdin (or the file) and never printed, logged, committed
or stored in the database. Fund the printed address with a little ETH on Base (gas only; the trade capital is the
flash loan). `make k8s-apply` mounts the secret into the loop as MEV_SIGNER_KEY."""
import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mev.execution.signer import Signer, generate, load_signer  # noqa: E402

NS = os.environ.get("K8S_NS", "mev-platform")
SECRET = "mev-signer"


def kubectl(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["kubectl", "-n", NS, *args], input=stdin, text=True, capture_output=True)


def secret_exists() -> bool:
    return kubectl("get", "secret", SECRET).returncode == 0


def key_from_secret() -> str | None:
    r = kubectl("get", "secret", SECRET, "-o", "go-template={{index .data \"MEV_SIGNER_KEY\" | base64decode}}")
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def create(args) -> int:
    if args.file is None and secret_exists() and not args.force:
        print(f"secret {SECRET} already exists in {NS}; use --force to replace it (the old key is lost: sweep its ETH first)")
        return 1
    address, key = generate()
    if args.file:
        path = Path(args.file).expanduser()
        path.touch(mode=0o600)
        path.write_text(key + "\n")
        print(f"key written to {path} (mode 600). Set MEV_SIGNER_KEY_FILE={path}")
    else:
        manifest = kubectl("create", "secret", "generic", SECRET, "--from-literal=MEV_SIGNER_KEY=" + key, "--dry-run=client", "-o", "yaml")
        if manifest.returncode != 0:
            print(manifest.stderr)
            return 1
        r = kubectl("apply", "-f", "-", stdin=manifest.stdout)
        if r.returncode != 0:
            print(r.stderr)
            return 1
        print(f"secret {SECRET} written in namespace {NS}")
    print(f"signer address: {address}")
    print("fund it with ~0.01 ETH on Base (chain 8453) for gas; the contract owner will be this address")
    return 0


def resolve_signer() -> Signer | None:
    from mev.config import settings
    s = load_signer(settings)
    if s is None:
        key = key_from_secret()
        s = Signer(key) if key else None
    return s


def address(args) -> int:
    s = resolve_signer()
    if s is None:
        print("no signer: create one with `python scripts/wallet.py create`")
        return 1
    print(s.address)
    return 0


def balance(args) -> int:
    s = resolve_signer()
    if s is None:
        print("no signer")
        return 1
    from mev.chain.rpc import make_web3

    async def go():
        w3 = make_web3()
        wei = await w3.eth.get_balance(s.address)
        print(f"{s.address}: {wei / 1e18:.6f} ETH")
    asyncio.run(go())
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("--file", help="write the key to this file instead of the Kubernetes secret")
    c.add_argument("--force", action="store_true", help="replace an existing secret")
    c.set_defaults(fn=create)
    sub.add_parser("address").set_defaults(fn=address)
    sub.add_parser("balance").set_defaults(fn=balance)
    a = ap.parse_args()
    sys.exit(a.fn(a))
