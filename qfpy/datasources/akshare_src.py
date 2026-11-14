"""akshare 免费行情（东方财富为主、新浪/腾讯兜底）：分时、日K、涨停池/强势股池。

akshare 是可选依赖；没装时本数据源的每个方法都会报错，系统自动改用下一个数据源。
"""
from __future__ import annotations

import math
import threading
import time

from ..util import log, market_prefix, normalize_date, today_str
from .base import DataSource, PoolResult, register_source

_ak = None
_ak_lock = threading.Lock()


def ak():
    global _ak
    with _ak_lock:
        if _ak is None:
            try:
                import akshare  # type: ignore
            except ImportError as e:
                raise RuntimeError("未安装 akshare（pip install akshare）") from e
            _ak = akshare
        return _ak


_EM_DOWN_UNTIL = 0.0


def _f(v):
    try:
        x = float(v)
        return None if math.isnan(x) else x
    except (TypeError, ValueError):
        return None


POOLS = {
    "zt_pool": ("涨停池", "东方财富当日涨停股池"),
    "strong_pool": ("强势股池", "东方财富强势股池（创新高/连涨）"),
}


# akshare 里内部用 MiniRacer 执行 JS 的接口（多线程并发调用会段错误），见 AkshareSource._call
_JS_LOCK = threading.Lock()
_JS_FUNCS = {"stock_zh_a_daily", "stock_zh_index_daily", "tool_trade_date_hist_sina", "stock_zh_a_minute",
             "stock_zh_a_cdr_daily", "stock_zh_kcb_daily", "stock_zh_a_spot"}


