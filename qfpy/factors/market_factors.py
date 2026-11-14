"""第一批新增因子：换手、N 日涨幅/回撤、均线多头、MACD/KDJ/RSI/布林带、连板/炸板、排雷、股池来源。

统一口径：拿不到数据的一律返回 Outcome.missing_data()（不当成 0，也不当成通过）；
注册制新股上市初期无涨跌幅限制的日子，连板/炸板类返回 Outcome.not_applicable()。
参数都能只填一边（上限或下限）。
"""
from __future__ import annotations

from .. import indicators as ind
from .base import Factor, Outcome, ParamSpec, register_factor
from .builtin import fmt


def _num_or_none(v):
    if v is None or v == "":
        return None
    return float(v)


def _range_check(v: float, lo, hi, name: str, unit: str = "") -> Outcome:
    if lo is not None and v < lo:
        return Outcome.fail(v, f"{name} {fmt(v)}{unit}，低于 {fmt(lo)}{unit}")
    if hi is not None and v > hi:
        return Outcome.fail(v, f"{name} {fmt(v)}{unit}，高于 {fmt(hi)}{unit}")
    return Outcome.ok(v, f"{name} {fmt(v)}{unit}")


def _range_label(name: str, lo, hi, unit: str = "") -> str:
    if lo is not None and hi is not None:
        return f"{name} {fmt(lo)}~{fmt(hi)}{unit}"
    if lo is not None:
        return f"{name} ≥ {fmt(lo)}{unit}"
    if hi is not None:
        return f"{name} ≤ {fmt(hi)}{unit}"
    return name


def _need_one_side(f: Factor, a="min", b="max"):
    if f.num(a) is None and f.num(b) is None:
        raise ValueError("下限和上限至少填一个")


def _daily_missing(c) -> Outcome:
    return Outcome.missing_data(c.daily_error or "没有日K")


class _DailyFactor(Factor):
    def needs(self):
        return {"daily"}

    def dval(self, c, name: str, days_ago: int = 0):
        if not c.daily:
            return None
        return c.daily_at(name, days_ago)


# ======================= 量能换手 =======================
TURNOVER_WHICH = [("today", "今日实时换手"), ("avg5", "5日平均换手"), ("avg10", "10日平均换手")]
_TURNOVER_FIELD = {"today": "turnover", "avg5": "turnover_5d_avg", "avg10": "turnover_10d_avg"}


def turnover_value(c, which: str = "today"):
    """换手率：今日优先用数据源字段，没有就用日K（成交量/流通股本）算。"""
    if which == "today" and c.get("turnover") is not None:
        return float(c.get("turnover"))
    if not c.daily:
        return None
    return c.daily_at(_TURNOVER_FIELD[which], 0)


@register_factor
class TurnoverRange(_DailyFactor):
    kind = "turnover_range"
    title = "换手率区间"
    cat = "volume"
    description = ("换手率在上下限之间（只填一边也行）。可选今日实时换手、前 5 日或前 10 日平均换手。"
                   "换手率优先用数据源给的，没有就用 成交量/流通股本 算，都拿不到算「数据缺失」，不会当成 0。")
    example = "今日换手 3%~15%：太低没人气，太高容易是出货"
    params = [ParamSpec("which", "today", "用哪个换手率", "choice", TURNOVER_WHICH),
              ParamSpec("min", None, "下限，可空", unit="%"), ParamSpec("max", None, "上限，可空", unit="%")]

    def validate(self):
        _need_one_side(self)
        if self.p.get("which") not in _TURNOVER_FIELD:
            raise ValueError("which 只能是 today / avg5 / avg10")

    def label(self):
        name = dict(TURNOVER_WHICH)[self.p["which"]]
        return _range_label(name, self.num("min"), self.num("max"), "%")

    def evaluate(self, c, ctx):
        v = turnover_value(c, self.p["which"])
        if v is None:
            return Outcome.missing_data("拿不到换手率（数据源没给，也算不出流通股本）")
        return _range_check(v, self.num("min"), self.num("max"), dict(TURNOVER_WHICH)[self.p["which"]], "%")


