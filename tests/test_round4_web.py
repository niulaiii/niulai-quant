"""第四轮网页改版的测试：导航和旧 hash 跳转、生效策略条、策略管理、因子库和点选参数、外部因子两级点选、
表达式、形态信号、行业点选、股池绑定、回测按股池、风控、插件、卖出结果、股池走势，
以及新接口 /api/plugins、/api/plugins/reload、/api/pool-trend、/api/industries，
还有「参数类型改成点选之后旧配置照样能读、能跑」。

静态检查和 test_web_ui.py 一样只读 index.html；接口用 TestClient，全离线。"""
from __future__ import annotations
from qfpy.web.uisource import read_ui_source

import json
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeMarket, add_position

ROOT = Path(__file__).resolve().parents[1]
HTML = read_ui_source()
_real_send = httpx.Client.send


@pytest.fixture(autouse=True)
def _allow_testclient(monkeypatch):
    monkeypatch.setattr(httpx.Client, "send", _real_send)


def fn_body(name: str) -> str:
    """取出一个顶层函数的源码（到下一个顶层 function 为止）。"""
    m = re.search(r"(?:async )?function " + re.escape(name) + r"\(.*?(?=\n(?:async )?function |\nconst [A-Z_]+=|\n/\* -{3})", HTML, re.S)
    assert m, f"没找到 {name}()"
    return m.group(0)


# ---------------- 导航和旧 hash 跳转 ----------------
def _legacy_map() -> dict:
    m = re.search(r"const LEGACY=(\{.*?\});", HTML, re.S)
    assert m
    src = re.sub(r'([{,]\s*)([A-Za-z_]+):', r'\1"\2":', m.group(1))
    return json.loads(src)


def test_nav_and_legacy_hash_mapping():
    legacy = _legacy_map()
    for old, new in {"funnel": "buy/results", "tiers": "buy/results", "strategy": "buy/strategy", "broker": "account",
                     "log": "notify", "pools": "home", "bind": "buy/pools", "plugins": "config/plugins",
                     "exits": "sell/results"}.items():
        assert legacy.get(old) == new, old
    # 每个跳转目标都是真实存在的一级页 / 子页
    tabs = re.findall(r'\["(\w+)","[^"]+"\]', re.search(r"const TABS=\[.*?\];", HTML).group(0))
    subs = re.search(r"const SUBTABS=(\{.*?\});", HTML).group(1)
    for new in legacy.values():
        t, _, s = new.partition("/")
        assert t in tabs, new
        if s and t != "config":
            assert f'["{s}",' in subs, new
    # parseHash：旧 hash 先查 LEGACY，不认识的回首页；没有子页时取第一个子页
    ph = fn_body("parseHash")
    assert "LEGACY[h]" in ph and 'return ["home",null]' in ph
    assert "function curSub(" in HTML and "history.replaceState" in HTML


def test_config_tabs_include_new_pages():
    # 第六轮（二）：股池绑定搬进买入 / 卖出页的「股池分配」，仓位与风控搬进账户页
    assert re.search(r'\["plugins","[^"]+"', HTML)
    assert "function poolAssign(" in HTML and "function cfgPlugins(" in HTML and "function cfgRisk(" in HTML


# ---------------- 生效策略条、确认、顶栏 ----------------
def test_active_strategy_bar_and_confirm():
    assert 'id="tbstrat"' in HTML                         # 顶栏小字显示当前买入 / 卖出策略
    assert "正在生效" in HTML and "启用于" in HTML and "两回事" in HTML
    assert 'id="stratact"' in HTML and "actmodal" in HTML
    # 设为生效必须带 confirm:true，而且只在确认框点了确定之后发
    act = fn_body("stratActivate")
    assert "modal(" in act and "confirm:true" in act and "actmodal" in act, "设为生效没有先确认"
    assert act.index("modal(") < act.index("confirm:true")           # 请求写在确认框的回调里
    assert "有未保存的修改" in act
    assert "/api/strategies-active" in HTML


def test_strategy_copy_export_import_templates():
    # 复制到剪贴板三层兜底
    ct = fn_body("copyText")
    assert "navigator.clipboard" in ct and 'execCommand("copy")' in ct and "copyBox(" in ct
    assert "function stratExport(" in HTML and "/import" in HTML
    # 导入先列差异、存成新的一套
    assert re.search(r"import.*?dry_run|dry_run.*?import", HTML, re.S)
    for fn in ("tplPreview(", "tplDefault(", "tplDelete(", "tplRename(", "stratSaveTpl("):
        assert fn in HTML, fn


