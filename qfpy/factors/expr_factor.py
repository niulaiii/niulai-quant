"""表达式因子：配置里直接写条件，如 price >= vwap * 1.005 and change_rate < 7，或 close > ma5 and close[1] <= ma5[1]。"""
from __future__ import annotations

from ..expr import Expr, MissingField, candidate_resolver
from .base import Factor, Outcome, ParamSpec, register_factor


@register_factor
class ExprFactor(Factor):
    kind = "expr"
    sides = ("buy", "sell")
    title = "表达式"
    description = ("自定义条件：四则运算、比较、and/or/not、abs/min/max/round；"
                   "日K字段可回看 N 天前：close[1]、ref(ma5, 2)；卖出链里还能用持仓字段 pnl_pct、held_days、now_min 等")
    params = [ParamSpec("expr", "", "表达式", "expr")]

    def validate(self):
        self.e = Expr(str(self.p.get("expr", "")))

    def needs(self):
        return self.e.needs()

    def label(self):
        return self.e.src

    def describe(self):
        return f"表达式：{self.e.src}"

    def evaluate(self, c, ctx):
        if "daily" in self.needs() and not c.daily:
            return Outcome(False, None, c.daily_error or "无日K数据", missing=True)
        if "minute" in self.needs() and not c.minute_loaded:
            return Outcome(False, None, c.minute_error or "无分时数据", missing=True)
        r = candidate_resolver(c)
        try:
            ok = bool(self.e.eval(r))
        except MissingField as m:
            return Outcome(False, None, f"数据缺失：缺少字段 {m}", missing=True)
        vals = []
        for f in self.e.fields()[:6]:
            name, _, rest = f.partition("[")
            v = r(name, int(rest[:-1]) if rest else 0)
            if isinstance(v, (int, float)):
                vals.append(f"{f}={v:.4g}")
        note = "，".join(vals)
        return Outcome(ok, None, ("满足：" if ok else "不满足：") + note)