@register_factor
class TurnoverSurge(_DailyFactor):
    kind = "turnover_surge"
    title = "换手放大"
    cat = "volume"
    description = "今天的换手率是前 5 个交易日平均换手的几倍。盘中用实时换手，所以早盘倍数天然偏小。"
    example = "倍数 ≥ 2：今天换手至少是平时的两倍"
    params = [ParamSpec("min_ratio", 2.0, "至少几倍", unit="倍"), ParamSpec("max_ratio", None, "最多几倍，可空", unit="倍")]

    def validate(self):
        _need_one_side(self, "min_ratio", "max_ratio")

    def label(self):
        return _range_label("换手放大", self.num("min_ratio"), self.num("max_ratio"), "倍")

    def evaluate(self, c, ctx):
        today, avg = turnover_value(c, "today"), turnover_value(c, "avg5")
        if today is None or avg is None:
            return Outcome.missing_data("拿不到今日换手或 5 日平均换手")
        if avg <= 0:
            return Outcome.missing_data("5 日平均换手为 0（可能停牌）")
        return _range_check(today / avg, self.num("min_ratio"), self.num("max_ratio"), "换手倍数", "倍")


# ======================= 价格涨跌 =======================
@register_factor
class NDayChange(_DailyFactor):
    kind = "n_day_change"
    title = "N日涨幅"
    cat = "price"
    description = "现价（盘后用收盘价）相对 N 个交易日前收盘价的涨幅。可以用来排除短期涨太多的，或者只要强势的。"
    example = "5 日涨幅 ≤ 30%：不追一周内已经涨了三成的"
    params = [ParamSpec("days", 5, "N（交易日）", unit="天"), ParamSpec("min", None, "涨幅下限，可空", unit="%"),
              ParamSpec("max", None, "涨幅上限，可空", unit="%")]

    def validate(self):
        _need_one_side(self)
        if not 1 <= int(self.num("days") or 0) <= 120:
            raise ValueError("N 应在 1~120")

    def label(self):
        return _range_label(f"{int(self.num('days'))}日涨幅", self.num("min"), self.num("max"), "%")

    def evaluate(self, c, ctx):
        if not c.daily:
            return _daily_missing(c)
        n = int(self.num("days"))
        v = ind.pct_change(c.daily, len(c.daily) - 1, n)
        if v is None:
            return Outcome.missing_data(f"日K不足 {n + 1} 根")
        return _range_check(v, self.num("min"), self.num("max"), f"{n}日涨幅", "%")


@register_factor
class DrawdownFromHigh(_DailyFactor):
    kind = "drawdown_from_n_high"
    title = "距N日高点回撤"
    cat = "price"
    description = "现价比近 N 个交易日（含今天）最高价低了百分之几。0 表示就在高点。"
    example = "20 日回撤 ≤ 5%：只要离近期高点不远的强势股；或 ≥ 20%：找跌深的"
    params = [ParamSpec("days", 20, "N（交易日）", unit="天"), ParamSpec("min", None, "回撤下限，可空", unit="%"),
              ParamSpec("max", None, "回撤上限，可空", unit="%")]

    def validate(self):
        _need_one_side(self)
        if not 2 <= int(self.num("days") or 0) <= 250:
            raise ValueError("N 应在 2~250")

    def label(self):
        return _range_label(f"距{int(self.num('days'))}日高点回撤", self.num("min"), self.num("max"), "%")

    def evaluate(self, c, ctx):
        if not c.daily:
            return _daily_missing(c)
        n = int(self.num("days"))
        v = ind.drawdown(c.daily, len(c.daily) - 1, n)
        if v is None:
            return Outcome.missing_data(f"日K不足 {n} 根")
        return _range_check(v, self.num("min"), self.num("max"), "回撤", "%")


# ======================= 均线 =======================
@register_factor
class MaBull(_DailyFactor):
    kind = "ma_bull"
    title = "均线多头排列"
    cat = "ma"
    description = "短期均线在长期均线上方依次排列（如 MA5 > MA10 > MA20）。当天均线用现价算。"
    example = "5,10,20：MA5>MA10>MA20；5,10,20,60 更严格"
    params = [ParamSpec("periods", "5,10,20", "均线周期，短到长", "choice",
                        [("5,10", "MA5 > MA10"), ("5,10,20", "MA5 > MA10 > MA20"), ("5,10,20,60", "MA5 > MA10 > MA20 > MA60")])]

    def _periods(self):
        ps = [int(x) for x in str(self.p.get("periods") or "5,10,20").replace("，", ",").split(",") if x.strip()]
        return ps

    def validate(self):
        ps = self._periods()
        if len(ps) < 2 or any(p not in (5, 10, 20, 60) for p in ps) or ps != sorted(ps):
            raise ValueError("周期从 5/10/20/60 里选，短到长")

    def label(self):
        return "均线多头 " + ">".join(f"MA{p}" for p in self._periods())

    def evaluate(self, c, ctx):
        if not c.daily:
            return _daily_missing(c)
        vals = [self.dval(c, f"ma{p}") for p in self._periods()]
        if any(v is None for v in vals):
            return Outcome.missing_data("日K不够长，算不出均线")
        ok = all(a > b for a, b in zip(vals, vals[1:]))
        txt = " / ".join(f"MA{p} {fmt(v)}" for p, v in zip(self._periods(), vals))
        return Outcome(ok, None, ("多头：" if ok else "不是多头：") + txt)


