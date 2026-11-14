"""买入漏斗：自定义层级、all/any 组合、开关和顺序、校验、模板套用、旧配置兼容。全部离线。"""
from __future__ import annotations

import tomllib

import pytest

from qfpy import configsvc
from qfpy.factors.base import FactorContext
from qfpy.funnel import (BUY_TEMPLATES, SELL_TEMPLATES, build_layers, combine, layers_of, legacy_buy_layers,
                         run_buy_funnel, validate_layers)
from qfpy.model import Candidate


def cand(code, **f):
    return Candidate(code, f"票{code}", ["t:x"], dict(f))


def rng(field, lo=None, hi=None, **kw):
    p = {"field": field, **kw}
    if lo is not None:
        p["min"] = lo
    if hi is not None:
        p["max"] = hi
    return {"kind": "range", "params": p}


def run(layers, cands):
    built = build_layers(layers, None, "buy")
    warns: list = []
    left, stages = run_buy_funnel(built, cands, FactorContext("2026-11-10", None, {"side": "buy"}),
                                  lambda cs, needs: None, lambda c: c.brief(), warns)
    return [c.code for c in left], stages, warns


CANDS = lambda: [cand("600001", change_rate=3.0, amount_yi=2.0), cand("600002", change_rate=8.0, amount_yi=0.2),  # noqa: E731
                 cand("600003", change_rate=-1.0, amount_yi=5.0), cand("600004", change_rate=1.0, amount_yi=0.1)]


def test_combine_modes():
    assert combine("all", [True, True]) and not combine("all", [True, False])
    assert combine("any", [False, True]) and not combine("any", [False, False])


def test_layer_all_vs_any():
    facs = [rng("change_rate", 0, 5), rng("amount_yi", 1)]
    left, st, _ = run([{"name": "全部", "mode": "all", "factors": facs}], CANDS())
    assert left == ["600001"]
    left, st, _ = run([{"name": "任一", "mode": "any", "factors": facs}], CANDS())
    assert left == ["600001", "600003", "600004"]
    # 每只票都记了本层每个因子的结果（网页用来展示）
    row = st[0]["dropped"][0]
    assert row["code"] == "600002" and [c["passed"] for c in row["checks"]] == [False, False]
    assert st[0]["mode"] == "any" and st[0]["input"] == 4 and len(st[0]["kept"]) == 3


def test_multiple_layers_order_and_counts():
    layers = [{"id": "a", "name": "涨幅", "factors": [rng("change_rate", 0)]},
              {"id": "b", "name": "成交", "factors": [rng("amount_yi", 1)]}]
    left, st, _ = run(layers, CANDS())
    assert left == ["600001"] and [s["input"] for s in st] == [4, 3] and [s["id"] for s in st] == ["a", "b"]
    left2, st2, _ = run(list(reversed(layers)), CANDS())      # 换顺序：结果一样，各层输入数不同
    assert left2 == ["600001"] and [s["input"] for s in st2] == [4, 2]


def test_disabled_layer_and_factor():
    layers = [{"name": "关掉的层", "enabled": False, "factors": [rng("change_rate", 100)]},
              {"name": "层内关掉一个", "factors": [rng("change_rate", 0), {**rng("amount_yi", 100), "enabled": False}]}]
    left, st, _ = run(layers, CANDS())
    assert left == ["600001", "600002", "600004"] and len(st) == 1


def test_empty_layer_passes_all_and_bad_factor_skipped_with_warning():
    layers = [{"name": "空层", "factors": []}, {"name": "有错", "factors": [{"kind": "range", "params": {"field": "nope"}}]}]
    left, st, warns = run(layers, CANDS())
    assert len(left) == 4 and st[0]["skipped"] and any("配置有误" in w for w in warns)


def test_validate_layers_errors():
    errs = validate_layers([{"mode": "xor", "factors": [{"kind": "nope"}, {"kind": "stop_loss"}]}], "buy", None)
    joined = "；".join(errs)
    assert "mode" in joined and "未知因子类型 nope" in joined and "不能用在买入漏斗" in joined
    assert validate_layers([{"factors": [{"kind": "above_vwap"}]}], "sell", None)      # 买入因子不能放卖出链
    assert validate_layers([{"id": "x"}, {"id": "x"}], "buy", None)                    # id 重复
    assert validate_layers([{"factors": [{"kind": "expr", "params": {"expr": "__import__('os')"}}]}], "buy", None)
    assert validate_layers([{"name": "ok", "mode": "any", "factors": [rng("change_rate", 0)]}], "buy", None) == []


