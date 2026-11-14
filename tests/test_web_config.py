"""网页「设置」：所有设置可改、校验、写回 TOML + .bak、日志 + 推送、切实盘直接切换（不要确认文字）、敏感字段掩码、
免保存测试连接（数据源 / 下单程序）。全部离线。"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from qfpy.devtools.mock_xiadan import MockState, asgi_transport, make_app
from tests.test_xiadan_reconcile import _real_send


@pytest.fixture
def web(make_engine, monkeypatch):
    from qfpy.web.server import create_app
    e = make_engine("paper", extra='\n[notify.dingtalk]\nwebhook = "https://oapi.dingtalk.com/robot/send?access_token=SAVED1111"\n'
                                   'secret = "SECsaved2222"\n[live]\nauth_password = "pw-saved-9999"\n')
    sent = []
    monkeypatch.setattr(e.notifier, "notify", lambda ev, title, text, **kw: sent.append((ev, title, text)))
    monkeypatch.setattr(httpx.Client, "send", _real_send)
    app = create_app(e)
    return e, TestClient(app), sent, app


def test_get_config_masks_secrets_and_lists_readonly(web):
    e, c, _, _ = web
    j = c.get("/api/config").json()
    txt = str(j["config"])
    assert "SAVED1111" not in txt and "SECsaved2222" not in txt and "pw-saved-9999" not in txt
    assert j["config"]["live"]["auth_password"].endswith("9999")
    assert j["readonly"] == ["web", "data_dir"] and "arm_env" in j["locked"]["live"]
    assert "live_confirm_text" not in j


def test_edit_risk_exits_schedule_live_params_written_with_backup_log_and_push(web):
    e, c, sent, _ = web
    m = c.get("/api/config").json()["config"]
    m["risk"]["max_order_amount"] = 12345
    m["exits"]["stop_loss_pct"] = 4.5
    m["schedule"]["runs"] = [{"name": "十点半", "start": "10:30", "end": "10:40", "pools": ["watchlist:all"]}]
    m["schedule"]["backfill_trade"] = True
    m["live"]["base_url"] = "http://192.168.1.20:8765"
    m["live"]["timeout_secs"] = 9
    m["live"]["reconcile"] = {"mismatch_min_gap_secs": 30}
    m["api"]["token"] = "a-long-enough-token-123"
    m["notify"]["dingtalk"]["secret"] = m["notify"]["dingtalk"]["secret"]       # 掩码原样回传 = 不改
    v = c.post("/api/config/validate", json=m).json()
    assert v["ok"] and any(x["key"] == "risk.max_order_amount" for x in v["changes"])
    assert e.cfg["risk"]["max_order_amount"] != 12345                      # 校验不保存
    r = c.put("/api/config", json=m)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["saved"] and e.cfg["risk"]["max_order_amount"] == 12345 and e.cfg["exits"]["stop_loss_pct"] == 4.5
    assert e.cfg["live"]["base_url"] == "http://192.168.1.20:8765" and e.link.client.live["base_url"] == "http://192.168.1.20:8765"
    assert e.cfg["notify"]["dingtalk"]["secret"] == "SECsaved2222"         # 掩码回传保留原值
    assert e.cfg["api"]["token"] == "a-long-enough-token-123"
    text = e.store.path.read_text("utf-8")
    assert "max_order_amount = 12345" in text and 'base_url = "http://192.168.1.20:8765"' in text
    assert e.store.path.with_suffix(".toml.bak").exists()
    tok = [d for d in j["changes"] if d["key"] == "api.token"][0]
    assert tok["secret"] and tok["new"] == "***"
    ev = [s for s in sent if s[0] == "config_change"]
    assert len(ev) == 1 and "risk.max_order_amount" in ev[0][2] and "a-long-enough-token-123" not in ev[0][2]
    runs = [j for j in c.get("/api/status").json()["schedule"]["jobs"] if j["kind"] == "run"]
    assert len(runs) == 1 and runs[0]["time"] == "10:30" and runs[0]["end"] == "10:40"


def test_save_does_not_write_untouched_default_sections(web):
    """文件里没写的默认段（如旧的平铺 factors = []）保存时不应被写进 TOML。"""
    import tomlkit
    e, c, _, _ = web
    before = tomlkit.parse(e.store.path.read_text("utf-8"))
    assert "factors" not in before
    m = c.get("/api/config").json()["config"]
    m["risk"]["max_order_amount"] = 11111
    assert c.put("/api/config", json=m).status_code == 200
    after = tomlkit.parse(e.store.path.read_text("utf-8"))
    assert "factors" not in after and after["risk"]["max_order_amount"] == 11111


def test_invalid_values_rejected_and_file_unchanged(web):
    e, c, sent, _ = web
    before = e.store.path.read_text("utf-8")
    m = c.get("/api/config").json()["config"]
    for patch, msg in [({"risk": {**m["risk"], "max_order_amount": 0}}, "max_order_amount"),
                       ({"live": {**m["live"], "base_url": "ftp://x"}}, "base_url"),
                       ({"live": {**m["live"], "timeout_secs": -1}}, "timeout_secs"),
                       ({"schedule": {**m["schedule"], "runs": [{"start": "10:40", "end": "10:30"}]}}, "晚于"),
                       ({"execution": {"mode": "yolo"}}, "execution.mode")]:
        r = c.put("/api/config", json=patch)
        assert r.status_code == 400 and msg in r.json()["detail"], (patch, r.text)
    assert e.store.path.read_text("utf-8") == before and not sent


def test_switch_mode_paper_to_dry_run_hot(web):
    e, c, sent, _ = web
    r = c.put("/api/config", json={"execution": {"mode": "dry_run"}})
    assert r.status_code == 200 and e.mode == "dry_run" and e.broker.mode == "dry_run"
    assert 'mode = "dry_run"' in e.store.path.read_text("utf-8")
    assert any("运行模式" in s[1] for s in sent)


def test_switch_to_live_directly_no_gate(web, monkeypatch):
    """取消切实盘门槛：不要确认文字、不要 QF_LIVE_ARMED、不要 live.enabled；照样记日志、推钉钉、写回 TOML。"""
    e, c, sent, _ = web
    monkeypatch.delenv("QF_LIVE_ARMED", raising=False)
    v = c.post("/api/config/validate", json={"execution": {"mode": "live"}}).json()
    assert v["ok"] and "need_confirm" not in v
    r = c.put("/api/config", json={"execution": {"mode": "live"}, "live": {**e.cfg["live"], "monitor": False}})
    assert r.status_code == 200, r.text
    assert e.mode == "live" and e.broker.mode == "live" and r.json()["to_live"]
    assert 'mode = "live"' in e.store.path.read_text("utf-8")
    assert any(s[1] == "已切换到实盘" for s in sent)
    # 风控参数网页可改
    assert c.put("/api/config", json={"risk": {**e.cfg["risk"], "max_order_amount": 12345, "max_daily_orders": 3}}).status_code == 200
    assert e.risk.usage("2026-01-01")["max_order_amount"] == 12345 and e.risk.usage("2026-01-01")["max_daily_orders"] == 3
    assert c.put("/api/config", json={"execution": {"mode": "paper"}}).status_code == 200 and e.mode == "paper"
    assert any("运行模式" in s[1] for s in sent)


def test_live_order_safety_unchanged_after_web_switch(web, monkeypatch):
    """网页切到实盘后，下单安全约束照旧：只发数字限价、整数股数、batch_count=1；不碰禁用接口。"""
    from qfpy.exec.brokers import build_order_body
    e, c, _, _ = web
    live = {**e.cfg["live"], "monitor": False}
    c.put("/api/config", json={"execution": {"mode": "live"}, "live": live})
    assert e.mode == "live"
    with pytest.raises(ValueError):
        build_order_body({"side": "buy", "symbol": "600000", "price": "market", "quantity": 100})
    with pytest.raises(ValueError):
        build_order_body({"side": "buy", "symbol": "600000", "price": 10.0, "quantity": 100.5})
    b = build_order_body({"side": "buy", "symbol": "600000", "price": 10.0, "quantity": 100})
    assert b["batch_count"] == 1 and b["price"] == "10.00"
    st = MockState("pw-saved-9999")
    e.link.client.transport = asgi_transport(make_app(st))
    for p in ("/api/command", "/api/client/restart", "/api/connect"):
        with pytest.raises(Exception):
            e.link.client._call("POST", p)
    assert st.forbidden_hits == []


def test_test_xiadan_uses_form_values_without_saving(web):
    e, c, _, app = web
    st = MockState("form-pass")
    app.state.xiadan_test_transport = asgi_transport(make_app(st))
    before = e.store.path.read_text("utf-8")
    r = c.post("/api/test/xiadan", json={"live": {"base_url": "http://10.0.0.5:8765", "auth_password": "form-pass"}}).json()
    assert r["ok"] and r["paths"] == ["/api/status", "/api/snapshot"], r
    assert [x["path"] for x in st.calls] == ["/api/status", "/api/snapshot"] and st.trade_calls == [] and st.forbidden_hits == []
    r = c.post("/api/test/xiadan", json={"live": {"base_url": "http://10.0.0.5:8765", "auth_password": "wrong"}}).json()
    assert not r["ok"] and "口令" in r["checks"][0]["detail"] and r["auth_error"] and "一致" in r["hint"]
    # 真实下单程序的情形：/api/status 不验口令能连上，读快照时才 403（用户截图里的情况）
    st.status_needs_auth = False
    r = c.post("/api/test/xiadan", json={"live": {"base_url": "http://10.0.0.5:8765", "auth_password": "wrong"}}).json()
    assert not r["ok"] and r["checks"][0]["ok"] and not r["checks"][1]["ok"] and "一致" in r["checks"][1]["detail"]
    # 掩码回传：用已保存的口令
    st2 = MockState("pw-saved-9999")
    app.state.xiadan_test_transport = asgi_transport(make_app(st2))
    masked = c.get("/api/config").json()["config"]["live"]["auth_password"]
    assert c.post("/api/test/xiadan", json={"live": {"auth_password": masked}}).json()["ok"]
    assert e.store.path.read_text("utf-8") == before
    assert c.post("/api/test/xiadan", json={"live": {"base_url": "ftp://x"}}).status_code == 400


def test_test_xiadan_unreachable_reports_cleanly(web, monkeypatch):
    e, c, _, app = web

    def boom(*a, **k):
        raise httpx.ConnectError("refused")
    app.state.xiadan_test_transport = boom
    r = c.post("/api/test/xiadan", json={"live": {"base_url": "http://127.0.0.1:1"}}).json()
    assert not r["ok"] and "GET /api/status" in r["checks"][0]["check"] and "连不上" in r["checks"][0]["detail"]


def test_test_source_standard_with_form_values(web, monkeypatch):
    """「标准接口」数据源：网页填 URL + token 后直接测试，不保存。"""
    from tests.test_data_provider import route_httpx_to
    e, c, _, _ = web
    route_httpx_to(monkeypatch, "http://prov.local", token="tok-123456789")
    before = e.store.path.read_text("utf-8")
    r = c.post("/api/test/source", json={"name": "prov", "config": {"type": "standard", "url": "http://prov.local",
                                                                    "token": "tok-123456789"}}).json()
    assert r["ok"], r
    names = [x["check"] for x in r["checks"]]
    assert names[0] == "/meta" and "股池" in names and "指数行情" in names
    bad = c.post("/api/test/source", json={"name": "prov", "config": {"type": "standard", "url": "http://prov.local",
                                                                      "token": "nope"}}).json()
    assert not bad["ok"] and "UNAUTHORIZED" in bad["checks"][0]["detail"]
    assert e.store.path.read_text("utf-8") == before
    assert c.post("/api/test/source", json={"name": "x", "config": {"type": "nope"}}).status_code == 400


def test_add_standard_source_and_pool_via_web(web, monkeypatch):
    from tests.test_data_provider import route_httpx_to
    e, c, _, _ = web
    route_httpx_to(monkeypatch, "http://prov.local", token="")
    m = c.get("/api/config").json()["config"]
    srcs = {**m["sources"], "prov": {"type": "standard", "url": "http://prov.local", "token": ""}}
    r = c.put("/api/config", json={"sources": srcs, "pool": {**m["pool"], "pools": m["pool"]["pools"] + ["prov:demo_hot"]}})
    assert r.status_code == 200, r.text
    e.offline = False
    e._sources = None
    src = e.sources()["prov"]
    src.offline = False
    res = src.get_pool("demo_hot", None)
    assert res.key == "prov:demo_hot" and len(res.stocks) >= 3 and res.stocks[0]["code"].isdigit()
