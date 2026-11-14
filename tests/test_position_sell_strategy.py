"""持仓级卖出策略：手动给某一只票指定卖法，优先级高于股池绑定。全部离线。"""
from __future__ import annotations

import copy

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeMarket, add_position

_real_send = httpx.Client.send


@pytest.fixture(autouse=True)
def _allow_testclient(monkeypatch):
    monkeypatch.setattr(httpx.Client, "send", _real_send)


DAYS = ["2026-11-06", "2026-11-09", "2026-11-10"]


def _engine(make_engine):
    e = make_engine("paper")
    fm = FakeMarket("2026-11-10", DAYS, {"600000": (10.2, 10.0)})
    fm.daily_series = lambda code, day, fields: ([], "无", False)
    e.market = lambda: fm
    e.notifier.notify = lambda *a, **kw: None
    e.strategies.ensure()
    return e


def _new_sell(e, name, **exits):
    act = e.strategies.active_names()["sell"]["id"]
    content = copy.deepcopy(e.strategies.get("sell", act)["content"])
    content["exits"].update(exits)
    return e.strategies.create("sell", name, content)


def test_forced_strategy_wins_over_pool_binding(make_engine):
    e = _engine(make_engine)
    bound = _new_sell(e, "池绑定卖出", stop_loss_pct=8.0)
    forced = _new_sell(e, "个股专用卖出", stop_loss_pct=2.0, take_profit_pct=3.0)
    e.cfg["pool"]["bind"] = {"watchlist:all": {"sell": bound["id"]}}
    add_position(e, code="600000", qty=500, day="2026-11-09")

    pos = e.book.get("600000")
    plan = e._sell_plan(e.binder(), {}, pos)
    assert plan["strategy"] == "池绑定卖出" and plan["pool"] == "watchlist:all"

    e.set_position_sell_strategy("600000", forced["id"], by="pytest")
    pos = e.book.get("600000")
    plan = e._sell_plan(e.binder(), {}, pos)
    assert plan["strategy"] == "个股专用卖出" and plan["forced"] is True
    assert plan["cfg"]["exits"]["stop_loss_pct"] == 2.0
    assert "持仓手动指定" in plan["by"]


def test_forced_strategy_missing_falls_back_to_pool(make_engine):
    e = _engine(make_engine)
    bound = _new_sell(e, "池绑定卖出", stop_loss_pct=8.0)
    e.cfg["pool"]["bind"] = {"watchlist:all": {"sell": bound["id"]}}
    add_position(e, code="600000", qty=500, day="2026-11-09")
    # 直接写一个不存在的 id（模拟策略被删掉）
    e.book.set_sell_strategy("600000", "s-不存在", by="pytest")

    plan = e._sell_plan(e.binder(), {}, e.book.get("600000"))
    assert plan["strategy"] == "池绑定卖出" and plan["forced"] is False
    assert plan.get("broken") == "s-不存在" and "找不到" in plan["by"]


def test_clear_restores_pool_binding(make_engine):
    e = _engine(make_engine)
    bound = _new_sell(e, "池绑定卖出", stop_loss_pct=8.0)
    forced = _new_sell(e, "个股专用卖出", stop_loss_pct=2.0)
    e.cfg["pool"]["bind"] = {"watchlist:all": {"sell": bound["id"]}}
    add_position(e, code="600000", qty=500, day="2026-11-09")

    e.set_position_sell_strategy("600000", forced["id"])
    assert e._sell_plan(e.binder(), {}, e.book.get("600000"))["strategy"] == "个股专用卖出"
    e.set_position_sell_strategy("600000", "")
    plan = e._sell_plan(e.binder(), {}, e.book.get("600000"))
    assert plan["strategy"] == "池绑定卖出" and plan["forced"] is False
    assert e.book.get("600000")["sell_strategy_set"] is None


def test_manual_position_can_pick_its_own_strategy(make_engine):
    """手动填写的持仓没有来源股池，默认跟生效策略；指定后按指定的卖。"""
    e = _engine(make_engine)
    forced = _new_sell(e, "手动专用卖出", stop_loss_pct=1.5)
    e.book.manual_upsert("600000", "浦发银行", 300, 10.0, 300, "2026-11-09")

    plan = e._sell_plan(e.binder(), {}, e.book.get("600000"))
    assert plan["pool"] is None and plan["forced"] is False

    e.set_position_sell_strategy("600000", forced["id"])
    plan = e._sell_plan(e.binder(), {}, e.book.get("600000"))
    assert plan["strategy"] == "手动专用卖出" and plan["cfg"]["exits"]["stop_loss_pct"] == 1.5


def test_bound_sell_contents_includes_forced_for_scheduler(make_engine):
    """定时任务要排到「到点卖出」的时刻，所以持仓指定的策略也必须收进来。"""
    from qfpy.exits import exit_times

    e = _engine(make_engine)
    forced = _new_sell(e, "手动专用卖出", exit_time="13:07")
    add_position(e, code="600000", qty=500, day="2026-11-09")
    assert "13:07" not in exit_times(e.cfg, e.bound_sell_contents())

    e.set_position_sell_strategy("600000", forced["id"])
    assert "13:07" in exit_times(e.cfg, e.bound_sell_contents())


def test_api_set_and_clear(make_engine):
    from qfpy.web.server import create_app
    e = _engine(make_engine)
    forced = _new_sell(e, "个股专用卖出", stop_loss_pct=2.0)
    add_position(e, code="600000", qty=500, day="2026-11-09")
    c = TestClient(create_app(e))

    r = c.post("/api/positions/600000/sell_strategy", json={"strategy_id": forced["id"]})
    assert r.status_code == 200 and r.json()["strategy"]["name"] == "个股专用卖出"
    assert e.book.get("600000")["sell_strategy_id"] == forced["id"]

    r = c.post("/api/positions/600000/sell_strategy", json={"strategy_id": ""})
    assert r.status_code == 200 and r.json()["strategy"] is None
    assert e.book.get("600000")["sell_strategy_id"] == ""

    assert c.post("/api/positions/999999/sell_strategy", json={"strategy_id": ""}).status_code == 404
    assert c.post("/api/positions/600000/sell_strategy", json={"strategy_id": "s-nope"}).status_code == 400
