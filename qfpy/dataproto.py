"""标准数据接口规范（QF Data Provider Protocol，简称 qfdp v1）。

一套规范，两种接法：

1. **HTTP JSON 协议**：任何语言写的服务只要按本规范返回 JSON，就能在网页上填 URL + token 接进来
   （数据源类型 ``standard``）。详细说明见 docs/DATA_PROVIDER.md，机器可读描述见 :func:`openapi_spec`。
2. **Python 插件接口**：继承 :class:`qfpy.datasources.base.DataSource`，按能力实现下面这些方法，
   用 ``@register_source`` 注册。内置的 gushi / akshare / 自选股适配器都按这套方法实现。

   ============== ======================================= ==================================
   能力            方法                                     返回（内部格式）
   ============== ======================================= ==================================
   pool           list_pools() / get_pool(key, date)        [{key,name,desc}] / PoolResult
   quote          get_quotes(codes)                         {6位代码: Quote}
   minute         get_minute(code, date)                    MinuteSeries
   daily          get_daily(code, start, end)               [DailyBar]
   index          get_indices(codes)                        [Quote]（code 用带交易所后缀的标准代码）
   index_minute   get_index_minute(code, date)              MinuteSeries
   market_stats   get_market_stats(date)                    MarketStats
   ============== ======================================= ==================================

本模块提供：代码格式转换、各数据类型的字段校验、错误码、把任意 DataSource 包装成标准 HTTP 服务
（:func:`make_provider_app`，示例提供方服务就是用它写的），以及 OpenAPI 描述。
"""
from __future__ import annotations

import math
import re
import time
from typing import Any

PROTOCOL = "qfdp"
VERSION = "1.0"

# 规范里定义的能力（HTTP 的 /meta.capabilities 和 DataSource.capabilities 用同一套名字）
CAPABILITIES = ("pool", "quote", "minute", "daily", "index", "index_minute", "market_stats")
CAPABILITY_DOCS = {
    "pool": "股池列表和成分", "quote": "个股实时行情", "minute": "个股分时（1 分钟）", "daily": "个股日K（不复权）",
    "index": "指数实时行情", "index_minute": "指数分时", "market_stats": "市场情绪（涨跌家数、涨跌停、成交额）",
}

# 错误码 → HTTP 状态码
ERROR_CODES = {
    "BAD_REQUEST": 400,      # 参数缺失或格式不对
    "UNAUTHORIZED": 401,     # 没带 token 或 token 不对
    "FORBIDDEN": 403,        # token 对但没有权限
    "NOT_FOUND": 404,        # 代码 / 股池 / 日期没有数据
    "RATE_LIMITED": 429,     # 调用太频繁，可带 retry_after 秒
    "INTERNAL": 500,         # 提供方内部错误
    "NOT_SUPPORTED": 501,    # 不提供这类数据（能力里没声明）
    "UPSTREAM_ERROR": 502,   # 提供方自己的上游失败
    "UNAVAILABLE": 503,      # 暂时不可用（维护、非交易时段没有数据等）
}

MAX_CODES_PER_QUOTE = 200          # /quotes 一次最多多少个代码
MAX_PAGE_SIZE = 500                # 分页每页上限
DEFAULT_PAGE_SIZE = 200

# 主要指数（首页默认显示）
DEFAULT_INDICES = [
    ("000001.SH", "上证指数"), ("399001.SZ", "深证成指"), ("399006.SZ", "创业板指"),
    ("000688.SH", "科创50"), ("000300.SH", "沪深300"), ("899050.BJ", "北证50"),
]

_STD_RE = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$")


