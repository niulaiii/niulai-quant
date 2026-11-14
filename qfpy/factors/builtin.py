"""内置因子（与 Rust 版 9 种一一对应，表达式因子见 expr_factor.py）。"""
from __future__ import annotations

from ..market import FIELD_DOCS, MINUTE_FIELDS, DAILY_FIELDS
from ..limits import BOARD_LABELS, board_of
from ..limits import is_risk_name as is_st   # 排雷口径：ST、*ST、名字带「退」
from .base import Factor, Outcome, ParamSpec, register_factor


def fmt(v: float) -> str:
    return f"{v:.1f}" if abs(v) >= 100 else f"{v:.2f}"


def _minute_missing(c) -> Outcome:
    return Outcome(False, None, c.minute_error or "无分时数据", missing=True)


@register_factor
class ExcludeBoard(Factor):
    kind = "exclude_board"
    title = "板块排除"
    description = "按代码前缀排除板块（main 主板 / gem 创业板 / star 科创板 / bse 北交所）"
    params = [ParamSpec("boards", ["star", "bse"], "要排除的板块（可多选）", "multi", choices=[("main", "主板"), ("gem", "创业板"), ("star", "科创板"), ("bse", "北交所")])]

    def validate(self):
        for b in self.p["boards"]:
            if b not in BOARD_LABELS:
                raise ValueError(f"未知板块 {b}，可选 main/gem/star/bse")

    def label(self):
        return "排除" + "/".join(BOARD_LABELS[b] for b in self.p["boards"])

    def evaluate(self, c, ctx):
        b = board_of(c.code)
        if b in self.p["boards"]:
            return Outcome.fail(None, f"{BOARD_LABELS[b]}股票，已排除")
        return Outcome.ok(None, BOARD_LABELS[b])


@register_factor
class ExcludeSt(Factor):
    kind = "exclude_st"
    title = "排除 ST"
    description = "名称含 ST 或“退”的股票直接排除"

    def label(self):
        return "排除 ST/退市"

    def evaluate(self, c, ctx):
        if is_st(c.name):
            return Outcome.fail(None, "风险警示/退市股")
        return Outcome.ok(None, "非 ST")


@register_factor
class Range(Factor):
    kind = "range"
    sides = ("buy", "sell")
    title = "字段区间"
    description = "任意数值字段的区间过滤（含边界）"
    params = [ParamSpec("field", "market_value", "字段（点选）", "field"), ParamSpec("min", None, "下限，可空"),
              ParamSpec("max", None, "上限，可空"), ParamSpec("unit", "", "展示单位", "string"),
              ParamSpec("missing", "drop", "缺少该字段时：drop 按数据缺失淘汰（默认）/ pass 放行（如自选股没有市值）", "choice",
                        [("drop", "按数据缺失淘汰（默认）"), ("pass", "放行")])]

    def validate(self):
        if not self.p.get("field"):
            raise ValueError("缺少 field")
        if self.num("min") is None and self.num("max") is None:
            raise ValueError("min 和 max 至少填一个")

    def needs(self):
        f = self.p["field"]
        return {"minute"} if f in MINUTE_FIELDS else ({"daily"} if f in DAILY_FIELDS else set())

    def label(self):
        f = self.p["field"]
        name = next((d.split("（")[0].split("，")[0] for k, d in FIELD_DOCS if k == f), f)
        a, b, u = self.num("min"), self.num("max"), self.p.get("unit", "")
        if a is not None and b is not None:
            return f"{name} {fmt(a)}~{fmt(b)}{u}"
        if a is not None:
            return f"{name} ≥ {fmt(a)}{u}"
        return f"{name} ≤ {fmt(b)}{u}"

    def evaluate(self, c, ctx):
        f, u = self.p["field"], self.p.get("unit", "")
        v = c.get(f)
        if v is None:      # 放不放行由漏斗按 missing 参数统一处理（旧的 allow_missing = true 也认）
            return Outcome.missing_data(f"缺少字段 {f}")
        a, b = self.num("min"), self.num("max")
        if a is not None and v < a:
            return Outcome.fail(v, f"{f} = {fmt(v)}{u}，低于 {fmt(a)}")
        if b is not None and v > b:
            return Outcome.fail(v, f"{f} = {fmt(v)}{u}，高于 {fmt(b)}")
        return Outcome.ok(v, f"{f} = {fmt(v)}{u}")


