"""人工保留策略插件（公共能力，独立目录，不依赖其他插件的 import）。

主程序只在「持仓有保留标记、且这一层允许被跳过」时调用 decide()；
返回 None = 不干预，按主程序默认（挡一次、清标记）。本插件实现另外两种：

  until_day_end   挡到 set_at 所属（非交易日则之后第一个）交易日结束
  until_manual    一直挡，直到手工点「取消保留」

交易日按公共数据文件契约读 data/cache/trade_calendar.json（trade_calendar 插件产出），
不 import 任何插件，加载顺序无关。止损层由主程序最先检查且不可跳过，本插件不参与。
"""
from __future__ import annotations

import json
import tomllib
from datetime import date, timedelta
from pathlib import Path

from qfpy.plugins import register_hold_policy

_HERE = Path(__file__).resolve().parent

MODE_NEXT_CHECK = "until_next_check"
MODE_DAY_END = "until_day_end"
MODE_MANUAL = "until_manual"


def _settings() -> dict:
    try:
        return tomllib.loads((_HERE / "plugin.toml").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _trading_days(data_dir) -> list[str]:
    """公共数据文件契约：data/cache/trade_calendar.json 的 dates 数组。"""
    try:
        p = Path(data_dir) / "cache" / "trade_calendar.json"
        return sorted(str(d) for d in (json.loads(p.read_text(encoding="utf-8")).get("dates") or []))
    except Exception:
        return []


def _day_end(set_at: str, days: list[str]) -> str | None:
    """这次「到该交易日结束」的有效日：set_at 当天若是交易日就是它，否则取之后第一个交易日。

    日历文件缺失或没覆盖到时，退化为「当天之后的第一个工作日」，保证功能不崩。
    """
    d = str(set_at or "")[:10]
    if not d:
        return None
    for x in days:
        if x >= d:
            return x
    try:
        cur = date.fromisoformat(d)
    except Exception:
        return None
    for _ in range(10):
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            return cur.isoformat()
    return None


class HoldPolicy:
    name = "hold_policy"

    def label(self) -> str:
        return "保留三选一（到下一次检查 / 到该交易日结束 / 到手工解除）"

    def decide(self, code, pos, ev, day, now_hm, data_dir):
        if not _settings().get("enabled", True):
            return None
        hold = pos.get("hold") or {}
        mode = hold.get("mode") or MODE_NEXT_CHECK  # 老数据没有 mode：按原来的「只挡一次」
        if mode == MODE_NEXT_CHECK:
            return None
        if mode == MODE_MANUAL:
            return {"skip": True, "consume": False,
                    "note": "已点「保留」（到手工解除）：本次不卖，标记还在；止损除外，照卖"}
        if mode == MODE_DAY_END:
            end = _day_end(hold.get("set_at", ""), _trading_days(data_dir))
            if end and str(day) <= end:
                return {"skip": True, "consume": False,
                        "note": f"已点「保留」（到 {end} 收盘）：本次不卖，标记还在；止损除外，照卖"}
            return {"skip": False, "consume": True,
                    "note": f"保留已到 {end or '该交易日'} 收盘，标记失效，本次照常卖"}
        return None


register_hold_policy("hold_policy", HoldPolicy())
