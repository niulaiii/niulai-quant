"""第三轮：股池绑定策略、卖单不受买入上限、止损不再悄悄跳过、股池存档和按股池回测。全部离线。"""
from __future__ import annotations

import copy

import httpx
import pytest
from fastapi.testclient import TestClient

from qfpy.util import read_jsonl
from tests.conftest import FakeMarket, add_position

_real_send = httpx.Client.send


@pytest.fixture(autouse=True)
def _allow_testclient(monkeypatch):
    monkeypatch.setattr(httpx.Client, "send", _real_send)     # TestClient 走的是 httpx，只连本进程里的 app


DAYS = ["2026-11-06", "2026-11-09", "2026-11-10", "2026-11-11"]
EXTRA = '\n[sources.wl2]\ntype = "watchlist"\nenabled = true\ncodes = ["600001"]\n'


def _engine(make_engine, prices=None, today="2026-11-10"):
    e = make_engine("paper", extra=EXTRA)
    fm = FakeMarket(today, DAYS, {"600000": (10.2, 10.0), "600001": (10.2, 10.0)} if prices is None else prices)
    fm.daily_series = lambda code, day, fields: ([], "无", False)
    e.market = lambda: fm
    sent = []
    e.notifier.notify = lambda ev, title, text, **kw: sent.append((ev, title, text))
    e.strategies.ensure()
    return e, sent


def _new_sell(e, name, **exits):
    act = e.strategies.active_names()["sell"]["id"]
    content = copy.deepcopy(e.strategies.get("sell", act)["content"])
    content["exits"].update(exits)
    return e.strategies.create("sell", name, content)


def _new_buy(e, name, layers, top_n=1):
    return e.strategies.create("buy", name, {"layers": layers, "select": {"top_n": top_n, "weights": {}}})


# ---------------- 卖出：按买入时的来源股池找策略 ----------------
def test_sell_resolves_bound_strategy_and_its_own_override(make_engine):
    e, _ = _engine(make_engine)
    st = _new_sell(e, "金池专用卖出", stop_loss_pct=8.0, by_pool={"gushi:gold_230": {"max_hold_days": 5}})
    e.cfg["pool"]["bind"] = {"gushi:gold_230": {"sell": st["id"]}}
    b = e.binder()
    r = b.resolve_sell(["watchlist:all", "gushi:gold_230"])
    assert r["pool"] == "gushi:gold_230" and r["strategy"] == "金池专用卖出" and r["bound"]
    from qfpy.exits import rule_for
    rule = rule_for(r["cfg"], {"sources": []}, r["pool"])
    # 绑定策略自己的 by_pool 生效（持有 5 天），生效策略里对同一个池的覆盖（2 天）不会混进来
    assert rule["max_hold_days"] == 5 and rule["stop_loss_pct"] == 8.0 and rule["from"] == "gushi:gold_230"
    assert e.cfg["exits"]["by_pool"]["gushi:gold_230"]["max_hold_days"] == 2      # 生效配置没被改
    # 没绑定的池：生效策略
    r2 = b.resolve_sell(["watchlist:all"])
    assert r2["pool"] is None and r2["strategy"] == e.strategy_names()["sell"] and not r2["bound"]


def test_sell_unbound_pool_keeps_old_by_pool_behaviour(make_engine):
    """没有绑定时跟以前一样：生效策略 + 它对这个池的覆盖参数。"""
    e, _ = _engine(make_engine)
    r = e.binder().resolve_sell(["gushi:gold_230"])
    from qfpy.exits import rule_for
    rule = rule_for(r["cfg"], {}, r["pool"])
    assert r["pool"] == "gushi:gold_230" and r["override"] and rule["max_hold_days"] == 2 and rule["exit_time"] == "14:45"
    assert not r["bound"]


def test_multi_pool_first_in_config_order(make_engine):
    e, _ = _engine(make_engine)
    a = _new_sell(e, "A", stop_loss_pct=4.0)
    b = _new_sell(e, "B", stop_loss_pct=6.0)
    e.cfg["pool"]["bind"] = {"wl2:all": {"sell": b["id"]}, "watchlist:all": {"sell": a["id"]}}
    r = e.binder().resolve_sell(["watchlist:all", "wl2:all"])
    assert r["pool"] == "wl2:all" and r["strategy"] == "B"           # 按绑定表的顺序，不是持仓 sources 的顺序


