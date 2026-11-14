"""外部数据源：推（push）和拉（http）。

push：外部程序调用本系统 REST API 写入股池和字段：
    PUT  /api/v1/sources/<数据源>/pools/<股池>   {"date": "2026-11-11", "name": "我的股池", "stocks": [{"code": "600000", "name": "浦发银行", "fields": {"ext_score": 0.8}}]}
    POST /api/v1/sources/<数据源>/fields         {"date": "2026-11-11", "items": [{"code": "600000", "fields": {"ext_hot": 3}}]}
  配置：[sources.my_push] type = "push"。股池 key 写成 "my_push:<股池>"。

http：本系统到点去请求外部地址拉取（哪个地址填了就有哪种能力）：
    pools_url   GET  → {"pools": [{"key": "hot", "name": "热门"}]}
    pool_url    GET  （可写 {key} {date}）→ {"name": "热门", "stocks": [{"code": "600000", "name": "...", "fields": {...}}]}
    fields_url  POST {"codes": [...], "date": "..."} → {"items": [{"code": "600000", "fields": {...}}]}
    factor_url  POST 候选列表 → {"results": [{"code": "600000", "pass": true, "score": 0.8}]}（策略里「外部因子（HTTP 拉取）」选这个数据源时用）
    minute_url  GET  （{code} {date}）→ {"preclose": 10.0, "points": [{"t": "09:31", "p": 10.1, "avg": 10.05, "vol": 1200, "amt": 12000}]}
    daily_url   GET  （{code} {start} {end}）→ {"bars": [{"date": "2026-11-10", "open": .., "high": .., "low": .., "close": .., "volume": .., "amount": ..}]}
  token 非空时作为 Authorization: Bearer 发出。外部字段名建议以 ext_ 开头，表达式里直接能用。
"""
from __future__ import annotations

from urllib.parse import quote

from .. import external as ext
from .base import DataSource, NotSupported, PoolResult, register_source


@register_source
class PushSource(DataSource):
    type_name = "push"
    title = "外部推送"
    description = "外部程序通过 REST API 推送股池和字段（PUT /api/v1/sources/<名字>/pools/<股池>、POST .../fields）"
    capabilities = ("pool", "fields")
    config_fields = [{"name": "use_latest_if_missing", "hint": "当天没推送时用最近一次的股池（默认 false）"},
                     {"name": "fields", "hint": "可空：这个数据源会推送的字段名，逗号分隔；还没推送过时也能在策略里点选"}]

    def __init__(self, name, cfg, data_dir, offline=False, root=None):
        super().__init__(name, cfg, data_dir, offline, root)
        self.store = ext.ExternalStore(data_dir)

    def list_pools(self):
        return [{"key": p["key"], "name": p["name"], "desc": f"最近 {p['latest_date']}，{p['count']} 只"}
                for p in self.store.list_pools(self.name)]

    def get_pool(self, key, date):
        rec = self.store.get_pool(self.name, key, date or "", bool(self.cfg.get("use_latest_if_missing", False)))
        if rec is None:
            return PoolResult(key=f"{self.name}:{key}", name=key, trading_date=date or "",
                              error=f"{date} 还没有收到推送的股池 {self.name}:{key}")
        return PoolResult(key=f"{self.name}:{key}", name=rec.get("name") or key, trading_date=rec.get("date") or date or "",
                          stocks=[{"code": s["code"], "name": s.get("name") or "", "fields": dict(s.get("fields") or {})}
                                  for s in rec.get("stocks", [])])

    def get_fields(self, codes, date):
        allf = self.store.get_fields(self.name, date)
        return {c: allf[c] for c in codes if c in allf}


