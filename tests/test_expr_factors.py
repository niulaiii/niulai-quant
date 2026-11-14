"""因子与表达式（含日K回看）。"""
from __future__ import annotations

import pytest

from qfpy.expr import Expr, ExprError, MissingField, candidate_resolver
from qfpy.factors import build_chain
from qfpy.factors.base import FactorContext, apply_missing
from qfpy.model import Candidate


def bars(closes, vols=None):
    vols = vols or [1000] * len(closes)
    out = []
    for i, (c, v) in enumerate(zip(closes, vols)):
        out.append({"date": f"2026-10-{i + 1:02d}", "open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": v,
                    "amount": c * v})
    return out


def cand(fields=None, daily=None, sources=("watchlist:all",), name="测试股份", code="600000"):
    c = Candidate(code, name, list(sources), dict(fields or {}))
    c.daily = daily
    if fields and "vwap" in fields:
        c.minute_loaded = True
    return c


def ev(expr, c):
    return Expr(expr).eval(candidate_resolver(c))


# ---------------- 表达式 ----------------

def test_expr_arithmetic_and_logic():
    c = cand({"price": 10.1, "vwap": 10.0, "change_rate": 5.0})
    assert ev("price >= vwap * 1.005 and change_rate < 7", c) is True
    assert ev("price >= vwap * 1.02 or change_rate > 6", c) is False
    assert ev("not (change_rate > 6)", c) is True
    assert ev("abs(-change_rate) == 5 and max(price, vwap) == price and min(1, 2) == 1", c) is True
    assert ev("1 < change_rate < 6", c) is True        # 链式比较
    assert ev("round(price / vwap, 2) == 1.01", c) is True
    assert ev("change_rate > 4 && price > 10", c) is True  # && / || 写法兼容


def test_expr_lookback_daily():
    # 前 4 天 10，昨天 9（在 5 日线下），今天 11（站上 5 日线）
    c = cand(daily=bars([10, 10, 10, 10, 9, 11]))
    assert ev("close[1] == 9 and close[2] == 10", c) is True
    assert ev("ref(close, 1) == close[1]", c) is True
    assert ev("close > ma5 and close[1] <= ma5[1]", c) is True     # 上穿 5 日线
    assert ev("price[1] == 9", c) is True                           # price[1] 映射到昨收
    assert Expr("close > ma5 and close[1] <= ma5[1]").needs() == {"daily"}
    assert Expr("price > vwap").needs() == {"minute"}


def test_expr_lookback_beyond_history_is_missing():
    c = cand(daily=bars([10, 11]))
    with pytest.raises(MissingField):
        ev("close[5] > 0", c)
    with pytest.raises(MissingField):
        ev("ma5 > 0", c)   # 不足 5 根算不出 MA5


@pytest.mark.parametrize("bad, msg", [
    ("__import__('os').system('x')", "不支持"),
    ("__import__('os')", "不支持的函数"),
    ("eval('1') > 0", "不支持的函数"),
    ("price.real > 1", "不支持的写法"),
    ("foo > 1", "未知字段"),
    ("vwap[1] > 1", "只有日K字段能回看"),
    ("close[n] > 1", "整数常量"),
    ("close[999] > 1", "回看天数"),
    ("price >", "语法错误"),
    ("", "为空"),
])
def test_expr_rejects_unsafe_or_invalid(bad, msg):
    with pytest.raises(ExprError, match=msg):
        Expr(bad)


def test_expr_division_by_zero_is_missing_not_crash():
    with pytest.raises(MissingField):
        ev("price / vwap > 1", cand({"price": 1.0, "vwap": 0.0}))


# ---------------- 因子 ----------------

def run_chain(specs, cands):
    chain = build_chain(specs)
    for _spec, fac, err in chain:
        assert err is None, err
    ctx = FactorContext("2026-11-10", None)
    for spec, fac, _ in chain:
        cands = [c for c in cands if apply_missing(fac.evaluate(c, ctx), spec.get("params")).passed]
    return [c.code for c in cands]