# ======================= 技术指标 =======================
def _cross_within(c, field: str, days: int):
    """最近 days 个交易日（含今天）里第一次出现 field=1 是几天前；没有返回 -1；数据缺失返回 None。"""
    seen_any = False
    for d in range(days):
        v = c.daily_at(field, d)
        if v is None:
            continue
        seen_any = True
        if v >= 1:
            return d
    return -1 if seen_any else None


@register_factor
class MacdCross(_DailyFactor):
    kind = "macd_cross"
    title = "MACD金叉/死叉"
    cat = "tech"
    description = ("MACD(12,26,9) 的 DIF 上穿 DEA 叫金叉、下穿叫死叉。可以要求最近几天内发生过，"
                   "也可以要求金叉发生在零轴上方。日K少于 35 根算不准，按数据缺失处理。")
    example = "最近 3 天内 MACD 金叉；或「排除今天死叉」"
    params = [ParamSpec("cross", "golden", "哪种交叉", "choice", [("golden", "金叉"), ("death", "死叉")]),
              ParamSpec("within_days", 1, "最近几天内（1 = 只看今天）", unit="天"),
              ParamSpec("action", "require", "要还是排除", "choice", [("require", "要求出现"), ("exclude", "排除出现的")]),
              ParamSpec("above_zero", False, "只算零轴上方的交叉（DIF>0）", "bool")]

    def label(self):
        n = int(self.num("within_days") or 1)
        x = "金叉" if self.p["cross"] == "golden" else "死叉"
        when = "今天" if n <= 1 else f"近{n}天"
        z = "（零轴上）" if self.p.get("above_zero") else ""
        return ("排除" if self.p.get("action") == "exclude" else "") + f"{when}MACD{x}{z}"

    def evaluate(self, c, ctx):
        if not c.daily:
            return _daily_missing(c)
        n = max(1, int(self.num("within_days") or 1))
        if self.dval(c, "macd_dif") is None:
            return Outcome.missing_data(f"日K不足 {ind.MACD_MIN_BARS} 根，算不出 MACD")
        field = "macd_golden" if self.p["cross"] == "golden" else "macd_death"
        d = _cross_within(c, field, n)
        if d is not None and d >= 0 and self.p.get("above_zero") and (self.dval(c, "macd_dif", d) or 0) <= 0:
            d = -1
        hit = d is not None and d >= 0
        dif = self.dval(c, "macd_dif")
        note = (f"{d} 天前出现" if d else "今天出现") if hit else "没有出现"
        note += f"（DIF {fmt(dif)} / DEA {fmt(self.dval(c, 'macd_dea'))}）"
        passed = hit if self.p.get("action") != "exclude" else not hit
        return Outcome(passed, dif, note)


KDJ_MODES = [("golden", "KDJ 金叉"), ("death", "KDJ 死叉"), ("j_range", "J 值区间"), ("k_range", "K 值区间")]


