"""行情聚合：分时/日K按配置顺序在多个数据源之间自动切换；分时摘要字段；日K派生字段；交易日历。"""
from __future__ import annotations

from datetime import date as _date, timedelta
import time as _time
from pathlib import Path
from typing import Any

from .datasources import DataSource, NotSupported
from .util import log, normalize_date, read_json, today_cn, today_str, write_json
from .vendor import tsp_price_limits as pl

# 腾讯分时（web.ifzq.gtimg.cn）会返回 501 + 反爬验证页；失败后 5 分钟内不再尝试，
# 否则每只股票都要先白跑一次请求再回退。
_QQ_DOWN_UNTIL = 0.0
# 东方财富 push2his 对并发很敏感：短时间内连发几十个请求会直接断连并临时封 IP（约 5 分钟）。
# 连续 3 次传输层失败就熔断 5 分钟，避免越试越久。
_EM_DOWN_UNTIL = 0.0
_EM_FAILS = 0


def _transport_error(exc: BaseException) -> bool:
    """是网络/协议层故障（该熔断），还是「这只票这天本来就没有分时」（不该熔断）。"""
    import httpx

    if isinstance(exc, (httpx.TransportError, httpx.HTTPStatusError, TimeoutError, OSError)):
        return True
    text = f"{type(exc).__name__} {exc}"
    return any(k in text for k in ("RemoteProtocolError", "ConnectError", "ReadTimeout", "disconnected"))

# 分时字段（按需拉取）
MINUTE_FIELDS = ("vwap", "vwap_dev_pct", "day_high", "day_low", "amount_yi", "drawdown_pct", "minute_last", "minute_points")
# 日K字段（按需拉取），表达式里可用 close[1] 或 ref(close, 1) 取 N 个交易日前的值
DAILY_FIELDS = ("open", "high", "low", "close", "volume", "amount", "pct_chg", "ma5", "ma10", "ma20", "ma60",
                "vol_ma5", "vol_ratio_5d", "high_20d", "low_20d", "high_60d", "low_60d", "amplitude",
                # 以下由 qfpy.indicators 计算（日K不够长或数据源没给时取不到 = 数据缺失）
                "turnover", "turnover_5d_avg", "turnover_10d_avg", "turnover_ratio_5d", "float_mv",
                "macd_dif", "macd_dea", "macd_hist", "macd_golden", "macd_death",
                "kdj_k", "kdj_d", "kdj_j", "kdj_golden", "kdj_death", "rsi6", "rsi12", "rsi24",
                "boll_mid", "boll_up", "boll_low", "boll_pos", "ma_bull", "ma_bull_60",
                "chg_3d", "chg_5d", "chg_10d", "chg_20d", "chg_60d", "drawdown_20d", "drawdown_60d",
                "limit_up_close", "touched_limit_up", "broke_board", "board_count", "prev_board_count",
                "limit_up_count_5d", "limit_up_count_10d", "limit_up_count_20d", "broke_count_5d", "no_limit_day")