def test_builtin_factors_filter_as_expected():
    good = cand({"price": 10.5, "change_rate": 5, "market_value": 100, "limit_gap_pct": 5, "vwap": 10.2,
                 "vwap_dev_pct": 2.9, "drawdown_pct": 1, "amount_yi": 3}, code="600001")
    st = cand({**good.fields}, code="600002", name="*ST风险")
    star = cand({**good.fields}, code="688001")
    small = cand({**good.fields, "market_value": 10}, code="600003")
    near_up = cand({**good.fields, "limit_gap_pct": 0.5}, code="600004")
    below = cand({**good.fields, "vwap_dev_pct": -0.4}, code="600005")
    dumped = cand({**good.fields, "drawdown_pct": 6}, code="600006")
    thin = cand({**good.fields, "amount_yi": 0.2}, code="600007")
    specs = [{"kind": "exclude_st"}, {"kind": "exclude_board", "params": {"boards": ["star", "bse"]}},
             {"kind": "range", "params": {"field": "market_value", "min": 30, "max": 800}},
             {"kind": "not_limit_up", "params": {"buffer_pct": 1.0}}, {"kind": "above_vwap"},
             {"kind": "max_drawdown", "params": {"max_pct": 4}}, {"kind": "min_amount", "params": {"min_yi": 1}}]
    assert run_chain(specs, [good, st, star, small, near_up, below, dumped, thin]) == ["600001"]


def test_min_hits_and_range_allow_missing():
    a = cand({}, sources=["gushi:auction_long", "gushi:morning_star"], code="600010")
    b = cand({}, sources=["watchlist:all"], code="600011")
    assert run_chain([{"kind": "min_hits", "params": {"min": 2}}], [a, b]) == ["600010"]
    spec = [{"kind": "range", "params": {"field": "market_value", "min": 30, "allow_missing": True}}]
    assert run_chain(spec, [b]) == ["600011"]
    spec[0]["params"]["allow_missing"] = False
    assert run_chain(spec, [b]) == []
    spec = [{"kind": "range", "params": {"field": "market_value", "min": 30, "missing": "pass"}}]   # 新写法
    assert run_chain(spec, [b]) == ["600011"]
    spec[0]["params"]["missing"] = "drop"
    assert run_chain(spec, [b]) == []


def test_expr_factor_with_lookback_and_missing_daily():
    up = cand(daily=bars([10, 10, 10, 10, 9, 11]), code="600020")
    flat = cand(daily=bars([10, 10, 10, 10, 10, 10]), code="600021")
    nodaily = cand(code="600022")
    nodaily.daily_error = "日K拉取失败"
    spec = [{"kind": "expr", "params": {"expr": "close > ma5 and close[1] <= ma5[1]"}}]
    assert run_chain(spec, [up, flat, nodaily]) == ["600020"]
    fac = build_chain(spec)[0][1]
    out = fac.evaluate(nodaily, FactorContext("2026-11-10", None))
    assert not out.passed and "日K" in out.note


def test_signal_factor_tick_stock_panel_format():
    vols = [100, 100, 100, 100, 100, 300]
    surge = cand(daily=bars([10] * 6, vols), code="600030")
    quiet = cand(daily=bars([10] * 6), code="600031")
    spec = [{"kind": "signal", "params": {"conditions": [
        {"left": "vol_ratio_5d", "op": ">=", "right": "2", "leftDays": 0, "rightDays": 0},
        {"left": "close", "op": ">=", "right": "field:close", "leftDays": 0, "rightDays": 1}]}}]
    assert run_chain(spec, [surge, quiet]) == ["600030"]


def test_bad_factor_config_reports_error():
    chain = build_chain([{"kind": "expr", "params": {"expr": "nope > 1"}}, {"kind": "no_such_kind"}])
    assert chain[0][1] is None and "未知字段" in chain[0][2]
    assert chain[1][1] is None and "未知因子" in chain[1][2]
