"""自选股列表：配置里手填 codes，或 CSV（code,name）。价格字段由分时数据补全。"""
from __future__ import annotations

import csv
import re

from ..util import today_str
from .base import DataSource, PoolResult, register_source


def read_watchlist_csv(path) -> list[tuple[str, str]]:
    out = []
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.reader(f):
                if not row:
                    continue
                code = re.sub(r"\D", "", row[0])
                if len(code) != 6:
                    continue  # 跳过表头/非法行
                out.append((code, (row[1].strip() if len(row) > 1 else "")))
    except FileNotFoundError:
        pass
    return out


@register_source
class WatchlistSource(DataSource):
    type_name = "watchlist"
    title = "自选股列表"
    description = "手填代码或 CSV（code,name）；现价/涨幅等字段由分时数据补全，没有市值字段"
    capabilities = ("pool",)
    config_fields = [{"name": "codes", "hint": "代码列表"}, {"name": "csv", "hint": "CSV 文件路径"}]

    def entries(self) -> list[tuple[str, str]]:
        seen: dict[str, str] = {}
        for c in self.cfg.get("codes", []) or []:
            c = re.sub(r"\D", "", str(c))
            if len(c) == 6:
                seen.setdefault(c, "")
        p = self.cfg.get("csv")
        if p:
            path = self.root / p if not str(p).startswith("/") else p
            for code, name in read_watchlist_csv(path):
                if not seen.get(code):
                    seen[code] = name
        return list(seen.items())

    def list_pools(self) -> list[dict]:
        return [{"key": "all", "name": "自选股", "desc": f"{len(self.entries())} 只"}]

    def get_pool(self, key: str, date: str | None) -> PoolResult:
        stocks = [{"code": c, "name": n, "fields": {}} for c, n in self.entries()]
        return PoolResult(key=f"{self.name}:{key}", name="自选股", trading_date=date or today_str(), stocks=stocks)