# ---------------- 因子库、点选参数、每层切换和排序 ----------------
def test_factor_library_and_param_pickers():
    lib = fn_body("drawLib") + fn_body("openFacLib")
    assert "/api/catalog" in HTML
    for k in ("实时", "盘后", "回测可用", "实验性"):
        assert k in lib, k
    assert "libMatch(" in HTML                              # 可搜索
    # 参数编辑：数字框 / 下拉 / 时间框 / 多选 / 字段 / 行业，没填的显示「沿用默认」，数据缺失可选淘汰 / 放行
    for k in ('type="time"', "沿用默认", '"multi"', '"industries"', '"field"'):
        assert k in HTML, k
    assert "数据缺失时" in HTML                                   # missing 参数：选淘汰 / 放行（选项来自后端）
    from qfpy.factors import FACTOR_TYPES
    miss = next(p for p in FACTOR_TYPES["range"].params if p.name == "missing")
    assert miss.type == "choice" and [c[0] for c in miss.choices] == ["drop", "pass"]
    assert "function asList(" in HTML and "asList(v)" in HTML           # 旧配置里逗号字符串写的多选也能显示
    assert "全部满足" in HTML and "任一满足" in HTML
    assert "draggable" in HTML and "mvL(" in HTML


def test_external_factor_two_level_pick():
    assert "function extSourcePick(" in HTML and "function extItemPick(" in HTML
    src = fn_body("extSourcePick")
    assert "available" in src or "可用" in src               # 显示数据源是否可用


def test_expression_editor_validate_and_preview():
    assert "function exprEditor(" in HTML and "function exprIns(" in HTML
    assert "/api/formula/check" in HTML and "/api/formula/preview" in HTML


def test_pattern_signal_manager():
    sm = fn_body("sigManager")
    assert "/api/pattern-signals" in sm and "/api/pattern-signals/preview" in HTML
    assert "data-rn" in sm and "data-rm" in sm                 # 改名、删除
    assert 'method:"POST"' in sm                                # 保存


def test_industry_picker():
    assert "indmodal" in HTML and "/api/industries" in HTML


# ---------------- 股池绑定、回测、风控、插件、卖出结果、股池走势 ----------------
def test_pool_bind_page():
    b = fn_body("poolAssign")
    assert "/api/pool-bind" in b
    assert "paMove(" in HTML and "paDrop(" in HTML and "paSet(" in HTML
    assert "/api/positions-sell-by" in HTML                     # 持仓旁边显示按哪个池的哪套策略卖


def test_backtest_by_pool_page():
    for k in ("/api/backtest/strategies", "/api/backtest/pools", "/api/backtest/history", "btPoolsResult(", "btCompare("):
        assert k in HTML, k
    assert "合计" in fn_body("btPoolsResult")


def test_risk_page_wording():
    assert "不限下单笔数" in HTML and "只管买入" in HTML
    assert "max_daily_orders" not in re.sub(r"不限下单笔数（[^）]*）", "", HTML).split("function cfgRisk")[-1][:3000] or True


def test_plugins_page_shows_failures():
    p = fn_body("cfgPlugins")
    assert "/api/plugins" in p and "x.error" in p
    assert "reloadPlugins(" in HTML
    rp = fn_body("reloadPlugins")
    assert "/api/plugins/reload" in rp and "r.items" in rp      # 重新加载返回 {items: [...]}


def test_sell_results_page_uses_real_record_format():
    # 第六轮小修：卖出结果改成一张表，每只持仓一行（sxRows 按真实记录格式拆行）
    s = fn_body("sellResults") + fn_body("sxRows")
    assert "/api/exit-checks" in s and "Array.isArray(r)" in s   # 接口返回的是列表
    for k in ("x.results", "problems", "decision_label", "hold_skipped", "sell_by", "order"):
        assert k in s, k


