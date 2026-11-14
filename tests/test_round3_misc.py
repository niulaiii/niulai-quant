"""第三轮顺手修的：防 DNS 重绑定/跨站表单、行业完全匹配、导出去掉网址里的密钥、插件不能覆盖内置 kind、
数据缺失统一参数、老漏斗接口转调策略接口、12 类、文档合并、板块/ST 只留一份。"""
from __future__ import annotations
from qfpy.web.uisource import read_ui_source

import re
import shutil
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from qfpy.factors import FACTOR_TYPES
from qfpy.factors.base import FactorContext, factor_catalog
from qfpy.funnel import build_layers, run_buy_funnel
from qfpy.model import Candidate

REPO = Path(__file__).resolve().parents[1]
_real_send = httpx.Client.send


@pytest.fixture(autouse=True)
def _allow_testclient(monkeypatch):
    monkeypatch.setattr(httpx.Client, "send", _real_send)


# ---------------- 防 DNS 重绑定和跨站表单 ----------------
def test_origin_guard(make_engine):
    from qfpy.web.server import create_app
    e = make_engine("paper")
    tc = TestClient(create_app(e))
    assert tc.get("/api/status").status_code == 200
    assert tc.get("/api/status", headers={"host": "127.0.0.1:8787"}).status_code == 200
    r = tc.get("/api/status", headers={"host": "evil.example.com"})
    assert r.status_code == 403 and "本机" in r.json()["detail"]
    # 跨站写操作拒绝；本机网页、没有 Origin 的脚本调用放行
    assert tc.post("/api/auto", json={"enabled": False}, headers={"origin": "https://evil.example.com"}).status_code == 403
    assert tc.post("/api/auto", json={"enabled": False}, headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert tc.post("/api/auto", json={"enabled": False}, headers={"origin": "null"}).status_code == 403
    assert tc.post("/api/auto", json={"enabled": False}, headers={"origin": "http://127.0.0.1:8787"}).status_code != 403
    assert tc.post("/api/auto", json={"enabled": False}, headers={"referer": "http://localhost:8787/#/home"}).status_code != 403
    assert tc.post("/api/auto", json={"enabled": False}).status_code != 403
    # 读接口带别的 Origin 不拦（浏览器本来就读不到跨域响应）
    assert tc.get("/api/status", headers={"origin": "https://evil.example.com"}).status_code == 200


def test_check_origin_ipv6_and_ports():
    from qfpy.web.server import check_origin
    assert check_origin("GET", {"host": "[::1]:8787"}) is None
    assert check_origin("GET", {"host": "localhost"}) is None
    assert check_origin("POST", {"host": "127.0.0.1:1", "origin": "http://[::1]:8787"}) is None
    assert check_origin("GET", {"host": "127.0.0.1.evil.com"})


# ---------------- 行业完全匹配 ----------------
def test_industry_exact_match():
    cls = FACTOR_TYPES["risk_screen"]
    ctx = FactorContext("2026-11-10", REPO, {})
    f = cls({"exclude_st": False, "industries_exclude": ["银行"]})
    bank = Candidate("600000", "甲", ["x"], {}, info={"industry": "银行"})
    equip = Candidate("600001", "乙", ["x"], {}, info={"industry": "银行设备"})
    assert not f.evaluate(bank, ctx).passed
    assert f.evaluate(equip, ctx).passed             # 以前包含匹配会误伤
    g = cls({"exclude_st": False, "industries_only": [" 银行 "]})
    assert g.evaluate(bank, ctx).passed and not g.evaluate(equip, ctx).passed


# ---------------- 导出去掉网址里的密钥 ----------------
def test_strip_secrets_urls():
    from qfpy.strategies import strip_secrets
    out = strip_secrets({"layers": [{"factors": [{"kind": "ext_http", "params": {
        "url": "https://u:p@host.example/f?token=abc&x=1", "token": "t", "name": "我的 https 因子"}}]}]})
    p = out["layers"][0]["factors"][0]["params"]
    assert p["url"] == "https://host.example/f" and "token" not in p and p["name"] == "我的 https 因子"


# ---------------- 插件不能覆盖内置 kind；示例不冲突 ----------------
def test_plugin_cannot_override_builtin(tmp_path):
    from qfpy.plugins import load_plugins
    builtin = FACTOR_TYPES["ma_bull"]
    (tmp_path / "clash.py").write_text('''
from qfpy.factors import Factor, Outcome, register_factor

@register_factor
class Clash(Factor):
    kind = "ma_bull"
    title = "冲突"
    def evaluate(self, c, ctx):
        return Outcome.ok(1.0, "x")
''', encoding="utf-8")
    info = {i["file"]: i for i in load_plugins(tmp_path)}
    assert FACTOR_TYPES["ma_bull"] is builtin
    assert info["clash.py"]["conflicts"] == ["ma_bull"] and "重名" in info["clash.py"]["warning"]


def test_example_plugins_do_not_clash(tmp_path):
    from qfpy.plugins import load_plugins
    d = tmp_path / "plugins"
    shutil.copytree(REPO / "plugins", d)
    info = load_plugins(d)
    assert info and all(not i.get("conflicts") for i in info), info
    regs = {k for i in info for k in (i.get("registered") or {}).get("factors", [])}
    assert "ma_trend" in regs and "ma_bull" not in regs
    for i in info:
        for k in (i.get("registered") or {}).get("factors", []):
            assert "写法演示" in FACTOR_TYPES[k].title


# ---------------- 数据缺失：一个参数名，默认淘汰 ----------------
def test_missing_param_unified_in_catalog():
    for f in factor_catalog("buy"):
        names = [p["name"] for p in f["params"]]
        assert "missing" in names, f["kind"]
        assert "allow_missing" not in names and "on_error" not in names
        mp = next(p for p in f["params"] if p["name"] == "missing")
        assert mp["default"] == "drop"


def test_missing_policy_in_funnel():
    from qfpy.factors.base import missing_policy
    assert missing_policy({}) == "drop"
    assert missing_policy({"allow_missing": True}) == "pass"
    assert missing_policy({"on_error": "pass"}) == "pass"
    assert missing_policy({"missing": "fail"}) == "drop"
    ctx = FactorContext("2026-11-10", REPO, {"side": "buy"})
    c = Candidate("600000", "甲", ["x"], {})
    for params, kept in (({"field": "market_value", "min": 1}, 0), ({"field": "market_value", "min": 1, "missing": "pass"}, 1)):
        layers = build_layers([{"id": "L", "name": "L", "mode": "all", "enabled": True,
                                "factors": [{"kind": "range", "params": params}]}], REPO, "buy")
        out, stages = run_buy_funnel(layers, [c], ctx, lambda cs, n: None, lambda x: x.brief(), [])
        assert len(out) == kept
        if kept:
            chk = stages[0]["kept"][0]["checks"][0] if stages[0].get("kept") and stages[0]["kept"][0].get("checks") else None
            assert chk is None or chk.get("missing_passed")


def test_templates_do_not_default_to_pass():
    txt = (REPO / "qfpy" / "funnel.py").read_text(encoding="utf-8")
    assert "allow_missing=True" not in txt and "allow_missing = true" not in txt
    tdir = REPO / "config" / "templates"
    for p in tdir.glob("*") if tdir.exists() else []:
        t = p.read_text(encoding="utf-8")
        assert "allow_missing" not in t and '"pass"' not in t


def test_vol_ma5_missing_day_is_missing_not_zero():
    from qfpy import market
    src = Path(market.__file__).read_text(encoding="utf-8")
    assert 'x.get("volume") or 0 for x in bars' not in src


# ---------------- 老漏斗接口转调策略接口 ----------------
def test_old_funnel_api_updates_active_strategy(make_engine):
    from qfpy.configsvc import put_funnel
    e = make_engine("paper")
    e.strategies.ensure()
    sid = e.strategies.active()["buy"]["id"]
    layers = [{"id": "N1", "name": "新层", "mode": "all", "enabled": True,
               "factors": [{"kind": "range", "params": {"field": "change_rate", "min": 1}}]}]
    r = put_funnel(e, "buy", layers)
    assert r["saved"] and r.get("strategy_id") == sid
    assert e.strategies.get("buy", sid)["content"]["layers"][0]["name"] == "新层"
    assert e.cfg["buy_funnel"]["layers"][0]["name"] == "新层"


# ---------------- 12 类、文档、单一来源 ----------------
def test_twelve_categories_everywhere():
    from qfpy import catalog
    cats = [c for c in dir(catalog) if c.isupper()]
    lst = next(getattr(catalog, c) for c in cats if isinstance(getattr(catalog, c), (list, dict))
               and len(getattr(catalog, c)) == 12)
    assert len(lst) == 12
    for name, t in [("README.md", (REPO / "README.md").read_text(encoding="utf-8")),
                     ("GUIDE.md", (REPO / "docs" / "GUIDE.md").read_text(encoding="utf-8")),
                     ("WebUI", read_ui_source())]:
        assert "11 类" not in t and "11类" not in t, name


def test_docs_merged():
    g = (REPO / "docs" / "GUIDE.md").read_text(encoding="utf-8")
    for must in ("股池绑定策略", "按股池回测", "插件", "/api/v1", "qfdp", "写法演示", "missing", "12 类"):
        assert must in g, must
    assert "GUIDE.md" in (REPO / "README.md").read_text(encoding="utf-8")
    for old in ("API.md", "DATA_PROVIDER.md"):
        assert "GUIDE.md" in (REPO / "docs" / old).read_text(encoding="utf-8")
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "代码里根本没有调用 `/api/cancel`" not in readme and "/api/cancel" in readme


def test_board_and_st_single_source():
    from qfpy import limits, util
    from qfpy.vendor import tsp_price_limits as v
    for code in ("600000", "000001", "300750", "688981", "830799", "920001"):
        assert util.board_of(code) == limits.board_of(code)
    assert util.is_st("*ST康美") and util.is_st("某某退") and not limits.is_st("某某退")
    assert v.is_risk_warning_name("ST东旭") and v.is_bse("830799")
    from datetime import date
    assert v.is_no_limit_day("301000", date(2026, 11, 2), date(2026, 11, 4), day_rank=3)
    assert not v.is_no_limit_day("301000", date(2026, 11, 2), date(2026, 11, 10), day_rank=7)
    assert not v.is_no_limit_day("600000", date(1999, 11, 10), date(1999, 11, 10))     # 注册制前的老股
    src = Path(util.__file__).read_text(encoding="utf-8")
    assert not re.search(r'startswith\(\("30"', src)
