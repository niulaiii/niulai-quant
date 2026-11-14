"""分级股池（借用 Alpha 的「候选 → 重点 → 买入」三级池和迁池校验）。

每次运行后：前池里的票进候选池，通过全部因子的晋升重点池，最终入选的晋升买入池（逐级晋升、不自动降级）。
你可以在网页上手动迁池跟踪，迁池规则由 Alpha 的 validate_pool_transition 判定（晋升必须逐级、降级可跨级）。
分级池只用于跟踪展示，不会触发下单。
"""
from __future__ import annotations

from pathlib import Path

from .util import lock_for, process_lock, read_json, ts, write_json
from .vendor.alpha_pool_transition import (POOL_BUY, POOL_CANDIDATE, POOL_FOCUS, POOL_LEVELS, POOL_TIER_NAMES,
                                           record_pool_move, validate_pool_transition)


class Tiers:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir) / "tiers"

    def load(self, day: str) -> dict:
        return read_json(self.dir / f"{day}.json", None) or {}

    def save(self, day: str, d: dict) -> None:
        write_json(self.dir / f"{day}.json", d)

    def apply_run(self, day: str, pool: list, focus: list, picks: list, run_id: str) -> dict:
        with process_lock(lock_for(self.dir / f"{day}.json")):
            d = self.load(day)
            now = ts()
            for c in pool:
                e = d.setdefault(c.code, {"code": c.code, "name": c.name, "tier": POOL_CANDIDATE, "sources": [], "trigger_log": []})
                e["sources"] = sorted(set(e.get("sources", [])) | set(c.sources))
                e["updated_at"] = now
            for target, group in ((POOL_FOCUS, focus), (POOL_BUY, picks)):
                for c in group:
                    e = d[c.code]
                    while POOL_LEVELS[e["tier"]] < POOL_LEVELS[target]:
                        nxt = [k for k, v in POOL_LEVELS.items() if v == POOL_LEVELS[e["tier"]] + 1][0]
                        dec = validate_pool_transition(e["tier"], nxt)
                        if not dec.allowed:
                            break
                        record_pool_move(e, f"自动晋升（运行 {run_id}）: {e['tier']} -> {nxt}", now)
                        e["tier"] = nxt
            self.save(day, d)
        return d

    def move(self, day: str, code: str, target: str) -> dict:
        with process_lock(lock_for(self.dir / f"{day}.json")):
            d = self.load(day)
            e = d.get(code)
            dec = validate_pool_transition(e["tier"] if e else None, target)
            if e is None:
                return {"ok": False, "code": "missing", "message": "股票不存在于当前选股池"}
            if not dec.allowed:
                return {"ok": False, "code": dec.code, "message": dec.message}
            if dec.code != "ok_idempotent":
                record_pool_move(e, dec.reason, ts())
                e["tier"] = target
                self.save(day, d)
        return {"ok": True, "code": dec.code, "message": dec.message, "entry": e}

    def view(self, day: str) -> dict:
        d = self.load(day)
        out = {k: [] for k in POOL_LEVELS}
        for e in d.values():
            out[e["tier"]].append(e)
        return {"date": day, "names": POOL_TIER_NAMES, "tiers": out}
