"""下单层安全边界 + 网页改配置。"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from qfpy.exec import brokers
from qfpy.exec.brokers import LiveNotArmed, build_order_body, interpret_order_response, live_gate
from tests.conftest import ROOT, FakeMarket, add_position


def it(**kw):
    d = {"id": "t-1", "side": "buy", "symbol": "600000", "name": "浦发银行", "price": 10.03, "quantity": 500,
         "trading_date": "2026-11-10"}
    d.update(kw)
    return d


def test_order_body_numeric_limit_only():
    b = build_order_body(it())
    assert b == {"side": "buy", "symbol": "600000", "price": "10.03", "quantity": "500", "batch_count": 1,
                 "confirm": brokers.CONFIRM_PHRASE}
    for bad in ({"price": "市价"}, {"price": 0}, {"quantity": "all"}, {"quantity": 100.0}, {"symbol": "sh600000"},
                {"side": "short"}):
        with pytest.raises(ValueError):
            build_order_body(it(**bad))


def test_interpret_response_never_assumes_success():
    assert interpret_order_response(200, {"ok": True, "entrust_nos": ["123"]})[0] == "submitted"
    assert interpret_order_response(200, {"ok": True, "entrust_nos": []})[0] == "unknown"   # 没合同号
    assert interpret_order_response(400, {"ok": False, "message": "x"})[0] == "rejected"
    assert interpret_order_response(200, {"ok": False, "status": "failed"})[0] == "rejected"
    assert interpret_order_response(500, {})[0] == "unknown"


def test_live_gate_only_needs_mode(make_engine, monkeypatch):
    """取消实盘门槛：是否实盘只看 [execution].mode，不再要 [live].enabled 和 QF_LIVE_ARMED。"""
    e = make_engine("paper")
    cfg = e.cfg
    assert not live_gate(cfg)[0]
    cfg["execution"]["mode"] = "live"
    monkeypatch.delenv("QF_LIVE_ARMED", raising=False)
    cfg["live"]["enabled"] = False
    assert live_gate(cfg) == (True, [])
    e2 = make_engine("live")          # 没有环境变量、没有 enabled 也能直接起实盘
    assert e2.mode == "live" and e2.broker.label
    assert LiveNotArmed


class FakeResp:
    def __init__(self, code, body):
        self.status_code, self._b = code, body

    def json(self):
        return self._b


@pytest.mark.parametrize("resp, status, state", [
    (FakeResp(200, {"ok": True, "entrust_nos": ["A1"]}), "submitted", "unconfirmed_buy"),
    (TimeoutError("timeout"), "unknown", "uncertain"),
])
def test_live_broker_posts_only_order_once_and_records_unconfirmed(make_engine, monkeypatch, resp, status, state):
    """用假 httpx.post 模拟下单程序：只调 /api/order、只调一次（不重试），本地持仓记成待确认/未知。"""
    import httpx
    monkeypatch.setenv("QF_LIVE_ARMED", "I_ACCEPT_REAL_MONEY_RISK")
    e = make_engine("live", extra="\n[live]\nenabled = true\nbase_url = \"http://fake-ths:8765\"\n")
    e.switches.set_positions_auto(True, "pytest")
    urls = []

    def fake_post(url, json=None, timeout=None, headers=None):
        urls.append(url)
        if isinstance(resp, Exception):
            raise resp
        return resp

    monkeypatch.setattr(httpx, "post", fake_post)
    r = e.broker.place(it())
    assert urls == ["http://fake-ths:8765/api/order"]
    assert r["status"] == status
    p = e.book.get("600000")
    assert p["state"] == state
    # 这样的持仓到点不会自动卖
    fm = FakeMarket("2026-11-12", ["2026-11-10", "2026-11-11", "2026-11-12"], {"600000": (10.1, 10.0)})
    e.market = lambda: fm
    e.set_auto(True, "pytest")
    ev = e.check_exits(now_hm="14:50")["results"][0]
    assert ev["action"] == "manual_needed" and urls == ["http://fake-ths:8765/api/order"]


def test_source_never_calls_cancel_or_command():
    """代码里不存在调用 /api/command、/api/client/restart 的路径（只在 FORBIDDEN 拦截表里出现）；
    /api/cancel 只出现在 xiadan.py 的交易接口白名单和 post_cancel 里（只有实盘三重开关全开时才可能走到）。"""
    hits = []
    for f in (ROOT / "qfpy").rglob("*.py"):
        if f.parent.name == "devtools":      # 模拟服务端（服务端路由，不是调用方）
            continue
        s = f.read_text(encoding="utf-8")
        for m in re.finditer(r"[\"']/api/(cancel|command|client/restart|connect)", s):
            line = s[: m.start()].count("\n") + 1
            text = s.splitlines()[line - 1]
            if "FORBIDDEN" in text:
                continue
            if m.group(1) == "cancel" and f.name == "xiadan.py" and ("TRADE_PATHS = " in text or "_post_trade(" in text):
                continue
            hits.append(f"{f.name}:{line}")
    assert hits == []


# ---------------- 网页改配置 ----------------

def test_config_update_validates_and_hot_reloads(make_engine):
    e = make_engine("paper")
    store = e.store
    new = store.prepare_update({"exits": {**e.cfg["exits"], "take_profit_pct": 8.0},
                                "execution": {"mode": "live"}})          # 只读段被忽略
    store.commit(new)
    assert e.cfg["exits"]["take_profit_pct"] == 8.0 and e.cfg["execution"]["mode"] == "paper"
    text = store.path.read_text(encoding="utf-8")
    assert "take_profit_pct = 8.0" in text and 'mode = "paper"' in text
    assert store.path.with_suffix(".toml.bak").exists()
    with pytest.raises(ValueError, match="HH:MM"):
        store.prepare_update({"exits": {**e.cfg["exits"], "exit_time": "25:99"}})
    with pytest.raises(ValueError, match="by_pool"):
        store.prepare_update({"exits": {**e.cfg["exits"], "by_pool": {"gushi:gold_230": {"bogus": 1}}}})


def test_secrets_masked_and_preserved(make_engine):
    e = make_engine("paper", extra='\n[notify.dingtalk]\nwebhook = "https://oapi.dingtalk.com/robot/send?access_token=abcd1234"\nsecret = "SECxyz9876"\n')
    m = e.store.masked()
    assert "abcd1234" not in str(m["notify"]["dingtalk"]["webhook"]) and m["notify"]["dingtalk"]["secret"].endswith("9876")
    new = e.store.prepare_update({"notify": m["notify"]})
    assert new["notify"]["dingtalk"]["secret"] == "SECxyz9876"


def test_dingtalk_sign_known_vector():
    from qfpy.notify.channels import dingtalk_sign
    import base64, hashlib, hmac, urllib.parse
    sec, t = "SEC000", 1700000000000
    want = urllib.parse.quote_plus(base64.b64encode(hmac.new(sec.encode(), f"{t}\n{sec}".encode(), hashlib.sha256).digest()))
    assert dingtalk_sign(sec, t) == want


def test_notify_without_token_goes_to_log(make_engine, caplog):
    e = make_engine("paper", extra='\n[notify.dingtalk]\nwebhook = ""\n')
    e.cfg["notify"]["channels"] = ["dingtalk"]
    with caplog.at_level("INFO", logger="qf"):
        res = e.notifier.send_sync("test", "测试消息", "hello")
    assert res == [{"channel": "dingtalk", "status": "未配置 token/webhook，仅打印到日志"}]
    assert any("测试消息" in r.getMessage() for r in caplog.records)
