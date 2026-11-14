"""人工保留三种挡法：核心薄钩子 + hold_policy 插件。全部离线（FakeMarket）。

模式值：until_next_check（只挡一次，原来的行为）/ until_day_end（到该交易日结束）
/ until_manual（到手工解除）。三种都挡不住止损——由核心的 hold_skippable 保证。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
from pathlib import Path

import pytest

from qfpy import plugins as P
from qfpy.config import ConfigStore
from qfpy.engine import Engine
from tests.conftest import BASE_TOML, FakeMarket, add_position

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "hold_policy"
DAYS = ["2026-11-06", "2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12"]


def _load_plugin():
    spec = importlib.util.spec_from_file_location("qf_test_hold_policy", PLUGIN_DIR / "plugin.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)          # 模块底部会 register_hold_policy
    return m


@pytest.fixture
def no_policy():
    """注册表是模块级对象，engine 里绑定的是同一个引用，只能 clear 不能替换。"""
    saved = dict(P.HOLD_POLICIES)
    P.HOLD_POLICIES.clear()
    yield
    P.HOLD_POLICIES.clear()
    P.HOLD_POLICIES.update(saved)


@pytest.fixture
def policy(no_policy):
    return _load_plugin()


def _calendar(e):
    d = e.data_dir / "cache"
    d.mkdir(parents=True, exist_ok=True)
    (d / "trade_calendar.json").write_text(json.dumps({"dates": DAYS}), encoding="utf-8")


def build(tmp_path, price=10.6, today="2026-11-10"):
    cdir = tmp_path / "config"
    cdir.mkdir(parents=True, exist_ok=True)
    p = cdir / "pipeline.toml"
    p.write_text(BASE_TOML.format(mode="paper", skip_sl="false"), encoding="utf-8")
    e = Engine(ConfigStore(p), offline=True)
    fm = FakeMarket(today, DAYS, {"600000": (price, 10.0)})
    e.market = lambda: fm
    e.set_auto(True, "pytest")
    add_position(e, day="2026-11-09", price=10.0)
    _calendar(e)
    return e, fm


def mark(e, mode, set_at="2026-11-10 09:00:00"):
    e.set_hold("600000", True, note="pytest", mode=mode)
    e.book.get("600000")["hold"]["set_at"] = set_at      # 控制「点保留」的时间
    return e.book.get("600000")


def once(e, hm="11:00"):
    return e.check_exits(now_hm=hm)["results"][0]


# ---------------- 原有行为不变 ----------------

def test_no_plugin_keeps_old_behavior(no_policy, tmp_path):
    """没装插件：挡一次、清标记、第二次照卖。"""
    e, _ = build(tmp_path)
    mark(e, "until_next_check")
    r1 = once(e)
    assert r1["action"] == "hold_skipped" and e.book.get("600000").get("hold") is None
    r2 = once(e, "11:05")
    assert r2["action"] == "filled"


def test_next_check_mode_same_as_default(policy, tmp_path):
    """模式一：插件返回 None，行为与原来完全一致。"""
    e, _ = build(tmp_path)
    mark(e, "until_next_check")
    assert once(e)["action"] == "hold_skipped"
    assert e.book.get("600000").get("hold") is None
    assert once(e, "11:05")["action"] == "filled"


def test_old_hold_data_without_mode(policy, tmp_path):
    """老数据没有 mode：按「只挡一次」处理。"""
    e, _ = build(tmp_path)
    mark(e, "until_next_check")
    del e.book.get("600000")["hold"]["mode"]
    assert once(e)["action"] == "hold_skipped"
    assert e.book.get("600000").get("hold") is None


# ---------------- 模式二 / 模式三 ----------------

def test_manual_mode_keeps_holding(policy, tmp_path):
    """到手工解除：多次检查都挡，标记一直不消耗。"""
    e, _ = build(tmp_path)
    mark(e, "until_manual")
    for hm in ("11:00", "11:05", "13:30"):
        r = once(e, hm)
        assert r["action"] == "hold_skipped", hm
        assert e.book.get("600000").get("hold") is not None
        assert "到手工解除" in r["note"]
    e.set_hold("600000", False)
    assert e.book.get("600000").get("hold") is None


def test_day_end_mode_expires_on_next_trading_day(policy, tmp_path):
    """到该交易日结束：当天全挡，下一个交易日标记失效、照常卖。"""
    e, fm = build(tmp_path)
    mark(e, "until_day_end", "2026-11-10 09:00:00")
    r1 = once(e, "11:00")
    assert r1["action"] == "hold_skipped" and e.book.get("600000").get("hold") is not None
    assert "2026-11-10" in r1["note"]
    r2 = once(e, "13:30")
    assert r2["action"] == "hold_skipped" and e.book.get("600000").get("hold") is not None
    fm.today = "2026-11-11"
    r3 = once(e, "11:00")
    assert r3["action"] == "filled", r3["note"]
    assert e.book.get("600000") is None


def test_day_end_non_trading_day_uses_next_trading_day(policy, tmp_path):
    """周末点「到该交易日结束」：至少保到下一个交易日（日历没覆盖时用工作日兜底）。"""
    e, fm = build(tmp_path, today="2026-11-16")
    mark(e, "until_day_end", "2026-11-14 20:00:00")     # 周六
    r = once(e, "11:00")
    assert r["action"] == "hold_skipped"
    fm.today = "2026-11-17"
    assert once(e, "11:00")["action"] == "filled"


# ---------------- 止损铁律 ----------------

def test_stop_loss_never_blocked_by_any_mode(policy, tmp_path):
    """三种模式都挡不住止损（核心在 hold_skippable=false 时根本不调用插件）。"""
    e, _ = build(tmp_path, price=9.7)                    # 浮亏 3%，触发止损
    mark(e, "until_manual")
    r = once(e)
    assert r["action"] == "filled"
    assert "止损不允许被保留跳过" in r["note"]


# ---------------- 插件与数据通道 ----------------

def test_plugin_decide_unit(policy, tmp_path):
    """直接测插件返回值：不干预 / 挡且不清 / 过期清标记照卖。"""
    obj = P.HOLD_POLICIES["hold_policy"]
    days_dir = tmp_path / "data"
    _calendar_like = {"dates": DAYS}
    (days_dir / "cache").mkdir(parents=True, exist_ok=True)
    (days_dir / "cache" / "trade_calendar.json").write_text(json.dumps(_calendar_like), encoding="utf-8")
    base = {"set_at": "2026-11-10 09:00:00"}
    assert obj.decide("600000", {"hold": {**base, "mode": "until_next_check"}}, {}, "2026-11-10", "11:00", days_dir) is None
    r = obj.decide("600000", {"hold": {**base, "mode": "until_manual"}}, {}, "2026-11-11", "11:00", days_dir)
    assert r["skip"] and not r["consume"]
    r = obj.decide("600000", {"hold": {**base, "mode": "until_day_end"}}, {}, "2026-11-10", "14:50", days_dir)
    assert r["skip"] and not r["consume"]
    r = obj.decide("600000", {"hold": {**base, "mode": "until_day_end"}}, {}, "2026-11-11", "11:00", days_dir)
    assert not r["skip"] and r["consume"]


def test_set_hold_stores_mode(policy, tmp_path):
    """网页/命令行传进来的 mode 落进持仓标记和 holds.jsonl。"""
    e, _ = build(tmp_path)
    p = e.set_hold("600000", True, "pytest", by="网页", mode="until_manual")
    assert p["hold"]["mode"] == "until_manual"
    lines = [json.loads(x) for x in (e.data_dir / "holds.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    assert lines[-1]["mode"] == "until_manual"


def test_manifest_and_registration(policy):
    """插件清单格式正确，且加载后确实注册了策略对象。"""
    meta = tomllib.loads((PLUGIN_DIR / "plugin.toml").read_text(encoding="utf-8"))
    assert meta["enabled"] is True and meta["module"] == "plugin"
    assert "hold_policy" in P.HOLD_POLICIES
    assert callable(getattr(P.HOLD_POLICIES["hold_policy"], "decide", None))
