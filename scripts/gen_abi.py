"""Regenerate FLASH_ARB in mev/chain/abi.py from the forge artifact (run after `make contract-build`)."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
art = json.loads((ROOT / "contracts/out/FlashArb.sol/FlashArb.json").read_text())
keep = {"execute", "owner", "paused", "setPaused", "setOwner", "rescue", "aave", "morpho"}
abi = [e for e in art["abi"] if e["type"] in ("event", "error") or (e["type"] == "function" and e["name"] in keep)]
p = ROOT / "mev/chain/abi.py"
s = p.read_text()
block = "FLASH_ARB = " + json.dumps(abi, indent=1).replace("true", "True").replace("false", "False") + "\n"
s = re.sub(r"FLASH_ARB = \[.*\]\n", block, s, flags=re.S)
p.write_text(s)
print(f"FLASH_ARB: {len(abi)} entries")
