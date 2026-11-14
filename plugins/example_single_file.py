"""最小插件示例（单文件写法）——只是插件写法演示，不是推荐策略。放进 plugins/ 就自动注册。
三个 kind（small_gap、profit_giveback、static_list）都和内置的不重名；插件用了内置已有的 kind 会被拒绝注册。

这个文件注册了三样东西：
  1. 买入因子 small_gap      高开幅度在区间内（只用股池字段，不拉额外数据）
  2. 卖出因子 profit_giveback 从当天高点把浮盈吐回一半就卖（用分时）
  3. 数据源   static_list    在配置里直接写一组代码当股池

用法：
  买入漏斗某层里加： [[buy_funnel.layers.factors]]  kind = "small_gap"   params = { min = 0.5, max = 3 }
  卖出链某层里加：   [[sell_funnel.layers.factors]] kind = "profit_giveback" params = { ratio = 0.5, min_peak_pct = 3 }
  数据源：           [sources.mylist] type = "static_list"  codes = ["600000", "000001"]   股池 key 写 "mylist:all"
改完文件后在网页「日志与系统 → 系统状态 → 重新加载插件」或 POST /api/v1/plugins/reload 即可生效，不用重启。
"""
from qfpy.datasources import DataSource, PoolResult, register_source
from qfpy.factors import Factor, Outcome, ParamSpec, register_factor

PLUGIN = {"name": "example_single_file", "description": "单文件插件示例：一个买入因子、一个卖出因子、一个数据源", "enabled": True}


@register_factor
class SmallGap(Factor):
    kind = "small_gap"
    title = "示例插件：小幅高开（写法演示）"
    description = "高开幅度在 [min, max] % 之间"
    sides = ("buy",)
    params = [ParamSpec("min", 0.5, "最小高开 %"), ParamSpec("max", 3.0, "最大高开 %")]

    def label(self):
        return f"高开 {self.num('min'):g}%~{self.num('max'):g}%"

    def evaluate(self, c, ctx):
        v = c.get("open_rise")
        if v is None:
            return Outcome.fail(None, "没有高开幅度")
        return Outcome(self.num("min") <= v <= self.num("max"), v, f"高开 {v:.2f}%")


@register_factor
class ProfitGiveback(Factor):
    kind = "profit_giveback"
    title = "示例插件：吐回浮盈（写法演示）"
    description = "当天最高浮盈 ≥ min_peak_pct 时，现在吐回了 ratio 比例就卖"
    sides = ("sell",)
    category = "卖出"
    params = [ParamSpec("ratio", 0.5, "吐回比例 0~1"), ParamSpec("min_peak_pct", 3.0, "最高浮盈至少多少 % 才启用")]

    def needs(self):
        return {"minute"}            # 声明需要分时，引擎会先把 day_high 等字段补上

    def evaluate(self, c, ctx):
        cost, hi, px = c.get("avg_cost"), c.get("day_high"), c.get("price")
        if not (cost and hi and px):
            return Outcome.fail(None, "缺少成本/高点/现价")
        peak = (hi / cost - 1) * 100
        now = (px / cost - 1) * 100
        if peak < self.num("min_peak_pct"):
            return Outcome.fail(now, f"最高浮盈 {peak:.2f}% 未到 {self.num('min_peak_pct'):g}%")
        gave = (peak - now) / peak
        return Outcome(gave >= self.num("ratio"), now, f"最高 {peak:.2f}% → 现在 {now:.2f}%，吐回 {gave:.0%}")


@register_source
class StaticList(DataSource):
    type_name = "static_list"
    title = "示例插件：固定代码列表（写法演示）"
    description = "在配置里写 codes = [...]，当作股池 <名字>:all"
    capabilities = ("pool",)
    config_fields = [{"name": "codes", "hint": "代码列表"}]

    def list_pools(self):
        return [{"key": "all", "name": "固定列表", "desc": f"{len(self.cfg.get('codes') or [])} 只"}]

    def get_pool(self, key, date):
        return PoolResult(key=f"{self.name}:all", name="固定列表", trading_date=date or "",
                          stocks=[{"code": str(c), "name": "", "fields": {}} for c in self.cfg.get("codes") or []])