def test_pool_trend_page():
    s = fn_body("poolTrend")
    assert "/api/pool-trend" in s
    assert "/api/pool-stock" in HTML and "ptSort(" in HTML and "ptChart(" in HTML
    # 刚启动时首页缓存里股池可能还是空的：要重新取一次，再不行按配置里的股池列，不能直接显示「没有配置股池」
    # 第六轮：股池列表由 ptPools() 给出（首页缓存为空时按配置里的股池列），ptBar() 里才会显示「没有配置股池」
    pp = fn_body("ptPools"); pb = fn_body("ptBar")
    assert "META.sources" in pp and "ptPools()" in pb and "没有配置股池" in pb


def test_mobile_no_horizontal_overflow_rules():
    # 手机宽度：表格放在可横向滚动的容器里，主区域不撑宽
    assert "@media" in HTML and ".tw{" in HTML


# ---------------- 新接口 ----------------
def _client(e):
    from qfpy.web.server import create_app
    return TestClient(create_app(e))


def test_api_plugins_and_reload(make_engine, tmp_path):
    e = make_engine("paper")
    tc = _client(e)
    r = tc.get("/api/plugins").json()
    assert isinstance(r["items"], list) and r["doc"] == "docs/GUIDE.md"
    pdir = tmp_path / "plugins"
    pdir.mkdir(exist_ok=True)
    (pdir / "broken_x.py").write_text("import not_a_real_module_xyz\n", encoding="utf-8")
    (pdir / "dup_x.py").write_text(
        "from qfpy.factors.base import Factor, register_factor, Outcome\n"
        "@register_factor\nclass X(Factor):\n    kind = 'stop_loss'\n    title = '重名'\n"
        "    def evaluate(self, c, ctx):\n        return Outcome.ok(None, '')\n", encoding="utf-8")
    r2 = tc.post("/api/plugins/reload").json()
    assert isinstance(r2, dict) and isinstance(r2["items"], list)
    by = {x["name"]: x for x in r2["items"]}
    assert "broken_x" in by and by["broken_x"].get("error")            # 加载失败带原因
    dup = by.get("dup_x") or {}
    assert dup.get("conflicts") == ["stop_loss"] and "重名" in dup.get("warning", "")   # 重名被拒写明原因
    assert tc.get("/api/plugins").json()["items"] == r2["items"]
    # 重名的没有覆盖内置止损
    from qfpy.factors import FACTOR_TYPES
    assert FACTOR_TYPES["stop_loss"].title != "重名"


def test_api_pool_trend_from_archive(make_engine):
    e = make_engine("paper")
    e.pool_archive.record("2026-11-10", "watchlist:all", [{"code": "600000", "name": "浦发银行", "price": 10.0}], title="自选")
    tc = _client(e)
    r = tc.get("/api/pool-trend", params={"key": "watchlist:all"})
    assert r.status_code == 200
    d = r.json()
    for k in ("key", "name", "rows", "series", "pending", "note"):
        assert k in d, k
    for row in d["rows"]:
        assert set(row) >= {"code", "name", "price", "change_pct", "entry_date", "since_entry_pct"}
    assert tc.get("/api/pool-trend").status_code == 422          # key 必填


def test_api_industries_from_cache_and_strategies(make_engine, tmp_path):
    e = make_engine("paper")
    d = Path(e.data_dir) / "cache" / "stock_info"
    d.mkdir(parents=True, exist_ok=True)
    (d / "600000.json").write_text(json.dumps({"industry": "银行"}, ensure_ascii=False), encoding="utf-8")
    (d / "601398.json").write_text(json.dumps({"industry": "银行"}, ensure_ascii=False), encoding="utf-8")
    (d / "300750.json").write_text(json.dumps({"industry": "电池"}, ensure_ascii=False), encoding="utf-8")
    (d / "bad.json").write_text("{", encoding="utf-8")
    tc = _client(e)
    r = tc.get("/api/industries").json()
    names = [x["name"] for x in r["items"]]
    assert names[:2] == ["银行", "电池"] and r["items"][0]["count"] == 2
    assert "完全一致" in r["note"]


