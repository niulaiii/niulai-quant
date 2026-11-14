"""因子基类与注册表（参考 tick-stock-panel app/factors/registry.py 的「注册表 + 元数据」做法）。"""
from __future__ import annotations

import re

from dataclasses import dataclass, field
from typing import Any

from ..model import Candidate

FACTOR_TYPES: dict[str, type["Factor"]] = {}


@dataclass
class ParamSpec:
    name: str
    default: Any
    hint: str
    type: str = "number"     # number / string / list / multi（多选，choices）/ field（点选字段）/ industries（点选行业）/ expr / file / bool / choice / time / pools / signal / ext_*
    choices: Any = None      # choice 类型的选项：[(值, 显示名), ...]
    unit: str = ""           # 单位（显示在数字框后面）


@dataclass
class Outcome:
    passed: bool
    value: float | None = None
    note: str = ""
    missing: bool = False        # 数据缺失：拿不到数据，既不当成 0 也不当成通过
    na: bool = False             # 不适用：例如新股上市初期无涨跌幅限制时的连板/炸板类因子

    @staticmethod
    def missing_data(note=""):
        return Outcome(False, None, "数据缺失" + (f"：{note}" if note else ""), missing=True)

    @staticmethod
    def not_applicable(note=""):
        return Outcome(False, None, "不适用" + (f"：{note}" if note else ""), na=True)

    @staticmethod
    def ok(value=None, note=""):
        return Outcome(True, value, note)

    @staticmethod
    def fail(value=None, note=""):
        return Outcome(False, value, note)


# ---------------- 数据缺失的统一口径 ----------------
# 拿不到数据时因子返回 Outcome.missing_data()；买入漏斗里怎么处理只看一个参数 missing：
#   "drop"（默认）按数据缺失淘汰 / "pass" 放行。
# 旧参数名照样认：allow_missing = true（字段区间）、on_error = "pass"（外部 HTTP 因子）、missing = "fail"。
# 卖出链里数据缺失一律「不触发」，并且引擎会记日志、推钉钉提醒（见 engine._alert_exit_problems），不受这个参数影响。
MISSING_CHOICES = [("drop", "按数据缺失淘汰（默认）"), ("pass", "放行")]
MISSING_HINT = "拿不到数据时：drop 按数据缺失淘汰（默认）/ pass 放行。只对买入漏斗有效"


def missing_policy(params: dict | None) -> str:
    p = params or {}
    v = p.get("missing")
    if v == "pass":
        return "pass"
    if v in ("drop", "fail"):
        return "drop"
    if p.get("allow_missing") is True or p.get("on_error") == "pass":
        return "pass"
    return "drop"


def apply_missing(o: "Outcome", params: dict | None, side: str = "buy") -> "Outcome":
    """把统一的 missing 口径套到一个因子结果上（漏斗 eval_layer 用的也是这条规则）。"""
    if getattr(o, "missing", False) and side != "sell" and missing_policy(params) == "pass":
        return Outcome(True, o.value, f"{o.note}（按设置放行）", missing=True)
    return o


def missing_param_spec() -> dict:
    return {"name": "missing", "default": "drop", "hint": MISSING_HINT, "type": "choice", "unit": "",
            "choices": [list(x) for x in MISSING_CHOICES], "common": True}


@dataclass
class FactorContext:
    trading_date: str
    root: Any = None
    extra: dict = field(default_factory=dict)


