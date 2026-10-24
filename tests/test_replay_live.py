"""Opt-in (MEV_LIVE_TESTS=1): replay three recent blocks twice into scratch databases and compare every table."""
import asyncio
import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("MEV_LIVE_TESTS"), reason="set MEV_LIVE_TESTS=1 to hit the public RPC")
TABLES = ["blocks", "pool_snapshots", "swaps", "opportunities", "simulations", "features", "paper_trades"]
VOLATILE = {"id", "created_at", "fetched_at", "rpc_latency_ms"}


def digest(path: str, table: str) -> tuple[int, str]:
    con = sqlite3.connect(path)
    cols = [r[1] for r in con.execute(f"pragma table_info({table})") if r[1] not in VOLATILE]
    rows = con.execute(f"select {', '.join(cols)} from {table} order by {', '.join(cols[:3])}").fetchall()
    return len(rows), hashlib.md5(repr(rows).encode()).hexdigest()


def test_replay_is_reproducible():
    from mev.chain.rpc import make_web3
    head = asyncio.run(make_web3().eth.block_number) - 20
    tmp = tempfile.mkdtemp(prefix="mev-replay-")
    paths = [f"{tmp}/r{i}.db" for i in (1, 2)]
    for p in paths:
        subprocess.run([sys.executable, "-m", "mev.replay", "--from-block", str(head), "--to-block", str(head + 2),
                        "--database-url", f"sqlite:///{p}"], check=True, capture_output=True)
    for t in TABLES:
        a, b = digest(paths[0], t), digest(paths[1], t)
        assert a == b, (t, a, b)
    assert digest(paths[0], "blocks")[0] == 3
