# Copyright (c) 2026 Shadowell
# SPDX-License-Identifier: MIT
# 来源: https://github.com/Shadowell/Alpha  app/services/pool_transition.py
# 改动: POOL_* 常量内联 (原版从 app.config 导入); 其余逐字保留。
"""分级股池（候选 → 重点 → 买入）迁池层级校验。

- 晋升必须逐级：``candidate -> focus -> buy``；跨级晋升（如候选直接进买入）非法。
- 降级允许跨级（买入/重点随时撤出），同级迁移幂等成功。
"""
from __future__ import annotations

from dataclasses import dataclass

POOL_CANDIDATE = "candidate"
POOL_FOCUS = "focus"
POOL_BUY = "buy"

POOL_LEVELS: dict[str, int] = {POOL_CANDIDATE: 0, POOL_FOCUS: 1, POOL_BUY: 2}
POOL_TIER_NAMES: dict[str, str] = {POOL_CANDIDATE: "候选", POOL_FOCUS: "重点", POOL_BUY: "买入"}

INVALID_TARGET_MESSAGE = "非法目标池"
MISSING_ENTRY_MESSAGE = "股票不存在于当前选股池"
INVALID_CURRENT_MESSAGE = "股票当前池位异常，无法迁移"


@dataclass(frozen=True)
class PoolTransitionDecision:
    allowed: bool
    code: str
    message: str
    reason: str = ""


def buy_pool_full_message(max_size: int) -> str:
    return f"买入池已满({max_size}只)"


def validate_pool_transition(current_pool: str | None, target_pool: str | None) -> PoolTransitionDecision:
    """校验一次手动迁池是否合法（不含容量校验，容量由调用方按各自池统计）。"""
    clean_target = str(target_pool or "").strip()
    clean_current = str(current_pool or "").strip()
    if clean_target not in POOL_LEVELS:
        return PoolTransitionDecision(False, "invalid_target", INVALID_TARGET_MESSAGE)
    if clean_current not in POOL_LEVELS:
        return PoolTransitionDecision(False, "invalid_current", INVALID_CURRENT_MESSAGE)

    diff = POOL_LEVELS[clean_target] - POOL_LEVELS[clean_current]
    kind = "手动迁移"
    if diff == 0:
        return PoolTransitionDecision(True, "ok_idempotent", "已在目标池（幂等成功）", reason=_reason(clean_current, clean_target, kind))
    if diff < 0:
        return PoolTransitionDecision(True, "ok_demote", "迁移成功", reason=_reason(clean_current, clean_target, kind))
    if diff == 1:
        return PoolTransitionDecision(True, "ok_promote", "迁移成功", reason=_reason(clean_current, clean_target, kind))

    current_name = POOL_TIER_NAMES[clean_current]
    target_name = POOL_TIER_NAMES[clean_target]
    return PoolTransitionDecision(
        False,
        "skip_level",
        f"禁止跨级晋升：{current_name}池不能直接进入{target_name}池，请逐级迁移（候选→重点→买入）",
    )


def _reason(current_pool: str, target_pool: str, kind: str) -> str:
    return f"{kind}: {current_pool} -> {target_pool}"


def record_pool_move(entry: dict, reason: str, moved_at: str) -> None:
    """把迁移原因写入条目的 trigger_log（与策略池自动迁池同一日志格式）。"""
    logs = entry.setdefault("trigger_log", [])
    logs.append({"time": moved_at, "level": "info", "note": reason})
    if len(logs) > 120:
        del logs[:-120]
