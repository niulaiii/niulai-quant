"""实盘「自动获取持仓」开关：关 = 网页手动填写；开 = 从下单程序快照自动获取。卖出风控不变。全部离线。"""
from __future__ import annotations

import httpx
import pytest

from qfpy.devtools.mock_xiadan import asgi_transport
from qfpy.util import today_str
from tests.test_xiadan_reconcile import LIVE_EXTRA, _real_send, mock  # noqa: F401  (fixture)


@pytest.fixture
def live(make_engine, monkeypatch, mock):  # noqa: F811
    st, app = mock
    monkeypatch.setenv("QF_LIVE_ARMED", "I_ACCEPT_REAL_MONEY_RISK")
    e = make_engine("live", extra=LIVE_EXTRA)
    e.link.client.transport = asgi_transport(app)
    return e, st


def _sell_at_exit(e, fm_prices, hm="14:50"):
    from tests.conftest import FakeMarket
    day = today_str()
    fm = FakeMarket(day, ["2026-11-09", "2026-11-10", day], fm_prices)
    e.market = lambda: fm
    e.set_auto(True, "pytest")
    import qfpy.exec.risk as riskmod
    orig = riskmod.in_sessions
    riskmod.in_sessions = lambda *a, **k: True
    try:
        return e.check_exits(now_hm=hm)
    finally:
        riskmod.in_sessions = orig


def test_default_is_manual_and_persists(live, tmp_path):
    e, _ = live
    assert e.switches.positions_auto() is False
    e.set_positions_auto(True, "pytest")
    from qfpy.exec.state import Switches
    assert Switches(e.data_dir, False).positions_auto() is True          # 重启后保持


def test_manual_upsert_validation_and_lots(live):
    e, _ = live
    p = e.manual_position("600000", {"name": "浦发银行", "quantity": 1000, "avg_cost": 10.5, "sellable": 600})
    assert p["state"] == "confirmed" and p["quantity"] == 1000 and p["origin"] == "manual"
    assert e.book.sellable("600000", today_str()) == 600            # 可卖按手填的，其余 T+1
    for bad, msg in [({"quantity": 0, "avg_cost": 1, "sellable": 0}, "大于 0"),
                     ({"quantity": 100, "avg_cost": 1, "sellable": 200}, "可卖数量"),
                     ({"quantity": 100, "avg_cost": 0, "sellable": 0}, "成本"),
                     ({"quantity": "abc", "avg_cost": 1, "sellable": 0}, "整数")]:
        with pytest.raises(ValueError, match=msg):
            e.manual_position("600000", bad)
    with pytest.raises(ValueError, match="6 位"):
        e.manual_position("60000", {"quantity": 100, "avg_cost": 1, "sellable": 0})
    e.manual_remove("600000")
    assert e.book.get("600000") is None


def test_manual_edit_refused_in_auto_mode_and_paper(live, make_engine):
    e, _ = live
    e.set_positions_auto(True, "pytest")
    with pytest.raises(ValueError, match="自动获取"):
        e.manual_position("600000", {"quantity": 100, "avg_cost": 1, "sellable": 0})
    # 模拟盘现在也可以手填：当作把股票按成本转入模拟账户，初始资金同步加上，收益率不跳变
    paper = make_engine("paper")
    paper.switches.set_positions_auto(False, "pytest")
    init0 = paper.book.summary()["initial_cash"]
    p = paper.manual_position("600000", {"quantity": 100, "avg_cost": 10, "sellable": 100})
    s = paper.book.summary()
    assert p["origin"] == "manual" and s["initial_cash"] == init0 + 1000 and s["total_return_pct"] == 0
    paper.manual_funds({"available": 50000})
    s = paper.book.summary()
    assert s["cash"] == 50000 and s["total_return_pct"] == 0 and s["initial_cash"] == 51000


def test_manual_edit_refused_while_order_pending(live):
    e, st = live
    from tests.test_xiadan_reconcile import item
    e.switches.set_positions_auto(True, "pytest")     # 自动获取持仓时才有对账在途记录
    e.broker.place(item())
    e.switches.set_positions_auto(False, "pytest")
    with pytest.raises(ValueError, match="在途"):
        e.manual_position("600000", {"quantity": 100, "avg_cost": 10, "sellable": 100})


