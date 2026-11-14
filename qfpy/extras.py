"""第二批（实验性）数据：竞价涨幅/金额、涨停封单额/封成比、主力净流入、龙虎榜。

数据都来自 akshare（东方财富网页接口），接口偶尔改版，所以这几个因子标「实验性」：
- 取不到就是「数据缺失」，不当成 0，也不放行；
- 只有当天（龙虎榜是最近几天）的数据，没有历史，回测里标「回测不可用」并跳过。

每类数据整个市场一次取、按天缓存（data/cache/extras/日期/），竞价是按只取。
"""
from __future__ import annotations

from datetime import date as _date, timedelta

from .factors.base import Factor, Outcome, ParamSpec, register_factor
from .factors.builtin import fmt
from .util import today_str


def _num(v):
    try:
        if v is None or v == "" or v == "-":
            return None
        x = float(v)
        return None if x != x else x
    except (TypeError, ValueError):
        return None


def _col(row, *names):
    for n in names:
        if n in row and _num(row[n]) is not None:
            return _num(row[n])
    return None


def _code(v) -> str:
    s = str(v).strip()
    return s[-6:] if len(s) >= 6 else s.zfill(6)


def fetch_akshare_extra(src, ak, kind: str, date: str) -> dict[str, dict] | None:
    """按 kind 取数据，返回 {6位代码: {字段: 值}}。取不到抛异常。"""
    d8 = date.replace("-", "")
    if kind == "seal":
        df = src._call(ak.stock_zt_pool_em, date=d8)
        out = {}
        for _, r in df.iterrows():
            seal, amt = _col(r, "封板资金"), _col(r, "成交额")
            out[_code(r["代码"])] = {"seal_amount": seal, "seal_ratio": (seal / amt) if seal and amt else None,
                                       "board_count_src": _col(r, "连板数"), "broke_times": _col(r, "炸板次数")}
        return out
    if kind == "fund_flow":
        if date != today_str():
            raise RuntimeError("主力资金只有当天的数据")
        df = src._call(ak.stock_individual_fund_flow_rank, indicator="今日")
        out = {}
        for _, r in df.iterrows():
            out[_code(r["代码"])] = {"main_net_inflow": _col(r, "今日主力净流入-净额", "主力净流入-净额"),
                                       "main_net_ratio": _col(r, "今日主力净流入-净占比", "主力净流入-净占比")}
        return out
    if kind == "lhb":
        start = (_date.fromisoformat(date) - timedelta(days=45)).strftime("%Y%m%d")
        df = src._call(ak.stock_lhb_detail_em, start_date=start, end_date=d8)
        out: dict[str, dict] = {}
        for _, r in df.iterrows():
            c = _code(r["代码"])
            day = str(r.get("上榜日", ""))[:10]
            e = out.setdefault(c, {"days": [], "net_buy": {}})
            if day and day not in e["days"]:
                e["days"].append(day)
            nb = _col(r, "龙虎榜净买额")
            if nb is not None and day:
                e["net_buy"][day] = e["net_buy"].get(day, 0) + nb
        return out
    if kind.startswith("auction_"):
        code = kind.split("_", 1)[1]
        if date != today_str():
            raise RuntimeError("竞价数据只有当天的")
        df = src._call(ak.stock_zh_a_hist_pre_min_em, symbol=code, start_time="09:00:00", end_time="09:30:00")
        best = None
        for _, r in df.iterrows():
            t = str(r.get("时间", ""))
            if t[:10] != date:
                continue
            hm = t[11:16]
            if "09:15" <= hm <= "09:25":
                best = r
        if best is None:
            return {}
        return {code: {"auction_price": _col(best, "最新价", "收盘"), "auction_amount": _col(best, "成交额")}}
    raise RuntimeError(f"不认识的数据类型 {kind}")


# ======================= 实验性因子 =======================
class _ExtraFactor(Factor):
    cat = "auction_fund"
    experimental = True
    backtest_ok = False
    backtest_note = "没有历史数据，回测不可用"
    extra_kind = ""

    def _market(self, ctx):
        return (ctx.extra or {}).get("market")

    def prepare(self, cands, ctx):
        self._data, self._err = None, None
        m = self._market(ctx)
        if m is None:
            self._err = "这里拿不到行情模块（例如回测）"
            return
        self._data, self._err = m.extras(self.extra_kind, ctx.trading_date)

    def _row(self, c, ctx):
        if getattr(self, "_data", None) is None and not getattr(self, "_err", None):
            self.prepare([c], ctx)
        if self._data is None:
            return None, Outcome.missing_data(self._err or "取不到")
        return self._data.get(c.code), None


