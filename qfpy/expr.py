"""安全表达式引擎。

语法与 Rust 版一致：四则运算、比较、and / or / not、abs / min / max；另加：
  - 回看 N 个交易日前：close[1]、ma5[2]、ref(close, 1)（参考 tick-stock-panel 信号 JSON 的 leftDays/rightDays）
  - round(x, n)、链式比较 1 < x < 5
用 Python ast 解析后只允许白名单里的节点，不会执行任意代码。
"""
from __future__ import annotations

import ast
import operator
from typing import Callable

from .market import DAILY_FIELDS, MINUTE_FIELDS

POOL_FIELDS = ("price", "open_price", "open_rise", "change_rate", "market_value", "hit_count", "preclose", "limit_pct",
               "limit_up", "limit_down", "limit_gap_pct", "no_limit", "score")
# 卖出因子链里对持仓逐只检查时可用的持仓字段
POSITION_FIELDS = ("pnl_pct", "held_days", "avg_cost", "quantity", "sellable", "now_min", "high_since_buy",
                   "drawdown_since_buy_pct")
# 个股资料字段（行业、上市日、总股本来自 akshare 个股资料；拿不到 = 数据缺失）
INFO_FIELDS = ("total_mv", "listing_days", "is_st")
KNOWN_FIELDS: set[str] = set(POOL_FIELDS) | set(MINUTE_FIELDS) | set(DAILY_FIELDS) | set(POSITION_FIELDS) | set(INFO_FIELDS)
# 外部数据源推送/拉取的自定义字段：以 ext_ 开头的字段名一律允许；插件也可以用 register_fields() 登记
EXT_PREFIX = "ext_"
EXTRA_FIELDS: dict[str, str] = {}


def register_fields(fields: dict[str, str] | list[str]) -> None:
    """插件登记自定义字段，表达式里就能用。fields 可以是 {字段名: 说明} 或字段名列表。"""
    items = fields.items() if isinstance(fields, dict) else ((f, "") for f in fields)
    for k, v in items:
        if not str(k).isidentifier():
            raise ValueError(f"字段名 {k!r} 不合法")
        EXTRA_FIELDS[str(k)] = v
        KNOWN_FIELDS.add(str(k))


def field_known(name: str) -> bool:
    return name in KNOWN_FIELDS or name.startswith(EXT_PREFIX)
# 回看时把当天字段映射到日K字段：price[1] = 昨收
LAG_ALIAS = {"price": "close", "change_rate": "pct_chg", "open_price": "open", "minute_last": "close"}
MAX_LOOKBACK = 120

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.Mod: operator.mod}
_CMP = {ast.Gt: operator.gt, ast.GtE: operator.ge, ast.Lt: operator.lt, ast.LtE: operator.le, ast.Eq: operator.eq, ast.NotEq: operator.ne}
_FUNCS = {"abs": abs, "min": min, "max": max, "round": round}


class MissingField(Exception):
    pass


class ExprError(ValueError):
    pass


