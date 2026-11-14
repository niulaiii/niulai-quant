"""卖出因子：用在卖出因子链上，对每只持仓逐只检查，passed=True 表示「该卖」。

参数留空的止损/止盈/持有天数/到点卖出，按 [exits] 规则取值（含 [exits.by_pool] 按股池覆盖），
这样原来的固定退出规则可以原样并入卖出因子链。

持仓候选的字段（表达式里也能用）：
  pnl_pct 浮盈 %、held_days 已持有交易日、avg_cost 成本、quantity 持仓、sellable 可卖、
  now_min 当前时刻（分钟数，14:50 = 890）、high_since_buy 买入以来最高价、drawdown_since_buy_pct 较买入以来最高价回撤 %
  以及分时字段（vwap、vwap_dev_pct、drawdown_pct、day_high …）和日K字段（ma5、close[1] …）
"""
from __future__ import annotations

from ..util import parse_hm
from .base import Factor, Outcome, ParamSpec, register_factor
from .builtin import fmt


def _rule(ctx) -> dict:
    return (ctx.extra or {}).get("rule") or {}


def _hm(s) -> str:
    """把 9:30 / 09:30 / 14:5 统一成两位数的 HH:MM，字符串比较才不会出错。"""
    h, m = parse_hm(str(s))
    return f"{h:02d}:{m:02d}"


def _now_hm(ctx) -> str:
    return (ctx.extra or {}).get("now_hm") or "00:00"


class SellFactor(Factor):
    sides = ("sell",)
    category = "卖出"


@register_factor
class StopLoss(SellFactor):
    kind = "stop_loss"
    title = "止损"
    description = "浮亏达到阈值就卖。止损层永远最先检查，点「保留」也挡不住"
    params = [ParamSpec("pct", None, "浮亏 %，留空 = 按 [exits].stop_loss_pct（可按股池覆盖）")]

    def _pct(self, ctx):
        v = self.num("pct")
        return v if v is not None else (float(_rule(ctx)["stop_loss_pct"]) if _rule(ctx).get("stop_loss_pct") else None)

    def label(self):
        v = self.num("pct")
        return f"止损 -{fmt(v)}%" if v is not None else "止损（按退出规则）"

    def evaluate(self, c, ctx):
        pnl, pct = c.get("pnl_pct"), self._pct(ctx)
        if pnl is None:
            return Outcome.fail(None, "没有最新价")
        if not pct:
            return Outcome.fail(pnl, "未设置止损")
        if pnl <= -pct:
            return Outcome.ok(pnl, f"浮亏 {pnl:.2f}% 触发止损（-{pct:g}%）")
        return Outcome.fail(pnl, f"浮盈 {pnl:+.2f}%，止损线 -{pct:g}%")


@register_factor
class TakeProfit(SellFactor):
    kind = "take_profit"
    title = "止盈"
    description = "浮盈达到阈值就卖"
    params = [ParamSpec("pct", None, "浮盈 %，留空 = 按 [exits].take_profit_pct（可按股池覆盖）")]

    def label(self):
        v = self.num("pct")
        return f"止盈 +{fmt(v)}%" if v is not None else "止盈（按退出规则）"

    def evaluate(self, c, ctx):
        pnl = c.get("pnl_pct")
        v = self.num("pct")
        pct = v if v is not None else (float(_rule(ctx)["take_profit_pct"]) if _rule(ctx).get("take_profit_pct") else None)
        if pnl is None:
            return Outcome.fail(None, "没有最新价")
        if not pct:
            return Outcome.fail(pnl, "未设置止盈")
        if pnl >= pct:
            return Outcome.ok(pnl, f"浮盈 {pnl:.2f}% 触发止盈（+{pct:g}%）")
        return Outcome.fail(pnl, f"浮盈 {pnl:+.2f}%，止盈线 +{pct:g}%")


@register_factor
class HoldDays(SellFactor):
    kind = "hold_days"
    title = "持有天数"
    description = "已持有满 N 个交易日（买入当天算 0）。通常和「到点卖出」放在同一层、选「全部满足」，就是到期退出"
    params = [ParamSpec("min_days", None, "交易日，留空 = 按 [exits].max_hold_days（可按股池覆盖；0 = 不按时间退出）")]

    def days(self, ctx) -> int:
        v = self.num("min_days")
        return int(v) if v is not None else int(_rule(ctx).get("max_hold_days") or 0)

    def label(self):
        v = self.num("min_days")
        return f"持有满 {int(v)} 个交易日" if v is not None else "持有满 N 天（按退出规则）"

    def evaluate(self, c, ctx):
        held, n = int(c.get("held_days") or 0), self.days(ctx)
        if n <= 0:
            return Outcome.fail(float(held), f"持有 {held} 天（不按时间退出）")
        return Outcome(held >= n, float(held), f"已持有 {held}/{n} 个交易日")


