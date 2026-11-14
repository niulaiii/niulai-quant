"""eltdx 通达信行情数据源（首选）。

eltdx（https://github.com/electkismet/eltdx）是通达信 7709 行情协议的 Python 库。
本插件把它接成牛来量化的「行情数据源」：进程内直接调用 TdxClient，单例长连主站，
不用起额外服务（eltdx 自带的 HTTP 网关是 JSON-RPC，不是本项目的 qfdp v1 标准接口）。

能力对应（规范见 qfpy/dataproto.py、docs/GUIDE.md 第四部分）：
    quote        个股实时快照    quotes.get_snapshots
    minute       个股分时        minutes.today / minutes.history
    daily        个股日K        bars.get(period="day", adjust="none")   ← 不复权，与 akshare 口径一致
    index        指数实时快照    quotes.get_snapshots（000001.SH → sh000001）
    index_minute 指数分时        minutes.today / minutes.history
    market_stats 市场情绪        quotes.legacy(880005/880001/880006)     ← 自己解析，见下方 ×10 校正

------------------------------------------------------------------------------
【×10 校正】eltdx 的 helpers.market_stat() 会把统计指数价格再乘 10，实测是错的。

2026-11-13 收盘，7709 原始值 / eltdx 结果 / 乐咕乐股真实值对照：

    字段        880005 原始    eltdx(×10)    乐咕乐股真实
    上涨          765.0          7650          744
    下跌         4748.0         47480         4415
    平盘           58.0           580           57
    涨停           38.0           380           38
    总数         5579.0         55790          eltdx all_a_shares()=5581
    停牌(推导)        8            80            8   ← 5579-765-4748-58，与真实完全一致

880005 的价格字段本身就是家数（停牌 8 的独立吻合、总数 5579 ≈ A 股 5581 都是证据），
eltdx 的 round(值 * 10) 是多乘了一次 10，把口径放大成了假象的「5.5 万只」。
所以这里不用 helpers.market_stat()，直接读 legacy 原始值按 round(值) 还原。
------------------------------------------------------------------------------

单位换算（qfdp：价格元、成交量股、成交额元、涨跌幅百分数）：
    eltdx 快照 / 分时的成交量是「手」，×100 才是股；
    分时点没有成交额，用「该分钟价格 × 该分钟成交量」近似（amt 只用于累计成交额类因子）。

失败降级：未安装 / 连不上 / 连续 3 次失败 → 熔断 5 分钟，期间直接抛异常，
由 qfpy/market.py 按 [market] 顺序自动回退到 gushi / akshare，不会拖慢首页。
"""
from __future__ import annotations

import math
import re
import threading
import time

from qfpy.datasources import DataSource, register_source
from qfpy.dataproto import DEFAULT_INDICES, code6, index_std_code, to_std_code
from qfpy.util import log, market_prefix, normalize_date, today_str

PLUGIN = {"name": "eltdx_source", "description": "eltdx 通达信 7709 行情（快照/分时/日K/指数/市场情绪）", "enabled": True}

