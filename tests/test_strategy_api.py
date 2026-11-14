"""这一轮新增：策略/模板/因子库/形态信号接口、外部因子和数据源绑定、回测自选策略、实验性因子回测跳过、涨跌停日期。"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from qfpy.factors.base import FactorContext, build_factor
from qfpy.model import Candidate
from qfpy.web.server import create_app

REPO = Path(__file__).resolve().parents[1]
_real_send = httpx.Client.send
TOKEN = "t0ken-for-tests"
H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def web(make_engine, monkeypatch):
    monkeypatch.setenv("QF_API_TOKEN", TOKEN)
    e = make_engine("paper", extra='\n[sources.mypush]\ntype = "push"\n'
                                   '\n[sources.myhttp]\ntype = "http"\nfields_url = "https://example.invalid/f"\n'
                                   'factor_url = "https://example.invalid/factor"\nfactor_names = "ai_score"\nfields = "hot"\n')
    sent = []
    monkeypatch.setattr(e.notifier, "notify", lambda ev, title, text, **kw: sent.append((ev, title, text)))
    monkeypatch.setattr(httpx.Client, "send", _real_send)
    return e, TestClient(create_app(e)), sent


# ---------------- 涨跌停规则：ST 5% → 10% 的日期 ----------------
def test_st_limit_switch_date_is_2026_07_06():
    from qfpy.limits import limit_pct
    assert limit_pct("600000", date(2026, 7, 3), st=True) == pytest.approx(0.05) or limit_pct("600000", date(2026, 7, 3), st=True) == 5
    before, on = limit_pct("600000", date(2026, 7, 3), st=True), limit_pct("600000", date(2026, 7, 6), st=True)
    assert on == before * 2                                      # 沪深《交易规则（2026年修订）》2026-07-06 起
    assert limit_pct("600000", date(2025, 7, 7), st=True) == before   # 2025 年那个日期不应该生效
    assert limit_pct("000001", date(2026, 7, 6), st=True) == on
    assert limit_pct("600000", date(2026, 7, 3)) == on           # 非 ST 主板一直是 10%


# ---------------- 策略接口 ----------------
def test_strategy_crud_activate_and_export(web):
    e, tc, sent = web
    act = tc.get("/api/strategies-active").json()
    assert act["buy"]["name"] and act["sell"]["name"] and "两回事" in act["note"]
    lst = tc.get("/api/strategies/buy").json()
    cur = lst["items"][0]
    assert cur["active"]
    # 复制为新策略
    cp = tc.post(f"/api/strategies/buy/{cur['id']}/copy", json={"name": "试验版"}).json()
    assert cp["name"] == "试验版" and not cp.get("active")
    # 改名
    assert tc.post(f"/api/strategies/buy/{cp['id']}/rename", json={"name": "试验版 2"}).json()["name"] == "试验版 2"
    # 保存前先看差异
    content = tc.get(f"/api/strategies/buy/{cp['id']}").json()["content"]
    content["select"]["top_n"] = 3
    dry = tc.put(f"/api/strategies/buy/{cp['id']}", json={"content": content, "dry_run": True}).json()
    assert dry["saved"] is False and dry["changes"]                # 只列差异，不保存
    tc.put(f"/api/strategies/buy/{cp['id']}", json={"content": content})
    # 设为生效必须确认
    assert tc.post(f"/api/strategies/buy/{cp['id']}/activate", json={}).status_code == 400
    r = tc.post(f"/api/strategies/buy/{cp['id']}/activate", json={"confirm": True})
    assert r.status_code == 200, r.text
    assert tc.get("/api/strategies-active").json()["buy"]["name"] == "试验版 2"
    assert e.cfg["select"]["top_n"] == 3                         # 写进了 pipeline.toml
    assert any("试验版 2" in t for _, t, x in sent) or any("试验版 2" in x for _, t, x in sent)
    # 生效的不能删
    assert tc.delete(f"/api/strategies/buy/{cp['id']}").status_code == 400
    # 导出 → 先列差异 → 导入成新的一套
    exp = tc.get(f"/api/strategies/buy/{cp['id']}/export").json()
    pv = tc.post("/api/strategies/buy/import/preview", json={"data": exp, "compare_to": cur["id"]}).json()
    assert "changes" in pv and not pv["errors"]
    imp = tc.post("/api/strategies/buy/import", json={"data": exp, "name": "导入的"}).json()
    assert imp["name"] == "导入的" and not imp.get("active")
    assert len(tc.get("/api/strategies/buy").json()["items"]) == 3


def test_templates_and_validate(web):
    _, tc, _ = web
    tpls = tc.get("/api/strategy-templates/buy").json()
    assert tpls and any(t.get("builtin") for t in tpls)
    b = next(t for t in tpls if t.get("builtin"))
    assert tc.delete(f"/api/strategy-templates/buy/{b['id']}").status_code == 400     # 自带的不能删
    pv = tc.post(f"/api/strategy-templates/buy/{b['id']}/preview", json={}).json()
    assert "changes" in pv and "content" in pv
    sid = tc.get("/api/strategies/buy").json()["items"][0]["id"]
    mine = tc.post("/api/strategy-templates/buy", json={"title": "我的模板", "from_id": sid}).json()
    assert not mine.get("builtin")
    assert tc.post("/api/strategy-templates/buy/default", json={"id": mine["id"]}).status_code == 200
    assert tc.delete(f"/api/strategy-templates/buy/{mine['id']}").status_code == 200
    bad = tc.post("/api/strategies/buy/validate", json={"content": {"layers": [
        {"name": "x", "factors": [{"kind": "no_such_factor"}]}]}}).json()
    assert bad["ok"] is False and bad["errors"]


def test_v1_write_requires_allow_config_write(web, make_engine, monkeypatch):
    e, tc, _ = web
    sid = tc.get("/api/v1/strategies/buy", headers=H).json()["items"][0]["id"]
    assert tc.post(f"/api/v1/strategies/buy/{sid}/copy", headers=H, json={}).status_code == 200   # 默认允许（和原版一致）
    assert tc.get("/api/v1/catalog?side=buy", headers=H).status_code == 200
    assert tc.get("/api/v1/strategies/buy").status_code == 401   # 没 token
    e.cfg.setdefault("api", {})["allow_config_write"] = False
    for r in (tc.post(f"/api/v1/strategies/buy/{sid}/copy", headers=H, json={}),
              tc.post(f"/api/v1/strategies/buy/{sid}/activate", headers=H, json={"confirm": True}),
              tc.post("/api/v1/pattern-signals", headers=H, json={"name": "x", "conditions": []}),
              tc.delete(f"/api/v1/strategies/buy/{sid}", headers=H)):
        assert r.status_code == 403, r.text                       # 只读时写操作一律拒绝
    assert tc.get("/api/v1/strategies/buy", headers=H).status_code == 200    # 读照常


def test_no_new_order_paths():
    """新增的接口里不能有下单/撤单/发命令的路径，也不能调用它们。"""
    for f in ("qfpy/web/strategy_api.py", "qfpy/viewsvc.py", "qfpy/extsources.py", "qfpy/settings_docs.py"):
        src = (REPO / f).read_text(encoding="utf-8")
        for bad in ("/api/order", "/api/cancel", "/api/command", "place_order", "submit_order"):
            assert bad not in src, (f, bad)


def test_catalog_docs_and_signals(web):
    e, tc, _ = web
    cat = tc.get("/api/catalog?side=buy").json()
    assert len(cat["categories"]) >= 10 and cat["factors"] and cat["fields"]
    ext = [f for f in cat["factors"] if f.get("external_source")]
    assert {f["kind"] for f in ext} >= {"external", "http", "ext_field"}
    assert all("外部数据源" in f["tags"] for f in ext)
    assert {s["id"] for s in cat["ext_sources"]} >= {"mypush", "myhttp", "@api"}
    docs = tc.get("/api/settings-docs").json()
    assert "对账" in docs["live.reconcile.not_found_grace_secs"]["what"]
    assert docs["sizing.buy_price_offset_pct"]["empty"]
    # 形态信号：校验 → 保存 → 改名 → 删除
    conds = [{"left": "close", "op": ">", "right": "ma5"}, {"left": "close", "op": ">", "right": "close", "rightDays": 1}]
    v = tc.post("/api/pattern-signals/validate", json={"conditions": conds}).json()
    assert v["ok"], v
    assert v["conditions"][0]["right"] == "field:ma5" and "且" in v["text"]
    assert tc.post("/api/pattern-signals/validate", json={"conditions": [{"left": "nope", "op": ">", "right": "1"}]}).json()["ok"] is False
    s = tc.post("/api/pattern-signals", json={"name": "我的信号", "conditions": conds}).json()
    assert tc.post(f"/api/pattern-signals/{s['id']}/rename", json={"name": "改名了"}).status_code == 200
    assert any(x["name"] == "改名了" for x in tc.get("/api/pattern-signals").json())
    assert tc.delete(f"/api/pattern-signals/{s['id']}").status_code == 200


def test_formula_check(web):
    _, tc, _ = web
    assert tc.post("/api/formula/check", json={"expr": "change_rate > 3 and amount > 1e8"}).json()["ok"]
    r = tc.post("/api/formula/check", json={"expr": "change_rate >"}).json()
    assert r["ok"] is False and r["error"]
    r = tc.post("/api/formula/check", json={"expr": "pnl_pct > 3", "side": "buy"}).json()
    assert r["ok"] is False and "卖出" in r["error"]
    # 试算不再要求先跑一次：有股池就直接现拉候选（没运行记录也能试算）
    p = tc.post("/api/formula/preview", json={"expr": "change_rate > 3"}).json()
    assert p["ok"] is True and p["checked"] >= 1
    assert "股池" in p["basis"]


# ---------------- 选股结果按层/按票 ----------------
REC = {"id": "r1", "trading_date": "2026-11-11", "strategy": {"buy": "当前买入策略"},
       "picks": [{"code": "600001"}],
       "stages": [
           {"index": 0, "type": "pool", "label": "前池", "input": 3,
            "kept": [{"code": "600001", "name": "甲", "fields": {"price": 10}}, {"code": "600002", "name": "乙"},
                     {"code": "600003", "name": "丙"}], "dropped": []},
           {"index": 1, "type": "layer", "label": "第一层", "input": 3,
            "kept": [{"code": "600001"}, {"code": "600002"}],
            "dropped": [{"code": "600003", "note": "换手率：数据缺失", "checks": [{"missing": True}]}]},
           {"index": 2, "type": "select", "label": "打分取前 1", "input": 2,
            "kept": [{"code": "600001", "value": 0.9, "score_parts": {"change_rate": 0.9}}],
            "dropped": [{"code": "600002", "value": 0.1, "note": "排名靠后"}]}]}


def test_run_layers_and_by_stock():
    from qfpy import viewsvc
    layers = viewsvc.run_layers(REC)
    assert [l["kept"] for l in layers] == [3, 2, 1] and layers[1]["missing"] == 1
    rows = {s["code"]: s for s in viewsvc.run_by_stock(REC)}
    assert rows["600001"]["picked"] and rows["600001"]["score_parts"] == {"change_rate": 0.9}
    assert rows["600003"]["dropped_at"] == "第一层" and rows["600003"]["missing"]
    assert rows["600002"]["dropped_at"] == "打分取前 1" and rows["600002"]["reached"] == "第一层"
    assert viewsvc.run_by_stock(REC)[0]["code"] == "600001"


def test_run_endpoints(web, monkeypatch):
    e, tc, _ = web
    monkeypatch.setattr(e, "get_run", lambda rid: REC if rid in ("r1", "latest") else None)
    assert tc.get("/api/runs/latest/layers").json()["layers"][2]["kept"] == 1
    assert len(tc.get("/api/runs/r1/stocks").json()["stocks"]) == 3
    assert tc.get("/api/runs/r1/stocks?q=丙").json()["stocks"][0]["code"] == "600003"
    assert tc.get("/api/runs/r1/stocks/600002").json()["dropped_at"] == "打分取前 1"
    assert tc.get("/api/runs/nope/layers").status_code == 404
    assert tc.get("/api/exit-checks").json() == []


# ---------------- 外部因子和数据源绑定 ----------------
def test_ext_sources_listing_and_availability(web):
    e, tc, _ = web
    srcs = {s["id"]: s for s in tc.get("/api/ext-sources").json()["sources"]}
    assert srcs["mypush"]["available"] is False and "没收到" in srcs["mypush"]["status"]
    assert srcs["myhttp"]["available"] is None and srcs["myhttp"]["fields"] == ["hot"]
    assert [f["name"] for f in srcs["myhttp"]["factors"]] == ["ai_score"]
    day = e.market().latest_trading_date() if hasattr(e.market(), "latest_trading_date") else None
    from qfpy.util import today_str
    e.ext_store.put_fields("mypush", {"600001": {"hot": 5}}, today_str())
    e.ext_store.put_factor_results("rank1", [{"code": "600001", "pass": True}], today_str(), source="mypush")
    e.ext_store.put_factor_results("loose", [{"code": "600001", "pass": True}], today_str())
    srcs = {s["id"]: s for s in tc.get("/api/ext-sources").json()["sources"]}
    assert srcs["mypush"]["available"] is True and "hot" in srcs["mypush"]["fields"]
    assert [f["name"] for f in srcs["mypush"]["factors"]] == ["rank1"]
    assert [f["name"] for f in srcs["@api"]["factors"]] == ["loose"]
    from qfpy import extsources
    extsources.record(e.data_dir, "myhttp", False, "连不上")
    srcs = {s["id"]: s for s in tc.get("/api/ext-sources").json()["sources"]}
    assert srcs["myhttp"]["available"] is False and "连不上" in srcs["myhttp"]["status"]


def _ctx(e, day):
    return FactorContext(day, e.root, {"side": "buy", **e.ext_ctx(day)})


def test_external_factor_bound_source_missing_data(web):
    e, _, _ = web
    from qfpy.util import today_str
    day = today_str()
    c = Candidate("600001", "甲", ["x"], {})
    f = build_factor({"kind": "external", "params": {"source": "mypush", "name": "rank1"}}, e.root)
    o = f.evaluate(c, _ctx(e, day))
    assert o.missing and not o.passed and "mypush" in o.note       # 数据源还没推送过 = 不可用 → 数据缺失
    e.ext_store.put_factor_results("rank1", [{"code": "600001", "pass": True}], day, source="mypush")
    f = build_factor({"kind": "external", "params": {"source": "mypush", "name": "rank1"}}, e.root)
    assert f.evaluate(c, _ctx(e, day)).passed
    o = f.evaluate(Candidate("600009", "癸", ["x"], {}), _ctx(e, day))
    assert o.missing and not o.passed                               # 这只票没拿到 → 数据缺失
    f2 = build_factor({"kind": "external", "params": {"source": "mypush", "name": "rank1", "missing": "pass"}}, e.root)
    o2 = f2.evaluate(Candidate("600009", "癸", ["x"], {}), _ctx(e, day))
    assert o2.missing and not o2.passed                            # 因子本身只报「数据缺失」
    from qfpy.factors.base import apply_missing
    assert apply_missing(o2, {"missing": "pass"}).passed and not apply_missing(o2, {}).passed   # 放行由统一口径处理，默认淘汰
    # 没配置的数据源
    f3 = build_factor({"kind": "external", "params": {"source": "ghost", "name": "rank1"}}, e.root)
    o = f3.evaluate(c, _ctx(e, day))
    assert o.missing and "没有配置" in o.note


def test_ext_field_factor(web):
    e, _, _ = web
    from qfpy.util import today_str
    day = today_str()
    e.ext_store.put_fields("mypush", {"600001": {"hot": 5}}, day)
    spec = {"kind": "ext_field", "params": {"source": "mypush", "field": "hot", "op": ">=", "value": 3}}
    f = build_factor(spec, e.root)
    c = Candidate("600001", "甲", ["x"], {})
    c.src_fields = {"mypush": {"hot": 5}}
    assert f.evaluate(c, _ctx(e, day)).passed
    c2 = Candidate("600002", "乙", ["x"], {"hot": 9})
    c2.src_fields = {"other": {"hot": 9}}                          # 别的数据源给的同名字段不算
    o = f.evaluate(c2, _ctx(e, day))
    assert o.missing and not o.passed
    with pytest.raises(ValueError):
        build_factor({"kind": "ext_field", "params": {"source": "mypush", "field": "", "op": ">=", "value": 3}}, e.root)


def test_http_factor_uses_bound_source(web, monkeypatch):
    e, _, _ = web
    from qfpy import external as ext
    calls = []

    def fake(method, url, body, headers, timeout):
        calls.append(url)
        return {"results": [{"code": "600001", "pass": True}]}
    monkeypatch.setattr(ext, "http_json", fake)
    from qfpy import extsources
    extsources.record(e.data_dir, "myhttp", True)
    day = "2026-11-11"
    f = build_factor({"kind": "http", "params": {"source": "myhttp", "name": "ai_score"}}, e.root)
    ctx = _ctx(e, day)
    cs = [Candidate("600001", "甲", ["x"], {}), Candidate("600002", "乙", ["x"], {})]
    f.prepare(cs, ctx)
    assert calls == ["https://example.invalid/factor"]
    assert f.evaluate(cs[0], ctx).passed
    assert f.evaluate(cs[1], ctx).missing

    def boom(*a, **k):
        raise OSError("down")
    monkeypatch.setattr(ext, "http_json", boom)
    f = build_factor({"kind": "http", "params": {"source": "myhttp", "name": "ai_score"}}, e.root)
    f.prepare(cs, ctx)
    o = f.evaluate(cs[0], ctx)
    assert o.missing and not o.passed                                 # 请求失败 → 数据缺失
    assert extsources.last_status(e.data_dir, "myhttp")["ok"] is False


# ---------------- 回测：自选策略、实验性因子跳过 ----------------
def test_backtest_strategy_choices_and_resolve(web):
    e, tc, _ = web
    ch = tc.get("/api/backtest/strategies").json()
    for side in ("buy", "sell"):
        opts = ch[side]["options"]
        assert ch[side]["default"] == next(o["ref"] for o in opts if o["active"])
        assert any(o["kind"] == "template" for o in opts)
    from qfpy.backtest import backtest_cfg
    tpl = next(o["ref"] for o in ch["buy"]["options"] if o["kind"] == "template")
    cfg, used = backtest_cfg(e, tpl, None)
    assert used["buy"]["kind"] == "template" and used["sell"]["kind"] == "strategy"
    assert e.cfg is not cfg                                           # 只在副本上套用，不改生效配置
    with pytest.raises(ValueError):
        backtest_cfg(e, "s:nope", None)


def test_backtest_records_strategy_and_skips_experimental(web, monkeypatch):
    e, tc, _ = web
    sid = tc.get("/api/strategies/buy").json()["items"][0]["id"]
    cp = tc.post(f"/api/strategies/buy/{sid}/copy", json={"name": "带实验因子"}).json()
    content = tc.get(f"/api/strategies/buy/{cp['id']}").json()["content"]
    from qfpy.factors.base import FACTOR_TYPES
    exp_kind = next(k for k, c in FACTOR_TYPES.items() if getattr(c, "experimental", False) and "buy" in c.sides)
    content["layers"] = [{"id": "L1", "name": "第一层", "mode": "all", "enabled": True,
                          "factors": [{"kind": exp_kind, "params": {"min": 1}}]}]
    r = tc.put(f"/api/strategies/buy/{cp['id']}", json={"content": content})
    assert r.status_code == 200, r.text

    import qfpy.backtest as bt
    bars = [{"date": f"2026-10-{d:02d}", "open": 10, "high": 10.5, "low": 9.8, "close": 10 + d / 100, "volume": 1e6,
             "amount": 1e7} for d in range(1, 29)]

    class Src:
        def get_daily(self, code, start, end):
            return bars

    class M:
        def _ordered(self, names, kind):
            return [Src()]
    monkeypatch.setattr(e, "market", lambda *a, **k: M())
    try:
        res = bt.run_backtest(e, "2026-10-10", "2026-10-28", ["600001"], buy_strategy=f"s:{cp['id']}")
    except Exception as ex:   # 数据加载方式不同导致跑不起来时，至少确认跳过判定
        pytest.fail(f"回测没跑起来：{ex}")
    assert res["strategy"]["buy"]["name"] == "带实验因子"
    assert any("回测不可用" in s["reason"] for s in res["factors_skipped"])


# ---------------- 股池走势：一只票的详情 ----------------
def test_pool_stock_detail_with_position(web, monkeypatch):
    e, tc, _ = web
    from tests.conftest import add_position
    e.cfg["exits"]["by_pool"] = {"gushi:gold_230": {"max_hold_days": 1, "exit_time": "14:45"}}
    add_position(e, code="600001", name="甲", price=10.0, qty=100, day="2026-11-10", sources=("gushi:gold_230",))

    class M:
        def latest_trading_date(self):
            return "2026-11-11"

        def quotes(self, codes):
            return {"600001": {"name": "甲", "price": 10.5, "preclose": 10.0}}, "fake", None

        def stock_minute(self, code, day):
            return {"preclose": 10.0, "points": [{"t": "09:31", "p": 10.1, "vol": 100, "amt": 1010},
                                                 {"t": "09:32", "p": 10.5, "vol": 100, "amt": 1050}]}, "fake", None

        def daily(self, code, day):
            return [], "日K拉不到"

        def known_trade_dates(self):
            return ["2026-11-10", "2026-11-11", "2026-11-12"]

        def trading_days_between(self, a, b):
            return sum(1 for d in self.known_trade_dates() if a < d <= b)
    monkeypatch.setattr(e, "market", lambda *a, **k: M())
    d = tc.get("/api/pool-stock/600001?pool=gushi:gold_230").json()
    pts = d["minute"]["points"]
    assert pts[-1]["avg"] == pytest.approx(10.3) and pts[-1]["pct"] == pytest.approx(5.0)    # 均价线 = 累计额/累计量
    ind = {i["key"]: i["value"] for i in d["indicators"]}
    assert ind["price"] == 10.5 and ind["turnover"] is None                                 # 拿不到的是 None，页面显示「—」
    assert any("日K" in x for x in d["errors"])
    pos = d["position"]
    assert pos["rule_from"] == "gushi:gold_230" and pos["due_date"] == "2026-11-11" and pos["exit_time"] == "14:45"
    assert pos["pnl_pct"] == pytest.approx((10.5 / pos["avg_cost"] - 1) * 100, abs=1e-3) and pos["held_days"] == 1
    assert pos["take_profit_price"] is None or pos["take_profit_price"] > pos["avg_cost"]
    assert tc.get("/api/pool-stock/abc").status_code == 400