@register_factor
class Kdj(_DailyFactor):
    kind = "kdj"
    title = "KDJ"
    cat = "tech"
    description = "KDJ(9,3,3)。可以要求最近几天内金叉/死叉，或 J、K 值在区间内（J > 100 常说超买，< 0 超卖）。"
    example = "J 值 ≤ 100：不买短线超买的"
    params = [ParamSpec("mode", "golden", "怎么用", "choice", KDJ_MODES),
              ParamSpec("within_days", 1, "金叉/死叉：最近几天内", unit="天"),
              ParamSpec("min", None, "区间下限，可空"), ParamSpec("max", None, "区间上限，可空")]

    def validate(self):
        if self.p.get("mode") not in dict(KDJ_MODES):
            raise ValueError("mode 只能是 golden / death / j_range / k_range")
        if self.p["mode"].endswith("_range"):
            _need_one_side(self)

    def label(self):
        m = self.p["mode"]
        if m.endswith("_range"):
            return _range_label("KDJ " + m[0].upper(), self.num("min"), self.num("max"))
        n = int(self.num("within_days") or 1)
        return ("今天" if n <= 1 else f"近{n}天") + dict(KDJ_MODES)[m]

    def evaluate(self, c, ctx):
        if not c.daily:
            return _daily_missing(c)
        k = self.dval(c, "kdj_k")
        if k is None:
            return Outcome.missing_data("日K不足 9 根，算不出 KDJ")
        m = self.p["mode"]
        if m.endswith("_range"):
            v = self.dval(c, "kdj_j" if m == "j_range" else "kdj_k")
            return _range_check(v, self.num("min"), self.num("max"), m[0].upper())
        d = _cross_within(c, "kdj_golden" if m == "golden" else "kdj_death", max(1, int(self.num("within_days") or 1)))
        if d is None:
            return Outcome.missing_data("算不出 KDJ 交叉")
        note = f"K {fmt(k)} / D {fmt(self.dval(c, 'kdj_d'))} / J {fmt(self.dval(c, 'kdj_j'))}"
        return Outcome(d >= 0, k, ("今天" if d == 0 else f"{d} 天前") + "出现，" + note if d >= 0 else "没有出现，" + note)


@register_factor
class Rsi(_DailyFactor):
    kind = "rsi"
    title = "RSI"
    cat = "tech"
    description = "相对强弱指标（0~100）。常见用法：RSI6 > 80 算超买、< 20 算超卖。当天的值用现价算。"
    example = "RSI6 在 50~80：强但未过热"
    params = [ParamSpec("period", 6, "周期", "choice", [(6, "6 日"), (12, "12 日"), (24, "24 日")]),
              ParamSpec("min", None, "下限，可空"), ParamSpec("max", None, "上限，可空")]

    def validate(self):
        _need_one_side(self)
        if int(self.num("period") or 0) not in (6, 12, 24):
            raise ValueError("周期只能是 6 / 12 / 24")

    def label(self):
        return _range_label(f"RSI{int(self.num('period'))}", self.num("min"), self.num("max"))

    def evaluate(self, c, ctx):
        if not c.daily:
            return _daily_missing(c)
        p = int(self.num("period"))
        v = self.dval(c, f"rsi{p}")
        if v is None:
            return Outcome.missing_data(f"日K不足 {p + 1} 根")
        return _range_check(v, self.num("min"), self.num("max"), f"RSI{p}")


@register_factor
class BollPosition(_DailyFactor):
    kind = "boll_position"
    title = "布林带位置"
    cat = "tech"
    description = ("现价在布林带(20,2)里的位置：0 = 下轨，50 = 中轨，100 = 上轨，突破上轨会超过 100，"
                   "跌破下轨会低于 0。")
    example = "位置 ≥ 50：站上中轨；≥ 100：突破上轨"
    params = [ParamSpec("min", None, "下限，可空"), ParamSpec("max", None, "上限，可空")]

    def validate(self):
        _need_one_side(self)

    def label(self):
        return _range_label("布林位置", self.num("min"), self.num("max"))

    def evaluate(self, c, ctx):
        if not c.daily:
            return _daily_missing(c)
        v = self.dval(c, "boll_pos")
        if v is None:
            return Outcome.missing_data("日K不足 20 根，或近 20 天价格没动")
        return _range_check(v, self.num("min"), self.num("max"), "布林位置")


# ======================= 涨停板 =======================
BOARD_MODES = [("first", "首板（今天第 1 板）"), ("second", "二板（今天第 2 板）"), ("nth", "N 连板及以上"),
               ("exact", "恰好第 N 板"), ("count", "近 N 天涨停次数"), ("max_streak", "连板数不超过 N")]


def _no_limit(c) -> bool:
    if c.get("no_limit"):
        return True
    return bool(c.daily and c.daily_at("no_limit_day", 0))


