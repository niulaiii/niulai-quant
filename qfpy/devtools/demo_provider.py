"""演示用数据源：按标准数据接口（qfdp v1）的 7 种能力全部实现，数据是确定性合成的（同一天同一代码每次一样）。

不联网，供 examples/data_provider_server.py 和测试使用。写自己的提供方时，把这里的合成数据换成你的数据即可。
"""
from __future__ import annotations

import hashlib
import math
import time
from datetime import date as _date
from datetime import timedelta

from ..dataproto import DEFAULT_INDICES, index_std_code, to_std_code
from ..datasources.base import DataSource, PoolResult
from ..util import today_str

STOCKS = [("600000", "浦发银行", 10.5), ("000001", "平安银行", 11.8), ("300825", "阿尔特", 94.5),
          ("603709", "中源家居", 18.2), ("688981", "中芯国际", 88.0), ("002594", "比亚迪", 260.0),
          ("601318", "中国平安", 52.3), ("300750", "宁德时代", 250.0)]
INDEX_BASE = {"000001.SH": 3300.0, "399001.SZ": 10500.0, "399006.SZ": 2150.0, "000688.SH": 1000.0, "000300.SH": 3900.0,
              "899050.BJ": 1200.0}
POOLS = {"demo_hot": ("演示·热点", ["600000", "300825", "603709", "002594"]),
         "demo_value": ("演示·价值", ["601318", "000001", "300750", "688981"])}


def _r(*parts) -> float:
    """[-1, 1) 的确定性伪随机数。"""
    h = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return int.from_bytes(h[:8], "big") / 2 ** 63 - 1


def _times() -> list[str]:
    out = []
    for h, m0, m1 in ((9, 30, 60), (10, 0, 60), (11, 0, 31), (13, 1, 60), (14, 0, 60)):
        out += [f"{h:02d}:{m:02d}" for m in range(m0, m1)]
    return out + ["15:00"]


class DemoSource(DataSource):
    type_name = "demo"
    title = "演示数据（合成）"
    description = "确定性合成数据，实现标准数据接口全部能力"
    capabilities = ("pool", "quote", "minute", "daily", "index", "index_minute", "market_stats", "meta")

    def __init__(self, name="demo", cfg=None, data_dir=None, offline=False, root=None):
        import tempfile
        from pathlib import Path
        super().__init__(name, cfg or {}, Path(data_dir or tempfile.mkdtemp(prefix="qfdemo")), offline, root)

    # 内部：某代码某天的分时路径
    def _path(self, key: str, base: float, date: str) -> tuple[float, list[dict]]:
        pre = round(base * (1 + 0.02 * _r(key, date, "pre")), 2)
        drift = 0.02 * _r(key, date, "drift")
        pts, px, cum_amt, cum_vol = [], pre * (1 + 0.005 * _r(key, date, "open")), 0.0, 0.0
        for i, t in enumerate(_times()):
            px = px * (1 + drift / 240 + 0.0015 * _r(key, date, t))
            vol = round(1e5 * (1.5 + _r(key, date, t, "v")) * (2 if i < 30 else 1))
            cum_vol += vol
            cum_amt += vol * px
            pts.append({"t": t, "p": round(px, 2), "avg": round(cum_amt / cum_vol, 3), "vol": vol, "amt": round(vol * px, 2)})
        return pre, pts

    def _quote(self, std: str, name: str, base: float, date: str, index=False) -> dict:
        pre, pts = self._path(std, base, date)
        px = pts[-1]["p"]
        return {"code": std, "name": name, "price": px, "preclose": pre, "open": pts[0]["p"],
                "high": max(p["p"] for p in pts), "low": min(p["p"] for p in pts),
                "change": round(px - pre, 4), "change_pct": round((px - pre) / pre * 100, 4),
                "volume": sum(p["vol"] for p in pts) * (100 if index else 1), "amount": round(sum(p["amt"] for p in pts), 2),
                "ts": int(time.time() * 1000)}

    def meta(self) -> dict:
        return {"trading_date": today_str()}

    def list_pools(self) -> list[dict]:
        return [{"key": k, "name": v[0], "desc": "演示股池"} for k, v in POOLS.items()]

    def get_pool(self, key: str, date: str | None) -> PoolResult:
        name, codes = POOLS[key]
        d = date or today_str()
        stocks = []
        for c in codes:
            n, b = next((n, b) for cc, n, b in STOCKS if cc == c)
            q = self._quote(to_std_code(c), n, b, d)
            stocks.append({"code": c, "name": n, "fields": {"price": q["price"], "change_rate": round(q["change_pct"], 2),
                                                         "open_rise": round((q["open"] - q["preclose"]) / q["preclose"] * 100, 2),
                                                         "market_value": round(b * 30, 1)}})
        return PoolResult(key=f"{self.name}:{key}", name=name, trading_date=d, stocks=stocks)

    def get_quotes(self, codes: list[str]) -> dict[str, dict]:
        out = {}
        for c in codes:
            hit = next(((n, b) for cc, n, b in STOCKS if cc == c), None)
            if hit:
                out[c] = self._quote(to_std_code(c), hit[0], hit[1], today_str())
        return out

    def get_minute(self, code: str, date: str) -> dict:
        hit = next(((n, b) for cc, n, b in STOCKS if cc == code), None)
        if not hit:
            return {}
        pre, pts = self._path(to_std_code(code), hit[1], date)
        return {"code": code, "date": date, "preclose": pre, "open": pts[0]["p"], "high": max(p["p"] for p in pts),
                "low": min(p["p"] for p in pts), "points": pts}

    def get_daily(self, code: str, start: str, end: str) -> list[dict]:
        hit = next(((n, b) for cc, n, b in STOCKS if cc == code), None)
        if not hit:
            return []
        d, e, out = _date.fromisoformat(start), _date.fromisoformat(end), []
        while d <= e:
            if d.weekday() < 5:
                q = self._quote(to_std_code(code), hit[0], hit[1], d.isoformat())
                out.append({"date": d.isoformat(), "open": q["open"], "high": q["high"], "low": q["low"], "close": q["price"],
                            "volume": q["volume"], "amount": q["amount"]})
            d += timedelta(days=1)
        return out

    def get_indices(self, codes: list[str] | None = None) -> list[dict]:
        names = dict(DEFAULT_INDICES)
        cs = [index_std_code(c) for c in codes] if codes else list(INDEX_BASE)
        return [self._quote(c, names.get(c, c), INDEX_BASE[c], today_str(), index=True) for c in cs if c in INDEX_BASE]

    def get_index_minute(self, code: str, date: str) -> dict:
        c = index_std_code(code)
        if c not in INDEX_BASE:
            return {}
        pre, pts = self._path(c, INDEX_BASE[c], date)
        return {"code": c, "date": date, "preclose": pre, "open": pts[0]["p"], "high": max(p["p"] for p in pts),
                "low": min(p["p"] for p in pts), "points": pts}

    def get_market_stats(self, date: str) -> dict:
        up = int(2600 + 1400 * _r("stats", date))
        return {"date": date, "up": up, "down": 5200 - up, "flat": 120, "limit_up": int(60 + 40 * math.fabs(_r("lu", date))),
                "limit_down": int(5 + 10 * math.fabs(_r("ld", date))), "suspended": 8,
                "amount": round(1.1e12 * (1 + 0.3 * _r("amt", date)), -6), "ts": int(time.time() * 1000)}
