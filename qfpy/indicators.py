"""日K技术指标、涨停板、换手率等派生字段。

一次算整条日K序列（series_rows），每根K线一行。算不出来的字段（日K太短、数据源没给）就不放进结果，
上层取不到时一律按「数据缺失」处理，绝不当成 0。

口径（与同花顺/通达信常见公式一致）：
  MACD(12,26,9)：DIF=EMA12-EMA26，DEA=EMA(DIF,9)，柱=2×(DIF-DEA)；少于 35 根日K不算
  KDJ(9,3,3)：RSV 用 9 日高低，K=⅔K′+⅓RSV，D=⅔D′+⅓K，J=3K-2D，初值 50
  RSI(6/12/24)：SMA(MAX(涨,0),N,1)/SMA(|涨跌|,N,1)×100
  布林带(20,2)：中轨 MA20，上下轨 ±2 倍标准差；位置 = (收盘-下轨)/(上轨-下轨)×100
  换手率：优先用数据源给的；没有就用 成交量/流通股本 算（当天拼的K线没有成交量时用 成交额/价格 估算）
"""
from __future__ import annotations

import math
from typing import Any

from . import limits
from .vendor.tsp_price_limits import limit_price

MACD_MIN_BARS = 35
TOL = 0.0051     # 价格比较容差（半分钱）


def _ema(vals: list[float], n: int) -> list[float]:
    out, k, e = [], 2 / (n + 1), None
    for v in vals:
        e = v if e is None else e + k * (v - e)
        out.append(e)
    return out


def _sma_cn(vals: list[float], n: int, m: int = 1) -> list[float]:
    """通达信 SMA(X,N,M)：Y = (M×X + (N-M)×Y′)/N。"""
    out, y = [], None
    for v in vals:
        y = v if y is None else (m * v + (n - m) * y) / n
        out.append(y)
    return out


def float_shares_of(bars: list[dict]) -> float | None:
    """流通股本（股）：数据源直接给的优先；否则用最近一根有换手率和成交量的K线反推。"""
    for b in reversed(bars):
        if b.get("float_shares"):
            return float(b["float_shares"])
    for b in reversed(bars):
        t, v = b.get("turnover"), b.get("volume")
        if t and v and t > 0:
            return float(v) / (float(t) / 100)
    return None


def bar_turnover(b: dict, float_shares: float | None) -> float | None:
    if b.get("turnover") is not None:
        return float(b["turnover"])
    if not float_shares:
        return None
    if b.get("volume"):
        return float(b["volume"]) / float_shares * 100
    if b.get("amount") and b.get("close"):
        return float(b["amount"]) / float(b["close"]) / float_shares * 100
    return None


