"""测试公共夹具。所有测试完全离线：网络请求会被直接拦成异常，确保测试里不可能碰到下单程序。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASE_TOML = """
data_dir = "data"

[sources.watchlist]
type = "watchlist"
enabled = true
codes = ["600000"]

[pool]
pools = ["watchlist:all"]

[execution]
mode = "{mode}"

[risk]
max_order_amount = 10000
max_daily_orders = 5
max_daily_amount = 30000
enforce_session = false

[exits]
enabled = true
take_profit_pct = 5.0
stop_loss_pct = 3.0
max_hold_days = 1
exit_time = "14:50"
check_times = ["09:35"]
hold_can_skip_stop_loss = {skip_sl}

[exits.by_pool."gushi:gold_230"]
max_hold_days = 2
exit_time = "14:45"

[notify]
enabled = true
channels = []
"""


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """任何 httpx 请求都报错：测试不联网，更不会调用 /api/order、/api/cancel、/api/command。"""
    import httpx
    calls: list[tuple] = []

    def boom(*a, **kw):
        calls.append((a, kw))
        raise AssertionError(f"测试中不允许发起网络请求：{a[:1]}")

    monkeypatch.setattr(httpx, "get", boom)
    monkeypatch.setattr(httpx, "post", boom)
    monkeypatch.setattr(httpx.Client, "send", boom)
    monkeypatch.delenv("QF_LIVE_ARMED", raising=False)
    monkeypatch.delenv("QF_SIM_NOW", raising=False)
    monkeypatch.delenv("QF_XIADAN_PASSWORD", raising=False)
    return calls


class FakeMarket:
    """替代真实行情：每只票一个现价和昨收，交易日历用给定列表。"""

    def __init__(self, today: str, days: list[str], prices: dict[str, tuple[float, float]] | None = None):
        self.today = today
        self.days = sorted(days)
        self.prices = dict(prices or {})

    def minute(self, code, day):
        p = self.prices.get(code)
        if not p:
            return None, "无分时"
        return {"points": [{"p": p[0], "avg": p[0]}], "preclose": p[1]}, None

    def latest_trading_date(self):
        return self.today

    def is_trading_day(self, d=None):
        return (d or self.today) in self.days

    def trading_days_between(self, a, b):
        return sum(1 for d in self.days if a < d <= b)


@pytest.fixture
def make_engine(tmp_path):
    """make_engine(mode="paper", skip_sl=False) -> Engine，配置和数据都在临时目录，离线。"""
    from qfpy.config import ConfigStore
    from qfpy.engine import Engine

    def _make(mode: str = "paper", skip_sl: bool = False, extra: str = "") -> Engine:
        cdir = tmp_path / "config"
        if not cdir.exists():        # 沙箱里 mkdir(exist_ok=True) 会被代执行层报 EEXIST，先判存在
            cdir.mkdir(parents=True, exist_ok=True)
        p = cdir / "pipeline.toml"
        p.write_text(BASE_TOML.format(mode=mode, skip_sl=str(skip_sl).lower()) + extra, encoding="utf-8")
        return Engine(ConfigStore(p), offline=True)

    return _make


def add_position(engine, code="600000", name="浦发银行", price=10.0, qty=500, day="2026-11-09",
                 sources=("watchlist:all",), state="confirmed"):
    """直接往账本里放一笔持仓。paper 用模拟成交，其他模式用本地记账。"""
    if engine.mode == "paper":
        engine.book.paper_fill_buy(code, name, price, qty, day, list(sources))
        if state != "confirmed":
            engine.book.positions()[code]["state"] = state
    else:
        engine.book.ledger_buy(code, name, price, qty, day, list(sources), state, f"T-{code}")
    return engine.book.get(code)
