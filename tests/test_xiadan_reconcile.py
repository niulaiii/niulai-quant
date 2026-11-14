"""新版下单程序接口：X-Auth、错误处理、调用频率、禁止接口；实盘自动对账；网页免保存测试推送。

全部离线：下单程序用 qfpy.devtools.mock_xiadan（按接口文档写的模拟服务）进程内调用，不走网络。
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta

import httpx
import pytest

from qfpy.devtools.mock_xiadan import MockState, asgi_transport, make_app
from qfpy.exec.brokers import build_cancel_body, interpret_order_response
from qfpy.exec.reconcile import reconcile
from qfpy.exec.xiadan import XiadanClient, XiadanError, auth_header_value, norm_order, normalize_snapshot
from qfpy.util import today_str
from tests.conftest import add_position

LIVE_EXTRA = """
[live]
enabled = true
base_url = "http://mock-xiadan:8765"
auth_password = "htfs"
monitor = false
[live.min_interval_secs]
status = 0
snapshot = 0
refresh = 0
requery = 0
[live.reconcile]
mismatch_min_gap_secs = 0
"""


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def mock():
    st = MockState("htfs")
    return st, make_app(st)


@pytest.fixture
def live_engine(make_engine, monkeypatch, mock):
    """实盘模式引擎（三重开关全开），下单程序换成进程内模拟服务。"""
    st, app = mock
    monkeypatch.setenv("QF_LIVE_ARMED", "I_ACCEPT_REAL_MONEY_RISK")
    e = make_engine("live", extra=LIVE_EXTRA)
    e.switches.set_positions_auto(True, "pytest")     # 自动对账只在「自动获取持仓」打开时进行
    e.link.client.transport = asgi_transport(app)
    return e, st


def item(**kw):
    d = {"id": "o-1", "side": "buy", "symbol": "600000", "name": "浦发银行", "price": 10.03, "quantity": 500,
         "trading_date": today_str()}
    d.update(kw)
    return d


def api_paths(st):
    return [c["path"] for c in st.calls]


# ---------------- X-Auth ----------------

def test_xauth_header_from_config_env_and_empty(monkeypatch):
    seen = []

    def tr(method, url, headers, params, body, timeout):
        seen.append(dict(headers))
        return 200, {"ok": True}

    XiadanClient({"auth_password": "htfs"}, transport=tr).status()
    assert seen[-1]["X-Auth"] == "aHRmcw==" == base64.b64encode(b"htfs").decode()
    monkeypatch.setenv("QF_XIADAN_PASSWORD", "交易口令")          # 环境变量优先，UTF-8 后再 base64
    XiadanClient({"auth_password": "htfs"}, transport=tr).status()
    assert seen[-1]["X-Auth"] == auth_header_value("交易口令") == base64.b64encode("交易口令".encode()).decode()
    monkeypatch.delenv("QF_XIADAN_PASSWORD")
    XiadanClient({}, transport=tr).status()
    assert "X-Auth" not in seen[-1]                                  # 没口令就不带头


def test_no_password_hardcoded_in_source():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "qfpy"
    for f in root.rglob("*.py"):
        if f.parent.name == "devtools":
            continue
        s = f.read_text(encoding="utf-8")
        assert "aHRmcw==" not in s and '"htfs"' not in s, f


def test_wrong_password_is_auth_error(mock):
    st, app = mock
    c = XiadanClient({"base_url": "http://m", "auth_password": "wrong"}, transport=asgi_transport(app))
    with pytest.raises(XiadanError) as ei:
        c.status()
    assert ei.value.kind == "auth" and ei.value.http_status == 403
    ok = XiadanClient({"base_url": "http://m", "auth_password": "htfs"}, transport=asgi_transport(app))
    assert ok.status()["ok"] is True


# ---------------- 错误处理 / 禁止接口 / 频率 ----------------

def test_error_kinds(mock):
    st, app = mock
    c = XiadanClient({"base_url": "http://m", "auth_password": "htfs", "min_interval_secs": {"snapshot": 0}},
                     transport=asgi_transport(app))
    st.connected = False
    assert c.status(force=True)["guard"]["state"] == "disconnected"     # status 本身 ok=false 不抛错
    with pytest.raises(XiadanError) as ei:
        c.snapshot()
    assert ei.value.kind == "not_connected" and ei.value.http_status == 503

    def timeout_tr(*a):
        raise httpx.ReadTimeout("slow")

    def refused_tr(*a):
        raise httpx.ConnectError("refused")

    def html_tr(*a):
        return 200, {"_raw": "<html>", "_bad_json": True}

    def e500(*a):
        return 500, {"ok": False, "message": "boom"}

    for tr, kind in ((timeout_tr, "timeout"), (refused_tr, "network"), (html_tr, "bad_json"), (e500, "http")):
        with pytest.raises(XiadanError) as ei:
            XiadanClient({}, transport=tr).status()
        assert ei.value.kind == kind


def test_forbidden_paths_never_reach_transport():
    calls = []

    def tr(*a):
        calls.append(a)
        return 200, {"ok": True}

    c = XiadanClient({}, transport=tr)
    for method, path in (("POST", "/api/command"), ("POST", "/api/client/restart"), ("POST", "/api/connect"),
                         ("GET", "/api/order"), ("POST", "/api/status"), ("GET", "/api/whatever")):
        with pytest.raises(XiadanError) as ei:
            c._call(method, path, timeout_key="status", force=True)
        assert ei.value.kind == "forbidden"
    assert calls == []


def test_timeouts_and_rate_limit():
    clock = Clock()
    got = []

    def tr(method, url, headers, params, body, timeout):
        got.append((url.rsplit("/", 1)[-1], params, timeout))
        return 200, {"ok": True}

    c = XiadanClient({"base_url": "http://m"}, transport=tr, clock=clock)
    c.status()
    with pytest.raises(XiadanError) as ei:
        c.status()                                   # 2 秒内重复
    assert ei.value.kind == "rate_limited"
    c.refresh()
    clock.t += 10
    with pytest.raises(XiadanError):
        c.refresh()                                  # refresh 至少隔 30 秒
    clock.t += 25
    c.refresh()
    c.snapshot_after_trade()
    c.snapshot_after_trade()                         # 下单后同步不限频
    c.requery()
    by = {}
    for name, params, t in got:
        by.setdefault(name, []).append((params, t))
    assert by["refresh"][0][1] >= 20                 # 切页可能 12~17 秒
    assert by["requery"][0][1] >= 45                 # 子进程脚本超时 45 秒
    assert by["status"][0][1] <= 5
    assert by["snapshot"][0][0] == {"refresh": 1, "no_render": 1}


def test_interpret_new_order_response_and_cancel_body():
    ok = {"ok": True, "command": "buy 600000 10.00 100 -notip", "confirmed_count": 1, "message": "已确认 1/1 笔委托",
          "status": "confirmed"}
    assert interpret_order_response(200, ok)[0] == "submitted"
    assert interpret_order_response(200, {**ok, "ok": False, "confirmed_count": 0, "status": "unconfirmed"})[0] == "unknown"
    assert interpret_order_response(403, {"ok": False, "message": "口令错误"})[0] == "rejected"
    assert interpret_order_response(503, {"ok": False})[0] == "rejected"
    assert interpret_order_response(200, {"ok": True, "message": "委托已提交，合同编号：5322。"})[2] == "5322"
    assert build_cancel_body("5322") == {"kind": "order", "entrust_no": "5322", "confirm": "我确认这是股票交易委托"}
    for bad in ("all", "", "53a"):
        with pytest.raises(ValueError):
            build_cancel_body(bad)


def test_norm_order_status_from_columns():
    base = {"证券代码": "600000", "操作": "买入", "委托数量": "500", "委托价格": "10.03", "合同编号": "5301"}
    assert norm_order({**base, "成交数量": "0", "备注": "未成交"})["status"] == "pending"
    assert norm_order({**base, "成交数量": "200", "备注": "部分成交"})["status"] == "partial"
    assert norm_order({**base, "成交数量": "500", "备注": "全部成交"})["status"] == "filled"
    assert norm_order({**base, "成交数量": "0", "撤单数量": "500", "备注": "已撤单"})["status"] == "cancelled"
    assert norm_order({**base, "成交数量": "200", "撤单数量": "300"})["status"] == "partial_cancelled"
    assert norm_order({**base, "备注": "废单：资金不足"})["status"] == "rejected"
    assert norm_order({**base, "买卖标志": "卖出", "操作": ""})["side"] == "sell"


# ---------------- 实盘：下单 → 同步 → 对账 ----------------

def test_buy_then_fill_auto_confirms(live_engine):
    e, st = live_engine
    r = e.broker.place(item())
    assert r["status"] == "submitted"
    # 下单后按文档推荐调 snapshot?refresh=1&no_render=1 同步
    snaps = [c for c in st.calls if c["path"] == "/api/snapshot"]
    assert snaps and "refresh=1" in snaps[0]["query"] and "no_render=1" in snaps[0]["query"]
    rec = e.book.pending()["o-1"]
    assert rec["entrust_no"] == "5301"                          # 响应没带合同号，对账从券商委托里对上了
    assert e.book.get("600000")["state"] == "unconfirmed_buy"
    st.fill("5301", 500, 10.01)
    rep = e.link.reconcile_now("fresh")
    p = e.book.get("600000")
    assert p["state"] == "confirmed" and p["quantity"] == 500 and abs(p["avg_cost"] - 10.01) < 1e-6
    assert "o-1" not in e.book.pending()
    assert [x["kind"] for x in rep["events"]][:1] == ["buy_filled"]
    assert [c["path"] for c in st.trade_calls] == ["/api/order"] and st.forbidden_hits == []


def test_partial_fill_then_cancel_adjusts_quantity(live_engine):
    e, st = live_engine
    e.broker.place(item())
    st.fill("5301", 200, 10.0)
    e.link.reconcile_now("fresh")
    assert "o-1" in e.book.pending() and e.book.get("600000")["state"] == "unconfirmed_buy"   # 部成：仍在途
    r = e.cancel_order("o-1")
    assert r["status"] == "cancel_submitted"
    assert st.trade_calls[-1] == {"path": "/api/cancel", "body": build_cancel_body("5301")}
    p = e.book.get("600000")                                  # 撤单后同步已经对完账
    assert p["quantity"] == 200 and p["state"] == "confirmed" and "o-1" not in e.book.pending()
    assert abs(p["cost"] - 2000.0) < 0.01


def test_buy_cancelled_without_fill_removes_position(live_engine):
    e, st = live_engine
    e.broker.place(item())
    e.cancel_order("o-1")
    assert e.book.get("600000") is None and not e.book.pending()


def test_order_timeout_marks_unknown_no_retry_then_found(live_engine, mock):
    e, st = live_engine
    _, app = mock
    inner = asgi_transport(app)

    def flaky(method, url, headers, params, body, timeout):
        if url.endswith("/api/order"):
            inner(method, url, headers, params, body, timeout)      # 委托其实报上了
            raise httpx.ReadTimeout("没等到响应")
        return inner(method, url, headers, params, body, timeout)

    e.link.client.transport = flaky
    r = e.broker.place(item())
    assert r["status"] == "unknown"
    assert [c["path"] for c in st.trade_calls] == ["/api/order"]        # 只发了一次，没重试
    assert e.book.pending()["o-1"]["entrust_no"] == "5301"              # 同步时在券商委托里找到了
    st.fill("5301", 500, 10.03)
    e.link.reconcile_now("fresh")
    assert e.book.get("600000")["state"] == "confirmed"


def test_unknown_order_not_found_and_position_unchanged_is_dropped(live_engine, mock):
    e, st = live_engine

    def dead_order(method, url, headers, params, body, timeout):
        if url.endswith("/api/order"):
            raise httpx.ReadTimeout("x")
        return asgi_transport(mock[1])(method, url, headers, params, body, timeout)

    e.link.client.transport = dead_order
    e.broker.place(item())
    assert e.book.get("600000")["state"] == "uncertain"
    e.link.reconcile_now("fresh")                          # 宽限时间内：不下结论
    assert "o-1" in e.book.pending()
    from qfpy.util import now_cn
    later = now_cn() + timedelta(minutes=5)
    e.link.now_fn = lambda: later
    rep = e.link.reconcile_now("cached")                   # 非新鲜数据：也不下否定结论
    assert "o-1" in e.book.pending()
    st.empty_positions = False
    rep = e.link.reconcile_now("fresh")
    assert e.book.get("600000") is None and not e.book.pending()
    ev = [x for x in rep["events"] if x["kind"] == "buy_not_placed"]
    assert ev and ev[0]["notify"] is True


def test_sell_flow_and_rejected_order(live_engine):
    e, st = live_engine
    add_position(e, qty=500, price=10.0, day="2026-11-09")
    st.add_position("600000", "浦发银行", 500, 10.0, 10.4)
    e.link.reconcile_now("fresh")
    r = e.broker.place(item(id="s-1", side="sell", price=10.4, quantity=500, reason="止盈"))
    assert r["status"] == "submitted" and e.book.get("600000")["state"] == "selling"
    st.fill("5301", 500, 10.41)
    rep = e.link.reconcile_now("fresh")
    assert e.book.get("600000") is None
    closed = e.book.s["closed"][-1]
    assert closed["quantity"] == 500 and closed["sell_price"] == 10.41 and abs(closed["pnl"] - 205.0) < 0.01
    assert any(x["kind"] == "sell_filled" for x in rep["events"])
    st.order_mode = "fail"
    r = e.broker.place(item(id="o-2"))
    assert r["status"] == "rejected" and e.book.get("600000") is None and "o-2" not in e.book.pending()


def test_qty_mismatch_needs_two_fresh_checks_then_notifies(live_engine):
    """手动账本时不对账；这条只验证对账函数本身：缓存数据不判、新数据要两次才判不一致。"""
    e, st = live_engine
    e.switches.set_positions_auto(False, "pytest")
    add_position(e, qty=500, state="confirmed")
    st.add_position("600000", "浦发银行", 400, 10.0)
    snap = normalize_snapshot(st.snapshot())
    from datetime import datetime
    mem = {}
    r1 = reconcile(e.book, snap, datetime.now(), today_str(), False, {}, mem)
    assert not r1["events"] and e.book.get("600000")["state"] == "confirmed"   # 缓存数据不判不一致
    r2 = reconcile(e.book, snap, datetime.now(), today_str(), True, {"mismatch_min_gap_secs": 0}, mem)
    assert not r2["events"]                                                     # 第一次只记下
    r3 = reconcile(e.book, snap, datetime.now(), today_str(), True, {"mismatch_min_gap_secs": 0}, mem)
    ev = [x for x in r3["events"] if x["kind"] == "qty_mismatch"]
    assert ev and ev[0]["local"] == 500 and ev[0]["broker"] == 400
    assert e.book.get("600000")["state"] == "uncertain"


def test_auto_positions_adopts_broker_qty(live_engine):
    """自动获取持仓打开：本地持仓直接照搬券商数量。"""
    e, st = live_engine
    add_position(e, qty=500, state="confirmed")
    st.add_position("600000", "浦发银行", 400, 10.0)
    e.link.reconcile_now("fresh")
    assert e.book.get("600000")["quantity"] == 400


def test_uncertain_position_matching_broker_is_auto_confirmed(live_engine):
    e, st = live_engine
    add_position(e, qty=300, price=20.0, state="uncertain")
    st.add_position("600000", "浦发银行", 300, 19.8, 20.5, available=300)
    rep = e.link.reconcile_now("cached")
    p = e.book.get("600000")
    assert p["state"] == "confirmed" and abs(p["avg_cost"] - 19.8) < 1e-6 and p["broker"]["available"] == 300
    assert rep["events"][0]["kind"] == "auto_confirmed"
    assert e.link.broker_available("600000") == 300


def test_manual_sell_in_ths_is_detected(live_engine):
    e, st = live_engine
    add_position(e, qty=500, state="confirmed")
    st.add_position("600000", "浦发银行", 500, 10.0)
    e.link.reconcile_now("cached")
    o = st.new_order("sell", "600000", "浦发银行", 500, 10.5)       # 你在同花顺里手动卖了
    st.fill(o["合同编号"], 500, 10.5)
    rep = e.link.reconcile_now("cached")
    assert e.book.get("600000") is None
    assert rep["events"][0]["kind"] == "manual_sell_detected" and e.book.s["closed"][-1]["sell_price"] == 10.5


def test_unreadable_positions_do_not_cause_false_alarm(live_engine):
    e, st = live_engine
    add_position(e, qty=500, state="confirmed")
    st.add_position("999999", "其他", 100, 5.0)        # 市值不为 0
    st.empty_positions = True                          # 但持仓表读不到，返回空列表
    for _ in range(3):
        rep = e.link.reconcile_now("fresh")
    assert rep["positions_trusted"] is False and not rep["events"]
    assert e.book.get("600000")["state"] == "confirmed"


def test_stale_position_table_after_fill_is_not_mismatch(live_engine):
    e, st = live_engine
    st.stale_positions = True                          # 成交后持仓表没重绘（下单程序持仓缓存 120 秒）
    st.add_position("000001", "平安银行", 100, 11.0)
    add_position(e, code="000001", name="平安银行", qty=100, price=11.0, state="confirmed")
    e.broker.place(item())
    st.fill("5301", 500, 10.0)
    for _ in range(3):
        rep = e.link.reconcile_now("fresh")
    p = e.book.get("600000")
    assert p["state"] == "confirmed" and p["quantity"] == 500        # 委托表已成 → 确认
    assert any(x["kind"] == "positions_stale" for x in rep["events"])
    assert not any(x["kind"] == "qty_mismatch" for x in rep["events"])


def test_reconcile_skipped_outside_live(make_engine, mock):
    st, app = mock
    e = make_engine("paper", extra=LIVE_EXTRA.replace("enabled = true", "enabled = false"))
    e.link.client.transport = asgi_transport(app)
    add_position(e, qty=500)
    st.add_position("600000", "浦发银行", 100, 10.0)
    rep = e.link.reconcile_now("fresh")
    assert "skipped" in rep and e.book.get("600000")["quantity"] == 500
    assert e.link.view()["snapshot"]["positions"][0]["qty"] == 100      # 只显示券商数据


def test_brokerlink_tick_polls_status_then_snapshot_with_intervals(live_engine):
    e, st = live_engine
    clock = Clock()
    e.link.clock = clock
    e.link.client.clock = clock
    e.link.live_cfg = {**e.link.live_cfg, "monitor": True, "poll_secs": 20, "reconcile_secs": 60}
    e.link.tick(True)
    assert api_paths(st) == ["/api/status", "/api/snapshot"]
    clock.t += 5
    e.link.tick(True)                                 # 不到 20 秒：什么都不调
    assert len(st.calls) == 2
    clock.t += 20
    e.link.tick(True)                                 # 只查状态，快照要等 60 秒
    assert api_paths(st)[-1] == "/api/status" and len(st.calls) == 3
    st.connected = False
    clock.t += 60
    e.link.tick(True)
    assert e.link.conn["state"] == "reachable" and api_paths(st)[-1] == "/api/status"
    assert st.forbidden_hits == [] and not st.trade_calls


def test_live_exit_sell_capped_by_broker_available(live_engine):
    from tests.conftest import FakeMarket
    e, st = live_engine
    add_position(e, qty=500, price=10.0, day="2026-11-09")
    st.add_position("600000", "浦发银行", 500, 10.0, 10.0, available=300)
    e.link.reconcile_now("fresh")
    day = today_str()
    fm = FakeMarket(day, ["2026-11-09", "2026-11-10", day], {"600000": (10.0, 10.0)})
    e.market = lambda: fm
    e.set_auto(True, "pytest")
    e.cfg["risk"]["enforce_session"] = False
    import qfpy.exec.risk as riskmod
    orig = riskmod.in_sessions
    riskmod.in_sessions = lambda *a, **k: True      # 实盘强制交易时段：测试里放行
    try:
        out = e.check_exits(now_hm="14:50")
    finally:
        riskmod.in_sessions = orig
    sells = [c for c in st.trade_calls if c["path"] == "/api/order"]
    res = out["results"][0]
    assert res["sellable"] == 300 and res["action"] == "submitted"     # 本地 500 股，券商可卖只有 300
    assert len(sells) == 1 and sells[0]["body"]["quantity"] == "300" and sells[0]["body"]["side"] == "sell"
    assert sells[0]["body"]["batch_count"] == 1 and "." in sells[0]["body"]["price"]
    assert e.book.get("600000")["state"] == "selling" and st.forbidden_hits == []


# ---------------- 网页：免保存测试推送、下单程序状态 ----------------

@pytest.fixture
def web(make_engine, monkeypatch):
    from fastapi.testclient import TestClient

    from qfpy.web.server import create_app
    e = make_engine("paper", extra='\n[notify.dingtalk]\nwebhook = "https://oapi.dingtalk.com/robot/send?access_token=SAVED1111"\n'
                                   'secret = "SECsaved2222"\n')
    monkeypatch.setattr(httpx.Client, "send", _real_send)     # TestClient 走进程内 ASGI，不联网
    return e, TestClient(create_app(e))


_real_send = httpx.Client.send     # 模块加载时保存（conftest 的断网在测试里才生效），TestClient 要用


def test_notify_test_uses_form_values_without_saving(web, monkeypatch):
    e, tc = web
    sent = []

    def fake_post(url, json=None, timeout=None, **kw):
        sent.append((url, json))

        class R:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"errcode": 0, "errmsg": "ok"}
        return R()

    import qfpy.notify.channels as ch
    monkeypatch.setattr(ch.httpx, "post", fake_post)
    before = e.store.path.read_text(encoding="utf-8")
    ver = e.store.version
    r = tc.post("/api/notify/test", json={"channel": "dingtalk", "config": {
        "webhook": "https://oapi.dingtalk.com/robot/send?access_token=FORM3333", "secret": "SECform4444", "keyword": "量化"}})
    assert r.status_code == 200 and r.json()["results"][0]["status"] == "已发送" and r.json()["unsaved"] is True
    url, payload = sent[-1]
    assert "access_token=FORM3333" in url and "sign=" in url and payload["markdown"]["title"].startswith("量化")
    # 没有写入配置、没有改内存里的设置
    assert e.store.path.read_text(encoding="utf-8") == before and e.store.version == ver
    assert e.cfg["notify"]["dingtalk"]["webhook"].endswith("SAVED1111") and "keyword" not in e.cfg["notify"]["dingtalk"]
    assert not e.store.path.with_suffix(".toml.bak").exists()
    # 表单里仍是掩码的字段 = 没改过，用已保存的值
    masked = tc.get("/api/config").json()["config"]["notify"]["dingtalk"]
    r = tc.post("/api/notify/test", json={"channel": "dingtalk", "config": {**masked, "keyword": ""}})
    assert "access_token=SAVED1111" in sent[-1][0]
    # 渠道还没启用、也没保存过（企业微信）也能测
    r = tc.post("/api/notify/test", json={"channel": "wecom", "config": {"webhook": "https://qyapi.weixin.qq.com/x?key=K"}})
    assert r.json()["results"][0]["status"] == "已发送"
    assert (e.cfg["notify"].get("wecom") or {}).get("webhook") != "https://qyapi.weixin.qq.com/x?key=K"
    assert "wecom" not in (e.cfg["notify"].get("channels") or [])
    # 缺必填项
    r = tc.post("/api/notify/test", json={"channel": "pushplus", "config": {"token": ""}})
    assert r.json()["results"][0]["ok"] is False and "缺少" in r.json()["results"][0]["status"]
    assert tc.post("/api/notify/test", json={"channel": "nope", "config": {}}).status_code == 400


def test_web_broker_endpoints(web, mock):
    e, tc = web
    st, app = mock
    e.link.client.transport = asgi_transport(app)
    e.link.client.min_interval = {k: 0 for k in e.link.client.min_interval}
    st.add_position("300825", "阿尔特", 400, 13.6, 14.0)
    e.cfg["live"]["auth_password"] = "htfs"
    # 自动获取持仓关闭：不读券商数据，账户页也不给券商数据
    assert tc.post("/api/broker/sync", json={"how": "fresh"}).status_code == 400
    assert tc.get("/api/account").json()["broker"] is None
    assert st.snapshot_calls == 0 if hasattr(st, "snapshot_calls") else True
    e.switches.set_positions_auto(True, "pytest")
    e.auto_pos = e.link.auto_positions_fn = lambda: True     # 模拟盘实例上模拟实盘的自动获取持仓
    v = tc.post("/api/broker/sync", json={"how": "fresh"}).json()
    assert v["sync_started"] is True          # 后台执行，请求立刻返回
    e.link.sync_job.wait(10)
    v = tc.get("/api/broker").json()
    assert v["sync"]["running"] is False and v["sync"]["sync_error"] is None
    assert v["conn"]["state"] == "connected" and v["snapshot"]["positions"][0]["symbol"] == "300825"
    assert tc.get("/api/broker").json()["snapshot"]["positions"][0]["qty"] == 400
    acc = tc.get("/api/account").json()["broker"]
    assert acc["freshness"]["fresh"] and acc["positions"][0]["symbol"] == "300825" and acc["freshness"]["data_time"]
    st.password = "other"
    tc.post("/api/broker/sync", json={"how": "fresh"})
    e.link.sync_job.wait(10)
    v = tc.get("/api/broker").json()
    assert v["conn"]["state"] == "auth_failed" and v["sync"]["sync_error"]
    assert tc.post("/api/broker/cancel", json={"order_id": "x"}).status_code == 400   # 模拟盘没有委托可撤
    assert st.trade_calls == [] and st.forbidden_hits == []


def test_normalize_snapshot_trust_rules():
    from qfpy.exec.reconcile import positions_trustworthy
    s = normalize_snapshot({"ok": True, "money": {"market_value": "0.00"}, "positions": []})
    assert positions_trustworthy(s)
    s = normalize_snapshot({"ok": True, "money": {"market_value": "100944.60"}, "positions": []})
    assert not positions_trustworthy(s)
    s = normalize_snapshot({"ok": True, "money": {}, "positions": [{"code": "600000", "qty": 1, "code_verified": False}]})
    assert not positions_trustworthy(s)
    assert reconcile  # noqa