def _rng(f, v, name, unit=""):
    lo, hi = f.num("min"), f.num("max")
    if lo is not None and v < lo:
        return Outcome.fail(v, f"{name} {fmt(v)}{unit}，低于 {fmt(lo)}{unit}")
    if hi is not None and v > hi:
        return Outcome.fail(v, f"{name} {fmt(v)}{unit}，高于 {fmt(hi)}{unit}")
    return Outcome.ok(v, f"{name} {fmt(v)}{unit}")


def _lbl(name, lo, hi, unit=""):
    if lo is not None and hi is not None:
        return f"{name} {fmt(lo)}~{fmt(hi)}{unit}"
    return f"{name} ≥ {fmt(lo)}{unit}" if lo is not None else (f"{name} ≤ {fmt(hi)}{unit}" if hi is not None else name)


def _need_side(f):
    if f.num("min") is None and f.num("max") is None:
        raise ValueError("下限和上限至少填一个")


AUCTION_WHAT = [("pct", "竞价涨幅 %"), ("amount", "竞价金额（万元）")]


@register_factor
class AuctionFactor(_ExtraFactor):
    kind = "auction"
    title = "竞价涨幅/金额"
    description = ("9:25 集合竞价的成交价相对昨收的涨幅，或竞价成交金额。按只从东方财富取盘前分时，"
                   "只有当天的数据；9:25 之前或取不到时算「数据缺失」。")
    example = "竞价涨幅 2%~6%；竞价金额 ≥ 1000 万"
    params = [ParamSpec("what", "pct", "看什么", "choice", AUCTION_WHAT),
              ParamSpec("min", None, "下限，可空"), ParamSpec("max", None, "上限，可空")]

    def validate(self):
        _need_side(self)

    def label(self):
        return _lbl(dict(AUCTION_WHAT)[self.p.get("what", "pct")], self.num("min"), self.num("max"))

    def prepare(self, cands, ctx):
        self._rows, self._err = {}, None
        m = self._market(ctx)
        if m is None:
            self._err = "这里拿不到行情模块（例如回测）"
            return
        for c in cands:
            d, err = m.extras(f"auction_{c.code}", ctx.trading_date, ttl=3600)
            self._rows[c.code] = (d or {}).get(c.code) if d is not None else err

    def evaluate(self, c, ctx):
        if not hasattr(self, "_rows"):
            self.prepare([c], ctx)
        if self._err:
            return Outcome.missing_data(self._err)
        r = self._rows.get(c.code)
        if not isinstance(r, dict):
            return Outcome.missing_data(r if isinstance(r, str) else "没有竞价数据")
        if self.p.get("what") == "amount":
            if r.get("auction_amount") is None:
                return Outcome.missing_data("没有竞价金额")
            return _rng(self, r["auction_amount"] / 1e4, "竞价金额", "万")
        pre = c.get("preclose")
        if r.get("auction_price") is None or not pre:
            return Outcome.missing_data("没有竞价价格或昨收")
        return _rng(self, (r["auction_price"] / pre - 1) * 100, "竞价涨幅", "%")


SEAL_WHAT = [("amount", "封单额（万元）"), ("ratio", "封成比（封单额 ÷ 成交额）")]


@register_factor
class SealFactor(_ExtraFactor):
    kind = "seal"
    title = "封单额/封成比"
    extra_kind = "seal"
    description = ("涨停票的封板资金，或封单额与当天成交额之比（越大封得越牢）。数据来自东方财富涨停板池，"
                   "不在涨停板池里的票视为没有封板（不通过）；板池整个取不到时算「数据缺失」。")
    example = "封单额 ≥ 5000 万；封成比 ≥ 0.5"
    params = [ParamSpec("what", "amount", "看什么", "choice", SEAL_WHAT),
              ParamSpec("min", None, "下限，可空"), ParamSpec("max", None, "上限，可空")]

    def validate(self):
        _need_side(self)

    def label(self):
        return _lbl(dict(SEAL_WHAT)[self.p.get("what", "amount")].split("（")[0], self.num("min"), self.num("max"))

    def evaluate(self, c, ctx):
        r, miss = self._row(c, ctx)
        if miss:
            return miss
        if not r:
            return Outcome.fail(None, "不在涨停板池里（当前没有封涨停）")
        if self.p.get("what") == "ratio":
            if r.get("seal_ratio") is None:
                return Outcome.missing_data("算不出封成比")
            return _rng(self, r["seal_ratio"], "封成比")
        if r.get("seal_amount") is None:
            return Outcome.missing_data("没有封单额")
        return _rng(self, r["seal_amount"] / 1e4, "封单额", "万")


