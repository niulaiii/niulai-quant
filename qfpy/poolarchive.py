"""股池成分股存档：每天把每个股池的成分股记下来，按股池回测就用这份历史。

存在 data/pool_archive/<日期>.json，一天一个文件：

    {"date": "2026-11-12", "pools": {
        "gushi:auction_long": {"title": "竞价龙头", "updated_at": "...", "origins": ["选股 09:26", "定时 15:10"],
                               "stocks": [{"code": "600000", "name": "...", "fields": {...入池字段...},
                                           "first_seen": "09:26:03"}]}}}

规则：
- 同一天多次存档（每次选股运行 + 收盘后定时一次）取并集：出现过就算当天在池里，字段用最新一次的。
- 只记录真实拉到的数据；拉取失败的池当天就没有记录，回测时会写明「这天没有存档」，不会拿别的日子补。
- 不会倒推历史：开始存档之前的日子永远没有数据。
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from .util import lock_for, now_cn, process_lock

_LOCK = threading.Lock()
_SKIP_FIELDS = {"minute", "daily"}


def _clean_fields(fields: dict) -> dict:
    out = {}
    for k, v in (fields or {}).items():
        if k.startswith("_") or k in _SKIP_FIELDS:
            continue
        if isinstance(v, (int, float, str, bool)) or v is None:
            out[k] = v
    return out


class PoolArchive:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir) / "pool_archive"

    def _path(self, day: str) -> Path:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day or ""):
            raise ValueError(f"日期格式不对：{day}")
        return self.dir / f"{day}.json"

    def load(self, day: str) -> dict:
        p = self._path(day)
        if not p.exists():
            return {"date": day, "pools": {}}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {"date": day, "pools": {}, "broken": True}

    def record(self, day: str, key: str, stocks: list[dict], title: str = "", origin: str = "") -> int:
        """stocks: [{code, name, fields}]。返回这个池当天存档后的只数。"""
        t = now_cn().strftime("%H:%M:%S")
        with _LOCK, process_lock(lock_for(self._path(day))):
            doc = self.load(day)
            doc.pop("broken", None)
            pool = doc["pools"].setdefault(key, {"title": title, "stocks": [], "origins": []})
            if title:
                pool["title"] = title
            by_code = {s["code"]: s for s in pool["stocks"]}
            for s in stocks:
                code = str(s.get("code") or "").strip()
                if not code:
                    continue
                old = by_code.get(code)
                row = {"code": code, "name": s.get("name") or (old or {}).get("name") or "",
                       "fields": _clean_fields(s.get("fields") or {}) or (old or {}).get("fields") or {},
                       "first_seen": (old or {}).get("first_seen") or t}
                by_code[code] = row
            pool["stocks"] = list(by_code.values())
            pool["updated_at"] = f"{day} {t}"
            if origin and origin not in pool["origins"]:
                pool["origins"].append(origin)
            self.dir.mkdir(parents=True, exist_ok=True)
            p = self._path(day)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            tmp.replace(p)
            return len(pool["stocks"])

    def days(self) -> list[str]:
        if not self.dir.exists():
            return []
        return sorted(p.stem for p in self.dir.glob("*.json") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem))

    def members(self, key: str, start: str, end: str) -> dict[str, list[dict]]:
        """某个池在 [start, end] 内每个有存档的日子的成分股：{日期: [{code,name,fields}]}。没存档的日子不出现。"""
        out = {}
        for d in self.days():
            if start <= d <= end:
                pool = (self.load(d).get("pools") or {}).get(key)
                if pool is not None:
                    out[d] = pool.get("stocks") or []
        return out

    def summary(self) -> dict:
        """网页用：每个池有哪些天的存档、最近一天几只。"""
        pools: dict[str, dict] = {}
        for d in self.days():
            for key, p in (self.load(d).get("pools") or {}).items():
                it = pools.setdefault(key, {"pool": key, "title": p.get("title") or "", "days": 0, "first": d, "last": d,
                                            "last_count": 0})
                it["days"] += 1
                it["last"] = d
                it["last_count"] = len(p.get("stocks") or [])
                it["title"] = p.get("title") or it["title"]
        return {"days": self.days(), "pools": list(pools.values())}
