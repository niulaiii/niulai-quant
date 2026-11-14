"""示例插件（目录写法）——只是插件写法演示，不是推荐策略。内置因子里已有「均线多头排列」（kind = ma_bull），
这个示例的 kind 是 ma_trend，不会和内置冲突。更简单的单文件写法见 plugins/example_single_file.py。

启用：在买入漏斗某一层里加
    [[buy_funnel.layers.factors]]
    kind = "ma_trend"
    params = { require_close_above_ma5 = true }
或在网页「漏斗」页某一层点「+ 因子」选它。
"""
from qfpy.factors import Factor, Outcome, ParamSpec, register_factor


@register_factor
class MaTrend(Factor):
    kind = "ma_trend"
    title = "示例插件：均线趋势（写法演示）"
    description = "MA5 > MA10 > MA20（日K），可选要求当天收盘/现价在 MA5 之上"
    params = [ParamSpec("require_close_above_ma5", True, "要求收盘在 MA5 之上", "bool")]

    def needs(self):
        return {"daily"}          # 声明需要日K，漏斗会自动为到达这一层的票拉日K

    def evaluate(self, c, ctx):
        if not c.daily:
            return Outcome.fail(None, c.daily_error or "无日K")
        ma5, ma10, ma20, close = (c.daily_at(k, 0) for k in ("ma5", "ma10", "ma20", "close"))
        if None in (ma5, ma10, ma20, close):
            return Outcome.fail(None, "日K不足 20 根")
        ok = ma5 > ma10 > ma20 and (close > ma5 or not self.p["require_close_above_ma5"])
        return Outcome(ok, close / ma20 - 1, f"MA5 {ma5:.2f} / MA10 {ma10:.2f} / MA20 {ma20:.2f}，收 {close:.2f}")
