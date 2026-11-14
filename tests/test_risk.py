"""风控：对所有模式生效。"""
from __future__ import annotations

from datetime import datetime

import pytest

from qfpy.exec.risk import Risk
from qfpy.util import CN

DAY = "2026-11-10"


def order(**kw):
    it = {"side": "buy", "symbol": "600000", "name": "浦发银行", "price": 10.03, "quantity": 500, "ref_price": 10.0,
          "limit_up": 11.0, "limit_down": 9.0, "trading_date": DAY, "is_trading_day": True}
    it.update(kw)
    return it


@pytest.fixture
def eng(make_engine):
    return make_engine("paper")


def fail(eng, it, mode="paper"):
    return Risk.first_fail(eng.risk.check(it, mode, DAY))


def test_normal_buy_passes(eng):
    assert fail(eng, order()) is None


@pytest.mark.parametrize("kw, expect", [
    ({"quantity": 150}, "整手"),
    ({"quantity": 1000, "price": 10.03 + 0}, "单笔上限"),           # 10030 > 10000
    ({"price": 10.035}, "数字限价"),                               # 三位小数
    ({"price": "市价"}, "数字限价"),                                # 价格关键字
    ({"quantity": 100.0}, "整数股数"),
    ({"symbol": "60000"}, "代码格式"),
    ({"price": 11.2, "ref_price": 11.1}, "涨跌停范围"),
    ({"price": 10.5}, "偏离参考价"),                               # 5% > 3%
    ({"limit_up": None, "limit_down": None}, "涨跌停范围"),
    ({"symbol": "688001", "quantity": 100, "price": 10.03}, "整手"),  # 科创板至少 200
])
def test_buy_rejections(eng, kw, expect):
    msg = fail(eng, order(**kw))
    assert msg and msg.startswith(expect), msg


def test_white_and_black_list(eng, make_engine):
    e = make_engine("paper", extra="")
    e.cfg["risk"]["blacklist"] = ["600000"]
    assert fail(e, order()).startswith("黑名单")
    # 黑名单只拦买入，卖出要能走（否则拉黑后就卖不掉了）
    assert fail(e, order(side="sell", price=9.97, sellable=500)) is None
    e.cfg["risk"]["blacklist"] = []
    e.cfg["risk"]["whitelist"] = ["000001"]
    assert fail(e, order()).startswith("白名单")


def test_daily_limits_and_one_buy_per_symbol(eng):
    it = order()
    eng.risk.record(it, DAY)
    assert fail(eng, order()).startswith("同票每日一次")
    for i in range(3):
        eng.risk.record(order(symbol=f"60000{i + 1}"), DAY)
    # 已 4 笔、累计 20060 元；再来一笔 5015 元仍在上限内
    assert fail(eng, order(symbol="600009")) is None
    eng.risk.record(order(symbol="600009"), DAY)
    # 不再限制每日笔数：第 6 笔照样能下（金额 30090 超了，所以换一笔小的）
    assert fail(eng, order(symbol="600008", quantity=100)) is None
    u = eng.risk.usage(DAY)
    assert u["orders"] == 5 and u["amount"] == pytest.approx(5 * 5015)


def test_sells_not_limited_by_buy_caps(eng):
    """止损/止盈/到点卖出的卖单不受单笔上限、每日金额、每日笔数、白名单限制。"""
    for i in range(5):
        eng.risk.record(order(symbol=f"60000{i + 1}", quantity=1100), DAY)     # 买到超过每日金额
    assert fail(eng, order(symbol="600009")).startswith("每日金额")
    big = order(side="sell", price=9.97, quantity=5000, sellable=5000)          # 49850 元，远超单笔上限
    assert fail(eng, big) is None
    eng.cfg["risk"]["whitelist"] = ["000001"]
    assert fail(eng, big) is None
    eng.risk.record(big, DAY)
    u = eng.risk.usage(DAY)
    assert u["sell_orders"] == 1 and u["amount"] == pytest.approx(5 * 1100 * 10.03)   # 卖出不计入每日买入金额


def test_daily_amount_limit(eng):
    eng.risk.record(order(quantity=900, symbol="600001"), DAY)
    eng.risk.record(order(quantity=900, symbol="600002"), DAY)
    eng.risk.record(order(quantity=900, symbol="600003"), DAY)   # 累计 27081
    assert fail(eng, order(quantity=500, symbol="600004")).startswith("每日金额")


def test_halt_blocks_buy_and_sell(eng):
    eng.switches.halt("测试", "pytest")
    assert fail(eng, order()).startswith("一键停止")
    assert fail(eng, order(side="sell", price=9.97, sellable=500)).startswith("一键停止")
    eng.switches.resume("pytest")
    assert fail(eng, order()) is None


def test_sell_checks_t1_sellable_and_odd_lot(eng):
    base = dict(side="sell", price=9.97)
    assert fail(eng, order(**base, quantity=500, sellable=0)).startswith("可卖数量")   # 当天买的 T+1 不能卖
    assert fail(eng, order(**base, quantity=500, sellable=300)).startswith("可卖数量")
    assert fail(eng, order(**base, quantity=50, sellable=150)).startswith("零股")
    assert fail(eng, order(**base, quantity=150, sellable=150)) is None                  # 零股一次卖完


def test_session_enforced_for_live_even_if_disabled(eng, monkeypatch):
    import qfpy.exec.risk as risk_mod
    monkeypatch.setattr(risk_mod, "now_cn", lambda: datetime(2026, 11, 10, 12, 0, tzinfo=CN))
    msg = fail(eng, order(), mode="live")
    assert msg.startswith("交易时段")
    monkeypatch.setattr(risk_mod, "now_cn", lambda: datetime(2026, 11, 10, 10, 0, tzinfo=CN))
    assert fail(eng, order(), mode="live") is None
    # 实盘只能用当天数据
    assert fail(eng, order(trading_date="2026-11-09"), mode="live").startswith("实盘用当天数据")
    # 非交易日
    assert fail(eng, order(is_trading_day=False), mode="live").startswith("交易时段")
