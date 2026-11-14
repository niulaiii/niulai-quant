# Copyright (c) 2026 tick-stock-panel contributors
# SPDX-License-Identifier: MIT
# 来源: https://github.com/shy3130/tick-stock-panel  backend/app/strategy/paper.py
#   _available_of (T+1 按批次记账)、normalize_qty / qty_from_amount、apply_slippage。
# 改动: 去掉账户/文件依赖, 只保留纯函数。
"""T+1 批次记账与数量取整 (纯函数)。"""

from __future__ import annotations

LOT_SIZE = 100  # 一手 100 股


def available_of(pos: dict, today: str | None = None) -> int:
    """T+1 可卖数量: buy_date < today 的批次之和。today 为空则全部可卖 (重建口径)。"""
    if today is None:
        return int(sum(lot["qty"] for lot in pos["lots"]))
    return int(sum(lot["qty"] for lot in pos["lots"] if lot["date"] < today))


def normalize_qty(qty: int) -> int:
    """下取整到百股整数倍。"""
    return int(qty // LOT_SIZE) * LOT_SIZE


def qty_from_amount(amount: float, ref_price: float) -> int:
    """按金额估算数量: 金额 / 参考价 向下取整到百股 (未计费用, 手续费从现金扣)。"""
    if ref_price <= 0:
        return 0
    return normalize_qty(int(amount / ref_price))


def apply_slippage(price: float, side: str, slippage_bps: float) -> float:
    """买入向上滑、卖出向下滑 (对成交价不利方向)。"""
    adj = price * (1 + slippage_bps / 10000.0) if side == "buy" else price * (1 - slippage_bps / 10000.0)
    return round(max(adj, 0.01), 4)


def consume_lots(lots: list[dict], qty: int, today: str) -> list[dict]:
    """按先进先出从可卖批次中扣减 qty (本项目新增, 配合 available_of 使用)。"""
    left = qty
    out = []
    for lot in sorted(lots, key=lambda x: x["date"]):
        if left > 0 and lot["date"] < today:
            take = min(left, lot["qty"])
            left -= take
            rest = lot["qty"] - take
            if rest > 0:
                out.append({**lot, "qty": rest})
        else:
            out.append(lot)
    return out
