"""Point the package at a scratch SQLite file before any mev module reads Settings."""
import os
import tempfile

os.environ["MEV_DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp(prefix='mev-test-')}/test.db"

import pytest  # noqa: E402

from mev.db.session import init_db  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _db():
    init_db()