def test_check_exits_uses_bound_strategy_and_records_it(make_engine):
    e, _ = _engine(make_engine, prices={"600000": (9.5, 9.6)}, today="2026-11-10")
    e.set_auto(True, "pytest")
    st = _new_sell(e, "紧止损", stop_loss_pct=4.0)
    e.cfg["pool"]["bind"] = {"watchlist:all": {"sell": st["id"]}}
    add_position(e, day="2026-11-09")         # 成本 10，现价 9.5：-5%
    res = e.check_exits(now_hm="09:40")["results"]
    ev = res[0]
    assert ev["sell_by"]["strategy"] == "紧止损" and ev["sell_by"]["pool"] == "watchlist:all"
    assert ev["decision"] == "stop_loss"
    o = [x for x in read_jsonl(e.data_dir / "orders.jsonl", 100) if x.get("side") == "sell"]
    assert o and o[-1]["strategy"] == "紧止损" and o[-1]["pool"] == "watchlist:all"
    chk = read_jsonl(e.data_dir / "exit_checks.jsonl", 10)
    assert any((r.get("sell_by") or {}).get("strategy") == "紧止损" for c in chk for r in (c.get("results") or [c]))


def test_positions_sell_by_api_and_pool_bind_api(make_engine):
    e, _ = _engine(make_engine)
    st = _new_sell(e, "池卖出", stop_loss_pct=7.0)
    add_position(e, day="2026-11-09", sources=("wl2:all",), code="600001", name="乙")
    from qfpy.web.server import create_app
    tc = TestClient(create_app(e))
    assert tc.put("/api/pool-bind", json={"pool": "wl2:all", "sell": "s-nope"}).status_code == 400
    r = tc.put("/api/pool-bind", json={"pool": "wl2:all", "sell": st["id"]})
    assert r.status_code == 200, r.text
    assert e.cfg["pool"]["bind"] == {"wl2:all": {"sell": st["id"]}}            # 写进了 pipeline.toml
    assert "pool.bind" in (e.store.path.read_text(encoding="utf-8")) or "wl2:all" in e.store.path.read_text(encoding="utf-8")
    row = next(x for x in r.json()["rows"] if x["pool"] == "wl2:all")
    assert row["sell"]["name"] == "池卖出" and row["sell"]["bound"] and not row["buy"]["bound"]
    items = tc.get("/api/positions-sell-by").json()["items"]
    assert items[0]["strategy"] == "池卖出" and items[0]["rule"]["stop_loss_pct"] == 7.0
    # 还绑着的策略不能删
    assert tc.delete(f"/api/strategies/sell/{st['id']}").status_code == 400
    # 解除绑定
    tc.put("/api/pool-bind", json={"pool": "wl2:all"})
    assert e.cfg["pool"]["bind"] == {}
    assert tc.delete(f"/api/strategies/sell/{st['id']}").status_code == 200


def test_bound_sell_strategy_exit_time_scheduled(make_engine):
    e, _ = _engine(make_engine)
    st = _new_sell(e, "两点半卖", exit_time="14:30")
    e.cfg["pool"]["bind"] = {"wl2:all": {"sell": st["id"]}}
    from qfpy.scheduler import jobs_for
    times = {j["time"] for j in jobs_for(e.cfg, e.bound_sell_contents()) if j["kind"] in ("exit", "remind", "check")} | \
        {j.get("exit_time") for j in jobs_for(e.cfg, e.bound_sell_contents())}
    assert "14:30" in times
    assert any(j["kind"] == "archive" for j in jobs_for(e.cfg))


def test_bind_validation():
    from qfpy.poolbind import validate
    assert validate({"pool": {"bind": {"nocolon": {"buy": "x"}}}})
    assert validate({"pool": {"bind": {"a:b": {"bogus": "x"}}}})
    assert validate({"pool": {"bind": {"a:b": {"buy": "x"}}}}, lambda s, i: False)
    assert not validate({"pool": {"bind": {"a:b": {"buy": "x", "sell": ""}}}}, lambda s, i: True)


