"""tick-stock-panel 信号 JSON 因子：读 conditions [{left, op, right, leftDays, rightDays}]，按日K求值。

可以直接写在配置里（params.conditions），也可以指向一个 JSON 文件（params.file，格式与 tick-stock-panel 自定义信号相同）。
"""
from __future__ import annotations

import json
from pathlib import Path

from ..expr import KNOWN_FIELDS, LAG_ALIAS
from ..market import DAILY_FIELDS
from ..vendor.tsp_signal import OPS, evaluate_conditions
from .base import Factor, Outcome, ParamSpec, register_factor


@register_factor
class SignalFactor(Factor):
    kind = "signal"
    title = "形态信号"
    cat = "signal"
    description = ("用几条「字段 比较符 字段/数字」拼成的形态，全部满足才算出现信号。可以直接点选自带信号"
                   "（突破MA5、60日新高、放量），也可以自己拼。「几天前」= 取 N 个交易日前的值。"
                   "格式与 tick-stock-panel 自定义信号 JSON 相同，旧信号文件照样能用。"
                   "和「自定义公式」的分工：形态信号是点选版、只能「全部满足」、能存成文件复用；公式要手写，但能写 or、括号和四则运算。")
    example = "突破MA5：今天收盘 > MA5，且 1 天前收盘 ≤ 1 天前的 MA5"
    params = [ParamSpec("file", "", "信号（点选自带或自定义信号；与 conditions 二选一）", "signal"),
              ParamSpec("conditions", [], "条件列表 [{left, op, right, leftDays, rightDays}]", "conditions")]

    def validate(self):
        conds = self.p.get("conditions") or []
        self.sig_name = ""
        if self.p.get("file"):
            path = Path(self.p["file"])
            if not path.is_absolute() and self.root is not None:
                path = Path(self.root) / path
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError as e:
                raise ValueError(f"信号文件不存在：{self.p['file']}") from e
            conds = d.get("conditions") or []
            self.sig_name = d.get("name", "")
        if not conds:
            raise ValueError("信号没有条件")
        for c in conds:
            if c.get("op") not in OPS:
                raise ValueError(f"不支持的比较符 {c.get('op')}")
            for side in ("left", "right"):
                v = str(c.get(side, "")).replace("field:", "")
                try:
                    float(v)
                    continue
                except ValueError:
                    pass
                if v not in KNOWN_FIELDS:
                    raise ValueError(f"未知字段 {v}")
        self.conds = conds

    def needs(self):
        return {"daily"}

    def label(self):
        return f"形态信号：{self.sig_name}" if self.sig_name else "形态信号"

    def evaluate(self, c, ctx):
        if not c.daily:
            return Outcome.missing_data(c.daily_error or "无日K数据")

        def getter(field, days):
            if days == 0:
                v = c.get(field)
                if v is not None or field not in DAILY_FIELDS:
                    return v
            return c.daily_at(LAG_ALIAS.get(field, field) if days else field, days)

        ok, notes = evaluate_conditions(self.conds, getter)
        if not ok and any(n.endswith("缺少数据") for n in notes) and not any(n.endswith("✗") for n in notes):
            return Outcome.missing_data("；".join(notes))
        return Outcome(ok, None, "；".join(notes))