FIELD_DOCS: list[tuple[str, str]] = [
    ("price", "最新价（股池）"),    ("open_price", "开盘价（股池）"),
    ("open_rise", "开盘涨幅 %（股池）"),
    ("change_rate", "当前涨幅 %（股池）"),
    ("market_value", "总市值，亿元（股池；自选股没有）"),
    ("hit_count", "同时命中的股池数"),
    ("preclose", "昨收"),
    ("limit_pct", "涨跌停幅度 %"),
    ("limit_up", "涨停价"),
    ("limit_down", "跌停价"),
    ("limit_gap_pct", "距涨停还有多少个百分点"),
    ("no_limit", "1 = 注册制新股上市初期无涨跌幅限制"),
    ("vwap", "分时均价（分时）"),
    ("vwap_dev_pct", "现价相对均价偏离 %（分时）"),
    ("day_high", "日内最高（分时）"),
    ("day_low", "日内最低（分时）"),
    ("amount_yi", "累计成交额，亿元（分时）"),
    ("drawdown_pct", "现价较日内最高回撤 %（分时）"),
    ("minute_last", "分时最后一个点的价格（分时）"),
    ("minute_points", "分时点数（分时）"),
    ("open/high/low/close", "日K 开高低收（日K；当天日K未出时用分时/股池拼一根）"),
    ("volume/amount", "日成交量（股）/成交额（元）（日K）"),
    ("pct_chg", "日涨跌幅 %（日K）"),
    ("amplitude", "日振幅 %（日K）"),
    ("ma5/ma10/ma20/ma60", "收盘均线（日K）"),
    ("vol_ma5", "5 日均量（不含当天，日K）"),
    ("vol_ratio_5d", "量比 = 当天量 / 前 5 日均量（日K）"),
    ("high_20d/low_20d/high_60d/low_60d", "含当天的 N 日最高/最低（日K）"),
    ("turnover", "换手率 %（盘中实时：数据源给的优先，没有就用 成交量/流通股本 算）"),
    ("turnover_5d_avg/turnover_10d_avg", "前 5 / 10 个交易日平均换手率 %（不含今天，日K）"),
    ("turnover_ratio_5d", "今日换手相对 5 日均值的倍数（日K）"),
    ("float_mv", "流通市值，亿元（数据源给的优先，没有就用 现价×流通股本 算）"),
    ("total_mv", "总市值，亿元（股池/个股资料；拿不到 = 数据缺失）"),
    ("macd_dif/macd_dea/macd_hist", "MACD(12,26,9) 的 DIF、DEA、柱（日K，少于 35 根不算）"),
    ("macd_golden/macd_death", "1 = 当天 MACD 金叉 / 死叉（日K）"),
    ("kdj_k/kdj_d/kdj_j", "KDJ(9,3,3)（日K）"),
    ("kdj_golden/kdj_death", "1 = 当天 KDJ 金叉 / 死叉（日K）"),
    ("rsi6/rsi12/rsi24", "RSI 6 / 12 / 24 日（日K）"),
    ("boll_mid/boll_up/boll_low", "布林带(20,2) 中轨/上轨/下轨（日K）"),
    ("boll_pos", "布林带位置：0 = 下轨，50 = 中轨，100 = 上轨（可超出 0~100）"),
    ("ma_bull/ma_bull_60", "1 = 均线多头排列 MA5>MA10>MA20（_60 再加 >MA60）"),
    ("chg_3d/chg_5d/chg_10d/chg_20d/chg_60d", "N 日涨幅 %：今天收盘相对 N 个交易日前收盘"),
    ("drawdown_20d/drawdown_60d", "距 N 日最高点回撤 %（正数，0 = 就在高点）"),
    ("limit_up_close", "1 = 当天收在涨停价（盘中按现价算）"),
    ("touched_limit_up", "1 = 当天最高价摸到过涨停价"),
    ("broke_board", "1 = 炸板：摸到涨停但没封住"),
    ("board_count", "今天是第几板（连续涨停收盘天数，今天没涨停 = 0）"),
    ("prev_board_count", "截至昨天的连板数（盘中判断「今天封住就是第几板」用）"),
    ("limit_up_count_5d/limit_up_count_10d/limit_up_count_20d", "近 5 / 10 / 20 个交易日涨停次数（含今天）"),
    ("broke_count_5d", "近 5 个交易日炸板次数"),
    ("no_limit_day", "1 = 注册制新股上市初期无涨跌幅限制的日子（连板/炸板类因子此时不适用）"),
    ("listing_days", "上市以来的自然日天数（只在知道上市日时有值）"),
    ("is_st", "1 = ST / *ST"),
    ("score", "综合分（打分层之后）"),
]