# ---------------- 买入：每个股池用自己的买入策略 ----------------
def test_buy_groups_per_pool_strategy(make_engine):
    e, _ = _engine(make_engine)
    strict = [{"id": "L1", "name": "严", "mode": "all", "enabled": True,
               "factors": [{"kind": "range", "params": {"field": "change_rate", "min": 50}}]}]
    loose = [{"id": "L1", "name": "松", "mode": "all", "enabled": True,
              "factors": [{"kind": "range", "params": {"field": "change_rate", "min": 1}}]}]
    a = _new_buy(e, "严格买入", strict)
    e.strategies.activate("buy", a["id"], by="pytest")
    b = _new_buy(e, "宽松买入", loose)
    e.cfg["pool"]["bind"] = {"wl2:all": {"buy": b["id"]}}
    rec = e.run(date="2026-11-10", pools=["watchlist:all", "wl2:all"], trade=False)
    assert [p["code"] for p in rec["picks"]] == ["600001"]
    pk = rec["picks"][0]
    assert pk["pool"] == "wl2:all" and pk["buy_strategy"] == "宽松买入"
    names = {g["name"]: g for g in rec["groups"]}
    assert names["严格买入"]["pools"] == ["watchlist:all"] and names["严格买入"]["picks"] == []
    assert names["宽松买入"]["picks"] == ["600001"]
    # 第六轮：漏斗按股池分开，每层都标了 pool
    assert {s.get("pool") for s in rec["stages"]} == {"watchlist:all", "wl2:all"}
    assert [r["strategy"] for r in rec["pool_runs"]] == ["宽松买入", "严格买入"] or \
        sorted(r["strategy"] for r in rec["pool_runs"]) == ["宽松买入", "严格买入"]


def test_buy_order_records_pool_and_strategy(make_engine):
    e, _ = _engine(make_engine)
    e.set_auto(True, "pytest")
    loose = [{"id": "L1", "name": "松", "mode": "all", "enabled": True,
              "factors": [{"kind": "range", "params": {"field": "change_rate", "min": 1}}]}]
    b = _new_buy(e, "宽松买入", loose)
    e.cfg["pool"]["bind"] = {"wl2:all": {"buy": b["id"]}}
    e.cfg["buy_funnel"] = {"layers": [{"id": "X", "name": "全挡", "mode": "all", "enabled": True,
                                       "factors": [{"kind": "range", "params": {"field": "change_rate", "min": 99}}]}]}
    rec = e.run(date="2026-11-10", pools=["watchlist:all", "wl2:all"], trade=True)
    buys = [o for o in rec["orders"] if o.get("side") == "buy"]
    assert buys and buys[0]["symbol"] == "600001" and buys[0]["pool"] == "wl2:all" and buys[0]["strategy"] == "宽松买入"


# ---------------- 止损不再悄悄跳过 ----------------
def test_no_price_alerts_instead_of_silent_skip(make_engine):
    e, sent = _engine(make_engine, prices={})          # 拿不到最新价
    e.set_auto(True, "pytest")
    add_position(e, day="2026-11-09")
    ev = e.check_exits(now_hm="09:40")["results"][0]
    assert ev["problems"] and ev["problems"][0]["kind"] == "no_price"
    assert any(x[0] == "exit_problem" and "600000" in x[1] for x in sent)
    chk = read_jsonl(e.data_dir / "exit_checks.jsonl", 10)
    assert any(r.get("problems") for c in chk for r in (c.get("results") or [c]))


def test_sell_factor_error_alerts(make_engine, monkeypatch):
    e, sent = _engine(make_engine, prices={"600000": (9.9, 10.0)})
    e.set_auto(True, "pytest")
    add_position(e, day="2026-11-09")
    from qfpy.factors import sell
    cls = next(c for c in vars(sell).values() if isinstance(c, type) and getattr(c, "kind", None) == "stop_loss")

    def boom(self, c, ctx):
        raise RuntimeError("模拟因子出错")
    monkeypatch.setattr(cls, "evaluate", boom)
    ev = e.check_exits(now_hm="09:40")["results"][0]
    assert any(p["kind"] == "error" for p in ev["problems"])
    assert any(x[0] == "exit_problem" for x in sent)


# ---------------- 股池存档 + 按股池回测 ----------------
def test_run_archives_pool_members(make_engine):
    e, _ = _engine(make_engine)
    e.run(date="2026-11-10", pools=["watchlist:all", "wl2:all"], trade=False)
    doc = e.pool_archive.load("2026-11-10")
    assert {s["code"] for s in doc["pools"]["watchlist:all"]["stocks"]} == {"600000"}
    assert {s["code"] for s in doc["pools"]["wl2:all"]["stocks"]} == {"600001"}
    assert e.pool_archive.members("wl2:all", "2026-11-01", "2026-11-30").keys() == {"2026-11-10"}
    # 同一天再存一次取并集
    e.pool_archive.record("2026-11-10", "wl2:all", [{"code": "600002", "name": "丙", "fields": {"x": 1, "daily": [1]}}])
    rows = e.pool_archive.load("2026-11-10")["pools"]["wl2:all"]["stocks"]
    assert {s["code"] for s in rows} == {"600001", "600002"}
    assert "daily" not in next(s for s in rows if s["code"] == "600002")["fields"]
    summ = e.pool_archive.summary()
    assert {p["pool"] for p in summ["pools"]} == {"watchlist:all", "wl2:all"}


