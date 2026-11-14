"""引擎层：自动交易开关、人工保留、本地持仓四种状态、T+1。全部离线（FakeMarket）。"""
from __future__ import annotations

import pytest

from qfpy.util import read_jsonl
from tests.conftest import FakeMarket, add_position

DAYS = ["2026-11-06", "2026-11-09", "2026-11-10", "2026-11-11"]


def setup(make_engine, mode="paper", price=10.1, today="2026-11-10", auto=True, skip_sl=False, state="confirmed"):
    e = make_engine(mode, skip_sl=skip_sl)
    fm = FakeMarket(today, DAYS, {"600000": (price, 10.0)})
    e.market = lambda: fm
    if auto:
        e.set_auto(True, "pytest")
    add_position(e, day="2026-11-09", state=state)
    return e


def orders(e):
    return read_jsonl(e.data_dir / "orders.jsonl", 1000)


def only(res):
    assert len(res["results"]) == 1
    return res["results"][0]


# ---------------- 自动交易开关 ----------------

def test_auto_switch_default_off_and_persisted(make_engine):
    e = make_engine("paper")
    assert e.switches.auto_enabled() is False          # 首次启动默认关
    e.set_auto(True, "pytest")
    e2 = make_engine("paper")                          # 同一个数据目录重建 = 重启
    assert e2.switches.auto_enabled() is True


def test_auto_off_sell_is_signal_only(make_engine):
    e = setup(make_engine, auto=False)
    ev = only(e.check_exits(now_hm="14:50"))
    assert ev["decision"] == "time" and ev["action"] == "signal_only"
    assert e.book.get("600000")["quantity"] == 500          # 没有真卖
    rec = orders(e)[-1]
    assert rec["status"] == "signal_only" and rec["status_label"].startswith("仅信号")


def test_auto_off_buy_is_signal_only_and_not_sent(make_engine, monkeypatch):
    e = make_engine("paper")
    called = []
    monkeypatch.setattr(e.broker, "place", lambda it: called.append(it) or {"status": "filled"})
    it = {"id": "t-1", "side": "buy", "symbol": "600000", "name": "浦发银行", "price": 10.03, "quantity": 500,
          "ref_price": 10.0, "limit_up": 11.0, "limit_down": 9.0, "trading_date": "2026-11-10", "is_trading_day": True}
    rec = e.submit(it)
    assert rec["status"] == "signal_only" and called == []
    assert e.risk.usage("2026-11-10")["orders"] == 0        # 仅信号不占额度
    assert "通过" in rec["message"]                             # 仍然做风控预检
    e.set_auto(True, "pytest")
    rec = e.submit(it)
    assert rec["status"] == "filled" and len(called) == 1


# ---------------- 人工保留 ----------------

def test_hold_skips_once_then_expires(make_engine):
    e = setup(make_engine)
    e.set_hold("600000", True, "看好")
    ev = only(e.check_exits(now_hm="14:50"))
    assert ev["action"] == "hold_skipped"
    p = e.book.get("600000")
    assert p["quantity"] == 500 and not p.get("hold")      # 没卖，标记已失效
    ev = only(e.check_exits(now_hm="14:50"))
    assert ev["action"] == "filled"
    assert e.book.get("600000") is None
    st = [o["status"] for o in orders(e)]
    assert st == ["hold_skipped", "filled"]


def test_hold_cannot_skip_stop_loss_by_default(make_engine):
    e = setup(make_engine, price=9.5)
    e.set_hold("600000", True)
    ev = only(e.check_exits(now_hm="10:00"))
    assert ev["decision"] == "stop_loss" and ev["action"] == "filled"
    assert "止损不允许被保留跳过" in ev["note"]


def test_hold_can_skip_stop_loss_when_enabled(make_engine):
    e = setup(make_engine, price=9.5, skip_sl=True)
    e.set_hold("600000", True)
    ev = only(e.check_exits(now_hm="10:00"))
    assert ev["action"] == "hold_skipped"


def test_hold_not_consumed_when_auto_off(make_engine):
    e = setup(make_engine, auto=False)
    e.set_hold("600000", True)
    ev = only(e.check_exits(now_hm="14:50"))
    assert ev["action"] == "signal_only"
    assert e.book.get("600000").get("hold")                 # 标记保留


def test_hold_not_consumed_when_halted(make_engine):
    e = setup(make_engine)
    e.set_hold("600000", True)
    e.halt("测试", "pytest")
    ev = only(e.check_exits(now_hm="14:50"))
    assert ev["action"] == "risk_rejected"
    assert e.book.get("600000").get("hold") and e.book.get("600000")["quantity"] == 500
    e.resume("pytest")
    assert only(e.check_exits(now_hm="14:50"))["action"] == "hold_skipped"


def test_reminder_lists_hold_status(make_engine):
    e = setup(make_engine)
    r = e.remind_exits("14:50")
    assert len(r["lines"]) == 1 and "未保留，到点卖出" in r["lines"][0]
    e.set_hold("600000", True)
    e.notifier._recent.clear()
    assert "已标记保留，将跳过这次" in e.remind_exits("14:50")["lines"][0]
    assert e.remind_exits("14:45")["lines"] == []          # 别的退出时间不提醒


# ---------------- T+1 ----------------

def test_t1_bought_today_not_sold(make_engine):
    e = make_engine("paper")
    fm = FakeMarket("2026-11-10", DAYS, {"600000": (9.0, 10.0)})   # 跌 10%，止损触发
    e.market = lambda: fm
    e.set_auto(True, "pytest")
    add_position(e, day="2026-11-10")
    ev = only(e.check_exits(now_hm="10:00"))
    assert ev["decision"] == "stop_loss" and ev["action"] == "t1_locked"
    assert e.book.get("600000")["quantity"] == 500


# ---------------- 本地持仓四种状态（非 paper 模式） ----------------

@pytest.mark.parametrize("state, auto_sells", [
    ("confirmed", True), ("unconfirmed_buy", False), ("selling", False), ("uncertain", False)])
def test_only_confirmed_positions_auto_sell(make_engine, state, auto_sells):
    e = setup(make_engine, mode="dry_run", state=state)
    sent = []
    e.broker.place = lambda it: sent.append(it) or {"status": "dry_run", "message": "演练"}
    ev = only(e.check_exits(now_hm="14:50"))
    assert ev["decision"] == "time"
    if auto_sells:
        assert len(sent) == 1 and sent[0]["side"] == "sell" and sent[0]["quantity"] == 500
    else:
        assert sent == [] and ev["action"] == "manual_needed"
        assert "不自动卖出" in ev["note"]


def test_ledger_confirm_and_manual_sell(make_engine):
    e = setup(make_engine, mode="dry_run", state="uncertain")
    e.book.confirm("600000", 400, 10.05)
    p = e.book.get("600000")
    assert p["state"] == "confirmed" and p["quantity"] == 400 and p["avg_cost"] == pytest.approx(10.05)
    assert e.book.ledger_sell_done("600000", 400, 10.2, "2026-11-10", "人工登记已卖出") == pytest.approx(60.0)
    assert e.book.get("600000") is None