def series_rows(bars: list[dict], code: str | None = None, name: str | None = None,
                first_rank: int | None = None) -> list[dict[str, Any]]:
    """整条序列的派生字段。first_rank = 第一根K线是上市第几个交易日（知道上市日时为 1；老股为 None）。"""
    n = len(bars)
    rows: list[dict[str, Any]] = [{} for _ in range(n)]
    if n == 0:
        return rows
    closes = [float(b.get("close") or 0) for b in bars]
    highs = [float(b.get("high") or b.get("close") or 0) for b in bars]
    lows = [float(b.get("low") or b.get("close") or 0) for b in bars]
    if any(c <= 0 for c in closes):
        return rows

    # ---- MACD ----
    if n >= MACD_MIN_BARS:
        e12, e26 = _ema(closes, 12), _ema(closes, 26)
        dif = [a - b for a, b in zip(e12, e26)]
        dea = _ema(dif, 9)
        for i in range(MACD_MIN_BARS - 1, n):
            r = rows[i]
            r["macd_dif"], r["macd_dea"], r["macd_hist"] = dif[i], dea[i], 2 * (dif[i] - dea[i])
            if i > 0:
                r["macd_golden"] = 1.0 if dif[i - 1] <= dea[i - 1] and dif[i] > dea[i] else 0.0
                r["macd_death"] = 1.0 if dif[i - 1] >= dea[i - 1] and dif[i] < dea[i] else 0.0

    # ---- KDJ ----
    k = d = 50.0
    for i in range(n):
        if i < 8:
            continue
        hh, ll = max(highs[i - 8: i + 1]), min(lows[i - 8: i + 1])
        rsv = 50.0 if hh == ll else (closes[i] - ll) / (hh - ll) * 100
        pk, pd = k, d
        k = 2 / 3 * k + rsv / 3
        d = 2 / 3 * d + k / 3
        rows[i].update(kdj_k=k, kdj_d=d, kdj_j=3 * k - 2 * d)
        if i > 8:
            rows[i]["kdj_golden"] = 1.0 if pk <= pd and k > d else 0.0
            rows[i]["kdj_death"] = 1.0 if pk >= pd and k < d else 0.0

    # ---- RSI ----
    diffs = [0.0] + [closes[i] - closes[i - 1] for i in range(1, n)]
    for p in (6, 12, 24):
        up = _sma_cn([max(x, 0.0) for x in diffs], p)
        ab = _sma_cn([abs(x) for x in diffs], p)
        for i in range(p, n):
            rows[i][f"rsi{p}"] = 50.0 if ab[i] == 0 else up[i] / ab[i] * 100

    # ---- 布林带 / 均线排列 / N日涨幅 / 回撤 ----
    for i in range(n):
        r = rows[i]
        if i >= 19:
            win = closes[i - 19: i + 1]
            mid = sum(win) / 20
            sd = math.sqrt(sum((x - mid) ** 2 for x in win) / 20)
            r.update(boll_mid=mid, boll_up=mid + 2 * sd, boll_low=mid - 2 * sd)
            if sd > 0:
                r["boll_pos"] = (closes[i] - (mid - 2 * sd)) / (4 * sd) * 100
        mas = {}
        for p in (5, 10, 20, 60):
            if i >= p - 1:
                mas[p] = sum(closes[i - p + 1: i + 1]) / p
        if all(p in mas for p in (5, 10, 20)):
            r["ma_bull"] = 1.0 if mas[5] > mas[10] > mas[20] else 0.0
        if all(p in mas for p in (5, 10, 20, 60)):
            r["ma_bull_60"] = 1.0 if mas[5] > mas[10] > mas[20] > mas[60] else 0.0
        for p in (3, 5, 10, 20, 60):
            if i >= p:
                r[f"chg_{p}d"] = (closes[i] / closes[i - p] - 1) * 100
        for p in (20, 60):
            if i >= p - 1:
                hh = max(highs[i - p + 1: i + 1])
                if hh > 0:
                    r[f"drawdown_{p}d"] = (hh - closes[i]) / hh * 100

    # ---- 涨停板 ----
    st = limits.is_st(name)
    if code:
        streak = 0
        lu_hist: list[float] = []
        broke_hist: list[float] = []
        for i in range(n):
            r = rows[i]
            rank = (first_rank + i) if first_rank else None
            if i == 0:
                lu_hist.append(0.0)
                broke_hist.append(0.0)
                continue
            if limits.in_no_limit_window(code, rank):
                r["no_limit_day"] = 1.0
                streak = 0
                lu_hist.append(0.0)
                broke_hist.append(0.0)
                continue
            pct = limits.limit_pct(code, bars[i]["date"], st)
            up = limit_price(closes[i - 1], pct, up=True)
            lu = closes[i] >= up - TOL
            touched = highs[i] >= up - TOL
            r["limit_up_close"] = 1.0 if lu else 0.0
            r["touched_limit_up"] = 1.0 if touched else 0.0
            r["broke_board"] = 1.0 if touched and not lu else 0.0
            r["prev_board_count"] = float(streak)
            streak = streak + 1 if lu else 0
            r["board_count"] = float(streak)
            lu_hist.append(1.0 if lu else 0.0)
            broke_hist.append(r["broke_board"])
            for p in (5, 10, 20):
                if i >= p - 1 or first_rank:     # 上市不到 N 天的新股也能数
                    r[f"limit_up_count_{p}d"] = sum(lu_hist[max(0, i - p + 1): i + 1])
            if i >= 4 or first_rank:
                r["broke_count_5d"] = sum(broke_hist[max(0, i - 4): i + 1])

    # ---- 换手率 / 市值 ----
    fs = float_shares_of(bars)
    tos = [bar_turnover(b, fs) for b in bars]
    for i in range(n):
        r = rows[i]
        if tos[i] is not None:
            r["turnover"] = tos[i]
        for p in (5, 10):
            prev = tos[max(0, i - p): i]
            if i >= p and all(x is not None for x in prev):
                avg = sum(prev) / p
                r[f"turnover_{p}d_avg"] = avg
                if p == 5 and avg > 0 and tos[i] is not None:
                    r["turnover_ratio_5d"] = tos[i] / avg
        if fs:
            r["float_mv"] = closes[i] * fs / 1e8
    return rows


def limit_up_count(rows: list[dict], idx: int, days: int) -> float | None:
    """第 idx 根（含）往前 days 天里涨停收盘的次数；数据不够返回 None。"""
    if idx - days + 1 < 1:
        return None
    vals = [rows[j].get("limit_up_close") for j in range(idx - days + 1, idx + 1)]
    if any(v is None for v in vals) and not any(rows[j].get("no_limit_day") for j in range(idx - days + 1, idx + 1)):
        return None
    return float(sum(v or 0 for v in vals))


def pct_change(bars: list[dict], idx: int, days: int) -> float | None:
    if idx - days < 0 or not bars[idx - days].get("close"):
        return None
    return (bars[idx]["close"] / bars[idx - days]["close"] - 1) * 100


def drawdown(bars: list[dict], idx: int, days: int) -> float | None:
    if idx - days + 1 < 0:
        return None
    hh = max(float(b.get("high") or b["close"]) for b in bars[idx - days + 1: idx + 1])
    return (hh - bars[idx]["close"]) / hh * 100 if hh > 0 else None
