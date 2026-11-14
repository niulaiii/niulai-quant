"""退出规则：优先级（止损 > 止盈 > 到期）、按股池覆盖、T+1。"""
from __future__ import annotations

import pytest

from qfpy.config import DEFAULTS, deep_merge
from qfpy.exits import evaluate, exit_times, hold_can_skip, rule_for
from qfpy.vendor.tsp_lots import available_of, consume_lots


def cfg(**ex):
    return deep_merge(DEFAULTS, {"exits": {"take_profit_pct": 5, "stop_loss_pct": 3, "max_hold_days": 1, "exit_time": "14:50",
                                           **ex}})


def pos(**kw):
    p = {"symbol": "600000", "name": "浦发银行", "quantity": 500, "avg_cost": 10.0, "sources": ["watchlist:all"],
         "lots": [{"date": "2026-11-09", "qty": 500, "price": 10.0}], "state": "confirmed"}
    p.update(kw)
    return p


def decide(price, now="14:50", held=1, c=None, p=None):
    return evaluate(c or cfg(), p or pos(), price, "2026-11-10", now, held, 500)["decision"]


def test_priority_stop_loss_over_time():
    # 已到期且到点，同时亏损触发止损 → 按止损
    assert decide(9.6) == "stop_loss"


def test_priority_take_profit_over_time():
    assert decide(10.6) == "take_profit"


def test_priority_stop_loss_over_take_profit_when_both_configured_weirdly():
    # 人为构造：止损线设成 -10%（即下跌不到 -(-10)%）、止盈 5%，当涨 6% 时两者都满足，必须报止损
    c = cfg(stop_loss_pct=-10)
    assert decide(10.6, c=c) == "stop_loss"


def test_time_exit_only_at_exit_time():
    assert decide(10.1, now="14:49") is None
    r = evaluate(cfg(), pos(), 10.1, "2026-11-10", "14:49", 1, 500)
    assert r["due_at_exit_time"] is True and "到期卖出" in r["reason"]
    assert decide(10.1, now="14:50") == "time"
    assert decide(10.1, now="14:50", held=0) is None          # 没持有满


def test_tp_sl_any_time():
    assert decide(9.6, now="09:35", held=0) == "stop_loss"
    assert decide(10.6, now="09:35", held=0) == "take_profit"


def test_zero_max_hold_days_disables_time_exit():
    assert decide(10.1, held=30, c=cfg(max_hold_days=0)) is None


def test_no_price_no_decision():
    r = evaluate(cfg(), pos(), None, "2026-11-10", "14:50", 1, 500)
    assert r["decision"] is None and "没有最新价" in r["reason"]


def test_by_pool_override():
    c = cfg(by_pool={"gushi:gold_230": {"max_hold_days": 2, "exit_time": "14:45"}})
    p = pos(sources=["gushi:gold_230", "watchlist:all"])
    r = rule_for(c, p)
    assert r["max_hold_days"] == 2 and r["exit_time"] == "14:45" and r["from"] == "gushi:gold_230"
    assert r["stop_loss_pct"] == 3          # 未覆盖的沿用全局
    assert decide(10.1, now="14:45", held=1, c=c, p=p) is None
    assert decide(10.1, now="14:45", held=2, c=c, p=p) == "time"
    assert exit_times(c) == ["14:45", "14:50"]


def test_hold_can_skip_rules():
    assert hold_can_skip(cfg(), "time") and hold_can_skip(cfg(), "take_profit")
    assert not hold_can_skip(cfg(), "stop_loss")
    assert hold_can_skip(cfg(hold_can_skip_stop_loss=True), "stop_loss")


# ---------------- T+1 ----------------

def test_t1_available_only_lots_bought_before_today():
    p = pos(lots=[{"date": "2026-11-09", "qty": 300, "price": 10}, {"date": "2026-11-10", "qty": 200, "price": 10}])
    assert available_of(p, "2026-11-10") == 300
    assert available_of(p, "2026-11-11") == 500
    assert available_of(p, "2026-11-09") == 0


def test_consume_lots_fifo_and_skips_today():
    lots = [{"date": "2026-11-10", "qty": 200, "price": 10}, {"date": "2026-11-09", "qty": 300, "price": 10}]
    left = consume_lots(lots, 300, "2026-11-10")
    assert left == [{"date": "2026-11-10", "qty": 200, "price": 10}]


@pytest.mark.parametrize("today, expect", [("2026-11-09", 0), ("2026-11-10", 500)])
def test_book_sellable_t1(make_engine, today, expect):
    from tests.conftest import add_position
    e = make_engine("paper")
    add_position(e, day="2026-11-09")
    assert e.book.sellable("600000", today) == expect
