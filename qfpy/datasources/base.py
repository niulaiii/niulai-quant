"""数据源基类、注册表、JSON 缓存。"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..util import RateLimiter, atomic_write_text, log, today_str, normalize_date

SOURCE_TYPES: dict[str, type["DataSource"]] = {}


class NotSupported(Exception):
    """该数据源不提供这类数据。"""


@dataclass
class PoolResult:
    key: str                       # 完整 key，如 gushi:auction_long
    name: str                      # 展示名
    trading_date: str
    stocks: list[dict] = field(default_factory=list)   # [{code, name, fields:{...}}]
    error: str | None = None
    # 拉取失败、改用缓存时：stale_at = 缓存是什么时候成功拉到的；problem/hint/detail = 中文问题、排查办法、原始报错
    stale_at: str | None = None
    problem: str | None = None
    hint: str | None = None
    detail: str | None = None


def register_source(cls):
    SOURCE_TYPES[cls.type_name] = cls
    return cls


class JsonCache:
    """文件缓存：当天数据按 TTL 过期，历史日期永久有效。离线模式忽略 TTL。"""

    def __init__(self, root: Path, ttl_secs: float, offline: bool):
        self.root = Path(root)
        self.ttl = float(ttl_secs)
        self.offline = offline

    def ttl_for(self, date: str | None) -> float | None:
        if date is None:
            return self.ttl
        return self.ttl if normalize_date(date) >= today_str() else None

    def get(self, rel: str, ttl: float | None) -> Any | None:
        f = self.root / rel
        if not f.exists():
            return None
        if not self.offline and ttl is not None and time.time() - f.stat().st_mtime > ttl:
            return None
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None

    def get_stale(self, rel: str) -> tuple[Any, float] | None:
        """不管过没过期，读最近一次成功写入的缓存：返回 (数据, 写入时间戳)。拉取失败时兜底用。"""
        f = self.root / rel
        if not f.exists():
            return None
        try:
            return json.loads(f.read_text(encoding="utf-8")), f.stat().st_mtime
        except Exception:  # noqa: BLE001
            return None

    def put(self, rel: str, data: Any) -> None:
        # 原子写：行情缓存以前是直接覆盖，进程被中断会留下半个 json，get() 只能当缓存失效重拉。
        # 两个进程同时写同一份缓存时最后一个 replace 胜出，内容等价，不需要跨进程锁。
        atomic_write_text(self.root / rel, json.dumps(data, ensure_ascii=False))


class DataSource:
    """数据源接口。子类按能力实现其中几个方法，其余抛 NotSupported。

    - list_pools()            本源能提供的股池 [{key, name, desc}]
    - get_pool(key, date)     股池成分 → PoolResult
    - get_minute(code, date)  分时 → {code,date,preclose,open,high,low,points:[{t,p,avg,vol,amt}]}
    - get_daily(code, start, end) 日K → [{date,open,high,low,close,volume,amount}]（volume 单位：股）
    - get_fields(codes, date) 给候选补字段 → {code: {字段: 数值}}（能力 "fields"；外部行情/自定义字段）
    - meta()                  交易日信息 {trading_date, allowed_dates}（可选）

    以下几项是「标准数据接口」（qfdp v1，见 qfpy/dataproto.py 和 docs/DATA_PROVIDER.md）新增的能力：
    - get_quotes(codes)       个股实时行情（能力 "quote"）→ {6位代码: {code,name,price,preclose,open,high,low,change_pct,volume,amount,ts}}
    - get_indices(codes)      指数实时行情（能力 "index"）→ [同上，code 用 000001.SH 这种带后缀的标准代码]；codes=None 时返回主要指数
    - get_index_minute(code, date) 指数分时（能力 "index_minute"）→ 与 get_minute 相同结构，code 为标准代码
    - get_market_stats(date)  市场情绪（能力 "market_stats"）→ {date,up,down,flat,limit_up,limit_down,suspended,amount,ts}
    单位：价格 元、成交量 股、成交额 元、涨跌幅 百分数（1.23 = 1.23%）、市值 亿元、ts 毫秒时间戳、时间 北京时间。
    """

    type_name = ""
    title = ""
    description = ""
    capabilities: tuple[str, ...] = ()   # pool / quote / minute / daily / index / index_minute / market_stats / fields / meta
    plugin: str | None = None
    config_fields: list[dict] = []       # 网页显示的可配置项 [{name, hint, secret?}]

    def __init__(self, name: str, cfg: dict, data_dir: Path, offline: bool = False, root: Path | None = None):
        self.name = name
        self.cfg = cfg
        self.offline = offline
        self.root = root or Path(".")
        self.cache = JsonCache(Path(data_dir) / "cache" / name, cfg.get("cache_ttl_secs", 60), offline)
        self.limiter = RateLimiter(cfg.get("min_interval_ms", 500))

    def list_pools(self) -> list[dict]:
        return []

    def get_pool(self, key: str, date: str | None) -> PoolResult:
        raise NotSupported(f"{self.name} 不提供股池")

    def get_minute(self, code: str, date: str) -> dict:
        raise NotSupported(f"{self.name} 不提供分时")

    def get_daily(self, code: str, start: str, end: str) -> list[dict]:
        raise NotSupported(f"{self.name} 不提供日K")

    def get_fields(self, codes: list[str], date: str) -> dict[str, dict]:
        raise NotSupported(f"{self.name} 不提供字段")

    def get_quotes(self, codes: list[str]) -> dict[str, dict]:
        raise NotSupported(f"{self.name} 不提供实时行情")

    def get_indices(self, codes: list[str] | None = None) -> list[dict]:
        raise NotSupported(f"{self.name} 不提供指数行情")

    def get_index_minute(self, code: str, date: str) -> dict:
        raise NotSupported(f"{self.name} 不提供指数分时")

    def get_market_stats(self, date: str) -> dict:
        raise NotSupported(f"{self.name} 不提供市场情绪")

    def get_stock_info(self, code: str) -> dict:
        """个股资料（可选）→ {industry, listing_date(YYYY-MM-DD), total_shares, float_shares, total_mv, float_mv}，拿不到的键不给。"""
        raise NotSupported(f"{self.name} 不提供个股资料")

    def get_extra(self, kind: str, date: str) -> dict[str, dict]:
        """第二批实验性数据（可选）：auction 竞价 / seal 涨停封单 / fund_flow 主力资金 / lhb 龙虎榜 → {6位代码: {字段: 值}}。"""
        raise NotSupported(f"{self.name} 不提供 {kind}")

    def test_connection(self) -> list[dict]:
        """网页「测试连接」：按声明的能力各做一次最小请求，返回 [{check, ok, detail}]。子类可覆盖。"""
        from ..dataproto import DEFAULT_INDICES, validate_minute, validate_pool_list, validate_quote
        out: list[dict] = []

        def step(name, fn):
            import time as _t
            t0 = _t.monotonic()
            try:
                detail = fn()
                out.append({"check": name, "ok": True, "detail": detail, "ms": int((_t.monotonic() - t0) * 1000)})
            except Exception as e:  # noqa: BLE001
                out.append({"check": name, "ok": False, "detail": f"{type(e).__name__}: {str(e)[:300]}",
                            "ms": int((_t.monotonic() - t0) * 1000)})
        caps = self.capabilities
        if "meta" in caps:
            step("交易日信息", lambda: f"交易日 {(self.meta() or {}).get('trading_date', '?')}")
        if "pool" in caps:
            def pools():
                ps = self.list_pools()
                errs = validate_pool_list([{"key": p.get("key"), "name": p.get("name", "")} for p in ps])
                if errs:
                    raise ValueError("；".join(errs[:3]))
                if not ps:
                    return "没有股池"
                r = self.get_pool(ps[0]["key"], None)
                return f"{len(ps)} 个股池；{r.name} {r.trading_date} 共 {len(r.stocks)} 只"
            step("股池", pools)
        if "index" in caps:
            def idx():
                qs = self.get_indices([DEFAULT_INDICES[0][0]])
                if not qs:
                    raise ValueError("没有返回指数")
                errs = validate_quote(qs[0], "index", index=True)
                if errs:
                    raise ValueError("；".join(errs[:3]))
                q = qs[0]
                return f"{q.get('name', q['code'])} {q['price']}（{q.get('change_pct', 0):+.2f}%）"
            step("指数行情", idx)
        if "quote" in caps:
            def qt():
                got = self.get_quotes(["600000"])
                if not got:
                    raise ValueError("没有返回行情")
                from ..dataproto import quote_out
                q = quote_out(next(iter(got.values())))
                errs = validate_quote(q)
                if errs:
                    raise ValueError("；".join(errs[:3]))
                return f"{q['code']} {q.get('name', '')} {q['price']}"
            step("个股行情", qt)
        if "index_minute" in caps:
            def im():
                from ..util import today_str
                d = self.get_index_minute(DEFAULT_INDICES[0][0], today_str())
                errs = validate_minute(d)
                if errs:
                    raise ValueError("；".join(errs[:3]))
                return f"{d['date']} 共 {len(d['points'])} 个点"
            step("指数分时", im)
        if not out:
            out.append({"check": "能力", "ok": True, "detail": "这个数据源没有可在线测试的能力（只提供字段或推送数据）"})
        return out

    def meta(self) -> dict | None:
        return None

    def info(self) -> dict:
        return {"name": self.name, "type": self.type_name, "title": self.title, "description": self.description,
                "capabilities": list(self.capabilities), "enabled": self.cfg.get("enabled", True), "plugin": self.plugin}


def source_catalog() -> list[dict]:
    return [{"type": t, "title": c.title, "description": c.description, "capabilities": list(c.capabilities),
             "config_fields": c.config_fields, "plugin": c.plugin} for t, c in SOURCE_TYPES.items()]


def build_sources(cfg: dict, data_dir: Path, offline: bool = False, root: Path | None = None) -> dict[str, DataSource]:
    out: dict[str, DataSource] = {}
    for name, scfg in cfg.get("sources", {}).items():
        if not scfg.get("enabled", True):
            continue
        t = scfg.get("type")
        cls = SOURCE_TYPES.get(t)
        if cls is None:
            log.warning("数据源 %s 的 type=%s 未注册，已跳过（可用：%s）", name, t, ", ".join(SOURCE_TYPES))
            continue
        out[name] = cls(name, scfg, data_dir, offline, root)
    return out