@register_factor
class LimitBoard(_DailyFactor):
    kind = "limit_board"
    title = "连板"
    cat = "limit"
    description = ("按日K数连续涨停收盘的天数。「今天第几板」盘中按现价是否封在涨停算；也可以数近 N 天涨停了几次。"
                   "涨停价按板块规则表算（主板 10%、创业板/科创板 20%、北交所 30%、主板 ST 2026-07-06 起 10%）。"
                   "注册制新股上市前 5 个交易日（北交所首日）没有涨跌幅限制，这几天标「不适用」。")
    example = "首板；二板；近 10 天涨停 ≥ 2 次；连板数不超过 3（不接高位板）"
    params = [ParamSpec("mode", "first", "怎么数", "choice", BOARD_MODES),
              ParamSpec("n", 3, "N（N 连板 / 恰好第 N 板 / 不超过 N / 至少几次）"),
              ParamSpec("days", 10, "近 N 天涨停次数：统计几天", unit="天")]

    def validate(self):
        if self.p.get("mode") not in dict(BOARD_MODES):
            raise ValueError("mode 不对")

    def label(self):
        m, n = self.p["mode"], int(self.num("n") or 0)
        return {"first": "首板", "second": "二板", "nth": f"{n}连板及以上", "exact": f"第{n}板",
                "count": f"近{int(self.num('days') or 10)}天涨停≥{n}次", "max_streak": f"连板≤{n}"}[m]

    def evaluate(self, c, ctx):
        if not c.daily:
            return _daily_missing(c)
        if _no_limit(c):
            return Outcome.not_applicable("新股上市初期无涨跌幅限制")
        m, n = self.p["mode"], int(self.num("n") or 0)
        if m == "count":
            days = int(self.num("days") or 10)
            v = ind.limit_up_count(c.indicator_rows(), len(c.daily) - 1, days)
            if v is None:
                return Outcome.missing_data(f"日K不足 {days + 1} 根")
            return Outcome(v >= n, v, f"近 {days} 天涨停 {int(v)} 次")
        bc = self.dval(c, "board_count")
        if bc is None:
            return Outcome.missing_data("日K不足 2 根，算不出涨停价")
        need = {"first": lambda b: b == 1, "second": lambda b: b == 2, "nth": lambda b: b >= n,
                "exact": lambda b: b == n, "max_streak": lambda b: b <= n}[m]
        note = f"今天第 {int(bc)} 板" if bc else f"今天没有涨停（昨天连板 {int(self.dval(c, 'prev_board_count') or 0)}）"
        return Outcome(bool(need(bc)), bc, note)


BROKE_MODES = [("exclude_today", "排除今天炸板的"), ("only_today", "只要今天炸板的"),
               ("max_count", "近 5 天炸板不超过 N 次")]


@register_factor
class BrokeBoard(_DailyFactor):
    kind = "broke_board"
    title = "炸板"
    cat = "limit"
    description = ("炸板 = 当天最高价摸到过涨停价，但现价（盘后为收盘价）没封住。"
                   "新股上市初期无涨跌幅限制的日子标「不适用」。")
    example = "排除今天炸板的：炸板说明上方抓得重"
    params = [ParamSpec("mode", "exclude_today", "怎么用", "choice", BROKE_MODES),
              ParamSpec("n", 1, "近 5 天最多炸几次")]

    def label(self):
        m = self.p.get("mode")
        return {"exclude_today": "排除今天炸板", "only_today": "今天炸板",
                "max_count": f"近5天炸板≤{int(self.num('n') or 0)}次"}.get(m, self.title)

    def evaluate(self, c, ctx):
        if not c.daily:
            return _daily_missing(c)
        if _no_limit(c):
            return Outcome.not_applicable("新股上市初期无涨跌幅限制")
        m = self.p.get("mode")
        if m == "max_count":
            v = self.dval(c, "broke_count_5d")
            if v is None:
                return Outcome.missing_data("日K不足 6 根")
            n = int(self.num("n") or 0)
            return Outcome(v <= n, v, f"近 5 天炸板 {int(v)} 次")
        b = self.dval(c, "broke_board")
        if b is None:
            return Outcome.missing_data("日K不足 2 根，算不出涨停价")
        broke = b >= 1
        note = "今天炸板" if broke else "今天没有炸板"
        return Outcome(broke if m == "only_today" else not broke, b, note)