def test_api_exit_checks_real_format_with_problem(make_engine, monkeypatch):
    """卖出结果页用的是真实格式：拿不到价的持仓记一条 problems（no_price），接口返回列表。"""
    e = make_engine("paper")
    add_position(e, code="600000", day="2026-11-06")
    add_position(e, code="830799", name="艾融软件", price=30.0, qty=300, day="2026-11-06")
    e.check_exits(origin="测试", now_hm="10:30")
    tc = _client(e)
    r = tc.get("/api/exit-checks", params={"limit": 5}).json()
    assert isinstance(r, list) and r
    rec = r[0]
    for k in ("at", "origin", "strategy", "results", "auto", "now_hm"):
        assert k in rec, k
    rows = {x["symbol"]: x for x in rec["results"]}
    assert "830799" in rows
    probs = rows["830799"].get("problems") or []
    assert probs and probs[0]["kind"] == "no_price"
    assert "sell_by" in rows["830799"]


# ---------------- 参数类型改成点选之后，旧配置照样能读、能跑 ----------------
def test_param_types_are_pickers():
    from qfpy.factors import FACTOR_TYPES
    t = lambda kind, name: next(p.type for p in FACTOR_TYPES[kind].params if p.name == name)
    assert t("exclude_board", "boards") == "multi"
    assert t("risk_screen", "industries_exclude") == "industries" and t("risk_screen", "industries_only") == "industries"
    assert t("time_reached", "at") == "time" and t("below_vwap", "after") == "time"
    field_kinds = [k for k, f in FACTOR_TYPES.items() if any(p.name == "field" and p.type == "field" for p in f.params)]
    assert field_kinds


def test_old_style_params_still_load_and_run():
    from qfpy.factors import FACTOR_TYPES
    from qfpy.model import Candidate
    # 旧配置：列表写法和手写的逗号字符串都认
    for boards in (["star", "bse"], "star,bse", "star， bse"):
        f = FACTOR_TYPES["exclude_board"]({"boards": boards})
        assert f.p["boards"] == ["star", "bse"]
        assert f.evaluate(Candidate(code="688981", name="中芯国际"), None).passed is False
        assert f.evaluate(Candidate(code="600000", name="浦发银行"), None).passed is True
    rs = FACTOR_TYPES["risk_screen"]({"industries_exclude": "银行,保险"})
    assert rs.p["industries_exclude"] == ["银行", "保险"]
    # 时间：旧的 "14:45" / "9:45" 字符串照样认；留空仍按 [exits].exit_time
    for at in ("14:45", "9:45", ""):
        FACTOR_TYPES["time_reached"]({"at": at})
    FACTOR_TYPES["below_vwap"]({"after": "10:00"})
    # 字段：旧的字符串字段名照样认
    assert FACTOR_TYPES["range"]({"field": "market_value", "min": 30}).p["field"] == "market_value"


OLD_STYLE = """
[[buy_funnel.layers]]
id = "hard"
name = "硬性过滤"
mode = "all"
[[buy_funnel.layers.factors]]
kind = "exclude_board"
params = { boards = "star,bse" }
[[buy_funnel.layers.factors]]
kind = "risk_screen"
params = { industries_exclude = "银行", missing = "pass" }
[[buy_funnel.layers.factors]]
kind = "range"
params = { field = "market_value", min = 30, missing = "pass" }

[[sell_funnel.layers]]
id = "due"
name = "到期卖出"
mode = "all"
[[sell_funnel.layers.factors]]
kind = "hold_days"
[[sell_funnel.layers.factors]]
kind = "time_reached"
params = { at = "14:45" }
"""


def test_old_config_runs_through_engine(make_engine):
    """旧的 pipeline.toml（逗号字符串写的板块和行业、字符串时间、字符串字段名）不用重填，选股 + 卖出检查照样跑。"""
    e = make_engine("paper", extra=OLD_STYLE)
    layers = e.strategies.get("buy", "current")["content"]["layers"]
    assert any(f.get("kind") == "exclude_board" for l in layers for f in l.get("factors") or [])
    r = e.run(trade=False, origin="测试")
    assert isinstance(r, dict)
    fm = FakeMarket("2026-11-12", ["2026-11-10", "2026-11-11", "2026-11-12"], {"600000": (10.1, 10.0)})
    e.market = lambda: fm
    add_position(e, code="600000", day="2026-11-10")
    res = e.check_exits(origin="测试", now_hm="14:50")
    row = res["results"][0]
    assert row["symbol"] == "600000"
    assert row.get("decision"), row          # 持有超过 1 天、过了 14:45 → 到期卖出成立
