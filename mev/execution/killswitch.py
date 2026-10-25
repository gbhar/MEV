"""Manual emergency shutdown (spec Phase 7). Three independent switches, any one of which stops live sends:
the halt file (create it next to the process or at MEV_EXECUTION_HALT_FILE), MEV_EXECUTION_HALT=true, or
meta.execution_halt = "1" in the database (set by the agent or `python -m mev.execution.killswitch on`)."""
import sys
from pathlib import Path

from sqlalchemy.orm import Session

from mev.config import Settings
from mev.db.models import Meta

KEY = "execution_halt"


def halted(s: Settings, db: Session) -> str | None:
    """The reason execution is halted, or None."""
    if s.execution_halt:
        return "MEV_EXECUTION_HALT is set"
    if Path(s.execution_halt_file).exists():
        return f"halt file {s.execution_halt_file} exists"
    row = db.get(Meta, KEY)
    if row is not None and row.value.split(" ", 1)[0] == "1":
        return f"meta.{KEY}: {row.value}"
    return None


def set_halt(db: Session, on: bool, reason: str = "") -> None:
    row = db.get(Meta, KEY)
    value = f"1 {reason}".strip() if on else "0"
    if row is None:
        db.add(Meta(key=KEY, value=value))
    else:
        row.value = value
    db.commit()


if __name__ == "__main__":   # python -m mev.execution.killswitch on|off [reason]
    from mev.db.session import init_db, session
    init_db()
    on = (sys.argv[1:2] or ["on"])[0] == "on"
    with session() as db:
        set_halt(db, on, " ".join(sys.argv[2:]))
    print("execution halted" if on else "execution halt cleared")
