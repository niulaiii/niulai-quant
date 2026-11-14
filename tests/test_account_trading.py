"""账户页：手动资金、委托记录（系统委托 + 手动补记/修改、成交入账）、手动下单/撤单、合并后的自动交易开关、
自动获取持仓关闭时不读券商数据。全部离线：下单程序是进程内的模拟服务，不走网络。"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from qfpy.devtools.mock_xiadan import MockState, asgi_transport, make_app
from qfpy.util import today_str
from tests.conftest import add_position
from tests.test_xiadan_reconcile import LIVE_EXTRA, _real_send


class FakeMarket:
    """只给手动下单用的行情：现价 10.00，昨收 10.00，今天是交易日。"""
    def __init__(self, price=10.0, pre=10.0):
        self.price, self.pre = price, pre

    def quotes(self, codes):
        return {c: {"code": c, "name": "浦发银行" if c == "600000" else "测试股", "price": self.price, "preclose": self.pre}
                for c in codes}, "fake", None

    def is_trading_day(self, d=None):
        return True

    def __getattr__(self, name):
        raise RuntimeError("fake market")


@pytest.fixture
def sessions_open(monkeypatch):
    import qfpy.exec.risk as risk
    monkeypatch.setattr(risk, "in_sessions", lambda now, s: True)


@pytest.fixture
def paper(make_engine, monkeypatch, sessions_open):
    from qfpy.web.server import create_app
    monkeypatch.setattr(httpx.Client, "send", _real_send)
    e = make_engine("paper")
    monkeypatch.setattr(e, "market", lambda: FakeMarket())
    sent = []
    monkeypatch.setattr(e.notifier, "notify", lambda ev, title, text, **kw: sent.append((ev, title, text)))
    return e, TestClient(create_app(e)), sent


@pytest.fixture
def live(make_engine, monkeypatch, sessions_open):
    """实盘模式 + 进程内模拟下单程序（/api/order、/api/cancel 只打到这个模拟服务）。"""
    from qfpy.web.server import create_app
    monkeypatch.setattr(httpx.Client, "send", _real_send)
    st = MockState("htfs")
    e = make_engine("live", extra=LIVE_EXTRA)
    e.link.client.transport = asgi_transport(make_app(st))
    monkeypatch.setattr(e, "market", lambda: FakeMarket())
    sent = []
    monkeypatch.setattr(e.notifier, "notify", lambda ev, title, text, **kw: sent.append((ev, title, text)))
    return e, TestClient(create_app(e)), st, sent


# ---------------- 手动资金 / 手动委托入账 ----------------

def test_manual_funds_and_manual_fill_update_ledger(live):
    e, c, st, _ = live
    acc = c.get("/api/account").json()
    assert acc["auto_positions"] is False and acc["broker"] is None and acc["summary"]["cash"] is None
    r = c.post("/api/funds/manual", json={"available": 50000, "balance": 52000})
    assert r.status_code == 200 and r.json()["available"] == 50000
    assert c.post("/api/funds/manual", json={"available": 50000, "balance": 100}).status_code == 400
    # 补记一笔已成交的买入：持仓 +1000、可用 -10005（含 5 元费用）
    r = c.post("/api/orders/manual", json={"symbol": "600000", "side": "buy", "price": 10, "quantity": 1000,
                                           "filled_qty": 1000, "fill_price": 10.0, "fee": 5, "time": "2026-11-10 09:35"}).json()
    o = r["order"]
    assert o["origin"] == "手动补记" and o["status"] == "filled" and o["applied_qty"] == 1000 and r["applied"]["cash_delta"] == -10005
    acc = c.get("/api/account").json()
    assert acc["summary"]["cash"] == 39995 and acc["funds"]["balance"] == 41995
    p = e.book.get("600000")
    assert p["quantity"] == 1000 and e.book.sellable("600000", "2026-11-11") == 1000   # 11-10 买入，T+1 后可卖
    assert e.book.sellable("600000", "2026-11-10") == 0
    # 补记一笔卖出，先部分成交再改成全部成交：只入账新增的部分
    r = c.post("/api/orders/manual", json={"symbol": "600000", "side": "sell", "price": 11, "quantity": 600, "filled_qty": 200,
                                           "time": "2026-11-11 10:00"}).json()
    oid = r["order"]["id"]
    assert r["order"]["status"] == "partial" and e.book.get("600000")["quantity"] == 800
    r = c.post("/api/orders/manual", json={"id": oid, "filled_qty": 600}).json()
    assert r["order"]["status"] == "filled" and r["applied"]["quantity"] == 400 and e.book.get("600000")["quantity"] == 400
    assert c.get("/api/account").json()["summary"]["cash"] == 39995 + 6600
    # 已入账的成交不能改小，不能删
    assert c.post("/api/orders/manual", json={"id": oid, "filled_qty": 100}).status_code == 400
    assert c.delete(f"/api/orders/manual/{oid}").status_code == 400
    # 没成交的补记可以删
    r = c.post("/api/orders/manual", json={"symbol": "000001", "side": "buy", "price": 9, "quantity": 100}).json()
    assert r["order"]["status"] == "submitted" and c.delete(f"/api/orders/manual/{r['order']['id']}").status_code == 200
    # 校验
    assert c.post("/api/orders/manual", json={"symbol": "60000", "side": "buy", "quantity": 100}).status_code == 400
    assert c.post("/api/orders/manual", json={"symbol": "600000", "side": "buy", "price": 9, "quantity": 100,
                                              "filled_qty": 200}).status_code == 400
    assert st.calls == []          # 手动账本：一次都没碰下单程序


def test_auto_positions_off_never_touches_broker(live):
    """自动获取持仓关闭：账户接口不给券商数据，后台 tick 不拉，卖出前也不对账。"""
    e, c, st, _ = live
    e.cfg["live"]["monitor"] = True
    for _ in range(3):
        e.link.tick(True)
    assert e.link.sync_before_sell() is False and e.link.broker_available("600000") is None
    j = c.get("/api/account").json()
    assert j["broker"] is None
    assert c.post("/api/broker/sync", json={"how": "refresh"}).status_code == 400
    assert c.get("/api/status").json()["broker_conn"] is None
    assert st.calls == []


def test_auto_positions_on_shows_only_fresh_data(live, monkeypatch):
    """打开自动获取持仓：旧快照作废，拿到最新数据前不显示；拿到后带数据时间。"""
    e, c, st, _ = live
    # 打开开关时会在后台线程里立刻同步一次；后台线程偶尔抢在下面第一次读账户之前完成，测试就时过时不过。
    # 这里把后台同步挡住，「拿到新数据」改由下面的 reconcile_now 显式触发，结果就稳定了。
    monkeypatch.setattr(e.link, "request_sync", lambda how="status": False)
    st.add_position("300825", "阿尔特", 300, 20.0)
    e.link.snapshot = {"positions": [{"symbol": "111111", "qty": 1}], "money": {"available": 1}, "ts": 1}
    e.link.snapshot_at = "2026-01-01 09:00:00"
    import time as _t
    e.link._snap_mono = _t.monotonic()
    e.set_positions_auto(True, "pytest") if hasattr(e, "set_positions_auto") else e.switches.set_positions_auto(True, "pytest")
    e.link.invalidate()
    b = c.get("/api/account").json()["broker"]
    assert b["freshness"]["fresh"] is False and b["positions"] is None and b["money"] is None   # 旧数据不冒充
    e.link.reconcile_now("fresh")
    b = c.get("/api/account").json()["broker"]
    assert b["freshness"]["fresh"] and b["freshness"]["data_time"] and [p["symbol"] for p in b["positions"]] == ["300825"]


# ---------------- 手动下单 ----------------

def test_manual_order_paper_fills_and_records(paper):
    e, c, sent = paper
    q = c.get("/api/trade/quote/600000").json()
    assert q["name"] == "浦发银行" and q["price"] == 10.0 and q["limit_up"] == 11.0 and q["sellable"] == 0
    cash0 = e.book.summary()["cash"]
    e.switches.set_auto(False, "pytest")         # 自动交易关着也能手动下单
    r = c.post("/api/trade/order", json={"symbol": "600000", "side": "buy", "price_type": "limit", "price": 10.05, "quantity": 500})
    assert r.status_code == 200, r.text
    res, o = r.json()["result"], r.json()["order"]
    assert res["status"] == "filled" and o["origin"] == "手动" and o["strategy"] == "手动" and o["filled_qty"] == 500
    assert e.book.get("600000")["quantity"] == 500 and e.book.summary()["cash"] < cash0
    assert any("模拟盘" in t for _, t, _ in sent)
    # 市价：换成保护限价（现价 +1%），仍是数字限价
    r = c.post("/api/trade/order", json={"symbol": "000001", "side": "buy", "price_type": "market", "quantity": 100}).json()
    assert r["result"]["price"] == 10.1 and r["result"]["status"] == "filled"
    # 风控：单笔上限 10000，买 2000 股 × 10 元被拦
    r = c.post("/api/trade/order", json={"symbol": "600036", "side": "buy", "price": 10, "quantity": 2000}).json()
    assert r["result"]["status"] == "risk_rejected" and "单笔上限" in r["result"]["message"]
    # 卖出：今天买的 T+1 不能卖
    r = c.post("/api/trade/order", json={"symbol": "600000", "side": "sell", "price": 10, "quantity": 100}).json()
    assert r["result"]["status"] == "risk_rejected" and "可卖" in r["result"]["message"]
    assert c.post("/api/trade/order", json={"symbol": "600000", "side": "buy", "price": 10, "quantity": 0}).status_code == 400
    rows = c.get("/api/orders").json()["orders"]
    assert [r["origin"] for r in rows].count("手动") == 4
    # 模拟盘委托没有未成交状态，不能撤
    assert c.post(f"/api/trade/cancel/{rows[0]['id']}").status_code == 400


def test_manual_order_paper_sell_old_lot(paper):
    e, c, _ = paper
    add_position(e, qty=500, day="2026-11-09")
    q = c.get("/api/trade/quote/600000").json()
    assert q["held"] == 500 and q["sellable"] == 500
    r = c.post("/api/trade/order", json={"symbol": "600000", "side": "sell", "price": 9.98, "quantity": 300}).json()
    assert r["result"]["status"] == "filled" and e.book.get("600000")["quantity"] == 200


def test_manual_order_and_cancel_live_manual_ledger(live):
    """实盘 + 手动账本：下单发 /api/order（模拟服务），只读委托表对上合同号；部分成交入手动账本；撤单发 /api/cancel。"""
    e, c, st, sent = live
    c.post("/api/funds/manual", json={"available": 50000})
    r = c.post("/api/trade/order", json={"symbol": "600000", "side": "buy", "price": 10.02, "quantity": 500})
    assert r.status_code == 200, r.text
    o = r.json()["order"]
    assert r.json()["result"]["status"] == "submitted" and o["origin"] == "手动" and o["entrust_no"]
    assert [t["path"] for t in st.trade_calls] == ["/api/order"]
    snaps = [x for x in st.calls if x["path"] == "/api/snapshot"]
    assert snaps and all("only=orders" in x["query"] for x in snaps)          # 只读委托表，不读持仓和资金
    # 券商那边成交 200 股，刷新委托：部分成交并入账
    st.fill(o["entrust_no"], 200, 10.01)
    assert c.post("/api/orders/refresh").json()["updated"] == 1
    oo = next(x for x in c.get("/api/orders").json()["orders"] if x["id"] == o["id"])
    assert oo["status"] == "partial" and oo["filled_qty"] == 200 and oo["applied_qty"] == 200
    assert e.book.get("600000")["quantity"] == 200 and e.book.summary()["cash"] == round(50000 - 2002, 2)
    # 撤单：调 /api/cancel（按合同号），然后刷新成「部成部撤」
    r = c.post(f"/api/trade/cancel/{o['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["result"]["status"] == "cancel_submitted"
    assert st.trade_calls[-1]["path"] == "/api/cancel" and st.trade_calls[-1]["body"]["entrust_no"] == o["entrust_no"]
    assert st.trade_calls[-1]["body"]["kind"] == "order"
    oo = r.json()["order"]
    assert oo["status"] == "partial_cancelled" and oo["filled_qty"] == 200
    assert c.post(f"/api/trade/cancel/{o['id']}").status_code == 400     # 已撤的不能再撤
    assert any("撤单" in t for _, t, _ in sent)
    # 卖出：今天买的（T+1）不能卖
    r = c.post("/api/trade/order", json={"symbol": "600000", "side": "sell", "price": 10, "quantity": 200}).json()
    assert r["result"]["status"] == "risk_rejected"
    assert st.forbidden_hits == []
    assert {t["path"] for t in st.trade_calls} == {"/api/order", "/api/cancel"}


def test_manual_order_live_auto_positions(live):
    """实盘 + 自动获取持仓：手动下单后走自动对账，委托记录跟着对账结果更新。"""
    e, c, st, _ = live
    e.switches.set_positions_auto(True, "pytest")
    r = c.post("/api/trade/order", json={"symbol": "600000", "side": "buy", "price": 10.02, "quantity": 500}).json()
    oid = r["order"]["id"]
    no = next(o["合同编号"] for o in st.orders)
    st.fill(no, 500, 10.02)
    e.link.reconcile_now("fresh")
    oo = next(x for x in c.get("/api/orders").json()["orders"] if x["id"] == oid)
    assert oo["status"] == "filled" and oo["filled_qty"] == 500
    assert e.book.get("600000")["state"] == "confirmed"


def test_manual_order_not_blocked_by_trading_switch_but_by_risk(live):
    e, c, st, _ = live
    c.post("/api/trading", json={"on": False})
    assert e.trading_on() is False
    r = c.post("/api/trade/order", json={"symbol": "600000", "side": "buy", "price": 10.0, "quantity": 100}).json()
    assert r["result"]["status"] == "submitted"
    e.cfg["risk"]["max_daily_orders"] = 1      # 已停用：笔数不再限制
    r = c.post("/api/trade/order", json={"symbol": "600036", "side": "buy", "price": 10.0, "quantity": 100}).json()
    assert r["result"]["status"] == "submitted"
    e.cfg["risk"]["max_order_amount"] = 500
    r = c.post("/api/trade/order", json={"symbol": "601318", "side": "buy", "price": 10.0, "quantity": 100}).json()
    assert r["result"]["status"] == "risk_rejected" and "单笔上限" in r["result"]["message"]


# ---------------- 合并后的自动交易开关 ----------------

def test_trading_switch_off_cancels_system_open_orders_only(live):
    e, c, st, sent = live
    e.switches.set_positions_auto(True, "pytest")
    c.post("/api/trading", json={"on": True})
    assert e.trading_on()
    # 一笔系统委托 + 一笔手动委托，都未成交
    sys_rec = e.submit({"id": "R1-B1", "side": "buy", "symbol": "600036", "name": "招商银行", "price": 10.0, "quantity": 100,
                        "trading_date": today_str(), "ref_price": 10.0, "limit_up": 11.0, "limit_down": 9.0, "is_trading_day": True,
                        "sources": ["竞价多头"]})
    assert sys_rec["status"] == "submitted"
    man = c.post("/api/trade/order", json={"symbol": "600000", "side": "buy", "price": 10.0, "quantity": 100}).json()["order"]
    r = c.post("/api/trading", json={"on": False}).json()
    assert r["on"] is False and [x["id"] for x in r["cancelled"]] == ["R1-B1"]
    assert r["cancelled"][0]["status"] == "cancel_submitted"
    cancels = [t for t in st.trade_calls if t["path"] == "/api/cancel"]
    assert len(cancels) == 1 and all(t["body"]["kind"] == "order" for t in cancels)
    assert next(x for x in e.journal.list() if x["id"] == man["id"])["status"] in ("submitted", "unknown")
    assert any(s[1] == "自动交易已关闭" for s in sent)
    # 关掉后自动信号只记不发
    rec = e.submit({"id": "R2-B1", "side": "buy", "symbol": "600016", "name": "民生银行", "price": 10.0, "quantity": 100,
                    "trading_date": today_str(), "ref_price": 10.0, "limit_up": 11.0, "limit_down": 9.0, "is_trading_day": True})
    assert rec["status"] == "signal_only"
    # 旧的一键停止接口 = 关闭自动交易
    c.post("/api/trading", json={"on": True})
    assert c.post("/api/halt", json={}).json()["on"] is False and not e.trading_on()
    assert st.forbidden_hits == []


def test_paper_trading_switch(paper):
    e, c, _ = paper
    assert c.post("/api/trading", json={"on": True}).json()["on"] is True
    j = c.get("/api/status").json()
    assert j["trading_on"] is True
    assert c.post("/api/trading", json={"on": False}).json() == {"on": False, "auto_trade": e.switches.auto_state(), "cancelled": []}
    assert c.post("/api/trading", json={}).status_code == 400


def test_journal_records_system_orders(paper):
    e, c, _ = paper
    e.switches.set_auto(False, "pytest")
    e.submit({"id": "R9-B1", "side": "buy", "symbol": "600036", "name": "招商银行", "price": 10.0, "quantity": 100,
              "trading_date": today_str(), "ref_price": 10.0, "limit_up": 11.0, "limit_down": 9.0, "is_trading_day": True,
              "sources": ["十点半"]})
    rows = c.get("/api/orders").json()
    o = rows["orders"][0]
    assert o["id"] == "R9-B1" and o["status"] == "signal_only" and o["strategy"] == "十点半" and o["origin"] == "系统"
    assert rows["counts"]["total"] == 0          # 只记信号不算委托
    assert c.get("/api/orders?signals=0").json()["orders"] == []
