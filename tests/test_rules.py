from decimal import Decimal

from mev.config import Settings
from mev.risk import rules
from mev.simulator.simulate import SimResult

S = Settings(_env_file=None, max_daily_loss_usd=500.0, max_failed_trades=5)


def sim(net="5", size=1000, gas="0.05", exact=True):
    return SimResult(size, size * 10**6, 10**18, 0, Decimal(net) + Decimal("1"), Decimal("0.5"), Decimal(gas), Decimal("0.2"), Decimal(net), exact)


def test_clean_trade_passes():
    assert rules.check(sim(), 1, 1, S) == []


def test_daily_loss_breaker():
    assert rules.check(sim(), 1, 1, S, rules.Book(daily_loss_usd=Decimal("500"))) == []
    v = rules.check(sim(), 1, 1, S, rules.Book(daily_loss_usd=Decimal("500.01")))
    assert v == ["daily_loss 500.01 > max 500.0"]


def test_failed_trades_breaker():
    assert rules.check(sim(), 1, 1, S, rules.Book(failed_trades=5)) == []
    assert rules.check(sim(), 1, 1, S, rules.Book(failed_trades=6)) == ["failed_trades 6 > max 5"]


def test_pool_busy_and_repeat_block():
    v = rules.check(sim(), 1, 1, S, rules.Book(pool_busy=True, repeat=True))
    assert [x.split(" ")[0].rstrip(":") for x in v] == ["pool_busy", "repeat"]


def test_book_cannot_loosen_other_rules():
    v = rules.check(sim(net="0.5", exact=False), 4, 1, S, rules.Book())   # age 3 > max_state_age_blocks (2)
    assert len(v) == 3 and v[0].startswith("net_profit") and "stale" in v[1] and "not exact" in v[2]
