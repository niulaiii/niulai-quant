# Copyright (c) 2026 Shadowell
# SPDX-License-Identifier: MIT
# 来源: https://github.com/Shadowell/Alpha  app/services/paper_trading.py
#   默认费率常量、SETTINGS_LIMITS / _coerce_setting 参数校验、_calc_buy_cost / _calc_sell_cost、
#   get_summary 里的胜率与已实现盈亏回撤算法。
# 改动: 从 PaperTradingService 类里抽成纯函数, 去掉 sqlite 依赖。
"""A 股模拟交易费用与汇总统计。"""

from __future__ import annotations

import math
from typing import Any

# A股交易费用默认值
DEFAULT_COMMISSION_RATE = 0.00025  # 佣金费率 万2.5（双向）
DEFAULT_MIN_COMMISSION = 5.0  # 最低佣金 5元
DEFAULT_STAMP_TAX_RATE = 0.0005  # 印花税 万5（卖出单向）
DEFAULT_SLIPPAGE_RATE = 0.001  # 滑点 0.1%

SETTINGS_LIMITS: dict[str, tuple[float, float]] = {
    "commission_rate": (0.0, 1.0),
    "min_commission": (0.0, 100.0),
    "stamp_tax_rate": (0.0, 1.0),
    "slippage_rate": (0.0, 0.1),
}


def coerce_setting(name: str, value: Any, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number between {minimum} and {maximum}") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    if number < minimum or number > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}, got {number}")
    return number


def validate_fee_settings(d: dict) -> dict[str, float]:
    out = {}
    for name, (lo, hi) in SETTINGS_LIMITS.items():
        if name in d:
            out[name] = coerce_setting(name, d[name], lo, hi)
    return out


def calc_buy_cost(price: float, qty: int, s: dict) -> tuple[float, float]:
    """计算买入实际成交价和费用。返回 (实际成交价, 总费用)。"""
    slip_price = price * (1 + s.get("slippage_rate", DEFAULT_SLIPPAGE_RATE))
    amount = slip_price * qty
    commission = max(amount * s.get("commission_rate", DEFAULT_COMMISSION_RATE), s.get("min_commission", DEFAULT_MIN_COMMISSION))
    return round(slip_price, 4), round(commission, 2)


def calc_sell_cost(price: float, qty: int, s: dict) -> tuple[float, float]:
    """计算卖出实际成交价和费用。返回 (实际成交价, 总费用)。"""
    slip_price = price * (1 - s.get("slippage_rate", DEFAULT_SLIPPAGE_RATE))
    amount = slip_price * qty
    commission = max(amount * s.get("commission_rate", DEFAULT_COMMISSION_RATE), s.get("min_commission", DEFAULT_MIN_COMMISSION))
    stamp_tax = amount * s.get("stamp_tax_rate", DEFAULT_STAMP_TAX_RATE)
    return round(slip_price, 4), round(commission + stamp_tax, 2)


def closed_trade_stats(realized: list[float]) -> dict:
    """胜率与已实现盈亏的最大回撤（Alpha get_summary 同算法）。"""
    wins = sum(1 for r in realized if r > 0)
    max_drawdown = 0.0
    cumulative = peak = 0.0
    for r in realized:
        cumulative += r
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    return {
        "closed_count": len(realized),
        "win_count": wins,
        "lose_count": len(realized) - wins,
        "win_rate": round(wins / len(realized) * 100, 1) if realized else 0,
        "total_realized_pnl": round(sum(realized), 2),
        "realized_max_drawdown": round(max_drawdown, 2),
    }
