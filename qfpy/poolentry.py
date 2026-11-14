"""新入池判定：股池每次更新时，只把「新加入」的票送进这个池绑定的买入策略。

规则（按股池各算各的）：
- 每个股池记一份「上一次成功拉到的成分股」和「已评估过的票」，存在 data/pool_entry/<交易日>.json。
- 一只票在这次拉到的成分股里、而且今天在这个池里还没评估过 → 新入池，送进策略。
- 已经评估过、一直在池里的票，后面每次更新都不再重复送进策略。
- 被移出（某次成功拉取时不在池里）又重新出现 → 算新入池，再评估一次。
- 「今天」按交易日算：默认 [pool].entry_scope = "day"，每个交易日重新开始（gushi 这类按日期出的策略股池，
  每天的名单都是当天的新信号）。设成 "continuous" 时跨交易日连续算：昨天已在池里、今天还在的票不算新入池，
  适合自选股这类长期不变的池。也可以在 [pool].entry_scope_by_pool 里按池单独设。
- [pool].entry_mode = "all" 时关掉这套判定，每次更新把池里全部的票都送进策略（旧做法）。

只有正式运行（定时任务、补录、网页上「立即运行」）才会记下「已评估」和成分股；只看不下单的试跑不记，
所以试跑不会把新票「用掉」。拉取失败、或用的是其他日期的缓存时，这个池这一次没有新入池，也不更新成分股。
"""
from __future__ import annotations

import threading
from pathlib import Path

from .util import lock_for, process_lock, read_json, write_json

_LOCK = threading.Lock()


def entry_mode(cfg: dict, pool: str | None = None) -> str:
    """new = 只送新入池的票；all = 每次送全部成分股。
    按池单独设：[pool].entry_scope_by_pool 里写 "all"（网页「新入池方式」选「全部」）时，这个池按 all 算。"""
    p = cfg.get("pool") or {}
    if pool and str((p.get("entry_scope_by_pool") or {}).get(pool) or "").lower() in ("all", "全部"):
        return "all"
    return "all" if str(p.get("entry_mode", "new")).lower() in ("all", "全部") else "new"


def entry_scope(cfg: dict, pool: str) -> str:
    p = cfg.get("pool") or {}
    v = (p.get("entry_scope_by_pool") or {}).get(pool) or p.get("entry_scope") or "day"
    if str(v).lower() in ("all", "全部"):
        v = p.get("entry_scope") or "day"
    return "continuous" if str(v).lower() in ("continuous", "连续") else "day"


class PoolEntry:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir) / "pool_entry"

    def _file(self, day: str) -> Path:
        return self.dir / f"{day}.json"

    def load(self, day: str) -> dict:
        return read_json(self._file(day), None) or {}

    def _prev_day_state(self, day: str) -> dict:
        if not self.dir.exists():
            return {}
        prev = sorted(f.stem for f in self.dir.glob("*.json") if f.stem < day)
        return self.load(prev[-1]) if prev else {}

    def split(self, cfg: dict, day: str, pool: str, codes: list[str]) -> dict:
        """把这次拉到的成分股分成新入池 / 不重复进入两组。只读，不写。

        返回 {"new": [...], "seen": {code: 第一次评估时间}, "returned": [被移出后重新入池的], "mode", "scope"}。
        """
        mode = entry_mode(cfg, pool)
        scope = entry_scope(cfg, pool)
        if mode == "all":
            return {"new": list(codes), "seen": {}, "returned": [], "mode": mode, "scope": scope}
        st = self.load(day).get(pool) if not pool.startswith("_") else None
        if st is None and scope == "continuous":
            prev = self._prev_day_state(day).get(pool) or {}
            # 跨日连续：上一个交易日收盘时还在池里、并且评估过的票，今天继续算「已评估」
            members = set(prev.get("members") or [])
            st = {"members": sorted(members),
                  "evaluated": {c: v for c, v in (prev.get("evaluated") or {}).items() if c in members}}
        st = st or {}
        evaluated = st.get("evaluated") or {}
        last = set(st.get("members") or [])
        new, seen, returned = [], {}, []
        for c in codes:
            if c in evaluated and c in last:
                seen[c] = evaluated[c]
            else:
                new.append(c)
                if c in evaluated and c not in last:
                    returned.append(c)
        return {"new": new, "seen": seen, "returned": returned, "mode": mode, "scope": scope}

    def commit(self, day: str, pool: str, members: list[str], evaluated: list[str], when: str, run_id: str) -> None:
        """正式运行后记下：这次的成分股；这次送进策略的票记为已评估。被移出的票保留评估记录，重新入池时靠 members 判断。"""
        with _LOCK, process_lock(lock_for(self._file(day))):
            d = self.load(day)
            st = d.setdefault(pool, {"members": [], "evaluated": {}})
            ev = st.setdefault("evaluated", {})
            for c in evaluated:
                ev[c] = {"at": when, "run": run_id}
            st["members"] = sorted(set(members))
            st["updated_at"] = when
            write_json(self._file(day), d)

    def mark_picked(self, day: str, picked: dict[str, str]) -> None:
        """记下今天哪只票由哪个池入选过：另一个池之后再新入池时不重复买。"""
        with _LOCK, process_lock(lock_for(self._file(day))):
            d = self.load(day)
            d.setdefault("_picked", {}).update({k: v for k, v in picked.items() if v})
            write_json(self._file(day), d)

    def view(self, day: str) -> dict:
        return self.load(day)