def _bars():
    return [{"date": f"2026-10-{d:02d}", "open": 10 + d / 50, "high": 10.5 + d / 50, "low": 9.8 + d / 50,
             "close": 10 + d / 40, "volume": 1e6, "amount": 1e7} for d in range(1, 29) if d % 7 not in (3, 4)]


def _fake_daily(e, monkeypatch):
    class Src:
        def get_daily(self, code, start, end):
            return _bars()

    class M:
        def _ordered(self, names, kind):
            return [Src()]
    monkeypatch.setattr(e, "market", lambda *a, **k: M())


def test_pool_backtest_uses_archive_and_bound_strategy(make_engine, monkeypatch):
    e, _ = _engine(make_engine)
    st = _new_sell(e, "池二卖出", stop_loss_pct=9.0)
    e.cfg["pool"]["bind"] = {"wl2:all": {"sell": st["id"]}}
    e.cfg["buy_funnel"] = {"layers": []}
    arc = e.pool_archive
    for d in ("2026-10-12", "2026-10-13", "2026-10-15"):
        arc.record(d, "wl2:all", [{"code": "600001", "name": "乙"}])
    arc.record("2026-10-12", "watchlist:all", [{"code": "600000", "name": "甲"}])
    _fake_daily(e, monkeypatch)
    from qfpy.backtest import pool_choices, run_pool_backtest
    res = run_pool_backtest(e, "2026-10-10", "2026-10-20", ["wl2:all", "watchlist:all", "gushi:none"])
    by = {p["pool"]: p for p in res["pools"]}
    assert set(by) == {"wl2:all", "watchlist:all"}
    assert by["wl2:all"]["strategy"]["sell"]["name"] == "池二卖出"
    assert by["watchlist:all"]["strategy"]["sell"]["name"] == e.strategy_names()["sell"]
    assert by["wl2:all"]["archive_days"] == 3
    # 没存档的交易日写明，不拿别的日子补
    assert "2026-10-14" in by["wl2:all"]["no_archive_days"] and "2026-10-12" not in by["wl2:all"]["no_archive_days"]
    det = res["detail"]["wl2:all"]
    sig_days = {s["date"] for s in det["signals"]}
    assert sig_days <= {"2026-10-12", "2026-10-13", "2026-10-15"}
    assert all(t["symbol"] == "600001" for t in det["trades"])
    assert any(p["pool"] == "gushi:none" and "没有" in p["error"] for p in res["problems"])
    assert res["summary"]["pool_count"] == 2
    assert res["summary"]["initial_cash"] == pytest.approx(2 * float(e.cfg["paper"]["initial_cash"]))
    # 手动指定策略优先
    act_sell = e.strategy_names()["sell_id"]
    res2 = run_pool_backtest(e, "2026-10-10", "2026-10-20", ["wl2:all"], {"wl2:all": {"sell": f"s:{act_sell}"}})
    assert res2["pools"][0]["strategy"]["sell"]["by"] == "手动指定"
    ch = pool_choices(e)
    assert next(p for p in ch["pools"] if p["pool"] == "wl2:all")["archive_days"] == 3


def test_pool_backtest_without_archive_errors(make_engine, monkeypatch):
    e, _ = _engine(make_engine)
    _fake_daily(e, monkeypatch)
    from qfpy.backtest import run_pool_backtest
    with pytest.raises(ValueError, match="没有这个股池的存档"):
        run_pool_backtest(e, "2026-10-10", "2026-10-20", ["wl2:all"])


def test_pool_backtest_api(make_engine, monkeypatch):
    e, _ = _engine(make_engine)
    e.pool_archive.record("2026-10-12", "wl2:all", [{"code": "600001", "name": "乙"}])
    _fake_daily(e, monkeypatch)
    from qfpy.web.server import create_app
    tc = TestClient(create_app(e))
    assert any(p["pool"] == "wl2:all" for p in tc.get("/api/backtest/pools").json()["pools"])
    r = tc.post("/api/backtest", json={"start": "2026-10-10", "end": "2026-10-20", "pools": ["wl2:all"]})
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "pools"
    h = tc.get("/api/backtest/history").json()["items"][0]
    assert h["mode"] == "pools" and h["pools"][0]["pool"] == "wl2:all"
    assert tc.get("/api/pool_archive/2026-10-12").json()["pools"]["wl2:all"]["stocks"][0]["code"] == "600001"
    assert tc.get("/api/pool_archive/bad").status_code == 400