@register_factor
class TimeReached(SellFactor):
    kind = "time_reached"
    title = "到点卖出"
    description = "当前时刻 ≥ 设定时刻。定时任务会在这个时刻自动做一次卖出检查，并在之前按 remind_minutes_before 推送提醒"
    params = [ParamSpec("at", "", "HH:MM，留空 = 按 [exits].exit_time（可按股池覆盖）", "time")]

    def validate(self):
        if self.p.get("at"):
            parse_hm(str(self.p["at"]))

    def at(self, ctx=None) -> str:
        return _hm(self.p.get("at") or (_rule(ctx).get("exit_time") if ctx else "") or "14:50")

    def label(self):
        return f"到 {self.p['at']} 卖出" if self.p.get("at") else "到退出时间（按退出规则）"

    def evaluate(self, c, ctx):
        at, now = self.at(ctx), _now_hm(ctx)
        return Outcome(now >= at, None, f"现在 {now}，{'已到' if now >= at else '未到'} {at}")


@register_factor
class BelowVwap(SellFactor):
    kind = "below_vwap"
    title = "跌破均价线"
    description = "现价低于分时均价（VWAP）超过阈值就卖（需分时）"
    params = [ParamSpec("min_below_pct", 0.0, "低于均价多少 % 才算跌破"), ParamSpec("after", "", "HH:MM 之后才检查，可空（避开开盘波动）", "time")]

    def validate(self):
        if self.p.get("after"):
            parse_hm(str(self.p["after"]))

    def needs(self):
        return {"minute"}

    def label(self):
        m = self.num("min_below_pct") or 0
        s = "跌破分时均价线" if m == 0 else f"低于均价线 {fmt(m)}%"
        return s + (f"（{self.p['after']} 后）" if self.p.get("after") else "")

    def evaluate(self, c, ctx):
        if self.p.get("after") and _now_hm(ctx) < _hm(self.p["after"]):
            return Outcome.fail(None, f"{self.p['after']} 之前不检查")
        d = c.get("vwap_dev_pct")
        if d is None:
            return Outcome(False, None, c.minute_error or "无分时数据", missing=True)
        lim = -(self.num("min_below_pct") or 0)
        return Outcome(d < lim, d, f"现价较均价 {d:+.2f}%")


@register_factor
class DrawdownFromHigh(SellFactor):
    kind = "drawdown_from_high"
    title = "从高点回撤"
    description = "现价较高点回撤 ≥ X% 就卖。高点可选当天分时最高（intraday）或买入以来最高（since_buy）"
    params = [ParamSpec("pct", 3.0, "回撤 %"), ParamSpec("basis", "intraday", "从哪里算最高点", "choice", choices=[("intraday", "当天最高"), ("since_buy", "买入以来最高")]),
              ParamSpec("min_profit_pct", None, "只在曾经浮盈 ≥ 这个值时才生效，可空（回撤止盈）")]

    def validate(self):
        if self.p.get("basis") not in ("intraday", "since_buy"):
            raise ValueError("basis 只能是 intraday 或 since_buy")
        if not self.num("pct"):
            raise ValueError("pct 必须大于 0")

    def needs(self):
        return {"minute", "daily"} if self.p.get("basis") == "since_buy" else {"minute"}

    def label(self):
        return f"{'买入以来' if self.p.get('basis') == 'since_buy' else '日内'}高点回撤 ≥ {fmt(self.num('pct'))}%"

    def evaluate(self, c, ctx):
        if self.p.get("basis") == "since_buy":
            d, high = c.get("drawdown_since_buy_pct"), c.get("high_since_buy")
        else:
            d, high = c.get("drawdown_pct"), c.get("day_high")
        if d is None or not high:
            return Outcome.fail(None, c.minute_error or "无高点数据")
        mp = self.num("min_profit_pct")
        if mp is not None and c.get("avg_cost"):
            peak = (high / c.get("avg_cost") - 1) * 100
            if peak < mp:
                return Outcome.fail(d, f"最高浮盈 {peak:.2f}% 未到 {fmt(mp)}%，不启用回撤卖出")
        pct = self.num("pct")
        return Outcome(d >= pct, d, f"较高点 {high:.2f} 回撤 {d:.2f}%（阈值 {fmt(pct)}%）")


@register_factor
class BelowMa(SellFactor):
    kind = "below_ma"
    title = "跌破均线"
    description = "现价跌破 N 日均线就卖（需日K）"
    params = [ParamSpec("ma", 5, "均线：5 / 10 / 20 / 60"), ParamSpec("buffer_pct", 0.0, "低于均线多少 % 才算跌破")]

    def validate(self):
        if int(self.num("ma") or 0) not in (5, 10, 20, 60):
            raise ValueError("ma 只能是 5 / 10 / 20 / 60")

    def needs(self):
        return {"daily", "minute"}

    def label(self):
        return f"跌破 {int(self.num('ma'))} 日线"

    def evaluate(self, c, ctx):
        n = int(self.num("ma"))
        ma, px = c.get(f"ma{n}"), c.get("price")
        if not ma or not px:
            return Outcome.fail(None, c.daily_error or f"缺少 ma{n} 或现价")
        dev = (px / ma - 1) * 100
        return Outcome(dev < -(self.num("buffer_pct") or 0), dev, f"现价 {px:.2f}，ma{n} {ma:.2f}（{dev:+.2f}%）")