def test_auto_adopts_broker_positions(live):
    e, st = live
    e.manual_position("000001", {"quantity": 100, "avg_cost": 12, "sellable": 100})   # 手填的，券商没有
    st.add_position("600000", "浦发银行", 800, 10.2, 10.5, available=500)
    st.add_position("300825", "阿尔特", 200, 94.5, 95.0)
    e.set_positions_auto(True, "pytest")
    rep = e.link.reconcile_now("fresh")
    p = e.book.get("600000")
    assert p["quantity"] == 800 and abs(p["avg_cost"] - 10.2) < 1e-6 and p["state"] == "confirmed" and p["origin"] == "broker"
    assert e.book.sellable("600000", today_str()) == 500
    assert e.book.get("300825")["quantity"] == 200
    assert e.book.get("000001") is None                                   # 券商没有 → 删除
    kinds = sorted(c["kind"] for c in rep["adopted"])
    assert kinds == ["added", "added", "removed"]
    # 券商卖了一部分 → 下次刷新跟着变
    st.positions["600000"]["qty"] = 300
    st.positions["600000"]["available"] = 300
    e.link.reconcile_now("fresh")
    assert e.book.get("600000")["quantity"] == 300
    assert e.link.view()["adopt_log"]
    assert st.trade_calls == [] and st.forbidden_hits == []


def test_auto_off_does_not_adopt(live):
    e, st = live
    st.add_position("600000", "浦发银行", 800, 10.2, 10.5)
    e.link.reconcile_now("fresh")
    assert e.book.get("600000") is None


def test_auto_does_not_delete_when_positions_untrustworthy(live):
    e, st = live
    e.manual_position("600000", {"quantity": 100, "avg_cost": 10, "sellable": 100})
    st.add_position("600000", "浦发银行", 100, 10, 10)
    st.empty_positions = True            # 持仓表读不到
    e.set_positions_auto(True, "pytest")
    e.link.reconcile_now("fresh")
    assert e.book.get("600000") is not None


def test_manual_mode_sell_respects_sellable(live):
    """手动模式：到期卖出不超过手填的可卖数量，也不超过券商可卖。"""
    e, st = live
    e.manual_position("600000", {"name": "浦发银行", "quantity": 500, "avg_cost": 10, "sellable": 200})
    st.add_position("600000", "浦发银行", 500, 10.0, 10.0, available=500)
    e.book.get("600000")["first_buy_date"] = "2026-11-01"
    e.cfg["exits"]["max_hold_days"] = 1
    out = _sell_at_exit(e, {"600000": (10.0, 10.0)})
    sells = [c for c in st.trade_calls if c["path"] == "/api/order"]
    res = out["results"][0]
    assert res["sellable"] == 200
    assert len(sells) == 1 and sells[0]["body"]["quantity"] == "200" and st.forbidden_hits == []


def test_auto_mode_sell_capped_by_broker_available(live):
    e, st = live
    st.add_position("600000", "浦发银行", 500, 10.0, 10.0, available=300)
    e.set_positions_auto(True, "pytest")
    e.link.reconcile_now("fresh")
    e.book.get("600000")["first_buy_date"] = "2026-11-01"
    e.cfg["exits"]["max_hold_days"] = 1
    out = _sell_at_exit(e, {"600000": (10.0, 10.0)})
    sells = [c for c in st.trade_calls if c["path"] == "/api/order"]
    assert out["results"][0]["sellable"] == 300
    assert len(sells) == 1 and sells[0]["body"]["quantity"] == "300"


def test_web_positions_mode_endpoints(live, monkeypatch):
    from fastapi.testclient import TestClient

    from qfpy.web.server import create_app
    e, st = live
    monkeypatch.setattr(httpx.Client, "send", _real_send)
    c = TestClient(create_app(e))
    st.add_position("600000", "浦发银行", 300, 10.0, 10.0)      # 打开自动获取时会后台同步一次，券商也有这只
    assert c.get("/api/positions_mode").json()["auto"] is False
    r = c.post("/api/positions/manual", json={"code": "600000", "name": "浦发银行", "quantity": 300, "avg_cost": 10, "sellable": 300})
    assert r.status_code == 200 and r.json()["quantity"] == 300
    assert c.post("/api/positions/manual", json={"code": "600000", "quantity": 300, "avg_cost": 10, "sellable": 900}).status_code == 400
    assert c.post("/api/positions_mode", json={"auto": True}).json()["auto"] is True
    assert c.post("/api/positions/manual", json={"code": "600000", "quantity": 1, "avg_cost": 1, "sellable": 0}).status_code == 400
    assert c.delete("/api/positions/manual/600000").status_code == 400
    import time
    for _ in range(50):
        if not e.link.view().get("sync", {}).get("running"):
            break
        time.sleep(0.05)
    c.post("/api/positions_mode", json={"auto": False})
    assert c.delete("/api/positions/manual/600000").status_code == 200
    assert c.delete("/api/positions/manual/600000").status_code == 404