@register_source
class AkshareSource(DataSource):
    type_name = "akshare"
    title = "akshare 免费行情"
    description = "东方财富/新浪/腾讯公开行情：实时行情、1 分钟分时、日K、指数与分时、市场情绪、涨停池/强势股池。无需 token，稳定性一般"
    capabilities = ("pool", "quote", "minute", "daily", "index", "index_minute", "market_stats")
    config_fields = [{"name": "min_interval_ms", "hint": "两次调用最小间隔（毫秒）"}, {"name": "cache_ttl_secs", "hint": "当天数据缓存秒数"}]

    def _call(self, fn, *a, **kw):
        if self.offline:
            raise RuntimeError("离线模式，不请求 akshare")
        self.limiter.wait()
        if getattr(fn, "__name__", "") in _JS_FUNCS:
            # 新浪日K等接口在 akshare 内部用 MiniRacer（V8）解码，多线程同时调用会让整个进程段错误崩溃，
            # 所以这些调用全进程串行。
            with _JS_LOCK:
                return fn(*a, **kw)
        return fn(*a, **kw)

    def list_pools(self) -> list[dict]:
        return [{"key": k, "name": n, "desc": d} for k, (n, d) in POOLS.items()]

    # ---------- 个股资料 / 第二批实验性数据 ----------
    def get_stock_info(self, code: str) -> dict:
        df = self._call(ak().stock_individual_info_em, symbol=code)
        kv = {str(r["item"]).strip(): r["value"] for _, r in df.iterrows()}
        out: dict = {}
        if kv.get("行业"):
            out["industry"] = str(kv["行业"]).strip()
        ld = str(kv.get("上市时间") or "").strip()
        if len(ld) == 8 and ld.isdigit():
            out["listing_date"] = f"{ld[:4]}-{ld[4:6]}-{ld[6:]}"
        for src, dst, scale in (("总股本", "total_shares", 1), ("流通股", "float_shares", 1),
                                ("总市值", "total_mv", 1e8), ("流通市值", "float_mv", 1e8)):
            v = _f(kv.get(src))
            if v:
                out[dst] = v / scale
        return out

    def get_extra(self, kind: str, date: str) -> dict[str, dict]:
        from ..extras import fetch_akshare_extra
        return fetch_akshare_extra(self, ak(), kind, date)

    def get_pool(self, key: str, date: str | None) -> PoolResult:
        if key not in POOLS:
            raise RuntimeError(f"akshare 没有股池 {key}（可选 {', '.join(POOLS)}）")
        date = normalize_date(date or today_str())
        d8 = date.replace("-", "")
        rel = f"pools/{date}/{key}.json"
        rows = self.cache.get(rel, self.cache.ttl_for(date))
        if rows is None:
            fn = ak().stock_zt_pool_em if key == "zt_pool" else ak().stock_zt_pool_strong_em
            df = self._call(fn, date=d8)
            rows = []
            for _, r in df.iterrows():
                price, chg = _f(r.get("最新价")), _f(r.get("涨跌幅"))
                mv, fmv = _f(r.get("总市值")), _f(r.get("流通市值"))
                fields = {"price": price, "change_rate": chg, "market_value": mv / 1e8 if mv else None,
                          "total_mv": mv / 1e8 if mv else None, "float_mv": fmv / 1e8 if fmv else None,
                          "turnover": _f(r.get("换手率")),
                          "broke_times": _f(r.get("炸板次数")), "seal_amount": _f(r.get("封板资金"))}
                rows.append({"code": str(r.get("代码")).zfill(6), "name": str(r.get("名称", "")),
                             "fields": {k: v for k, v in fields.items() if v is not None}})
            self.cache.put(rel, rows)
        return PoolResult(key=f"{self.name}:{key}", name=POOLS[key][0], trading_date=date, stocks=rows)

    def get_minute(self, code: str, date: str) -> dict:
        date = normalize_date(date)
        rel = f"minute/{date.replace('-', '')}/{code}.json"
        cached = self.cache.get(rel, self.cache.ttl_for(date))
        if cached is not None:
            return cached
        df = self._call(ak().stock_zh_a_hist_min_em, symbol=code, start_date=f"{date} 09:15:00",
                        end_date=f"{date} 15:00:00", period="1", adjust="")
        if df is None or len(df) == 0:
            raise RuntimeError(f"akshare 无 {code} {date} 分时")
        pts = []
        for _, r in df.iterrows():
            t = str(r["时间"])
            if not t.startswith(date):
                continue
            pts.append({"t": t[11:16], "p": _f(r["收盘"]), "avg": _f(r.get("均价")),
                        "vol": _f(r["成交量"]), "amt": _f(r["成交额"]),
                        "h": _f(r["最高"]), "l": _f(r["最低"])})
        if not pts:
            raise RuntimeError(f"akshare 无 {code} {date} 分时")
        highs = [p["h"] for p in pts if p["h"]]
        lows = [p["l"] for p in pts if p["l"]]
        # 昨收：用日K补（分时接口不带昨收）
        preclose = None
        try:
            bars = self.get_daily(code, _shift(date, -20), date)
            prev = [b for b in bars if b["date"] < date]
            if prev:
                preclose = prev[-1]["close"]
        except Exception as e:  # noqa: BLE001
            log.debug("akshare 补昨收失败 %s：%s", code, e)
        d = {"code": code, "date": date, "preclose": preclose, "open": _f(df.iloc[0]["开盘"]),
             "high": max(highs) if highs else None, "low": min(lows) if lows else None,
             "points": [{k: p[k] for k in ("t", "p", "avg", "vol", "amt")} for p in pts], "source": self.name}
        self.cache.put(rel, d)
        return d

    def get_daily(self, code: str, start: str, end: str) -> list[dict]:
        """不复权日K（与分时/委托价格口径一致）。缓存按代码整体存放，增量补齐。"""
        start, end = normalize_date(start), normalize_date(end)
        rel = f"daily/{code}.json"
        cached = self.cache.get(rel, None) or {}
        bars = cached.get("bars", [])
        cf, ct = cached.get("covered_from", "9999"), cached.get("covered_to", "0000")
        today = today_str()
        fresh = cf <= start and ct >= end and (end < today or time.time() - cached.get("fetched_ts", 0) < self.cache.ttl)
        if not fresh and not self.offline:
            fs = min(start, cf) if bars else start
            new = self._fetch_daily(code, fs, max(end, ct) if bars else end)
            merged = {b["date"]: b for b in bars}
            merged.update({b["date"]: b for b in new})
            bars = [merged[k] for k in sorted(merged)]
            self.cache.put(rel, {"bars": bars, "covered_from": fs, "covered_to": min(max(end, ct), today) if bars else end,
                                 "fetched_ts": time.time()})
        return [b for b in bars if start <= b["date"] <= end]

    def _fetch_daily(self, code: str, start: str, end: str) -> list[dict]:
        s8, e8 = start.replace("-", ""), end.replace("-", "")
        errors = []
        try:
            df = self._call(ak().stock_zh_a_hist, symbol=code, period="daily", start_date=s8, end_date=e8, adjust="")
            return [{"date": str(r["日期"])[:10], "open": _f(r["开盘"]), "high": _f(r["最高"]), "low": _f(r["最低"]),
                     "close": _f(r["收盘"]), "volume": (_f(r["成交量"]) or 0) * 100, "amount": _f(r["成交额"]),
                     **({"turnover": _f(r.get("换手率"))} if _f(r.get("换手率")) is not None else {})}
                    for _, r in df.iterrows()]
        except Exception as e:  # noqa: BLE001
            errors.append(f"东方财富：{e}")
        try:
            df = self._call(ak().stock_zh_a_daily, symbol=market_prefix(code) + code, start_date=s8, end_date=e8, adjust="")
            return [{"date": str(r["date"])[:10], "open": _f(r["open"]), "high": _f(r["high"]), "low": _f(r["low"]),
                     "close": _f(r["close"]), "volume": _f(r["volume"]), "amount": _f(r["amount"]),
                     # 新浪：outstanding_share = 流通股本（股），turnover = 换手率（小数）
                     **({"float_shares": _f(r.get("outstanding_share"))} if _f(r.get("outstanding_share")) else {}),
                     **({"turnover": _f(r.get("turnover")) * 100} if _f(r.get("turnover")) is not None else {})}
                    for _, r in df.iterrows()]
        except Exception as e:  # noqa: BLE001
            errors.append(f"新浪：{e}")
        raise RuntimeError("akshare 日K拉取失败：" + "；".join(x[:120] for x in errors))


    # ---------- 标准数据接口新增：实时行情 / 指数 / 指数分时 / 市场情绪 ----------
    # 实时行情走新浪 hq 接口（一次可查多只），指数分时走东方财富 trends2，市场情绪用 akshare 的乐咕乐股统计。
    def get_quotes(self, codes: list[str]) -> dict[str, dict]:
        from . import public_hq
        if self.offline:
            raise RuntimeError("离线模式，不请求 akshare")
        self.limiter.wait()
        return {q["code"].split(".")[0]: q for q in public_hq.sina_quotes([f"{c}.{market_prefix(c).upper()}" for c in codes])}

    def get_indices(self, codes: list[str] | None = None) -> list[dict]:
        from ..dataproto import DEFAULT_INDICES, index_std_code
        from . import public_hq
        if self.offline:
            raise RuntimeError("离线模式，不请求 akshare")
        cs = [index_std_code(c) for c in codes] if codes else [c for c, _ in DEFAULT_INDICES]
        rel = "indices/" + "_".join(cs) + ".json"
        hit = self.cache.get(rel, 10)
        if hit is not None:
            return hit
        self.limiter.wait()
        qs = public_hq.sina_quotes(cs, index=True)
        self.cache.put(rel, qs)
        return qs

    def get_index_minute(self, code: str, date: str) -> dict:
        from ..dataproto import index_std_code
        from . import public_hq
        c = index_std_code(code)
        date = normalize_date(date or today_str())
        rel = f"index_minute/{date}/{c}.json"
        hit = self.cache.get(rel, self.cache.ttl_for(date))
        if hit is not None:
            return hit
        if self.offline:
            raise RuntimeError("离线模式，不请求 akshare")
        self.limiter.wait()
        global _EM_DOWN_UNTIL
        d = None
        if time.monotonic() >= _EM_DOWN_UNTIL:
            try:
                d = public_hq.em_trends(c, date)
            except Exception as e:  # noqa: BLE001  东方财富偶尔断连 / 限流：5 分钟内改用腾讯分时（只有最近一个交易日）
                log.debug("东方财富指数分时失败，改用腾讯：%s", e)
                _EM_DOWN_UNTIL = time.monotonic() + 300
        if d is None:
            d = public_hq.qq_minute(c, date)
        d["source"] = self.name
        self.cache.put(rel, d)
        return d

    def get_market_stats(self, date: str) -> dict:
        from . import public_hq
        date = normalize_date(date or today_str())
        rel = f"market_stats/{date}.json"
        hit = self.cache.get(rel, 60 if date >= today_str() else None)
        if hit is not None:
            return hit
        if self.offline:
            raise RuntimeError("离线模式，不请求 akshare")
        self.limiter.wait()
        df = ak().stock_market_activity_legu()
        kv = {str(r["item"]).strip(): r["value"] for _, r in df.iterrows()}
        stat_date = str(kv.get("统计日期", ""))[:10]
        if date != today_str() and stat_date != date:
            raise RuntimeError(f"akshare 只有最新一个交易日（{stat_date}）的涨跌统计，没有 {date} 的")
        amount = None
        try:
            qs = public_hq.sina_quotes(["000001.SH", "399106.SZ", "899050.BJ"], index=True)
            amount = sum(q.get("amount") or 0 for q in qs) or None
        except Exception as e:  # noqa: BLE001
            log.debug("两市成交额获取失败：%s", e)

        def i(k):
            v = _f(kv.get(k))
            return int(v) if v is not None else None
        d = {"date": stat_date or date, "up": i("上涨") or 0, "down": i("下跌") or 0, "flat": i("平盘"),
             "limit_up": i("涨停"), "limit_down": i("跌停"), "suspended": i("停牌"), "amount": amount,
             "ts": int(time.time() * 1000)}
        self.cache.put(rel, d)
        return d


def _shift(date: str, days: int) -> str:
    from datetime import date as _d, timedelta
    return (_d.fromisoformat(date) + timedelta(days=days)).isoformat()