FLOW_WHAT = [("amount", "主力净流入（万元）"), ("ratio", "主力净占比 %")]


@register_factor
class FundFlowFactor(_ExtraFactor):
    kind = "main_fund_flow"
    title = "主力净流入"
    extra_kind = "fund_flow"
    description = "东方财富口径的今日主力（超大单+大单）净流入额或净占比，盘中每分钟刷新。只有当天的数据。"
    example = "主力净流入 ≥ 0；净占比 ≥ 5%"
    params = [ParamSpec("what", "amount", "看什么", "choice", FLOW_WHAT),
              ParamSpec("min", None, "下限，可空"), ParamSpec("max", None, "上限，可空")]

    def validate(self):
        _need_side(self)

    def label(self):
        return _lbl(dict(FLOW_WHAT)[self.p.get("what", "amount")].split("（")[0], self.num("min"), self.num("max"))

    def evaluate(self, c, ctx):
        r, miss = self._row(c, ctx)
        if miss:
            return miss
        key = "main_net_ratio" if self.p.get("what") == "ratio" else "main_net_inflow"
        if not r or r.get(key) is None:
            return Outcome.missing_data("资金流向榜里没有这只票")
        if key == "main_net_ratio":
            return _rng(self, r[key], "主力净占比", "%")
        return _rng(self, r[key] / 1e4, "主力净流入", "万")


LHB_MODES = [("on_list", "近 N 天上过龙虎榜"), ("not_on_list", "近 N 天没上过龙虎榜"), ("net_buy", "近 N 天龙虎榜净买额（万元）")]


@register_factor
class LhbFactor(_ExtraFactor):
    kind = "lhb"
    title = "龙虎榜"
    extra_kind = "lhb"
    update = "eod"
    description = ("东方财富龙虎榜明细（盘后公布，盘中只能看到前一交易日及以前）。"
                   "「近 N 天」按自然日算，不含今天。榜单整个取不到时算「数据缺失」。")
    example = "近 5 天上过龙虎榜；近 5 天龙虎榜净买 ≥ 1000 万"
    params = [ParamSpec("mode", "on_list", "怎么用", "choice", LHB_MODES),
              ParamSpec("days", 5, "近几天（自然日）", unit="天"),
              ParamSpec("min", None, "净买额下限（万元），可空"), ParamSpec("max", None, "净买额上限（万元），可空")]

    def validate(self):
        if self.p.get("mode") == "net_buy":
            _need_side(self)

    def label(self):
        n = int(self.num("days") or 5)
        m = self.p.get("mode", "on_list")
        if m == "net_buy":
            return _lbl(f"近{n}天龙虎榜净买", self.num("min"), self.num("max"), "万")
        return f"近{n}天" + ("上过" if m == "on_list" else "没上过") + "龙虎榜"

    def evaluate(self, c, ctx):
        if getattr(self, "_data", None) is None and not getattr(self, "_err", None):
            self.prepare([c], ctx)
        if self._data is None:
            return Outcome.missing_data(self._err or "取不到")
        n = int(self.num("days") or 5)
        since = (_date.fromisoformat(ctx.trading_date) - timedelta(days=n)).isoformat()
        e = self._data.get(c.code) or {}
        days = [d for d in e.get("days", []) if since <= d < ctx.trading_date]
        m = self.p.get("mode", "on_list")
        if m == "net_buy":
            if not days:
                return Outcome.fail(None, f"近 {n} 天没上龙虎榜")
            v = sum(e.get("net_buy", {}).get(d, 0) for d in days) / 1e4
            return _rng(self, v, "龙虎榜净买", "万")
        note = ("上榜：" + "、".join(sorted(days))) if days else f"近 {n} 天没上榜"
        return Outcome(bool(days) if m == "on_list" else not days, float(len(days)), note)
