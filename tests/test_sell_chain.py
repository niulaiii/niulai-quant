"""卖出因子链：自定义层级、卖出因子、可视化结果，以及和止损优先/人工保留/T+1/只卖已确认的交互。全部离线。"""
from __future__ import annotations

import pytest

from qfpy import configsvc
from qfpy.exits import evaluate, exit_times
from qfpy.util import read_jsonl
from tests.conftest import FakeMarket, add_position

DAYS = ["2026-11-06", "2026-11-09", "2026-11-10", "2026-11-11"]


class MinuteMarket(FakeMarket):
    """带分时形态的假行情：高点 high，现价 price，均价 avg。"""

    def __init__(self, today, price, preclose=10.0, high=None, avg=None):
        super().__init__(today, DAYS, {"600000": (price, preclose)})
        self.hi, self.avg = high or price, avg or price

    def minute(self, code, day):
        p, pc = self.prices[code]
        pts = [{"t": "09:31", "p": pc, "avg": self.avg, "vol": 100, "amt": 100 * pc},
               {"t": "10:30", "p": self.hi, "avg": self.avg, "vol": 100, "amt": 100 * self.hi},
               {"t": "14:00", "p": p, "avg": self.avg, "vol": 100, "amt": 100 * p}]
        return {"points": pts, "preclose": pc}, None

    def daily_series(self, code, day, fields):
        return [], "无日K", False


def eng(make_engine, layers_toml="", price=10.1, high=None, avg=None, skip_sl=False, mode="paper", state="confirmed",
        day="2026-11-09", today="2026-11-10"):
    e = make_engine(mode, skip_sl=skip_sl, extra=layers_toml)
    fm = MinuteMarket(today, price, high=high, avg=avg)
    e.market = lambda: fm
    e.set_auto(True, "pytest")
    add_position(e, day=day, state=state)
    return e


def only(res):
    assert len(res["results"]) == 1
    return res["results"][0]


TRAIL = '''
[[sell_funnel.layers]]
id = "stop_loss"
name = "止损"
mode = "any"
[[sell_funnel.layers.factors]]
kind = "stop_loss"

[[sell_funnel.layers]]
id = "weak"
name = "走弱（任一）"
mode = "any"
[[sell_funnel.layers.factors]]
kind = "drawdown_from_high"
params = { pct = 3.0, basis = "intraday" }
[[sell_funnel.layers.factors]]
kind = "below_vwap"
params = { min_below_pct = 1.0 }

[[sell_funnel.layers]]
id = "expr"
name = "表达式"
mode = "all"
[[sell_funnel.layers.factors]]
kind = "expr"
params = { expr = "pnl_pct > 1 and held_days >= 1" }
'''


# ---------------- 默认卖出链 = 原来的固定规则 ----------------

def test_default_chain_equals_old_rules(make_engine):
    e = make_engine("paper")
    pos = {"symbol": "600000", "name": "x", "quantity": 500, "avg_cost": 10.0, "sources": ["watchlist:all"]}
    cfg = e.cfg
    assert evaluate(cfg, pos, 9.6, "2026-11-10", "10:00", 1, 500)["decision"] == "stop_loss"
    assert evaluate(cfg, pos, 10.6, "2026-11-10", "10:00", 1, 500)["decision"] == "take_profit"
    ev = evaluate(cfg, pos, 10.1, "2026-11-10", "10:00", 1, 500)
    assert ev["decision"] is None and ev["due_at_exit_time"] and ev["due_at"] == "14:50"
    assert evaluate(cfg, pos, 10.1, "2026-11-10", "14:50", 1, 500)["decision"] == "time"
    assert evaluate(cfg, pos, 10.1, "2026-11-10", "14:50", 0, 500)["decision"] is None
    # 按股池覆盖：黄金两点半持有 2 天、14:45 卖
    gp = {**pos, "sources": ["gushi:gold_230"]}
    assert evaluate(cfg, gp, 10.1, "2026-11-10", "14:50", 1, 500)["decision"] is None
    assert evaluate(cfg, gp, 10.1, "2026-11-10", "14:45", 2, 500)["decision"] == "time"
    assert exit_times(cfg) == ["14:45", "14:50"]


