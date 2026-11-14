"""「标准接口」数据源：接入任何实现了 qfdp v1（docs/DATA_PROVIDER.md）的 HTTP 服务。

网页上填 URL 和 token 就能用：

    [sources.my_provider]
    type = "standard"
    url = "http://127.0.0.1:9100"     # 服务根地址，下面有 /meta /quotes /pools …
    token = ""                          # 可选，作为 Authorization: Bearer 发出；也可用环境变量 QF_SRC_<名字大写>_TOKEN
    timeout_secs = 8

能力来自服务的 /meta.capabilities（缓存 10 分钟；连不上时按配置里的 capabilities，默认全部声明，
调用失败会自动换下一个数据源）。每个响应都按规范校验，不合规范就报错，不会把脏数据喂给选股。
"""
from __future__ import annotations

import os
import time

import httpx

from .. import dataproto as dp
from ..util import normalize_date, today_str
from .base import DataSource, NotSupported, PoolResult, register_source


class ProviderError(RuntimeError):
    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(f"{code}：{message}")
        self.code, self.message, self.status = code, message, status


@register_source
class StandardSource(DataSource):
    type_name = "standard"
    title = "标准接口（qfdp）"
    description = "任何按「标准数据接口」规范提供 JSON 的服务：填 URL 和 token 即可；能力按服务 /meta 声明"
    capabilities = tuple(dp.CAPABILITIES) + ("meta",)
    config_fields = [{"name": "url", "hint": "服务根地址，如 http://127.0.0.1:9100"},
                     {"name": "token", "hint": "可选，Bearer token", "secret": True},
                     {"name": "timeout_secs", "hint": "单次请求超时秒数，默认 8"},
                     {"name": "pools", "hint": "只用这些股池（留空 = 服务提供的全部）"}]

    _META_TTL = 600

    def __init__(self, name, cfg, data_dir, offline=False, root=None, client: httpx.Client | None = None):
        super().__init__(name, cfg, data_dir, offline, root)
        self.url = str(cfg.get("url", "")).rstrip("/")
        self.token = os.environ.get(f"QF_SRC_{name.upper()}_TOKEN") or str(cfg.get("token", "") or "")
        self.timeout = float(cfg.get("timeout_secs", 8) or 8)
        self._client = client            # 测试时注入 TestClient / MockTransport
        self._meta: dict | None = None
        self._meta_ts = 0.0
        caps = cfg.get("capabilities")
        if isinstance(caps, list) and caps:
            self.capabilities = tuple(c for c in caps if c in dp.CAPABILITIES) + ("meta",)

    # ---------- HTTP ----------
    def _get(self, path: str, params: dict | None = None):
        if self.offline:
            raise RuntimeError("离线模式，不请求标准接口")
        if not self.url and self._client is None:
            raise RuntimeError(f"数据源 {self.name} 没填 url")
        if self.url and not self.url.startswith(("http://", "https://")):
            raise RuntimeError(f"url 必须以 http:// 或 https:// 开头：{self.url}")
        self.limiter.wait()
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            if self._client is not None:
                r = self._client.get(self.url + path, params=params or {}, headers=headers, timeout=self.timeout)
            else:
                r = httpx.get(self.url + path, params=params or {}, headers=headers, timeout=self.timeout)
        except httpx.TimeoutException as e:
            raise ProviderError("TIMEOUT", f"{self.timeout:g} 秒内没有响应（{path}）") from e
        except httpx.HTTPError as e:
            raise ProviderError("CONNECT", f"连不上 {self.url}：{type(e).__name__}") from e
        try:
            j = r.json()
        except ValueError:
            raise ProviderError("BAD_RESPONSE", f"HTTP {r.status_code}，返回的不是 JSON（{path}）", r.status_code) from None
        if not isinstance(j, dict) or "ok" not in j:
            raise ProviderError("BAD_RESPONSE", f"响应缺少 ok 字段，不是 qfdp 格式（{path}）", r.status_code)
        if not j.get("ok"):
            err = j.get("error") or {}
            raise ProviderError(str(err.get("code") or "INTERNAL"), str(err.get("message") or f"HTTP {r.status_code}"), r.status_code)
        if r.status_code >= 400:
            raise ProviderError("BAD_RESPONSE", f"HTTP {r.status_code} 却返回 ok=true", r.status_code)
        return j.get("data")

    @staticmethod
    def _check(errs: list[str], what: str):
        if errs:
            raise ProviderError("BAD_RESPONSE", f"{what}不合规范：" + "；".join(errs[:5]))

    def _need(self, cap: str):
        m = self.meta_info(silent=True)
        if m and cap not in m.get("capabilities", []):
            raise NotSupported(f"{self.name} 的 /meta 没有声明 {cap}")

    # ---------- meta ----------
    def meta_info(self, silent: bool = False, force: bool = False) -> dict | None:
        if not force and self._meta is not None and time.time() - self._meta_ts < self._META_TTL:
            return self._meta
        try:
            m = self._get("/meta")
            self._check(dp.validate_meta(m), "/meta ")
        except Exception:
            if silent:
                return None
            raise
        self._meta, self._meta_ts = m, time.time()
        return m

    def meta(self) -> dict:
        m = self.meta_info() or {}
        return {"trading_date": m.get("trading_date") or today_str()}

    # ---------- 股池 ----------
    def list_pools(self) -> list[dict]:
        self._need("pool")
        rel = "pools_list.json"
        hit = self.cache.get(rel, 300)
        if hit is None:
            hit = self._get("/pools")
            self._check(dp.validate_pool_list(hit), "/pools ")
            self.cache.put(rel, hit)
        only = [str(x) for x in (self.cfg.get("pools") or [])]
        return [{"key": p["key"], "name": p.get("name") or p["key"], "desc": p.get("desc", "")}
                for p in hit if not only or p["key"] in only]

    def get_pool(self, key: str, date: str | None) -> PoolResult:
        self._need("pool")
        date = normalize_date(date) if date else None
        rel = f"pools/{date or 'latest'}/{key}.json"
        hit = self.cache.get(rel, self.cache.ttl_for(date))
        if hit is None:
            stocks, page, name, td = [], 1, key, date or ""
            while True:
                params = {"page": page, "page_size": dp.MAX_PAGE_SIZE}
                if date:
                    params["date"] = date
                d = self._get(f"/pools/{key}", params)
                self._check(dp.validate_pool(d), f"/pools/{key} ")
                name, td = d.get("name") or name, d.get("trading_date") or td
                stocks += d["stocks"]
                total = int(d.get("total", len(stocks)))
                if not d["stocks"] or len(stocks) >= total or page >= 50:
                    break
                page += 1
            hit = {"name": name, "trading_date": td,
                   "stocks": [{"code": dp.code6(s["code"]), "name": s.get("name", ""), "fields": dict(s.get("fields") or {})}
                              for s in stocks]}
            self.cache.put(rel, hit)
        return PoolResult(key=f"{self.name}:{key}", name=hit["name"], trading_date=hit["trading_date"], stocks=hit["stocks"])

    # ---------- 行情 ----------
    def get_quotes(self, codes: list[str]) -> dict[str, dict]:
        self._need("quote")
        out: dict[str, dict] = {}
        for i in range(0, len(codes), dp.MAX_CODES_PER_QUOTE):
            part = codes[i:i + dp.MAX_CODES_PER_QUOTE]
            d = self._get("/quotes", {"codes": ",".join(dp.to_std_code(c) for c in part)})
            if not isinstance(d, list):
                raise ProviderError("BAD_RESPONSE", "/quotes 应返回数组")
            for j, q in enumerate(d):
                self._check(dp.validate_quote(q, f"quotes[{j}]"), "/quotes ")
                out[dp.code6(q["code"])] = q
        return out

    def get_minute(self, code: str, date: str) -> dict:
        self._need("minute")
        d = self._get("/minute", {"code": dp.to_std_code(code), "date": normalize_date(date)})
        self._check(dp.validate_minute(d), "/minute ")
        return {**d, "code": code, "source": self.name}

    def get_daily(self, code: str, start: str, end: str) -> list[dict]:
        self._need("daily")
        d = self._get("/daily", {"code": dp.to_std_code(code), "start": normalize_date(start), "end": normalize_date(end)})
        self._check(dp.validate_daily(d), "/daily ")
        return d

    def get_indices(self, codes: list[str] | None = None) -> list[dict]:
        self._need("index")
        params = {"codes": ",".join(dp.index_std_code(c) for c in codes)} if codes else {}
        d = self._get("/indices", params)
        if not isinstance(d, list):
            raise ProviderError("BAD_RESPONSE", "/indices 应返回数组")
        for j, q in enumerate(d):
            self._check(dp.validate_quote(q, f"indices[{j}]", index=True), "/indices ")
        return d

    def get_index_minute(self, code: str, date: str) -> dict:
        self._need("index_minute")
        d = self._get("/index_minute", {"code": dp.index_std_code(code), "date": normalize_date(date)})
        self._check(dp.validate_minute(d), "/index_minute ")
        return {**d, "source": self.name}

    def get_market_stats(self, date: str) -> dict:
        self._need("market_stats")
        d = self._get("/market_stats", {"date": normalize_date(date)})
        self._check(dp.validate_market_stats(d), "/market_stats ")
        return d

    # ---------- 测试连接 ----------
    def test_connection(self) -> list[dict]:
        out = []
        t0 = time.monotonic()
        try:
            m = self.meta_info(force=True)
        except Exception as e:  # noqa: BLE001
            return [{"check": "/meta", "ok": False, "detail": str(e)[:300], "ms": int((time.monotonic() - t0) * 1000)}]
        out.append({"check": "/meta", "ok": True, "ms": int((time.monotonic() - t0) * 1000),
                    "detail": f"{m.get('name', '')} qfdp {m.get('version')}，能力：{', '.join(m.get('capabilities', []))}"})
        self.capabilities = tuple(m.get("capabilities", [])) + ("meta",)
        rest = super().test_connection()
        return out + [x for x in rest if x["check"] != "交易日信息"]
