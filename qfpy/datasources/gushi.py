"""gushi.in 量化接口：5 个策略股池 + 历史分时。接口只允许查当天和上一交易日的股池。"""
from __future__ import annotations

import time

from ..nethelp import NetError, robust_get
from ..util import log, normalize_date
from .base import DataSource, PoolResult, register_source

STRATEGIES = [
    ("auction_long", "竞价多头策略"),
    ("morning_star", "晨星量化"),
    ("auction_alpha", "竞价阿尔法"),
    ("gold_230", "黄金两点半"),
    ("half_past_ten", "十点半"),
]


@register_source
class GushiSource(DataSource):
    type_name = "gushi"
    title = "gushi.in 策略股池"
    description = "5 个策略股池（只能查当天和上一交易日）和历史分时；限 40 次/分钟、2000 次/天"
    capabilities = ("pool", "minute", "meta")
    config_fields = [{"name": "key", "hint": "接口 key", "secret": True}, {"name": "base_url", "hint": "接口地址"},
                     {"name": "min_interval_ms", "hint": "两次调用最小间隔（毫秒）"}, {"name": "cache_ttl_secs", "hint": "当天数据缓存秒数"},
                     {"name": "retries", "hint": "网络出错时重试几次（默认 2）"},
                     {"name": "proxy", "hint": "代理：auto 跟随系统（失败再直连）/ none 直连 / 代理网址"}]

    def _get(self, path: str, params: dict, rel: str, ttl):
        cached = self.cache.get(rel, ttl)
        if cached is not None:
            return cached
        if self.offline:
            raise RuntimeError(f"离线模式且无缓存：{self.name}/{rel}")
        key = self.cfg.get("key", "")
        if not key:
            raise RuntimeError("未配置 gushi.in 接口 key（[sources.gushi].key 或环境变量 QF_GUSHI_KEY）")
        self.limiter.wait()
        url = self.cfg.get("base_url", "https://www.gushi.in").rstrip("/") + path
        r = robust_get(url, params={**params, "key": key}, timeout=float(self.cfg.get("timeout_secs", 15)),
                       headers={"User-Agent": "niulai-quant/0.3"}, retries=int(self.cfg.get("retries", 2)),
                       proxy=str(self.cfg.get("proxy", "auto")))
        try:
            v = r.json()
        except Exception as e:  # noqa: BLE001
            raise NetError(f"{path} 返回的不是正常数据（HTTP {r.status_code}）", "可能是网站临时故障或被代理改写了页面，稍后会自动再试。",
                           f"{type(e).__name__}: {e}; 前 200 字：{r.text[:200]}") from e
        if v.get("ok") is not True:
            err = v.get("error") or {}
            raise RuntimeError(f"{path} 接口报错 {err.get('code', '')}: {err.get('message', '未知错误')}")
        self.cache.put(rel, v)
        return v

    def meta(self) -> dict | None:
        v = self._get("/quant_api/v1/meta", {}, "meta.json", 300)
        return v.get("data")

    def list_pools(self) -> list[dict]:
        names = dict(STRATEGIES)
        try:
            m = self.meta() or {}
            for s in m.get("strategies", []):
                names[s["key"]] = s["name"]
        except Exception as e:  # noqa: BLE001
            log.debug("gushi meta 不可用：%s", e)
        return [{"key": k, "name": n, "desc": "gushi.in 策略股池"} for k, n in names.items()]

    def get_pool(self, key: str, date: str | None) -> PoolResult:
        if not date:
            date = (self.meta() or {}).get("trading_date")
        date = normalize_date(date)
        rel = f"pools/{date}/{key}.json"
        try:
            v = self._get("/quant_api/v1/pools", {"strategy": key, "date": date}, rel, self.cache.ttl_for(date))
            stale = None
        except NetError as e:
            stale = self._stale_pool(key, date)
            if stale is None:
                raise
            v, mtime, sdate = stale
            stale = (e, mtime, sdate)
        pr = self._pool_from(key, date, v)
        if stale:
            e, mtime, sdate = stale
            pr.stale_at = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
            pr.problem, pr.hint, pr.detail = e.problem, e.hint, e.detail
            if sdate != date:
                pr.trading_date = sdate
            log.warning("股池 %s:%s 拉取失败，改用 %s 的缓存（%s）：%s", self.name, key, pr.stale_at, sdate, e.detail)
        return pr

    def _stale_pool(self, key: str, date: str):
        """拉取失败时找最近一次成功拉到的这个股池：先找同一天的，没有再找之前最近的一天。"""
        got = self.cache.get_stale(f"pools/{date}/{key}.json")
        if got:
            return got[0], got[1], date
        root = self.cache.root / "pools"
        days = sorted((p.name for p in root.iterdir() if p.is_dir() and p.name < date and (p / f"{key}.json").exists()),
                      reverse=True) if root.exists() else []
        for d in days:
            got = self.cache.get_stale(f"pools/{d}/{key}.json")
            if got:
                return got[0], got[1], d
        return None

    def _pool_from(self, key: str, date: str, v: dict) -> PoolResult:
        d = v.get("data") or {}
        stocks = []
        for s in d.get("stock_list") or []:
            f = {k: s.get(k) for k in ("price", "open_price", "open_rise", "change_rate", "market_value")}
            stocks.append({"code": str(s.get("stock_code")), "name": s.get("stock_name", ""),
                           "fields": {k: float(x) for k, x in f.items() if x is not None}})
        name = (d.get("strategy") or {}).get("name") or dict(STRATEGIES).get(key, key)
        return PoolResult(key=f"{self.name}:{key}", name=name, trading_date=d.get("trading_date") or date, stocks=stocks)

    def get_minute(self, code: str, date: str) -> dict:
        d8 = normalize_date(date).replace("-", "")
        v = self._get("/hist_minute_api", {"code": code, "date": d8}, f"minute/{d8}/{code}.json", self.cache.ttl_for(d8))
        d = v.get("data") or {}
        d.setdefault("code", code)
        d["source"] = self.name
        return d