def test_layer_details_for_visualization(make_engine):
    e = make_engine("paper")
    pos = {"symbol": "600000", "name": "x", "quantity": 500, "avg_cost": 10.0, "sources": []}
    ev = evaluate(e.cfg, pos, 10.6, "2026-11-10", "10:00", 1, 500)
    assert [l["id"] for l in ev["layers"]] == ["stop_loss", "take_profit", "time"]
    tp = ev["layers"][1]
    assert tp["triggered"] and tp["checks"][0]["kind"] == "take_profit" and "触发止盈" in tp["checks"][0]["note"]
    assert ev["decision_label"] == "止盈" and "6.00%" in ev["reason"]


# ---------------- 卖出因子 ----------------

def test_drawdown_from_high_triggers(make_engine):
    e = eng(make_engine, TRAIL, price=10.15, high=10.6, avg=10.2)
    ev = only(e.check_exits(now_hm="11:00"))
    assert ev["decision"] == "weak" and ev["action"] == "filled"
    lay = {l["id"]: l for l in ev["layers"]}
    assert lay["weak"]["checks"][0]["passed"] is True and lay["weak"]["checks"][1]["passed"] is False


def test_below_vwap_triggers(make_engine):
    e = eng(make_engine, TRAIL, price=10.05, high=10.1, avg=10.3)
    ev = only(e.check_exits(now_hm="11:00"))
    assert ev["decision"] == "weak" and any("均价" in c["note"] for c in ev["layers"][1]["checks"] if c["passed"])


def test_expr_sell_factor_uses_position_fields(make_engine):
    e = eng(make_engine, TRAIL, price=10.2, high=10.2, avg=10.1)
    ev = only(e.check_exits(now_hm="11:00"))
    assert ev["decision"] == "expr"          # 浮盈 2% > 1 且持有 1 天


def test_nothing_triggers(make_engine):
    e = eng(make_engine, TRAIL, price=10.05, high=10.1, avg=10.0)
    ev = only(e.check_exits(now_hm="11:00"))
    assert ev["decision"] is None and ev["action"] is None
    assert e.book.get("600000")["quantity"] == 500


# ---------------- 和止损/保留/T+1/状态的交互 ----------------

SL_LAST = '''
[[sell_funnel.layers]]
id = "weak"
name = "走弱"
mode = "any"
[[sell_funnel.layers.factors]]
kind = "below_vwap"

[[sell_funnel.layers]]
id = "sl"
name = "我的止损"
mode = "any"
[[sell_funnel.layers.factors]]
kind = "stop_loss"
params = { pct = 2.0 }
'''


def test_stop_loss_layer_always_first(make_engine):
    """止损层写在最后也最先检查：同时触发时卖出原因是止损。"""
    e = eng(make_engine, SL_LAST, price=9.7, avg=10.0)
    ev = only(e.check_exits(now_hm="11:00"))
    assert ev["decision"] == "sl" and ev["stop_loss"] and ev["layers"][0]["id"] == "sl"


def test_hold_cannot_skip_custom_stop_loss(make_engine):
    e = eng(make_engine, SL_LAST, price=9.7, avg=10.0)
    e.set_hold("600000", True)
    ev = only(e.check_exits(now_hm="11:00"))
    assert ev["action"] == "filled" and "止损不允许被保留跳过" in ev["note"]


def test_hold_can_skip_stop_loss_when_enabled(make_engine):
    e = eng(make_engine, SL_LAST, price=9.7, avg=10.0, skip_sl=True)
    e.set_hold("600000", True)
    assert only(e.check_exits(now_hm="11:00"))["action"] == "hold_skipped"


