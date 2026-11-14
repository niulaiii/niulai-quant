# Copyright (c) 2026 tick-stock-panel contributors
# SPDX-License-Identifier: MIT
# 来源: https://github.com/shy3130/tick-stock-panel  backend/app/price_limits.py
# 改动: 只保留标量函数(去掉 polars/numpy 向量化部分); 代码改为 6 位纯数字也能识别北交所
#      (原版用 ".BJ" 后缀); 增加 limit_prices() 和 is_bse() 便捷函数。
#      主板 ST/*ST 改 10% 的生效日保持原版的 2026-07-06（沪深《交易规则（2026年修订）》）；price_limit_pct 改为查 qfpy.limits 的规则表
#      (规则只在那里定义一份)。板块、ST、新股无涨跌幅窗口也都转调 qfpy.limits；没人用的 board_limit_pct 和板块常量已删。
"""A-share price-limit rules shared by indicators, backtests, and APIs."""

from __future__ import annotations

from datetime import date, datetime


def is_bse(symbol: str) -> bool:
    from ..limits import board_of
    return board_of(symbol) == "bse"


def is_risk_warning_name(name: str | None) -> bool:
    from ..limits import is_st
    return is_st(name)


def price_limit_pct(symbol: str, trade_date: date, *, is_risk_warning: bool = False) -> float:
    from ..limits import limit_pct
    return limit_pct(symbol, trade_date, st=bool(is_risk_warning))


def no_limit_window_days(symbol: str, listing_date: date | None) -> int:
    """上市初期无涨跌幅限制的交易日窗口数; 0 = 不适用 (老股/无上市日)。转调 qfpy.limits。"""
    if listing_date is None:
        return 0
    from ..limits import no_limit_days
    return no_limit_days(symbol, listing_date)


def parse_listing_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def is_no_limit_day(symbol: str, listing_date: date | None, trade_date: date, *, day_rank: int | None = None) -> bool:
    """trade_date 是否落在上市初期无涨跌幅窗口内。转调 qfpy.limits.is_no_limit_day。"""
    from ..limits import is_no_limit_day as _f
    return _f(symbol, listing_date, trade_date, day_rank)


def limit_price(previous: float, limit_pct: float, *, up: bool) -> float:
    """交易所四舍五入到分 (整数分运算, 与原版 numpy_limit_price 同口径)。"""
    sign = 1 if up else -1
    numerator = int(round((1.0 + sign * limit_pct) * 100.0))
    cents = int(previous * 100.0 + 0.5)
    return ((cents * numerator + 50) // 100) / 100.0


def limit_prices(previous: float, limit_pct: float) -> tuple[float, float]:
    return limit_price(previous, limit_pct, up=True), limit_price(previous, limit_pct, up=False)