@register_factor
class NotLimitUp(Factor):
    kind = "not_limit_up"
    title = "未涨停"
    description = "按板块涨跌幅算涨停价；已封板或太接近涨停的票挂单大概率买不进，排除。注册制新股上市前 5 日无涨跌幅限制，直接放行"
    params = [ParamSpec("buffer_pct", 0.5, "距涨停小于等于多少个点算接近")]

    def label(self):
        return f"未涨停（距涨停 > {fmt(self.num('buffer_pct'))}%）"

    def evaluate(self, c, ctx):
        if c.get("no_limit"):
            return Outcome.ok(None, "上市初期无涨跌幅限制")
        gap = c.get("limit_gap_pct")
        if gap is None:
            return Outcome(False, None, "数据缺失：无法计算涨停价", missing=True)
        buf = self.num("buffer_pct")
        if gap <= buf:
            return Outcome.fail(gap, f"涨幅 {fmt(c.get('change_rate') or 0)}% 距涨停仅 {fmt(gap)} 个点，大概率买不进")
        return Outcome.ok(gap, f"距涨停 {fmt(gap)} 个点")


@register_factor
class MinHits(Factor):
    kind = "min_hits"
    title = "多策略共振"
    description = "至少被 N 个股池同时选中"
    params = [ParamSpec("min", 2, "最少命中数")]

    def label(self):
        return f"至少 {int(self.num('min'))} 个股池同时选中"

    def evaluate(self, c, ctx):
        n = len(c.sources)
        s = f"命中 {n} 个：{'+'.join(c.sources)}"
        return Outcome(n >= self.num("min"), float(n), s)


@register_factor
class AboveVwap(Factor):
    kind = "above_vwap"
    title = "站上均价线"
    description = "用分时最后一个点：现价相对分时均价（VWAP）的偏离必须 ≥ 阈值（需分时）"
    params = [ParamSpec("min_dev_pct", 0.0, "最少高出均价多少 %")]

    def needs(self):
        return {"minute"}

    def label(self):
        m = self.num("min_dev_pct")
        return "站上分时均价线" if m == 0 else f"高于均价线 {fmt(m)}%"

    def evaluate(self, c, ctx):
        d = c.get("vwap_dev_pct")
        if d is None:
            return _minute_missing(c)
        s = f"现价较均价 {d:+.2f}%"
        return Outcome.ok(d, s) if d >= self.num("min_dev_pct") else Outcome.fail(d, s + "，在均价线下方")


@register_factor
class MaxDrawdown(Factor):
    kind = "max_drawdown"
    title = "冲高回落限制"
    description = "现价相对日内最高价的回撤不能超过阈值，过滤冲高跳水的票（需分时）"
    params = [ParamSpec("max_pct", 3.0, "最大回撤 %")]

    def needs(self):
        return {"minute"}

    def label(self):
        return f"冲高回落 ≤ {fmt(self.num('max_pct'))}%"

    def evaluate(self, c, ctx):
        d = c.get("drawdown_pct")
        if d is None:
            return _minute_missing(c)
        s = f"较日内高点回落 {d:.2f}%"
        return Outcome(d <= self.num("max_pct"), d, s)


@register_factor
class MinAmount(Factor):
    kind = "min_amount"
    title = "流动性"
    description = "分时累计成交额不低于阈值（需分时）"
    params = [ParamSpec("min_yi", 1.0, "亿元")]

    def needs(self):
        return {"minute"}

    def label(self):
        return f"成交额 ≥ {fmt(self.num('min_yi'))} 亿"

    def evaluate(self, c, ctx):
        a = c.get("amount_yi")
        if a is None:
            return _minute_missing(c)
        return Outcome(a >= self.num("min_yi"), a, f"成交额 {a:.2f} 亿")