@pytest.mark.parametrize("side,key", [("buy", k) for k in BUY_TEMPLATES] + [("sell", k) for k in SELL_TEMPLATES])
def test_every_template_is_valid(side, key):
    tpl = (BUY_TEMPLATES if side == "buy" else SELL_TEMPLATES)[key]
    assert validate_layers(tpl["layers"], side, None) == []
    assert len(tpl["layers"]) >= 2 and tpl["title"] and tpl["description"]


def test_required_buy_templates_exist():
    titles = {v["title"] for v in BUY_TEMPLATES.values()}
    assert {"竞价强势", "尾盘黄金两点半", "稳健低吸"} <= titles
    assert all(f["kind"] == "expr" for l in BUY_TEMPLATES["expr_only"]["layers"] for f in l["factors"])


def test_legacy_flat_factors_still_work(make_engine):
    e = make_engine("paper", extra='\n[[factors]]\nkind = "exclude_st"\n[[factors]]\nkind = "min_amount"\nenabled = false\nparams = { min_yi = 1 }\n')
    ls = layers_of(e.cfg, "buy")
    assert [l["factors"][0]["kind"] for l in ls] == ["exclude_st", "min_amount"] and ls[1]["enabled"] is False
    assert legacy_buy_layers([{"kind": "exclude_st"}])[0]["mode"] == "all"


def test_apply_template_writes_toml_then_edit(make_engine):
    e = make_engine("paper")
    r = configsvc.apply_template(e, "buy", "gold_230")
    assert r["saved"] and r["template"] == "gold_230"
    on_disk = tomllib.loads(e.store.path.read_text(encoding="utf-8"))
    assert [l["id"] for l in on_disk["buy_funnel"]["layers"]] == ["hard", "trend", "room"]
    assert on_disk["buy_funnel"]["layers"][1]["factors"][0]["params"]["field"] == "change_rate"
    assert e.store.path.with_suffix(".toml.bak").exists()
    # 套用后继续改：删一层、把一层改成任一满足
    layers = configsvc.get_funnel(e, "buy")["layers"]
    layers = [layers[1], layers[0]]
    layers[0]["mode"] = "any"
    configsvc.put_funnel(e, "buy", layers)
    got = configsvc.get_funnel(e, "buy")
    assert got["explicit"] and [l["id"] for l in got["layers"]] == ["trend", "hard"] and got["layers"][0]["mode"] == "any"
    # 重启后仍然是改过的
    e2 = make_engine.__wrapped__ if hasattr(make_engine, "__wrapped__") else None  # noqa: F841
    from qfpy.config import ConfigStore
    from qfpy.engine import Engine
    e3 = Engine(ConfigStore(e.store.path), offline=True)
    assert [l["id"] for l in layers_of(e3.cfg, "buy")] == ["trend", "hard"]


def test_put_funnel_rejects_invalid_and_keeps_file(make_engine):
    e = make_engine("paper")
    before = e.store.path.read_text(encoding="utf-8")
    with pytest.raises(configsvc.ConfigError):
        configsvc.put_funnel(e, "buy", [{"name": "x", "factors": [{"kind": "stop_loss"}]}])
    with pytest.raises(configsvc.ConfigError):
        configsvc.apply_template(e, "buy", "no_such")
    assert e.store.path.read_text(encoding="utf-8") == before


def test_engine_run_uses_layers(make_engine):
    """引擎完整跑一次（自选股源 + 假分时），漏斗记录里是自定义的层。"""
    from tests.conftest import FakeMarket
    e = make_engine("paper", extra='''
[[buy_funnel.layers]]
id = "one"
name = "任一层"
mode = "any"
[[buy_funnel.layers.factors]]
kind = "range"
params = { field = "change_rate", min = 50 }
[[buy_funnel.layers.factors]]
kind = "above_vwap"
''')
    fm = FakeMarket("2026-11-10", ["2026-11-09", "2026-11-10"], {"600000": (10.2, 10.0)})
    fm.daily_series = lambda code, day, fields: ([], "无", False)
    e.market = lambda: fm
    rec = e.run(date="2026-11-10", trade=False)
    st = [s for s in rec["stages"] if s.get("type") == "layer"]
    assert [s["id"] for s in st] == ["one"] and st[0]["mode"] == "any"
    assert [k["code"] for k in st[0]["kept"]] == ["600000"]      # 涨幅没到 50% 但在均价上，任一满足