# ======================= 基本面与排雷 =======================
@register_factor
class RiskScreen(Factor):
    kind = "risk_screen"
    title = "排雷"
    cat = "fundamental"
    update = "static"
    description = ("一次排掉不想碰的票：ST/*ST、上市不满 N 天、次新股、指定行业。每一项留空就不检查。"
                   "上市日、行业来自个股资料（akshare），拿不到时这一项算「数据缺失」，票不会被当成通过。")
    example = "排除 ST + 上市不满 60 天 + 次新股（1 年内）+ 不要房地产"
    params = [ParamSpec("exclude_st", True, "排除 ST / *ST", "bool"),
              ParamSpec("min_listing_days", None, "上市至少多少天（自然日），可空", unit="天"),
              ParamSpec("exclude_new_days", None, "排除次新股：上市不满多少天算次新（常见 365），可空", unit="天"),
              ParamSpec("industries_exclude", [], "排除这些行业（可空；从行业列表点选，行业名要完全一致）", "industries"),
              ParamSpec("industries_only", [], "只要这些行业（可空 = 不限；从行业列表点选，行业名要完全一致）", "industries")]

    def needs(self):
        out = set()
        if self.num("min_listing_days") is not None or self.num("exclude_new_days") is not None:
            out |= {"daily", "info"}
        if self.p.get("industries_exclude") or self.p.get("industries_only"):
            out.add("info")
        return out

    def label(self):
        parts = []
        if self.p.get("exclude_st"):
            parts.append("非ST")
        if self.num("min_listing_days") is not None:
            parts.append(f"上市≥{int(self.num('min_listing_days'))}天")
        if self.num("exclude_new_days") is not None:
            parts.append(f"非次新（{int(self.num('exclude_new_days'))}天）")
        if self.p.get("industries_exclude"):
            parts.append("排除" + "/".join(self.p["industries_exclude"]))
        if self.p.get("industries_only"):
            parts.append("只要" + "/".join(self.p["industries_only"]))
        return "排雷：" + ("、".join(parts) or "未设置")

    def evaluate(self, c, ctx):
        from ..limits import is_risk_name as is_st  # 和「排除 ST」因子同一个口径（名字含 ST 或「退」）
        notes = []
        if self.p.get("exclude_st"):
            if is_st(c.name):
                return Outcome.fail(None, f"{c.name} 是 ST")
            notes.append("非 ST")
        need_days = [x for x in (self.num("min_listing_days"), self.num("exclude_new_days")) if x is not None]
        if need_days:
            days = c.listing_days(ctx.trading_date)
            if days is None:
                return Outcome.missing_data("不知道上市日（个股资料没拿到）")
            lim = max(need_days)
            if days < lim:
                return Outcome.fail(days, f"上市才 {days} 天（要求 ≥ {int(lim)} 天）")
            notes.append(f"上市 {days} 天")
        exc, only = self.p.get("industries_exclude") or [], self.p.get("industries_only") or []
        if exc or only:
            indus = c.info.get("industry")
            if not indus:
                return Outcome.missing_data("不知道所属行业（个股资料没拿到）")
            # 完全匹配（去掉首尾空格）：填「银行」不会误伤「银行设备」之类名字里带这两个字的行业
            ind = str(indus).strip()
            exc_s = {str(x).strip() for x in exc if str(x).strip()}
            only_s = {str(x).strip() for x in only if str(x).strip()}
            if ind in exc_s:
                return Outcome.fail(None, f"行业 {indus} 在排除名单里")
            if only_s and ind not in only_s:
                return Outcome.fail(None, f"行业 {indus} 不在「只要」名单里")
            notes.append(f"行业 {indus}")
        return Outcome.ok(None, "，".join(notes) or "未设置任何检查")


# ======================= 股池来源 =======================
@register_factor
class PoolSource(Factor):
    kind = "pool_source"
    title = "来自指定股池"
    cat = "pool"
    update = "realtime"
    backtest_ok = False
    backtest_note = "回测不重放当天是否入池"
    description = "只要（或排除）来自某几个股池的票。一只票同时在几个池里时，有一个匹配就算。"
    example = "只要 gushi:gold_230（黄金两点半）里的票"
    params = [ParamSpec("pools", [], "股池（点选）", "pools"),
              ParamSpec("action", "only", "只要还是排除", "choice", [("only", "只要这些池"), ("exclude", "排除这些池")])]

    def validate(self):
        if not self.p.get("pools"):
            raise ValueError("至少选一个股池")

    def label(self):
        return ("排除" if self.p.get("action") == "exclude" else "只要") + " " + "、".join(self.p["pools"])

    def evaluate(self, c, ctx):
        hit = [s for s in c.sources if s in self.p["pools"]]
        if self.p.get("action") == "exclude":
            return Outcome(not hit, None, ("在排除的池里：" + "、".join(hit)) if hit else "不在排除的池里")
        return Outcome(bool(hit), None, ("来自 " + "、".join(hit)) if hit else "不在指定的池里")