_HHMM = re.compile(r"^\d{2}:\d{2}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MARKET_STAT_CODES = ["sh880005", "sh880001", "sh880006"]

# 全局单例：连接一次长期复用（首次约 0.7s 测速选主站，之后每次请求约 40ms）。
# 熔断状态也放在同一把锁里，避免多线程重复建连或计数错乱。
_lock = threading.RLock()
_client = None
_fails = 0
_down_until = 0.0
_COOLDOWN = 300.0
_MAX_FAILS = 3
# 主站最近交易日，按「今天」缓存一次，跨自然日自动刷新
_server_day = {"requested": None, "trading": None}


def _f(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _i(v):
    x = _f(v)
    return int(round(x)) if x is not None else None


def _client_get(cfg):
    global _client, _fails
    with _lock:
        if _client is not None:
            return _client
        if time.monotonic() < _down_until:
            raise RuntimeError("eltdx 熔断中")
        try:
            from eltdx import TdxClient
        except ImportError as e:
            raise RuntimeError("未安装 eltdx（pip install eltdx），已回退其他数据源") from e
        kw = {"timeout": float(cfg.get("timeout_secs", 3) or 3)}
        host = str(cfg.get("host") or "").strip()
        if host:
            kw["host"] = host
            kw["probe_hosts"] = False
        c = TdxClient(**kw)
        c.connect()
        _client = c
        _fails = 0
        return c


def _note_ok():
    global _fails
    with _lock:
        _fails = 0


def _note_fail(exc):
    global _fails, _down_until, _client
    with _lock:
        _fails += 1
        if _fails >= _MAX_FAILS:
            _down_until = time.monotonic() + _COOLDOWN
            _client = None
            log.warning("eltdx 连续 %d 次失败，5 分钟内跳过并回退其他数据源：%s", _fails, str(exc)[:160])


def _recent_trading_day(c):
    """主站最近交易日；进程内按「今天」缓存，跨自然日自动刷新。

    minutes.today() 在非交易日 / 盘前会返回最近交易日的 240 个点，却把
    trading_date 留空，直接采用会把 11-13 的分时标成 11-14。用握手日期
    改走 minutes.history()，日期口径才对得上。
    """
    global _server_day
    t = today_str()
    with _lock:
        if _server_day["requested"] == t:
            return _server_day["trading"]
    hs = c.session.handshake()
    ds = []
    for v in (getattr(hs, "server_date_1", None), getattr(hs, "server_date_2", None)):
        if v:
            s = v.isoformat() if hasattr(v, "isoformat") else str(v)[:10]
            if _DATE.match(s):
                ds.append(s)
    trading = max(ds) if ds else None
    with _lock:
        _server_day = {"requested": t, "trading": trading}
    return trading


def _full(c6):
    """6 位个股代码 → 通达信完整代码，如 600000 → sh600000。"""
    return market_prefix(c6) + c6


def _index_full(std):
    """000001.SH → sh000001。指数不能按代码段推交易所（000001 既是上证指数也是平安银行）。"""
    d, ex = std.upper().split(".")
    return ex.lower() + d


def _index_std(full_code):
    """sh000001 → 000001.SH。"""
    fc = full_code.lower()
    return fc[2:] + "." + fc[:2].upper()


@register_source
class EltdxSource(DataSource):
    type_name = "eltdx"
    title = "eltdx 通达信行情（7709）"
    description = "通达信 7709 主站：实时快照、分时、日K、指数与市场情绪。直连主站，单次请求约 40ms"
    capabilities = ("quote", "minute", "daily", "index", "index_minute", "market_stats")
    config_fields = [
        {"name": "timeout_secs", "hint": "7709 请求超时秒数，默认 3"},
        {"name": "host", "hint": "可选：手动指定主站 host:port（留空 = 自动测速选最快）"},
        {"name": "cache_ttl_secs", "hint": "当天分时/日K/情绪缓存秒数，默认 60"},
    ]

    def _call(self, what, fn):
        """统一入口：熔断期直接抛（让 market 立刻回退）；失败计数，成功清零。"""
        if time.monotonic() < _down_until:
            raise RuntimeError(f"eltdx 熔断中（{what}）")
        try:
            c = _client_get(self.cfg)
            r = fn(c)
        except Exception as e:
            _note_fail(e)
            raise
        _note_ok()
        return r

    # ---------- 快照（个股 / 指数共用） ----------
    def _snapshot_rows(self, full_codes):
        def fetch(c):
            try:
                return c.quotes.get_snapshots(full_codes, on_missing="skip")
            except TypeError:   # eltdx < 3.3.2 没有 on_missing 参数
                return c.quotes.get_snapshots(full_codes)
        rows = self._call("快照", fetch)
        if not rows:
            raise RuntimeError("eltdx 快照没有返回数据")
        return rows

    @staticmethod
    def _quote(row, std_code):
        """QuoteSnapshot → qfdp Quote。成交量单位是「手」，×100 换成股。"""
        price = _f(getattr(row, "last_price", None))
        if not price:
            return None
        pre = _f(getattr(row, "pre_close_price", None))
        hand = _f(getattr(row, "total_hand", None))
        out = {
            "code": std_code,
            "price": price,
            "preclose": pre,
            "open": _f(getattr(row, "open_price", None)),
            "high": _f(getattr(row, "high_price", None)),
            "low": _f(getattr(row, "low_price", None)),
            "volume": hand * 100 if hand is not None else None,
            "amount": _f(getattr(row, "amount", None)),
            "change": round(price - pre, 4) if pre else None,
            "change_pct": round((price - pre) / pre * 100, 4) if pre else None,
            "ts": int(time.time() * 1000),
        }
        return {k: v for k, v in out.items() if v is not None}

    def get_quotes(self, codes):
        cs = [code6(str(c)) for c in codes]
        if not cs:
            return {}
        rows = self._snapshot_rows([_full(c) for c in cs])
        out = {}
        for r in rows:
            c6 = str(getattr(r, "code", "") or "").zfill(6)[-6:]
            if not c6.isdigit():
                continue
            q = self._quote(r, to_std_code(c6))
            if q:
                out[c6] = q
        if not out:
            raise RuntimeError("eltdx 快照没有可用记录")
        return out

    def get_indices(self, codes=None):
        cs = [index_std_code(str(c)) for c in codes] if codes else [c for c, _ in DEFAULT_INDICES]
        rows = self._snapshot_rows([_index_full(c) for c in cs])
        out, seen = [], set()
        for r in rows:
            fc = str(getattr(r, "full_code", "") or "")
            if len(fc) < 8:
                continue
            std = _index_std(fc)
            if std in seen:
                continue
            q = self._quote(r, std)
            if q:
                seen.add(std)
                out.append(q)
        if not out:
            raise RuntimeError("eltdx 指数快照没有可用记录")
        return out

    # ---------- 分时 ----------
    def _minute_series(self, full, date):
        def fetch(c):
            d = date
            if d == today_str():
                # 非交易日 / 盘前：minutes.today() 给的是最近交易日数据但 trading_date 为空，
                # 用主站握手日期改走 history，返回的 date 才是真实交易日。
                rd = _recent_trading_day(c)
                if rd:
                    d = rd
            if d == today_str():
                return c.minutes.today(full)
            try:
                return c.minutes.history(full, d)
            except Exception:
                return c.minutes.recent(full, d)
        return self._call("分时", fetch)

    @staticmethod
    def _minute_dict(series, code_out, date, source):
        pts = []
        for p in (getattr(series, "points", None) or ()):
            t = str(getattr(p, "time_label", "") or "")
            if not _HHMM.match(t):
                continue
            px = _f(getattr(p, "price", None))
            if px is None:
                continue
            vol = (_f(getattr(p, "volume", None)) or 0) * 100   # 手 → 股
            pts.append({"t": t, "p": px, "avg": _f(getattr(p, "avg_price", None)),
                        "vol": vol, "amt": round(px * vol, 2)})
        if not pts:
            raise RuntimeError(f"eltdx 无 {code_out} {date} 分时")
        prices = [x["p"] for x in pts]
        td = getattr(series, "trading_date", None)
        date_out = td.isoformat() if hasattr(td, "isoformat") else (str(td)[:10] if td else date)
        if not _DATE.match(date_out):
            date_out = date
        return {"code": code_out, "date": date_out,
                "preclose": _f(getattr(series, "prev_close", None)),
                "open": pts[0]["p"], "high": max(prices), "low": min(prices),
                "points": pts, "source": source}

    def get_minute(self, code, date):
        c6 = code6(str(code))
        d = normalize_date(date or today_str())
        rel = f"minute/{d.replace('-', '')}/{c6}.json"
        hit = self.cache.get(rel, self.cache.ttl_for(d))
        if hit is not None:
            return hit
        if self.offline:
            raise RuntimeError("离线模式，不请求 eltdx")
        series = self._minute_series(_full(c6), d)
        out = self._minute_dict(series, c6, d, self.name)
        self.cache.put(rel, out)
        return out

    def get_index_minute(self, code, date):
        std = index_std_code(str(code))
        d = normalize_date(date or today_str())
        rel = f"index_minute/{d}/{std.replace('.', '_')}.json"
        hit = self.cache.get(rel, self.cache.ttl_for(d))
        if hit is not None:
            return hit
        if self.offline:
            raise RuntimeError("离线模式，不请求 eltdx")
        series = self._minute_series(_index_full(std), d)
        out = self._minute_dict(series, std, d, self.name)
        self.cache.put(rel, out)
        return out

    # ---------- 日K ----------
    def get_daily(self, code, start, end):
        c6 = code6(str(code))
        start, end = normalize_date(start), normalize_date(end)
        rel = f"daily/{c6}.json"
        cached = self.cache.get(rel, self.cache.ttl_for(end))
        if cached is not None:
            bars = [b for b in cached if start <= b["date"] <= end]
            if bars:
                return bars
        if self.offline:
            raise RuntimeError("离线模式，不请求 eltdx")
        series = self._call("日K", lambda c: c.bars.get(_full(c6), period="day", adjust="none", count=800))
        bars = []
        for b in (getattr(series, "bars", None) or ()):
            t = getattr(b, "time", None)
            ds = t.date().isoformat() if hasattr(t, "date") else str(t)[:10]
            if not _DATE.match(ds):
                continue
            o, h, l, cl = (_f(getattr(b, k, None)) for k in ("open", "high", "low", "close"))
            if None in (o, h, l, cl):
                continue
            bars.append({"date": ds, "open": o, "high": h, "low": l, "close": cl,
                         "volume": _f(getattr(b, "volume_wire_value", None)),
                         "amount": _f(getattr(b, "amount", None))})
        bars.sort(key=lambda x: x["date"])
        if not bars:
            raise RuntimeError(f"eltdx 无 {c6} 日K")
        self.cache.put(rel, bars)
        return [b for b in bars if start <= b["date"] <= end]

    # ---------- 市场情绪（已校正 eltdx helpers.market_stat() 的 ×10） ----------
    def get_market_stats(self, date):
        d = normalize_date(date or today_str())
        rel = f"market_stats/{d}.json"
        hit = self.cache.get(rel, 60 if d >= today_str() else None)
        if hit is not None:
            return hit
        if self.offline:
            raise RuntimeError("离线模式，不请求 eltdx")

        def fetch(c):
            qs = c.quotes.legacy(_MARKET_STAT_CODES)
            hs = c.session.handshake()
            dates = [str(x) for x in (getattr(hs, "server_date_1", None),
                                      getattr(hs, "server_date_2", None)) if x]
            return qs, (max(dates) if dates else None)

        qs, server_date = self._call("市场情绪", fetch)
        if not qs or len(qs) < 3:
            raise RuntimeError("eltdx 没有返回统计指数 880005/880001/880006")
        # 主站只提供「最近交易日」的统计，别拿它冒充更早的日期
        if server_date and d != server_date and d != today_str():
            raise RuntimeError(f"eltdx 只有最近交易日（{server_date}）的涨跌统计，没有 {d} 的")
        q, lim = qs[0], qs[2]
        up, down = _i(q.last_price), _i(q.open_price)
        total, flat = _i(q.high_price), _i(q.low_price)
        if up is None or down is None:
            raise RuntimeError("eltdx 市场情绪字段缺失")
        suspended = max(0, (total or 0) - up - down - (flat or 0))
        out = {"date": server_date if server_date and _DATE.match(server_date) else d,
               "up": up, "down": down, "flat": flat,
               "limit_up": _i(lim.last_price), "limit_down": _i(lim.open_price),
               "suspended": suspended, "amount": _f(q.amount), "ts": int(time.time() * 1000)}
        out = {k: v for k, v in out.items() if v is not None}
        self.cache.put(rel, out)
        return out

    def test_connection(self):
        out = super().test_connection()
        t0 = time.monotonic()
        try:
            s = self.get_market_stats(today_str())
            out.append({"check": "市场情绪", "ok": True, "ms": int((time.monotonic() - t0) * 1000),
                        "detail": f"上涨 {s['up']} / 下跌 {s['down']} / 涨停 {s.get('limit_up')}（×10 已校正）"})
        except Exception as e:  # noqa: BLE001
            out.append({"check": "市场情绪", "ok": False, "ms": int((time.monotonic() - t0) * 1000),
                        "detail": str(e)[:200]})
        return out