class Factor:
    """子类需要设置 kind/title/description/params，并实现 evaluate()。

    needs 声明数据依赖："minute" 分时、"daily" 日K。漏斗只会为到达这一层的票拉数据，省接口配额。
    sides 声明能用在哪条链上："buy" 买入漏斗、"sell" 卖出因子链（对持仓逐只检查，passed=True 表示「该卖」）。
    prepare(cands, ctx) 可选：一层开始前拿到这一层的全部候选，适合批量请求外部接口。
    """

    kind = ""
    title = ""
    description = ""
    params: list[ParamSpec] = []
    plugin: str | None = None
    sides: tuple[str, ...] = ("buy",)
    category = "通用"            # 旧字段（插件可能还在用），新代码用 cat
    cat = ""                     # 分类 id，见 qfpy.catalog.CATEGORIES；留空按 kind 推断
    update = "realtime"          # realtime = 盘中实时；eod = 盘后更新；static = 基本不变（行业、上市日）
    backtest_ok = True           # 日K回测能不能重现
    backtest_note = ""           # 回测不可用的原因
    experimental = False         # 实验性
    example = ""                 # 一句话例子，显示在因子库里

    def __init__(self, p: dict, ctx_root=None):
        self.p = {s.name: s.default for s in self.params}
        self.p.update(p or {})
        # 旧配置兼容：多选 / 行业点选类参数以前可能手写成 "star,bse" 这样的字符串，统一转成列表
        for s in self.params:
            v = self.p.get(s.name)
            if s.type in ("multi", "industries", "list", "pools") and isinstance(v, str):
                self.p[s.name] = [x for x in re.split(r"[,，、;；\s]+", v) if x]
        self.root = ctx_root
        self.validate()

    def validate(self) -> None:
        pass

    def needs(self) -> set[str]:
        return set()

    def label(self) -> str:
        return self.title

    def describe(self) -> str:
        return self.description

    def prepare(self, cands: list[Candidate], ctx: FactorContext) -> None:
        """可选：批量预取（如外部 HTTP 因子一次请求整层候选）。"""
        return None

    def evaluate(self, c: Candidate, ctx: FactorContext) -> Outcome:  # pragma: no cover
        raise NotImplementedError

    # 工具
    def num(self, name: str, default=None):
        v = self.p.get(name, default)
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError) as e:
            raise ValueError(f"参数 {name} 应为数字，当前是 {v!r}") from e


def register_factor(cls):
    if not cls.kind:
        raise ValueError("因子类必须设置 kind")
    FACTOR_TYPES[cls.kind] = cls
    return cls


def build_factor(spec: dict, root=None) -> Factor:
    kind = spec.get("kind")
    cls = FACTOR_TYPES.get(kind)
    if cls is None:
        raise ValueError(f"未知因子 kind={kind}（可用：{', '.join(FACTOR_TYPES)}）")
    f = cls(spec.get("params") or {}, root)
    f.custom_label = spec.get("label")  # type: ignore[attr-defined]
    return f


def build_chain(specs: list[dict], root=None) -> list[tuple[dict, Factor | None, str | None]]:
    """构建整条因子链。返回 [(spec, factor or None, error)]；网页保存时任何 error 都会拒绝保存。"""
    out = []
    for i, spec in enumerate(specs):
        try:
            out.append((spec, build_factor(spec, root), None))
        except Exception as e:  # noqa: BLE001
            out.append((spec, None, f"第 {i + 1} 个因子（{spec.get('kind')}）：{e}"))
    return out


def factor_label(spec: dict, f: Factor | None) -> str:
    return spec.get("label") or (f.label() if f else spec.get("kind", "?"))


def _param_list(c) -> list[dict]:
    out = [{"name": s.name, "default": s.default, "hint": s.hint, "type": s.type, "unit": s.unit,
            "choices": [list(x) if isinstance(x, (tuple, list)) else [x, str(x)] for x in s.choices] if s.choices else None}
           for s in c.params if s.name not in ("allow_missing", "on_error")]
    if "buy" in c.sides and not any(x["name"] == "missing" for x in out):
        out.append(missing_param_spec())       # 每个买入因子都有同一个「数据缺失时」参数
    return out


def factor_catalog(side: str | None = None) -> list[dict]:
    from ..catalog import factor_meta
    return [{"kind": k, "title": c.title, "description": c.description, "plugin": c.plugin, "sides": list(c.sides),
             "category": c.category, **factor_meta(k, c), "params": _param_list(c)}
            for k, c in FACTOR_TYPES.items() if side is None or side in c.sides]