class ProtocolError(Exception):
    """按规范返回的错误（或客户端发现返回不合规范）。"""

    def __init__(self, code: str, message: str, http_status: int | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code if code in ERROR_CODES else "INTERNAL"
        self.message = message
        self.http_status = http_status or ERROR_CODES.get(self.code, 500)


# ---------------- 代码格式 ----------------
def stock_exchange(code6: str) -> str:
    """个股 6 位代码 → 交易所后缀（SH/SZ/BJ）。"""
    from .util import market_prefix
    return market_prefix(code6).upper()


def to_std_code(code: str) -> str:
    """个股代码 → 标准代码。已带后缀的原样返回（大写）；sh600000 / 600000 都能认。"""
    c = str(code).strip().upper()
    if _STD_RE.match(c):
        return c
    m = re.fullmatch(r"(SH|SZ|BJ)(\d{6})", c)
    if m:
        return f"{m.group(2)}.{m.group(1)}"
    if re.fullmatch(r"\d{6}", c):
        return f"{c}.{stock_exchange(c)}"
    raise ProtocolError("BAD_REQUEST", f"代码格式不对：{code}（应为 600000.SH 这样的 6 位数字 + 交易所后缀）")


def parse_std_code(code: str) -> tuple[str, str]:
    """标准代码 → (6 位数字, 交易所)。个股的 6 位代码也接受（按规则推交易所）。"""
    c = to_std_code(code)
    d, ex = c.split(".")
    return d, ex


def code6(code: str) -> str:
    return parse_std_code(code)[0]


def index_std_code(code: str) -> str:
    """指数代码必须带后缀（000001 既是平安银行也是上证指数）；sh000001 这种前缀写法也认。"""
    c = str(code).strip().upper()
    if _STD_RE.match(c):
        return c
    m = re.fullmatch(r"(SH|SZ|BJ)(\d{6})", c)
    if m:
        return f"{m.group(2)}.{m.group(1)}"
    raise ProtocolError("BAD_REQUEST", f"指数代码必须带交易所后缀：{code}（如 000001.SH 上证指数）")


# ---------------- 字段校验 ----------------
def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _opt_num(d: dict, k: str, where: str, errs: list, positive: bool = False) -> None:
    if k in d and d[k] is not None:
        if not _num(d[k]):
            errs.append(f"{where}.{k} 应为数字，实际是 {d[k]!r}")
        elif positive and d[k] < 0:
            errs.append(f"{where}.{k} 不能为负数")


def _date_ok(s) -> bool:
    return isinstance(s, str) and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", s))


def validate_quote(q: Any, where: str = "quote", index: bool = False) -> list[str]:
    errs: list[str] = []
    if not isinstance(q, dict):
        return [f"{where} 应为对象"]
    c = q.get("code")
    if not isinstance(c, str) or not _STD_RE.match(c.upper()):
        errs.append(f"{where}.code 应为带交易所后缀的标准代码（如 600000.SH），实际是 {c!r}")
    if not _num(q.get("price")):
        errs.append(f"{where}.price 缺失或不是数字")
    if q.get("preclose") is None and q.get("change_pct") is None:
        errs.append(f"{where} 至少要有 preclose 或 change_pct 之一")
    for k in ("preclose", "open", "high", "low", "change", "change_pct"):
        _opt_num(q, k, where, errs)
    for k in ("volume", "amount"):
        _opt_num(q, k, where, errs, positive=True)
    if q.get("ts") is not None and not (_num(q["ts"]) and q["ts"] > 1e12):
        errs.append(f"{where}.ts 应为毫秒级 Unix 时间戳")
    if q.get("change_pct") is not None and _num(q.get("change_pct")) and abs(q["change_pct"]) > 1000:
        errs.append(f"{where}.change_pct={q['change_pct']} 不像百分数（1.23 表示 1.23%）")
    return errs


def validate_minute(m: Any, where: str = "minute") -> list[str]:
    errs: list[str] = []
    if not isinstance(m, dict):
        return [f"{where} 应为对象"]
    if not _date_ok(m.get("date")):
        errs.append(f"{where}.date 应为 YYYY-MM-DD")
    pts = m.get("points")
    if not isinstance(pts, list):
        return errs + [f"{where}.points 应为数组"]
    for k in ("preclose", "open", "high", "low"):
        _opt_num(m, k, where, errs)
    last = ""
    for i, p in enumerate(pts[:2000]):
        w = f"{where}.points[{i}]"
        if not isinstance(p, dict):
            errs.append(f"{w} 应为对象")
            continue
        t = p.get("t")
        if not isinstance(t, str) or not re.fullmatch(r"\d{2}:\d{2}", t):
            errs.append(f"{w}.t 应为 HH:MM（北京时间）")
        elif t < last:
            errs.append(f"{w}.t 没有按时间升序")
        else:
            last = t
        if not _num(p.get("p")):
            errs.append(f"{w}.p 缺失或不是数字")
        for k in ("avg", "vol", "amt"):
            _opt_num(p, k, w, errs)
        if len(errs) > 20:
            break
    return errs


def validate_daily(bars: Any, where: str = "daily") -> list[str]:
    errs: list[str] = []
    if not isinstance(bars, list):
        return [f"{where} 应为数组"]
    last = ""
    for i, b in enumerate(bars):
        w = f"{where}[{i}]"
        if not isinstance(b, dict) or not _date_ok(b.get("date")):
            errs.append(f"{w}.date 应为 YYYY-MM-DD")
            continue
        if b["date"] <= last:
            errs.append(f"{w}.date 没有按日期升序或重复")
        last = b["date"]
        for k in ("open", "high", "low", "close"):
            if not _num(b.get(k)):
                errs.append(f"{w}.{k} 缺失或不是数字")
        for k in ("volume", "amount"):
            _opt_num(b, k, w, errs, positive=True)
        if len(errs) > 20:
            break
    return errs


def validate_market_stats(s: Any, where: str = "market_stats") -> list[str]:
    errs: list[str] = []
    if not isinstance(s, dict):
        return [f"{where} 应为对象"]
    if not _date_ok(s.get("date")):
        errs.append(f"{where}.date 应为 YYYY-MM-DD")
    for k in ("up", "down"):
        if not (_num(s.get(k)) and s[k] >= 0):
            errs.append(f"{where}.{k} 缺失或不是非负数")
    for k in ("flat", "limit_up", "limit_down", "suspended", "amount"):
        _opt_num(s, k, where, errs, positive=True)
    return errs


def validate_pool_list(items: Any, where: str = "pools") -> list[str]:
    if not isinstance(items, list):
        return [f"{where} 应为数组"]
    errs = []
    for i, p in enumerate(items):
        if not isinstance(p, dict) or not isinstance(p.get("key"), str) or not p.get("key"):
            errs.append(f"{where}[{i}].key 缺失")
        elif not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", p["key"]):
            errs.append(f"{where}[{i}].key 只能用字母、数字、下划线、减号")
        if isinstance(p, dict) and not isinstance(p.get("name"), str):
            errs.append(f"{where}[{i}].name 缺失")
    return errs


def validate_pool(p: Any, where: str = "pool") -> list[str]:
    errs: list[str] = []
    if not isinstance(p, dict):
        return [f"{where} 应为对象"]
    if not isinstance(p.get("key"), str):
        errs.append(f"{where}.key 缺失")
    if not _date_ok(p.get("trading_date")):
        errs.append(f"{where}.trading_date 应为 YYYY-MM-DD")
    stocks = p.get("stocks")
    if not isinstance(stocks, list):
        return errs + [f"{where}.stocks 应为数组"]
    for k in ("total", "page", "page_size"):
        if k in p and not (_num(p[k]) and p[k] >= 0):
            errs.append(f"{where}.{k} 应为非负整数")
    for i, s in enumerate(stocks):
        w = f"{where}.stocks[{i}]"
        if not isinstance(s, dict):
            errs.append(f"{w} 应为对象")
            continue
        c = s.get("code")
        if not isinstance(c, str) or not _STD_RE.match(c.upper()):
            errs.append(f"{w}.code 应为标准代码（如 600000.SH），实际是 {c!r}")
        f = s.get("fields", {})
        if not isinstance(f, dict):
            errs.append(f"{w}.fields 应为对象")
        else:
            for k, v in f.items():
                if v is not None and not _num(v):
                    errs.append(f"{w}.fields.{k} 应为数字")
        if len(errs) > 20:
            break
    return errs


VALIDATORS = {"quote": validate_quote, "minute": validate_minute, "daily": validate_daily, "index_minute": validate_minute,
              "market_stats": validate_market_stats, "pools": validate_pool_list, "pool": validate_pool}


def validate_meta(m: Any) -> list[str]:
    if not isinstance(m, dict):
        return ["meta 应为对象"]
    errs = []
    if m.get("protocol") != PROTOCOL:
        errs.append(f"meta.protocol 应为 \"{PROTOCOL}\"，实际是 {m.get('protocol')!r}")
    v = str(m.get("version", ""))
    if not v.startswith("1."):
        errs.append(f"meta.version 应为 1.x，实际是 {v!r}")
    caps = m.get("capabilities")
    if not isinstance(caps, list) or not caps:
        errs.append("meta.capabilities 应为非空数组")
    else:
        bad = [c for c in caps if c not in CAPABILITIES]
        if bad:
            errs.append(f"meta.capabilities 含未知能力 {bad}（规范定义：{', '.join(CAPABILITIES)}）")
    return errs


# ---------------- 内部格式 ↔ 协议格式 ----------------
def now_ms() -> int:
    return int(time.time() * 1000)


def quote_out(q: dict) -> dict:
    """内部 quote（code 可能是 6 位）→ 协议格式：标准代码、补 change/change_pct。"""
    o = {k: v for k, v in q.items() if v is not None}
    o["code"] = to_std_code(o["code"]) if "." not in str(o.get("code", "")) else str(o["code"]).upper()
    pre, px = o.get("preclose"), o.get("price")
    if pre and px is not None:
        o.setdefault("change", round(px - pre, 4))
        o.setdefault("change_pct", round((px - pre) / pre * 100, 4))
    o.setdefault("ts", now_ms())
    return o


def pool_out(res, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE) -> dict:
    """PoolResult → 协议格式（分页，代码转标准格式，key 去掉数据源前缀）。"""
    stocks = res.stocks
    total = len(stocks)
    page = max(1, int(page))
    page_size = max(1, min(MAX_PAGE_SIZE, int(page_size)))
    part = stocks[(page - 1) * page_size: page * page_size]
    return {"key": res.key.split(":", 1)[-1], "name": res.name, "trading_date": res.trading_date, "total": total,
            "page": page, "page_size": page_size,
            "stocks": [{"code": to_std_code(s["code"]), "name": s.get("name", ""),
                        "fields": {k: v for k, v in (s.get("fields") or {}).items() if _num(v)}} for s in part]}


def source_capabilities(src) -> list[str]:
    return [c for c in CAPABILITIES if c in getattr(src, "capabilities", ())]


# ---------------- 把任意 DataSource 包装成标准 HTTP 服务 ----------------
def make_provider_app(src, token: str = "", name: str | None = None, prefix: str = ""):
    """返回一个 FastAPI 应用：按 qfdp v1 暴露 src 的数据。token 非空时要求 Authorization: Bearer <token>。"""
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    from .datasources.base import NotSupported
    from .util import normalize_date, today_str

    app = FastAPI(title=f"qfdp provider: {name or src.name}", version=VERSION, openapi_url=prefix + "/openapi.json",
                  docs_url=None, redoc_url=None)
    caps = source_capabilities(src)

    def ok(data):
        return {"ok": True, "data": data, "ts": now_ms()}

    def fail(code: str, msg: str, **extra):
        return JSONResponse({"ok": False, "error": {"code": code, "message": msg, **extra}, "ts": now_ms()},
                            status_code=ERROR_CODES.get(code, 500))

    @app.middleware("http")
    async def _auth(request: Request, call_next):
        if token and request.url.path != prefix + "/openapi.json":
            h = request.headers.get("authorization", "")
            if h != f"Bearer {token}":
                return fail("UNAUTHORIZED", "缺少或错误的 token（Authorization: Bearer <token>）")
        try:
            return await call_next(request)
        except ProtocolError as e:
            return fail(e.code, e.message)
        except NotSupported as e:
            return fail("NOT_SUPPORTED", str(e))
        except Exception as e:  # noqa: BLE001
            return fail("UPSTREAM_ERROR", f"{type(e).__name__}: {str(e)[:300]}")

    def need(cap: str):
        if cap not in caps:
            raise ProtocolError("NOT_SUPPORTED", f"本服务不提供 {cap}（{CAPABILITY_DOCS.get(cap, '')}）")

    @app.get(prefix + "/meta")
    def meta():
        m = {}
        try:
            m = src.meta() or {}
        except Exception:  # noqa: BLE001
            m = {}
        return ok({"protocol": PROTOCOL, "version": VERSION, "name": name or src.title or src.name,
                   "capabilities": caps, "trading_date": m.get("trading_date") or today_str(),
                   "server_time": now_ms(), "max_codes_per_quote": MAX_CODES_PER_QUOTE, "max_page_size": MAX_PAGE_SIZE})

    @app.get(prefix + "/quotes")
    def quotes(codes: str = ""):
        need("quote")
        cs = [c for c in re.split(r"[,\s]+", codes) if c]
        if not cs:
            raise ProtocolError("BAD_REQUEST", "codes 不能为空")
        if len(cs) > MAX_CODES_PER_QUOTE:
            raise ProtocolError("BAD_REQUEST", f"一次最多 {MAX_CODES_PER_QUOTE} 个代码")
        got = src.get_quotes([code6(c) for c in cs])
        return ok([quote_out(got[k]) for k in (code6(c) for c in cs) if k in got])

    @app.get(prefix + "/minute")
    def minute(code: str = "", date: str = ""):
        need("minute")
        if not code:
            raise ProtocolError("BAD_REQUEST", "缺少 code")
        d = src.get_minute(code6(code), normalize_date(date or today_str()))
        if not d or not d.get("points"):
            raise ProtocolError("NOT_FOUND", f"{code} {date} 没有分时")
        return ok({**{k: v for k, v in d.items() if k != "source"}, "code": to_std_code(code)})

    @app.get(prefix + "/daily")
    def daily(code: str = "", start: str = "", end: str = "", adjust: str = "none"):
        need("daily")
        if not code or not start:
            raise ProtocolError("BAD_REQUEST", "缺少 code 或 start")
        if adjust != "none":
            raise ProtocolError("NOT_SUPPORTED", "只提供不复权日K（adjust=none）")
        bars = src.get_daily(code6(code), normalize_date(start), normalize_date(end or today_str()))
        return ok([{k: b.get(k) for k in ("date", "open", "high", "low", "close", "volume", "amount")} for b in bars])

    @app.get(prefix + "/indices")
    def indices(codes: str = ""):
        need("index")
        cs = [index_std_code(c) for c in re.split(r"[,\s]+", codes) if c] or None
        return ok([quote_out(q) for q in src.get_indices(cs)])

    @app.get(prefix + "/index_minute")
    def index_minute(code: str = "", date: str = ""):
        need("index_minute")
        c = index_std_code(code)
        d = src.get_index_minute(c, normalize_date(date or today_str()))
        if not d or not d.get("points"):
            raise ProtocolError("NOT_FOUND", f"{code} {date} 没有分时")
        return ok({**{k: v for k, v in d.items() if k != "source"}, "code": c})

    @app.get(prefix + "/market_stats")
    def market_stats(date: str = ""):
        need("market_stats")
        return ok(src.get_market_stats(normalize_date(date or today_str())))

    @app.get(prefix + "/pools")
    def pools():
        need("pool")
        return ok([{"key": p["key"], "name": p.get("name", p["key"]), "desc": p.get("desc", "")} for p in src.list_pools()])

    @app.get(prefix + "/pools/{key}")
    def pool(key: str, date: str = "", page: int = 1, page_size: int = DEFAULT_PAGE_SIZE):
        need("pool")
        if key not in {p["key"] for p in src.list_pools()}:
            raise ProtocolError("NOT_FOUND", f"没有股池 {key}")
        if page < 1 or page_size < 1 or page_size > MAX_PAGE_SIZE:
            raise ProtocolError("BAD_REQUEST", f"page 从 1 开始，page_size 取 1~{MAX_PAGE_SIZE}")
        res = src.get_pool(key, normalize_date(date) if date else None)
        return ok(pool_out(res, page, page_size))

    return app


# ---------------- OpenAPI 描述（docs/DATA_PROVIDER.md 引用，测试会核对和服务路由一致） ----------------
def openapi_spec() -> dict:
    num = {"type": "number"}
    quote = {"type": "object", "required": ["code", "price"], "properties": {
        "code": {"type": "string", "example": "600000.SH", "description": "6 位数字 + . + 交易所（SH/SZ/BJ）"},
        "name": {"type": "string"}, "price": {**num, "description": "最新价，元"}, "preclose": {**num, "description": "昨收，元"},
        "open": num, "high": num, "low": num, "change": {**num, "description": "涨跌额，元"},
        "change_pct": {**num, "description": "涨跌幅，百分数（1.23 表示 1.23%）"},
        "volume": {**num, "description": "成交量，股（指数为成分股合计股数）"}, "amount": {**num, "description": "成交额，元"},
        "ts": {"type": "integer", "description": "行情时间，Unix 毫秒"}}}
    point = {"type": "object", "required": ["t", "p"], "properties": {
        "t": {"type": "string", "example": "09:31", "description": "HH:MM，北京时间"}, "p": {**num, "description": "该分钟收盘价"},
        "avg": {**num, "description": "截至该分钟的均价"}, "vol": {**num, "description": "该分钟成交量，股"}, "amt": {**num, "description": "该分钟成交额，元"}}}
    minute = {"type": "object", "required": ["code", "date", "points"], "properties": {
        "code": {"type": "string"}, "date": {"type": "string", "format": "date"}, "preclose": num, "open": num, "high": num, "low": num,
        "points": {"type": "array", "items": {"$ref": "#/components/schemas/MinutePoint"}}}}
    bar = {"type": "object", "required": ["date", "open", "high", "low", "close"], "properties": {
        "date": {"type": "string", "format": "date"}, "open": num, "high": num, "low": num, "close": num,
        "volume": {**num, "description": "股"}, "amount": {**num, "description": "元"}}}
    stats = {"type": "object", "required": ["date", "up", "down"], "properties": {
        "date": {"type": "string", "format": "date"}, "up": {"type": "integer"}, "down": {"type": "integer"}, "flat": {"type": "integer"},
        "limit_up": {"type": "integer"}, "limit_down": {"type": "integer"}, "suspended": {"type": "integer"},
        "amount": {**num, "description": "两市成交额，元"}, "ts": {"type": "integer"}}}
    pool_info = {"type": "object", "required": ["key", "name"], "properties": {
        "key": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$"}, "name": {"type": "string"}, "desc": {"type": "string"}}}
    pool = {"type": "object", "required": ["key", "trading_date", "stocks"], "properties": {
        "key": {"type": "string"}, "name": {"type": "string"}, "trading_date": {"type": "string", "format": "date"},
        "total": {"type": "integer"}, "page": {"type": "integer"}, "page_size": {"type": "integer"},
        "stocks": {"type": "array", "items": {"type": "object", "required": ["code"], "properties": {
            "code": {"type": "string"}, "name": {"type": "string"},
            "fields": {"type": "object", "additionalProperties": num,
                       "description": "数值字段：price 元、change_rate 涨幅%、open_rise 开盘涨幅%、market_value 总市值亿元，可自定义"}}}}}}
    meta = {"type": "object", "required": ["protocol", "version", "capabilities"], "properties": {
        "protocol": {"type": "string", "enum": [PROTOCOL]}, "version": {"type": "string", "example": VERSION}, "name": {"type": "string"},
        "capabilities": {"type": "array", "items": {"type": "string", "enum": list(CAPABILITIES)}},
        "trading_date": {"type": "string", "format": "date"}, "server_time": {"type": "integer"},
        "max_codes_per_quote": {"type": "integer"}, "max_page_size": {"type": "integer"}}}

    def env(schema):
        return {"description": "成功", "content": {"application/json": {"schema": {"type": "object", "required": ["ok", "data"], "properties": {
            "ok": {"type": "boolean", "enum": [True]}, "data": schema, "ts": {"type": "integer"}}}}}}

    err = {"description": "错误（见错误码表）", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}
    ref = lambda n: {"$ref": f"#/components/schemas/{n}"}  # noqa: E731
    arr = lambda n: {"type": "array", "items": ref(n)}  # noqa: E731

    def q(name, desc, required=False, example=None, schema=None):
        p = {"name": name, "in": "query", "required": required, "description": desc, "schema": schema or {"type": "string"}}
        if example is not None:
            p["example"] = example
        return p

    def op(summary, cap, params, schema):
        return {"get": {"summary": summary, "x-capability": cap, "parameters": params,
                        "responses": {"200": env(schema), "default": err}}}

    date_p = q("date", "YYYY-MM-DD，省略 = 最近交易日", example="2026-11-11")
    return {
        "openapi": "3.0.3",
        "info": {"title": "牛来量化 标准数据接口（qfdp）", "version": VERSION,
                 "description": "牛来量化 标准数据接口（qfdp v1）。所有响应是 {ok, data, ts} 或 {ok:false, error:{code,message}}。"},
        "components": {
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer", "description": "可选；提供方要求时带 Authorization: Bearer <token>"}},
            "schemas": {"Quote": quote, "MinutePoint": point, "MinuteSeries": minute, "DailyBar": bar, "MarketStats": stats,
                        "PoolInfo": pool_info, "Pool": pool, "Meta": meta,
                        "Error": {"type": "object", "required": ["ok", "error"], "properties": {
                            "ok": {"type": "boolean", "enum": [False]}, "error": {"type": "object", "required": ["code", "message"], "properties": {
                                "code": {"type": "string", "enum": list(ERROR_CODES)}, "message": {"type": "string"},
                                "retry_after": {"type": "number", "description": "RATE_LIMITED 时建议等待秒数"}}}}}}},
        "security": [{"bearer": []}, {}],
        "paths": {
            "/meta": op("服务信息和能力声明（必需）", "meta", [], ref("Meta")),
            "/quotes": op("个股实时行情", "quote", [q("codes", f"逗号分隔，最多 {MAX_CODES_PER_QUOTE} 个", True, "600000.SH,000001.SZ")], arr("Quote")),
            "/minute": op("个股分时", "minute", [q("code", "标准代码", True, "600000.SH"), date_p], ref("MinuteSeries")),
            "/daily": op("个股日K（不复权，按日期升序）", "daily", [q("code", "标准代码", True, "600000.SH"), q("start", "开始日期", True, "2026-08-01"),
                                                     q("end", "结束日期（含），省略 = 今天"), q("adjust", "只支持 none", example="none")], arr("DailyBar")),
            "/indices": op("指数实时行情", "index", [q("codes", "逗号分隔，省略 = 提供方的主要指数", example="000001.SH,399006.SZ")], arr("Quote")),
            "/index_minute": op("指数分时", "index_minute", [q("code", "指数标准代码", True, "000001.SH"), date_p], ref("MinuteSeries")),
            "/market_stats": op("市场情绪", "market_stats", [date_p], ref("MarketStats")),
            "/pools": op("股池列表", "pool", [], arr("PoolInfo")),
            "/pools/{key}": op("股池成分（分页）", "pool", [
                {"name": "key", "in": "path", "required": True, "schema": {"type": "string"}}, date_p,
                q("page", "页码，从 1 开始", schema={"type": "integer", "default": 1}),
                q("page_size", f"每页条数，1~{MAX_PAGE_SIZE}", schema={"type": "integer", "default": DEFAULT_PAGE_SIZE})], ref("Pool")),
        },
    }