class Expr:
    def __init__(self, src: str):
        self.src = (src or "").strip()
        if not self.src:
            raise ExprError("表达式为空")
        try:
            self.tree = ast.parse(self.src.replace("&&", " and ").replace("||", " or "), mode="eval")
        except SyntaxError as e:
            raise ExprError(f"语法错误：{e.msg}（第 {e.offset} 个字符附近）") from e
        self.refs: set[tuple[str, int]] = set()
        self._check(self.tree.body)
        unknown = sorted({n for n, _ in self.refs if not field_known(n)})
        if unknown:
            raise ExprError(f"未知字段：{', '.join(unknown)}（可用字段见网页「字段说明」）")

    def _check(self, n: ast.AST) -> None:
        if isinstance(n, ast.BoolOp):
            for v in n.values:
                self._check(v)
        elif isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.Not, ast.USub, ast.UAdd)):
            self._check(n.operand)
        elif isinstance(n, ast.BinOp) and type(n.op) in _BIN:
            self._check(n.left)
            self._check(n.right)
        elif isinstance(n, ast.Compare) and all(type(o) in _CMP for o in n.ops):
            self._check(n.left)
            for c in n.comparators:
                self._check(c)
        elif isinstance(n, ast.Constant) and isinstance(n.value, (int, float, bool)):
            pass
        elif isinstance(n, ast.Name):
            self.refs.add((n.id, 0))
        elif isinstance(n, ast.Subscript):
            self.refs.add((self._lag_ref(n.value, n.slice)))
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            fn = n.func.id
            if n.keywords:
                raise ExprError("函数不支持关键字参数")
            if fn == "ref":
                if len(n.args) != 2:
                    raise ExprError("ref(字段, N) 需要两个参数")
                self.refs.add(self._lag_ref(n.args[0], n.args[1]))
            elif fn in _FUNCS:
                if not n.args:
                    raise ExprError(f"{fn}() 缺少参数")
                for a in n.args:
                    self._check(a)
            else:
                raise ExprError(f"不支持的函数 {fn}（可用 abs/min/max/round/ref）")
        else:
            raise ExprError(f"不支持的写法：{ast.unparse(n) if hasattr(ast, 'unparse') else type(n).__name__}")

    @staticmethod
    def _lag_ref(target, idx) -> tuple[str, int]:
        if not isinstance(target, ast.Name):
            raise ExprError("回看只能用在字段名上，如 close[1]")
        if not (isinstance(idx, ast.Constant) and isinstance(idx.value, int) and not isinstance(idx.value, bool)):
            raise ExprError("回看天数必须是整数常量，如 close[1]")
        if not 0 <= idx.value <= MAX_LOOKBACK:
            raise ExprError(f"回看天数应在 0~{MAX_LOOKBACK}")
        name = target.id
        if idx.value > 0:
            name = LAG_ALIAS.get(name, name)
            if name not in DAILY_FIELDS:
                raise ExprError(f"{target.id}[{idx.value}]：只有日K字段能回看（{', '.join(DAILY_FIELDS)}）")
        return name, idx.value

    def needs(self) -> set[str]:
        out = set()
        for n, d in self.refs:
            if n in MINUTE_FIELDS:
                out.add("minute")
            if n in DAILY_FIELDS or d > 0:
                out.add("daily")
            if n in ("total_mv", "listing_days"):
                out |= {"info", "daily"}
        return out

    def fields(self) -> list[str]:
        return sorted({n + (f"[{d}]" if d else "") for n, d in self.refs})

    def eval(self, resolve: Callable[[str, int], float | None]):
        """resolve(field, days_ago) 返回数值或 None；None 会抛 MissingField。"""
        return self._eval(self.tree.body, resolve)

    def _get(self, name, days, resolve):
        v = resolve(name, days)
        if v is None:
            raise MissingField(name + (f"[{days}]" if days else ""))
        return v

    def _eval(self, n, r):
        if isinstance(n, ast.BoolOp):
            if isinstance(n.op, ast.And):
                return all(bool(self._eval(v, r)) for v in n.values)
            return any(bool(self._eval(v, r)) for v in n.values)
        if isinstance(n, ast.UnaryOp):
            v = self._eval(n.operand, r)
            return (not v) if isinstance(n.op, ast.Not) else (-v if isinstance(n.op, ast.USub) else +v)
        if isinstance(n, ast.BinOp):
            a, b = self._eval(n.left, r), self._eval(n.right, r)
            if isinstance(n.op, (ast.Div, ast.Mod)) and b == 0:
                raise MissingField("除数为 0")
            return _BIN[type(n.op)](a, b)
        if isinstance(n, ast.Compare):
            left = self._eval(n.left, r)
            for op, comp in zip(n.ops, n.comparators):
                right = self._eval(comp, r)
                if not _CMP[type(op)](left, right):
                    return False
                left = right
            return True
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, ast.Name):
            return self._get(n.id, 0, r)
        if isinstance(n, ast.Subscript):
            name, d = self._lag_ref(n.value, n.slice)
            return self._get(name, d, r)
        if isinstance(n, ast.Call):
            if n.func.id == "ref":
                name, d = self._lag_ref(n.args[0], n.args[1])
                return self._get(name, d, r)
            args = [self._eval(a, r) for a in n.args]
            if n.func.id == "round":
                return round(args[0], int(args[1]) if len(args) > 1 else 0)
            return _FUNCS[n.func.id](*args) if n.func.id != "abs" else abs(args[0])
        raise ExprError("不支持的节点")


def candidate_resolver(c):
    """把候选股的字段做成表达式取值函数：当天字段取 fields，日K字段/回看取 daily。"""
    def resolve(name: str, days: int):
        if days == 0:
            v = c.get(name)
            if v is not None or name not in DAILY_FIELDS:
                return v
        return c.daily_at(name, days)
    return resolve