def test_hold_skips_custom_sell_layer_once(make_engine):
    e = eng(make_engine, TRAIL, price=10.15, high=10.6, avg=10.2)
    e.set_hold("600000", True, "再拿一天")
    ev = only(e.check_exits(now_hm="11:00"))
    assert ev["decision"] == "weak" and ev["action"] == "hold_skipped" and not e.book.get("600000").get("hold")
    assert only(e.check_exits(now_hm="11:05"))["action"] == "filled"
    rows = read_jsonl(e.data_dir / "orders.jsonl", 100)
    assert [r["status"] for r in rows] == ["hold_skipped", "filled"] and rows[1]["sell_layer"] == "weak"


def test_layer_hold_skippable_false(make_engine):
    toml = TRAIL.replace('name = "走弱（任一）"', 'name = "走弱（任一）"\nhold_skippable = false')
    e = eng(make_engine, toml, price=10.15, high=10.6, avg=10.2)
    e.set_hold("600000", True)
    ev = only(e.check_exits(now_hm="11:00"))
    assert ev["action"] == "filled" and "不可被保留跳过" in ev["note"]


def test_t1_blocks_custom_sell_layer(make_engine):
    e = eng(make_engine, TRAIL, price=10.15, high=10.6, avg=10.2, day="2026-11-10")
    ev = only(e.check_exits(now_hm="11:00"))
    assert ev["decision"] == "weak" and ev["action"] == "t1_locked" and e.book.get("600000")["quantity"] == 500


@pytest.mark.parametrize("state", ["pending_buy", "pending_sell", "unknown"])
def test_live_only_confirmed_sold_by_chain(make_engine, state):
    e = eng(make_engine, TRAIL, price=10.15, high=10.6, avg=10.2, mode="dry_run", state=state)
    ev = only(e.check_exits(now_hm="11:00"))
    assert ev["decision"] == "weak" and ev["action"] == "manual_needed"


def test_auto_off_chain_records_signal_only(make_engine):
    e = eng(make_engine, TRAIL, price=10.15, high=10.6, avg=10.2)
    e.set_auto(False, "pytest")
    ev = only(e.check_exits(now_hm="11:00"))
    assert ev["action"] == "signal_only" and e.book.get("600000")["quantity"] == 500


def test_custom_time_reached_drives_schedule_and_reminder(make_engine):
    toml = '''
[[sell_funnel.layers]]
id = "t"
name = "隔日 14:40"
[[sell_funnel.layers.factors]]
kind = "hold_days"
params = { min_days = 1 }
[[sell_funnel.layers.factors]]
kind = "time_reached"
params = { at = "14:40" }
'''
    e = eng(make_engine, toml, price=10.1)
    assert exit_times(e.cfg) == ["14:40"]
    pv = e.preview_exits(now_hm="10:00")   # 固定时刻，避免下午跑测试时已过 14:40
    assert pv[0]["due_at_exit_time"] and pv[0]["due_at"] == "14:40"
    r = e.remind_exits("14:40")
    assert len(r["lines"]) == 1 and "隔日 14:40" in r["lines"][0]
    assert only(e.check_exits(now_hm="14:39"))["action"] is None
    assert only(e.check_exits(now_hm="14:40"))["action"] == "filled"


def test_sell_template_apply_and_run(make_engine):
    e = eng(make_engine, "", price=10.6, high=10.6)
    configsvc.apply_template(e, "sell", "next_day_close")
    ev = only(e.check_exits(now_hm="10:00"))
    assert ev["decision"] == "take_profit" and ev["decision_label"] == "止盈或回撤止盈"


def test_sell_rejects_buy_only_factor(make_engine):
    e = make_engine("paper")
    with pytest.raises(configsvc.ConfigError):
        configsvc.put_funnel(e, "sell", [{"name": "x", "factors": [{"kind": "min_hits"}]}])