def summarize_minute(day: dict) -> dict[str, float]:
    """把分时摘要成可筛选的字段（与 Rust 版 summarize_minute 同口径）。"""
    pts = [p for p in day.get("points") or [] if p.get("p") is not None]
    out: dict[str, float] = {}
    if not pts:
        return out
    last = pts[-1]
    lp = float(last["p"])
    prices = [float(p["p"]) for p in pts]
    high = max(prices + ([day["high"]] if day.get("high") else []))
    low = min(prices + ([day["low"]] if day.get("low") else []))
    amount = sum(float(p.get("amt") or 0) for p in pts)
    out.update(minute_last=lp, minute_points=float(len(pts)), day_high=high, day_low=low, amount_yi=amount / 1e8)
    if high > 0:
        out["drawdown_pct"] = (high - lp) / high * 100
    avg = last.get("avg")
    if avg and avg > 0:
        out["vwap"] = float(avg)
        out["vwap_dev_pct"] = (lp - avg) / avg * 100
    if day.get("preclose"):
        out["minute_preclose"] = float(day["preclose"])
    if day.get("open"):
        out["minute_open"] = float(day["open"])
    return out


def daily_fields_at(bars: list[dict], idx: int, rows: list[dict] | None = None) -> dict[str, float]:
    """bars 按日期升序，计算第 idx 根的派生字段。rows = indicators.series_rows 的结果（不给就现算，不含涨停板字段）。"""
    if idx < 0 or idx >= len(bars):
        return {}
    if rows is None:
        from .indicators import series_rows
        rows = series_rows(bars)
    extra = rows[idx] if idx < len(rows) else {}
    b = bars[idx]
    out = {k: float(b[k]) for k in ("open", "high", "low", "close", "volume", "amount") if b.get(k) is not None}
    closes = [x["close"] for x in bars[: idx + 1] if x.get("close") is not None]
    if idx > 0 and bars[idx - 1].get("close"):
        pc = bars[idx - 1]["close"]
        out["pct_chg"] = (b["close"] / pc - 1) * 100
        if b.get("high") and b.get("low"):
            out["amplitude"] = (b["high"] - b["low"]) / pc * 100
    for n in (5, 10, 20, 60):
        if len(closes) >= n:
            out[f"ma{n}"] = sum(closes[-n:]) / n
    vols = [x.get("volume") for x in bars[max(0, idx - 5): idx]]
    # 5 日均量：前 5 天每天都要有成交量（缺一天就算数据缺失，不把缺的那天当成 0 拉低均量）
    if len(vols) == 5 and all(isinstance(v, (int, float)) and v > 0 for v in vols):
        out["vol_ma5"] = sum(vols) / 5
        if b.get("volume"):
            out["vol_ratio_5d"] = b["volume"] / out["vol_ma5"]
    for n in (20, 60):
        win = bars[max(0, idx - n + 1): idx + 1]
        if len(win) >= n:
            out[f"high_{n}d"] = max(x["high"] for x in win)
            out[f"low_{n}d"] = min(x["low"] for x in win)
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


# 每个数据源最近一次成功 / 失败（本进程内），给「设置 → 数据源」显示是否可用和最近的报错。
# {名字: {"ok_at", "err_at", "error", "what"}}；股池拉取由引擎记，行情由 Market 记。
SOURCE_HEALTH: dict[str, dict] = {}

CAP_CN = {"minute": "分时", "daily": "日K", "quote": "实时行情", "index": "指数", "index_minute": "指数分时",
          "market_stats": "市场情绪", "info": "个股资料", "pool": "股池", "extras": "扩展数据"}


def note_health(name: str, what: str, ok: bool, error: str | None = None) -> None:
    from .util import ts
    h = SOURCE_HEALTH.setdefault(name, {})
    if ok:
        h.update({"ok_at": ts(), "ok_what": CAP_CN.get(what, what)})
    else:
        h.update({"err_at": ts(), "error": (error or "")[:300], "err_what": CAP_CN.get(what, what)})