@register_source
class HttpSource(DataSource):
    type_name = "http"
    title = "外部 HTTP"
    description = "到点请求外部地址拉取股池/字段/分时/日K；哪个地址填了就有哪种能力"
    capabilities = ("pool", "fields", "minute", "daily")
    config_fields = [{"name": "pools_url", "hint": "GET 股池列表"}, {"name": "pool_url", "hint": "GET 股池成分，可写 {key} {date}"},
                     {"name": "fields_url", "hint": "POST 补字段"}, {"name": "minute_url", "hint": "GET 分时，{code} {date}"},
                     {"name": "daily_url", "hint": "GET 日K，{code} {start} {end}"}, {"name": "token", "hint": "Bearer 令牌", "secret": True},
                     {"name": "timeout_secs", "hint": "超时秒数，默认 10"},
                     {"name": "factor_url", "hint": "可空：POST 因子接口（收候选、回 pass/score），策略里点选这个数据源的因子时用"},
                     {"name": "factor_names", "hint": "可空：factor_url 提供的因子名，逗号分隔，供点选"},
                     {"name": "fields", "hint": "可空：fields_url 会返回的字段名，逗号分隔；没请求过时也能点选"}]

    def __init__(self, name, cfg, data_dir, offline=False, root=None):
        super().__init__(name, cfg, data_dir, offline, root)
        caps = []
        if cfg.get("pool_url"):
            caps.append("pool")
        for k, cap in (("fields_url", "fields"), ("minute_url", "minute"), ("daily_url", "daily")):
            if cfg.get(k):
                caps.append(cap)
        self.capabilities = tuple(caps)

    def _req(self, method, url, body=None):
        if self.offline:
            raise RuntimeError("离线模式不请求外部接口")
        h = {"Authorization": f"Bearer {self.cfg['token']}"} if self.cfg.get("token") else {}
        self.limiter.wait()
        return ext.http_json(method, url, body, h, float(self.cfg.get("timeout_secs", 10)))

    @staticmethod
    def _fmt(tpl, **kw):
        return tpl.format(**{k: quote(str(v), safe="") for k, v in kw.items()})

    def list_pools(self):
        if self.cfg.get("pools_url"):
            try:
                r = self._req("GET", self.cfg["pools_url"])
                return [{"key": str(p.get("key")), "name": p.get("name") or p.get("key"), "desc": p.get("desc", "")}
                        for p in (r.get("pools") if isinstance(r, dict) else r) or []]
            except Exception:  # noqa: BLE001
                return []
        keys = self.cfg.get("pools") or []
        return [{"key": k, "name": k, "desc": ""} for k in keys]

    def get_pool(self, key, date):
        if not self.cfg.get("pool_url"):
            raise NotSupported(f"{self.name} 没配 pool_url")
        r = self._req("GET", self._fmt(self.cfg["pool_url"], key=key, date=date or ""))
        stocks = []
        for s in (r.get("stocks") if isinstance(r, dict) else r) or []:
            if isinstance(s, str):
                s = {"code": s}
            code = str(s.get("code") or "")
            if ext.CODE_RE.match(code):
                stocks.append({"code": code, "name": s.get("name") or "", "fields": ext._clean_fields(s.get("fields") or {})})
        return PoolResult(key=f"{self.name}:{key}", name=(r.get("name") if isinstance(r, dict) else None) or key,
                          trading_date=(r.get("date") if isinstance(r, dict) else None) or date or "", stocks=stocks)

    def get_fields(self, codes, date):
        if not self.cfg.get("fields_url"):
            raise NotSupported(f"{self.name} 没配 fields_url")
        r = self._req("POST", self.cfg["fields_url"], {"codes": list(codes), "date": date})
        items = r.get("items", r) if isinstance(r, dict) else r
        if isinstance(items, dict):
            items = [{"code": k, "fields": v} for k, v in items.items()]
        return {str(i.get("code")): ext._clean_fields(i.get("fields") or {}) for i in items or [] if isinstance(i, dict)}

    def get_minute(self, code, date):
        if not self.cfg.get("minute_url"):
            raise NotSupported(f"{self.name} 没配 minute_url")
        r = self._req("GET", self._fmt(self.cfg["minute_url"], code=code, date=date))
        return {"code": code, "date": date, **r}

    def get_daily(self, code, start, end):
        if not self.cfg.get("daily_url"):
            raise NotSupported(f"{self.name} 没配 daily_url")
        r = self._req("GET", self._fmt(self.cfg["daily_url"], code=code, start=start, end=end))
        bars = r.get("bars", []) if isinstance(r, dict) else r
        return sorted([b for b in bars or [] if start <= str(b.get("date", "")) <= end], key=lambda b: b["date"])
