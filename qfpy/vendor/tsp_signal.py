# Copyright (c) 2026 tick-stock-panel contributors
# SPDX-License-Identifier: MIT
# 来源: https://github.com/shy3130/tick-stock-panel
#   信号 JSON 格式见 backend/tests/fixtures/custom_signals/*.json
#   ({id, name, kind, timeframe, enabled, conditions:[{left, op, right, leftDays, rightDays}], column})
# 这里是按该格式写的轻量求值器 (原项目基于 polars 列运算, 本项目对单只股票的日K序列逐条求值)。
# 语义保持一致: left 是字段名; right 是数字或 "field:字段名"; leftDays/rightDays 表示取 N 个交易日之前的值;
# 所有条件同时满足 (AND) 才算触发。
"""tick-stock-panel 信号 JSON 条件求值。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

OPS: dict[str, Callable[[float, float], bool]] = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: abs(a - b) < 1e-9,
    "!=": lambda a, b: abs(a - b) >= 1e-9,
}


def _operand(spec, days: int, getter):
    if isinstance(spec, (int, float)):
        return float(spec)
    s = str(spec).strip()
    if s.startswith("field:"):
        return getter(s[6:], days)
    try:
        return float(s)
    except ValueError:
        return getter(s, days)


def evaluate_conditions(conditions: list[dict], getter) -> tuple[bool, list[str]]:
    """getter(field, days_ago) -> float | None。返回 (是否全部满足, 每条条件的说明)。"""
    notes: list[str] = []
    ok = True
    for c in conditions:
        op = c.get("op", ">")
        if op not in OPS:
            raise ValueError(f"不支持的比较符 {op}")
        ld, rd = int(c.get("leftDays", 0) or 0), int(c.get("rightDays", 0) or 0)
        lv = _operand(c.get("left"), ld, getter)
        rv = _operand(c.get("right"), rd, getter)
        ltxt = f"{c.get('left')}" + (f"[{ld}天前]" if ld else "")
        rtxt = f"{c.get('right')}".replace("field:", "") + (f"[{rd}天前]" if rd else "")
        if lv is None or rv is None:
            notes.append(f"{ltxt} {op} {rtxt}：缺少数据")
            ok = False
            continue
        hit = OPS[op](lv, rv)
        notes.append(f"{ltxt}={lv:.4g} {op} {rtxt}={rv:.4g} {'✓' if hit else '✗'}")
        ok = ok and hit
    return ok, notes


def load_signal_files(directory: Path) -> list[dict]:
    out = []
    if not directory.exists():
        return out
    for f in sorted(directory.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            d.setdefault("id", f.stem)
            out.append(d)
        except Exception:  # noqa: BLE001
            continue
    return out