class Market:
    """分时、日K、交易日历的统一入口。内存里缓存本进程已取过的数据。"""

    def __init__(self, sources: dict[str, DataSource], cfg: dict, data_dir):
        self.sources = sources
        self.cfg = cfg
        self.data_dir = data_dir
        self._minute: dict[tuple, dict] = {}
        self._daily: dict[str, list] = {}

    def _ordered(self, names: list[str], cap: str) -> list[DataSource]:
        out = [self.sources[n] for n in names if n in self.sources and cap in self.sources[n].capabilities]
        # 其余有该能力的源当兜底
        out += [s for n, s in self.sources.items() if cap in s.capabilities and s not in out]
        return out

    def minute(self, code: str, date: str) -> tuple[dict | None, str | None]:
        """返回 (分时, 错误)。按 market.minute_sources 顺序尝试。"""
        key = (code, date)
        if key in self._minute:
            return self._minute[key], None
        errs = []
        for s in self._ordered(self.cfg["market"].get("minute_sources", []), "minute"):
            try:
                d = s.get_minute(code, date)
                if d and d.get("points"):
                    self._minute[key] = d
                    note_health(s.name, "minute", True)
                    return d, None
                errs.append(f"{s.name}: 分时为空")
            except NotSupported:
                continue
            except Exception as e:  # noqa: BLE001
                errs.append(f"{s.name}: {str(e)[:160]}")
                note_health(s.name, "minute", False, str(e))
        return None, ("分时拉取失败（" + "；".join(errs) + "）") if errs else "没有可用的分时数据源"

    def daily(self, code: str, end: str) -> tuple[list[dict], str | None]:
        """截至 end（含）的日K，按日期升序。"""
        end = normalize_date(end)
        key = f"{code}@{end}"
        if key in self._daily:
            return self._daily[key], None
        start = (_date.fromisoformat(end) - timedelta(days=int(self.cfg["market"].get("daily_lookback_days", 150)))).isoformat()
        errs = []
        for s in self._ordered(self.cfg["market"].get("daily_sources", []), "daily"):
            try:
                bars = [b for b in s.get_daily(code, start, end) if b.get("close")]
                if bars:
                    self._daily[key] = bars
                    self._remember_dates([b["date"] for b in bars])
                    note_health(s.name, "daily", True)
                    return bars, None
                errs.append(f"{s.name}: 日K为空")
            except NotSupported:
                continue
            except Exception as e:  # noqa: BLE001
                errs.append(f"{s.name}: {str(e)[:160]}")
                note_health(s.name, "daily", False, str(e))
        return [], ("日K拉取失败（" + "；".join(errs) + "）") if errs else "没有可用的日K数据源（启用 akshare）"

    def daily_series(self, code: str, date: str, today_fields: dict | None = None) -> tuple[list[dict], str | None, bool]:
        """日K序列，最后一根对应 date。当天日K还没出时用股池/分时字段拼一根。返回 (bars, 错误, 是否拼接)。"""
        bars, err = self.daily(code, date)
        if err and not bars:
            return [], err, False
        bars = [b for b in bars if b["date"] <= date]
        if bars and bars[-1]["date"] == date:
            return bars, None, False
        f = today_fields or {}
        price = f.get("minute_last") or f.get("price")
        if not price:
            return bars, f"日K没有 {date} 这一天，也没有现价可拼接", False
        bar = {"date": date, "open": f.get("open_price") or f.get("minute_open") or price,
               "high": max(f.get("day_high") or price, price), "low": min(f.get("day_low") or price, price),
               "close": price, "volume": None, "amount": (f.get("amount_yi") or 0) * 1e8 or None}
        return bars + [bar], None, True

    # ---------- 标准数据接口：行情 / 指数 / 指数分时 / 市场情绪（按 market.quote_sources 顺序，失败换下一个） ----------
    def _try(self, cap: str, fn, order_key: str = "quote_sources"):
        errs = []
        for s in self._ordered(self.cfg["market"].get(order_key, []), cap):
            try:
                v = fn(s)
                if v:
                    note_health(s.name, cap, True)
                    return v, s.name, None
                errs.append(f"{s.name}: 空")
            except NotSupported:
                continue
            except Exception as e:  # noqa: BLE001
                errs.append(f"{s.name}: {str(e)[:160]}")
                note_health(s.name, cap, False, str(e))
        return None, None, ("；".join(errs) if errs else f"没有提供 {cap} 的数据源（启用 akshare 或接入标准接口）")

    def quotes(self, codes: list[str]) -> tuple[dict, str | None, str | None]:
        """返回 ({6位代码: quote}, 数据源, 错误)。"""
        v, src, err = self._try("quote", lambda s: s.get_quotes(codes))
        return v or {}, src, err

    def indices(self, codes: list[str] | None = None) -> tuple[list, str | None, str | None]:
        v, src, err = self._try("index", lambda s: s.get_indices(codes))
        return v or [], src, err

    def index_minute(self, code: str, date: str) -> tuple[dict | None, str | None, str | None]:
        return self._try("index_minute", lambda s: (lambda d: d if d and d.get("points") else None)(s.get_index_minute(code, date)))

    def market_stats(self, date: str) -> tuple[dict | None, str | None, str | None]:
        return self._try("market_stats", lambda s: s.get_market_stats(date))

    # ---------- 新股无涨跌幅限制检测 ----------
    def stock_minute(self, code: str, date: str, ttl: float = 90.0) -> tuple[dict | None, str | None, str | None]:
        """个股分时（首页股池走势用）。依次尝试：腾讯（一只一次请求、只有最近一个交易日，不占 gushi 的次数）
        → 东方财富 trends2（httpx 直连，一次请求且自带昨收）→ akshare 分时（要走限速门控，还要补拉日K取昨收）。
        当天数据缓存 ttl 秒。返回 (分时, 来源, 错误)。"""
        import json
        import time as _time

        global _QQ_DOWN_UNTIL, _EM_DOWN_UNTIL, _EM_FAILS
        from .dataproto import to_std_code
        f = Path(self.data_dir) / "cache" / "pool_minute" / date / f"{code}.json"
        offline = any(getattr(s, "offline", False) for s in self.sources.values())
        if f.exists():
            age = _time.time() - f.stat().st_mtime
            if offline or date < today_str() or age < ttl:
                try:
                    return json.loads(f.read_text("utf-8")), "缓存", None
                except ValueError:
                    pass
        if offline:
            return None, None, "离线模式"
        errs: list[str] = []

        # 0) 首选：配置里排在前面的行情数据源（如 eltdx），一次 7709 请求就能拿到分时
        #    （插件自带磁盘缓存，重复刷新不打主站）。akshare / gushi 走限速门控还要补日K，
        #    留在下面的直连链里兜底，不在这里试。失败或熔断时立刻跳过，不会拖慢首页。
        for s in self._ordered(self.cfg["market"].get("minute_sources", []), "minute"):
            if getattr(s, "type_name", "") in ("akshare", "gushi"):
                continue
            try:
                d0 = s.get_minute(code, date)
                if d0 and d0.get("points"):
                    d0["code"] = code
                    f.parent.mkdir(parents=True, exist_ok=True)
                    f.write_text(json.dumps(d0, ensure_ascii=False), "utf-8")
                    note_health(s.name, "minute", True)
                    return d0, s.name, None
            except Exception as e:  # noqa: BLE001
                errs.append(f"{s.name}：{str(e)[:100]}")
                note_health(s.name, "minute", False, str(e))

        d, src = None, None
        from .datasources import public_hq

        # 1) 腾讯：最快，但接口不稳定（501 反爬页）。连续传输层失败就熔断 5 分钟，
        #    别让每只票都先白跑一次。注意「这只票这天没有分时」不算故障，不能因此熔断。
        if _time.monotonic() >= _QQ_DOWN_UNTIL:
            try:
                d = public_hq.qq_minute(to_std_code(code), date)
                src = "腾讯"
            except Exception as e:  # noqa: BLE001
                errs.append(f"腾讯：{str(e)[:100]}")
                if _transport_error(e):
                    _QQ_DOWN_UNTIL = _time.monotonic() + 300
                    log.debug("腾讯分时不可用，5 分钟内跳过：%s", e)
                d = None
        else:
            errs.append("腾讯：熔断中")

        # 2) 东方财富 trends2：一次请求，返回里自带 preClose，不用再补拉日K。
        #    东财对并发很敏感（连发几十个会直接断连并临时封 IP），所以连续 3 次传输层失败就熔断 5 分钟。
        if not d or not d.get("points"):
            if _time.monotonic() >= _EM_DOWN_UNTIL:
                try:
                    d = public_hq.em_trends(to_std_code(code), date)
                    src = "东方财富"
                    _EM_FAILS = 0
                except Exception as e:  # noqa: BLE001
                    errs.append(f"东方财富：{str(e)[:100]}")
                    if _transport_error(e):
                        _EM_FAILS += 1
                        if _EM_FAILS >= 3:
                            _EM_DOWN_UNTIL = _time.monotonic() + 300
                            log.debug("东方财富分时连续失败 %d 次，5 分钟内跳过：%s", _EM_FAILS, e)
                    d = None
            else:
                errs.append("东方财富：熔断中")

        # 3) 最后才用 akshare（限速门控 + 补昨收日K，最慢）。东财整体熔断时它也一样取不到，直接跳过。
        if (not d or not d.get("points")) and _time.monotonic() >= _EM_DOWN_UNTIL:
            for s in self._ordered(["akshare"], "minute"):
                if getattr(s, "type_name", "") != "akshare":
                    continue
                try:
                    d = s.get_minute(code, date)
                    src = s.name
                    break
                except Exception as e2:  # noqa: BLE001
                    errs.append(f"{s.name}：{str(e2)[:100]}")
        if not d or not d.get("points"):
            return None, None, "；".join(errs) or "没有分时"
        d["code"] = code          # 统一成 6 位代码（各数据源给的可能是 600519.SH）
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(d, ensure_ascii=False), "utf-8")
        return d, src, None

    # ---------- 个股资料 / 实验性数据 ----------
    def stock_info(self, code: str) -> tuple[dict, str | None]:
        """个股资料（行业、上市日、股本、市值），磁盘缓存 7 天。拿不到返回 ({}, 原因)。"""
        f = Path(self.data_dir) / "cache" / "stock_info" / f"{code}.json"
        offline = any(getattr(s, "offline", False) for s in self.sources.values())
        if f.exists() and (offline or _time.time() - f.stat().st_mtime < 7 * 86400):
            d = read_json(f, None)
            if isinstance(d, dict):
                return d, None
        if offline:
            return {}, "离线模式，没有缓存的个股资料"
        errs = []
        for s in self.sources.values():
            try:
                d = s.get_stock_info(code)
            except NotSupported:
                continue
            except Exception as e:  # noqa: BLE001
                errs.append(f"{s.name}：{str(e)[:120]}")
                continue
            if d:
                write_json(f, d)
                return d, None
        return {}, "；".join(errs) or "没有数据源提供个股资料"

    def extras(self, kind: str, date: str, ttl: float = 60) -> tuple[dict[str, dict] | None, str | None]:
        """实验性数据（竞价/封单/主力资金/龙虎榜）：整个市场一次取，按天缓存；当天的 ttl 秒内复用。"""
        key = (kind, date)
        mem = getattr(self, "_extras", None)
        if mem is None:
            mem = self._extras = {}
        if key in mem and _time.time() - mem[key][0] < ttl:
            return mem[key][1], None
        f = Path(self.data_dir) / "cache" / "extras" / date / f"{kind}.json"
        offline = any(getattr(s, "offline", False) for s in self.sources.values())
        if f.exists() and (offline or date < today_str() or _time.time() - f.stat().st_mtime < ttl):
            d = read_json(f, None)
            if isinstance(d, dict):
                mem[key] = (_time.time(), d)
                return d, None
        if offline:
            return None, "离线模式"
        errs = []
        for s in self.sources.values():
            try:
                d = s.get_extra(kind, date)
            except NotSupported:
                continue
            except Exception as e:  # noqa: BLE001
                errs.append(f"{s.name}：{str(e)[:120]}")
                continue
            if d is not None:
                write_json(f, d)
                mem[key] = (_time.time(), d)
                return d, None
        return None, "；".join(errs) or "没有数据源提供这项数据"

    def listing_info(self, code: str, date: str) -> tuple[_date | None, int | None]:
        """用日K推断上市日：拉到的第一根日K明显晚于请求起点，就把它当作上市首日。返回 (上市日, 当天是上市第几天)。"""
        bars, _err = self.daily(code, date)
        if not bars:
            return None, None
        lookback = int(self.cfg["market"].get("daily_lookback_days", 150))
        start = _date.fromisoformat(date) - timedelta(days=lookback)
        first = _date.fromisoformat(bars[0]["date"])
        if (first - start).days <= 15:
            return None, None          # 老股
        rank = sum(1 for b in bars if b["date"] <= date)
        if bars[-1]["date"] < date:
            rank += 1                  # 当天日K未出
        return first, rank

    # ---------- 交易日历 ----------
    def _cal_file(self):
        return self.data_dir / "cache" / "trade_dates.json"

    def _remember_dates(self, dates: list[str]) -> None:
        cur = set(read_json(self._cal_file(), []) or [])
        if set(dates) - cur:
            write_json(self._cal_file(), sorted(cur | set(dates)))

    def known_trade_dates(self) -> list[str]:
        return read_json(self._cal_file(), []) or []

    def is_trading_day(self, d: str | None = None) -> bool | None:
        """True/False/None(无法确认)。依据：gushi meta 的 trading_date、日K累积的交易日、周末。"""
        d = d or today_str()
        dd = _date.fromisoformat(d)
        if dd.weekday() >= 5:
            return False
        known = self.known_trade_dates()
        if d in known:
            return True
        if known and d < known[-1]:
            return False   # 已知区间内的工作日却没有行情 = 节假日
        for s in self.sources.values():
            if "meta" in s.capabilities:
                try:
                    m = s.meta() or {}
                    if m.get("trading_date") == d:
                        return True
                except Exception:  # noqa: BLE001
                    continue
        return None

    def latest_trading_date(self) -> str | None:
        """最近一个有数据的交易日（不晚于今天；模拟时间下也成立）。"""
        for s in self.sources.values():
            if "meta" in s.capabilities:
                try:
                    m = s.meta() or {}
                    if m.get("trading_date") and m["trading_date"] <= today_str():
                        return m["trading_date"]
                except Exception as e:  # noqa: BLE001
                    log.debug("meta 不可用：%s", e)
        known = [d for d in self.known_trade_dates() if d <= today_str()]
        if known:
            return known[-1]
        d = today_cn()
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d.isoformat()

    def trading_days_between(self, a: str, b: str) -> int:
        """a 之后到 b（含）经过了几个交易日。优先用已知交易日，缺的部分按工作日算。"""
        if b <= a:
            return 0
        known = set(self.known_trade_dates())
        last_known = max(known) if known else ""
        n = 0
        d = _date.fromisoformat(a) + timedelta(days=1)
        end = _date.fromisoformat(b)
        while d <= end:
            s = d.isoformat()
            if s in known or (s > last_known and d.weekday() < 5):
                n += 1
            d += timedelta(days=1)
        return n


def limit_info(code: str, name: str, preclose: float, trade_date: str, no_limit: bool = False) -> dict[str, Any]:
    td = _date.fromisoformat(trade_date)
    pct = pl.price_limit_pct(code, td, is_risk_warning=pl.is_risk_warning_name(name))
    up, dn = pl.limit_prices(preclose, pct)
    out = {"preclose": round(preclose, 2), "limit_pct": pct * 100, "limit_up": up, "limit_down": dn}
    if no_limit:
        out.update(no_limit=1.0, limit_up=None, limit_down=None)
    return out
