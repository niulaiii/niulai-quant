"""开放接口：插件加载、外部 HTTP 因子（推/拉）、外部数据源（推/拉）、REST API 鉴权和安全边界。全部离线。"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from qfpy import external as ext
from qfpy.factors import FACTOR_TYPES
from qfpy.factors.base import FactorContext
from qfpy.funnel import build_layers, run_buy_funnel
from qfpy.model import Candidate
from qfpy.plugins import load_plugins
from qfpy.web.api_v1 import FORBIDDEN_WORDS
from qfpy.web.server import create_app
from tests.conftest import FakeMarket, add_position

import httpx

REPO = Path(__file__).resolve().parents[1]
_real_send = httpx.Client.send     # TestClient 走进程内 ASGI，不联网；conftest 的断网在测试里才生效


def client(e, monkeypatch):
    monkeypatch.setattr(httpx.Client, "send", _real_send)
    return TestClient(create_app(e))
TOKEN = "t0ken-for-tests"
H = {"Authorization": f"Bearer {TOKEN}"}


def cands():
    return [Candidate("600001", "甲", ["x"], {"change_rate": 1.0}), Candidate("600002", "乙", ["x"], {"change_rate": 2.0}),
            Candidate("600003", "丙", ["x"], {"change_rate": 3.0})]


def run_layer(factors, cs, store=None, mode="all"):
    built = build_layers([{"name": "外部", "mode": mode, "factors": factors}], None, "buy")
    warns: list = []
    left, st = run_buy_funnel(built, cs, FactorContext("2026-11-10", None, {"side": "buy", "ext_store": store}),
                              lambda *_: None, lambda c: c.brief(), warns)
    return [c.code for c in left], st, warns


# ---------------- 插件 ----------------

def test_single_file_plugin_auto_registers(tmp_path):
    d = tmp_path / "plugins"
    d.mkdir()
    shutil.copy(REPO / "plugins" / "example_single_file.py", d / "example_single_file.py")
    (d / "_ignored.py").write_text("raise SystemExit('不该加载')", encoding="utf-8")
    (d / "broken.py").write_text("import no_such_module_xyz", encoding="utf-8")
    (d / "off.py").write_text("PLUGIN = {'enabled': False}\nraise SystemExit('不该执行')", encoding="utf-8")
    info = {i["file"]: i for i in load_plugins(d)}
    assert set(info) == {"example_single_file.py", "broken.py", "off.py"}
    ok = info["example_single_file.py"]
    assert ok["ok"] and set(ok["registered"]["factors"]) == {"small_gap", "profit_giveback"} and ok["registered"]["sources"] == ["static_list"]
    assert FACTOR_TYPES["profit_giveback"].sides == ("sell",) and FACTOR_TYPES["small_gap"].plugin == "example_single_file"
    assert not info["broken.py"]["ok"] and "no_such_module_xyz" in info["broken.py"]["error"]
    assert info["off.py"]["note"] == "已禁用"


def test_new_plugin_file_picked_up_on_reload(make_engine):
    e = make_engine("paper")
    pd = e.root / "plugins"
    pd.mkdir(exist_ok=True)
    (pd / "hot_reload_demo.py").write_text('''
from qfpy.factors import Factor, Outcome, register_factor
@register_factor
class AlwaysPass(Factor):
    kind = "always_pass_demo"
    title = "总是通过"
    sides = ("buy", "sell")
    def evaluate(self, c, ctx):
        return Outcome.ok(1.0, "ok")
''', encoding="utf-8")
    got = {p["name"]: p for p in e.reload_plugins()}
    assert got["hot_reload_demo"]["ok"] and "always_pass_demo" in FACTOR_TYPES
    left, _, _ = run_layer([{"kind": "always_pass_demo"}], cands())
    assert len(left) == 3


def test_directory_plugin_still_works(tmp_path):
    d = tmp_path / "plugins"
    shutil.copytree(REPO / "plugins" / "example_ma_trend", d / "example_ma_trend")
    info = load_plugins(d)
    assert info[0]["ok"] and "ma_trend" in info[0]["registered"]["factors"] or "ma_trend" in FACTOR_TYPES


# ---------------- 外部因子：推 ----------------

def test_normalize_results_formats_and_errors():
    assert ext.normalize_results({"results": [{"code": "600001", "pass": 1}]})["600001"]["pass"] is True
    assert ext.normalize_results({"600001": {"score": "0.5"}})["600001"]["score"] == 0.5
    assert ext.normalize_results([{"symbol": "600001", "passed": False}])["600001"]["pass"] is False
    for bad in ([{"code": "60001", "pass": True}], [{"code": "600001"}], [{"code": "600001", "score": "abc"}], "x"):
        with pytest.raises(ValueError):
            ext.normalize_results(bad)


def test_external_push_factor(tmp_path):
    store = ext.ExternalStore(tmp_path)
    store.put_factor_results("mine", [{"code": "600001", "pass": True}, {"code": "600002", "pass": False},
                                      {"code": "600003", "score": 0.9}], "2026-11-10")
    left, st, _ = run_layer([{"kind": "external", "params": {"name": "mine"}}], cands(), store)
    assert left == ["600001", "600003"]       # auto：有 pass 看 pass，只有 score 时 score ≥ 0
    left, _, _ = run_layer([{"kind": "external", "params": {"name": "mine", "use": "score", "min_score": 0.5}}], cands(), store)
    assert left == ["600003"]
    # 当天没有结果：默认整层淘汰；missing=pass 放行
    left, st, _ = run_layer([{"kind": "external", "params": {"name": "other"}}], cands(), store)
    assert left == [] and "还没有收到" in st[0]["dropped"][0]["note"]
    left, _, _ = run_layer([{"kind": "external", "params": {"name": "other", "missing": "pass"}}], cands(), store)
    assert len(left) == 3


def test_external_score_becomes_field_for_expr(tmp_path):
    store = ext.ExternalStore(tmp_path)
    store.put_factor_results("m", [{"code": c, "score": s} for c, s in (("600001", 0.1), ("600002", 0.6), ("600003", 0.9))], "2026-11-10")
    built = build_layers([{"name": "拿分", "factors": [{"kind": "external", "params": {"name": "m", "use": "score", "min_score": 0}}]},
                          {"name": "表达式", "factors": [{"kind": "expr", "params": {"expr": "ext_m >= 0.5 and change_rate >= 2.5"}}]}],
                         None, "buy")
    left, _ = run_buy_funnel(built, cands(), FactorContext("2026-11-10", None, {"ext_store": store}), lambda *_: None,
                             lambda c: c.brief(), [])
    assert [c.code for c in left] == ["600003"]


# ---------------- 外部因子：拉 ----------------

def test_http_pull_factor_batches_one_request(monkeypatch):
    calls = []

    def fake(method, url, body=None, headers=None, timeout=10):
        calls.append((method, url, body, headers))
        return {"results": [{"code": c["code"], "score": c["fields"]["change_rate"] / 3} for c in body["candidates"]]}
    monkeypatch.setattr(ext, "http_json", fake)
    f = {"kind": "http", "params": {"url": "http://127.0.0.1:9100/factor", "name": "demo", "use": "score", "min_score": 0.5,
                                    "token": "abc"}}
    left, st, _ = run_layer([f], cands())
    assert left == ["600002", "600003"] and len(calls) == 1           # 整层一次请求
    m, url, body, h = calls[0]
    assert m == "POST" and body["side"] == "buy" and body["trading_date"] == "2026-11-10" and len(body["candidates"]) == 3
    assert h == {"Authorization": "Bearer abc"}


@pytest.mark.parametrize("on_error,expect", [("fail", 0), ("pass", 3)])
def test_http_pull_factor_errors(monkeypatch, on_error, expect):
    monkeypatch.setattr(ext, "http_json", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("连不上")))
    left, st, _ = run_layer([{"kind": "http", "params": {"url": "http://x/f", "on_error": on_error}}], cands())
    assert len(left) == expect


def test_http_factor_real_function_is_offline_in_tests():
    """conftest 把 httpx 封了，真的 http_json 在测试里一定失败（证明测试不联网）。"""
    left, st, _ = run_layer([{"kind": "http", "params": {"url": "http://127.0.0.1:9/f"}}], cands())
    assert left == [] and "请求失败" in st[0]["dropped"][0]["note"]


def test_http_factor_in_sell_chain(make_engine, monkeypatch):
    monkeypatch.setattr(ext, "http_json", lambda m, u, body=None, headers=None, timeout=10:
                        {"results": [{"code": c["code"], "pass": True, "note": "模型说卖"} for c in body["candidates"]]})
    e = make_engine("paper", extra='''
[[sell_funnel.layers]]
id = "model"
name = "模型卖出"
[[sell_funnel.layers.factors]]
kind = "http"
params = { url = "http://127.0.0.1:9100/sell" }
''')
    fm = FakeMarket("2026-11-10", ["2026-11-09", "2026-11-10"], {"600000": (10.1, 10.0)})
    e.market = lambda: fm
    e.set_auto(True, "pytest")
    add_position(e)
    ev = e.check_exits(now_hm="10:00")["results"][0]
    assert ev["decision"] == "model" and ev["action"] == "filled" and "模型说卖" in ev["reason"]


# ---------------- 外部数据源 ----------------

def test_push_source_pool_and_fields_in_run(make_engine):
    e = make_engine("paper", extra='''
[sources.mypush]
type = "push"
[[buy_funnel.layers]]
name = "外部字段"
[[buy_funnel.layers.factors]]
kind = "expr"
params = { expr = "ext_hot >= 3" }
''')
    e.store.commit(e.store.prepare_update({"pool": {"pools": ["mypush:hot"]}}))
    e._sources = None
    e.ext_store.put_pool("mypush", "hot", [{"code": "600000", "name": "浦发"}, {"code": "000001", "name": "平安", "fields": {"ext_hot": 1}}],
                         "2026-11-10", "热门")
    e.ext_store.put_fields("mypush", [{"code": "600000", "fields": {"ext_hot": 5}}], "2026-11-10")
    fm = FakeMarket("2026-11-10", ["2026-11-09", "2026-11-10"], {"600000": (10.1, 10.0), "000001": (10.1, 10.0)})
    e.market = lambda: fm
    rec = e.run(date="2026-11-10", trade=False)
    layer = [s for s in rec["stages"] if s.get("type") == "layer"][0]
    assert layer["input"] == 2 and [k["code"] for k in layer["kept"]] == ["600000"]


def test_push_source_missing_day_reports_error(tmp_path):
    from qfpy.datasources.external_src import PushSource
    s = PushSource("p", {"type": "push"}, tmp_path)
    assert "还没有收到" in s.get_pool("hot", "2026-11-10").error
    ext.ExternalStore(tmp_path).put_pool("p", "hot", ["600000"], "2026-11-09")
    s2 = PushSource("p", {"type": "push", "use_latest_if_missing": True}, tmp_path)
    assert s2.get_pool("hot", "2026-11-10").stocks[0]["code"] == "600000"


def test_http_source_pull(tmp_path, monkeypatch):
    from qfpy.datasources.external_src import HttpSource
    seen = []

    def fake(method, url, body=None, headers=None, timeout=10):
        seen.append((method, url))
        if "/pool/" in url:
            return {"name": "示例", "stocks": [{"code": "600000", "fields": {"ext_x": 1}}, "000001", {"code": "bad"}]}
        if "/fields" in url:
            return {"items": [{"code": c, "fields": {"ext_y": 2}} for c in body["codes"]]}
        if "/minute/" in url:
            return {"preclose": 10, "points": [{"t": "09:31", "p": 10.1}]}
        return {"bars": [{"date": "2026-11-09", "close": 1}, {"date": "2026-10-01", "close": 1}]}
    monkeypatch.setattr(ext, "http_json", fake)
    s = HttpSource("h", {"type": "http", "pool_url": "http://x/pool/{key}?d={date}", "fields_url": "http://x/fields",
                         "minute_url": "http://x/minute/{code}/{date}", "daily_url": "http://x/daily/{code}?s={start}&e={end}",
                         "rate_per_min": 6000}, tmp_path)
    assert set(s.capabilities) == {"pool", "fields", "minute", "daily"}
    p = s.get_pool("hot", "2026-11-10")
    assert [x["code"] for x in p.stocks] == ["600000", "000001"] and p.key == "h:hot" and seen[0][1] == "http://x/pool/hot?d=2026-11-10"
    assert s.get_fields(["600000"], "2026-11-10") == {"600000": {"ext_y": 2.0}}
    assert s.get_minute("600000", "2026-11-10")["points"][0]["p"] == 10.1
    assert [b["date"] for b in s.get_daily("600000", "2026-11-01", "2026-11-10")] == ["2026-11-09"]
    assert HttpSource("h2", {"type": "http", "pool_url": "http://x"}, tmp_path, offline=True).capabilities == ("pool",)


# ---------------- REST API ----------------

@pytest.fixture
def api(make_engine, monkeypatch):
    monkeypatch.setenv("QF_API_TOKEN", TOKEN)
    e = make_engine("paper")
    return e, client(e, monkeypatch)


def test_api_requires_token(make_engine, monkeypatch):
    monkeypatch.delenv("QF_API_TOKEN", raising=False)
    e = make_engine("paper")
    tc = client(e, monkeypatch)
    r = tc.get("/api/v1/status", headers=H)
    assert r.status_code == 401 and "未配置 API token" in r.json()["detail"]     # 没配 token = 不开放
    monkeypatch.setenv("QF_API_TOKEN", TOKEN)
    assert tc.get("/api/v1/status").status_code == 401
    assert tc.get("/api/v1/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert tc.get("/api/v1/status", headers=H).status_code == 200
    assert tc.get("/api/v1/status", headers={"X-API-Token": TOKEN}).status_code == 200


def test_api_token_from_config(make_engine, monkeypatch):
    monkeypatch.delenv("QF_API_TOKEN", raising=False)
    e = make_engine("paper", extra='\n[api]\ntoken = "cfg-token"\n')
    tc = client(e, monkeypatch)
    assert tc.get("/api/v1/status", headers={"Authorization": "Bearer cfg-token"}).status_code == 200
    assert "cfg-token" not in tc.get("/api/config").text          # 网页配置里掩码


def test_api_has_no_trading_endpoints(api):
    _, tc = api
    spec = tc.get("/api/v1/openapi.json", headers=H)
    paths = [p for p in tc.app.openapi()["paths"] if p.startswith("/api/v1")]
    assert len(paths) >= 15
    for p in paths:
        assert not any(w in p.lower() for w in FORBIDDEN_WORDS), p
    assert spec.status_code == 200 and all(p.startswith("/api/v1") for p in spec.json()["paths"])
    # 触发运行永远不交易
    assert tc.post("/api/v1/runs", headers=H, json={"trade": True}).status_code == 400


def test_api_run_is_select_only(api):
    e, tc = api
    fm = FakeMarket("2026-11-10", ["2026-11-09", "2026-11-10"], {"600000": (10.1, 10.0)})
    e.market = lambda: fm
    e.set_auto(True, "pytest")
    seen = {}
    orig = e.run

    def spy(**kw):
        seen.update(kw)
        return orig(**kw)
    e.run = spy
    assert tc.post("/api/v1/runs", headers=H, json={"date": "2026-11-10"}).json()["trade"] is False
    import time
    for _ in range(100):
        st = tc.get("/api/v1/runs/current/state", headers=H).json()
        if not st["running"]:
            break
        time.sleep(0.05)
    assert seen["trade"] is False and st["run_id"] and st["error"] is None
    latest = tc.get("/api/v1/funnel/latest", headers=H).json()
    assert latest["id"] == st["run_id"] and latest["stages"]
    assert tc.get("/api/v1/signals", headers=H).json() == []            # 没有产生任何信号/委托


def test_api_funnel_templates_and_factor_results(api):
    e, tc = api
    assert any(t["key"] == "auction_strong" for t in tc.get("/api/v1/templates?side=buy", headers=H).json())
    assert tc.post("/api/v1/funnels/buy/apply-template", headers=H, json={"key": "steady_dip"}).status_code == 200
    got = tc.get("/api/v1/funnels/buy", headers=H).json()
    assert got["explicit"] and got["layers"][2]["mode"] == "any"
    bad = tc.put("/api/v1/funnels/sell", headers=H, json={"layers": [{"factors": [{"kind": "above_vwap"}]}]})
    assert bad.status_code == 400 and "不能用在卖出因子链" in bad.json()["detail"]
    v = tc.post("/api/v1/funnels/sell/validate", headers=H, json={"layers": [{"factors": [{"kind": "stop_loss"}]}]}).json()
    assert v == {"ok": True, "errors": []}
    sell = tc.get("/api/v1/factors?side=sell", headers=H).json()["factors"]
    assert {"stop_loss", "below_vwap", "drawdown_from_high", "expr", "http", "external"} <= {f["kind"] for f in sell}
    r = tc.post("/api/v1/factor-results/my_score", headers=H, json={"date": "2026-11-10", "results": [{"code": "600000", "score": 1}]})
    assert r.json()["count"] == 1
    assert tc.post("/api/v1/factor-results/bad name", headers=H, json={"results": []}).status_code in (400, 404)
    assert tc.post("/api/v1/factor-results/x", headers=H, json={"results": [{"code": "1"}]}).status_code == 400
    assert tc.get("/api/v1/factor-results/my_score?date=2026-11-10", headers=H).json()["items"]["600000"]["score"] == 1.0
    assert tc.get("/api/v1/factor-results", headers=H).json()[0]["name"] == "my_score"


def test_api_sources_crud_and_push(api):
    e, tc = api
    assert tc.put("/api/v1/sources/nope/pools/hot", headers=H, json={"stocks": []}).status_code == 404
    assert tc.put("/api/v1/sources/mypush", headers=H, json={"type": "bogus"}).status_code == 400
    assert tc.put("/api/v1/sources/mypush", headers=H, json={"type": "push"}).json()["saved"]
    assert tc.put("/api/v1/sources/mypush/pools/hot", headers=H,
                  json={"date": "2026-11-10", "stocks": ["600000", {"code": "000001", "fields": {"ext_a": 1}}]}).json()["count"] == 2
    assert tc.post("/api/v1/sources/mypush/fields", headers=H, json={"date": "2026-11-10", "items": [{"code": "600000", "fields": {"x": "no"}}]}).status_code == 400
    assert tc.get("/api/v1/sources/mypush/pools", headers=H).json()[0]["key"] == "hot"
    names = [s["name"] for s in tc.get("/api/v1/sources", headers=H).json()]
    assert "mypush" in names and "push" in [t["type"] for t in tc.get("/api/v1/source-types", headers=H).json()]
    assert tc.delete("/api/v1/sources/mypush", headers=H).json()["deleted"]
    assert "mypush" not in e.cfg["sources"]


def test_api_read_only_mode(make_engine, monkeypatch):
    monkeypatch.setenv("QF_API_TOKEN", TOKEN)
    e = make_engine("paper", extra="\n[api]\nallow_config_write = false\n")
    tc = client(e, monkeypatch)
    assert tc.post("/api/v1/funnels/buy/apply-template", headers=H, json={"key": "gold_230"}).status_code == 403
    assert tc.put("/api/v1/sources/x", headers=H, json={"type": "push"}).status_code == 403
    assert tc.get("/api/v1/funnels/buy", headers=H).status_code == 200


def test_api_positions_and_signals(api):
    e, tc = api
    add_position(e)
    pos = tc.get("/api/v1/positions", headers=H).json()
    assert pos["positions"][0]["symbol"] == "600000"
    assert isinstance(tc.get("/api/v1/signals?side=sell", headers=H).json(), list)


def test_web_config_rules_for_api_and_live_sections(api):
    """网页「设置」页现在能改 api / live / execution，但：token 太短拒绝；切实盘要二次确认 + 三重开关；
    监听地址（web）和第三道开关用的环境变量名（arm_env/arm_value）改不了。/api/v1 没有通用配置写入口。"""
    e, tc = api
    r = tc.put("/api/config", json={"api": {**e.cfg["api"], "token": "hijack"}})
    assert r.status_code == 400 and "12" in r.json()["detail"] and e.cfg["api"]["token"] == ""
    r = tc.put("/api/config", json={"web": {"host": "0.0.0.0", "port": 80},
                                    "live": {**e.cfg["live"], "arm_env": "HOME", "arm_value": "/root"}})
    assert e.cfg["web"]["host"] == "127.0.0.1" and e.cfg["live"]["arm_env"] == "QF_LIVE_ARMED"
    assert e.cfg["live"]["arm_value"] == "I_ACCEPT_REAL_MONEY_RISK"
    paths = {rt.path for rt in tc.app.routes if getattr(rt, "path", "").startswith("/api/v1")}
    assert not any(p.rstrip("/") in ("/api/v1/config",) for p in paths)
