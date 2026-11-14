"""核心数据结构。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Candidate:
    code: str
    name: str
    sources: list[str] = field(default_factory=list)     # 命中的股池 key，如 gushi:auction_long
    fields: dict[str, Any] = field(default_factory=dict)
    minute_loaded: bool = False
    minute_error: str | None = None
    daily: list[dict] | None = None                      # 日K（升序，最后一根是交易日当天）
    daily_error: str | None = None
    daily_synth: bool = False                            # 最后一根是否用分时/股池拼的
    first_rank: int | None = None                        # 第一根日K是上市第几个交易日（知道上市日时 = 1）
    info: dict = field(default_factory=dict)             # 个股资料：行业、上市日、总股本等（拿不到就是空）
    src_fields: dict = field(default_factory=dict)       # 外部数据源给的字段，按数据源分开记：{数据源: {字段: 值}}
    _daily_cache: dict = field(default_factory=dict, repr=False)
    _ind_rows: list | None = field(default=None, repr=False)

    def get(self, k: str):
        v = self.fields.get(k)
        return None if v is None else v

    def price(self) -> float:
        return float(self.fields.get("minute_last") or self.fields.get("price") or 0.0)

    def daily_at(self, field_name: str, days_ago: int):
        """取 N 个交易日前的日K字段；days_ago=0 就是当天。"""
        if not self.daily:
            return None
        idx = len(self.daily) - 1 - int(days_ago)
        if idx < 0:
            return None
        if idx not in self._daily_cache:
            from .market import daily_fields_at
            self._daily_cache[idx] = daily_fields_at(self.daily, idx, self.indicator_rows())
        return self._daily_cache[idx].get(field_name)

    def indicator_rows(self) -> list:
        if self._ind_rows is None:
            from .indicators import series_rows
            self._ind_rows = series_rows(self.daily or [], self.code, self.name, self.first_rank)
        return self._ind_rows

    def listing_date(self) -> str | None:
        if self.info.get("listing_date"):
            return str(self.info["listing_date"])[:10]
        if self.first_rank == 1 and self.daily:
            return self.daily[0]["date"]
        return None

    def listing_days(self, day: str | None = None) -> int | None:
        """上市以来的自然日天数（上市当天 = 0）；不知道上市日返回 None。"""
        ld = self.listing_date()
        if not ld:
            return None
        from datetime import date as _d
        end = day or (self.daily[-1]["date"] if self.daily else None)
        if not end:
            return None
        return (_d.fromisoformat(end[:10]) - _d.fromisoformat(ld)).days

    def brief(self, value=None, note: str = "") -> dict:
        keep = ("price", "change_rate", "open_rise", "market_value", "hit_count", "vwap_dev_pct", "amount_yi",
                "drawdown_pct", "limit_gap_pct", "score", "minute_last", "limit_up", "close", "ma5")
        return {"code": self.code, "name": self.name, "sources": self.sources, "value": value, "note": note,
                "fields": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in self.fields.items() if k in keep and v is not None}}
