"""持久化开关：自动交易开关、一键停止。都存在 data/state/ 下，重启后保持。

写入统一走 update_json（读-改-写整段在跨进程排他锁内），
避免网页和命令行同时改开关时后写的把先写的整份覆盖。
"""
from __future__ import annotations

from pathlib import Path

from ..util import read_json, ts, update_json


class Switches:
    def __init__(self, data_dir: Path, default_auto: bool):
        self.dir = Path(data_dir) / "state"
        self.default_auto = bool(default_auto)

    # ---- 自动交易开关 ----
    def _auto(self) -> dict:
        return read_json(self.dir / "automation.json", None) or {"enabled": self.default_auto, "changed_at": None,
                                                                 "by": "默认值", "history": []}

    def auto_enabled(self) -> bool:
        return bool(self._auto()["enabled"])

    def auto_state(self) -> dict:
        d = self._auto()
        return {"enabled": bool(d["enabled"]), "changed_at": d.get("changed_at"), "by": d.get("by"),
                "history": (d.get("history") or [])[-10:]}

    def set_auto(self, enabled: bool, by: str) -> dict:
        t = ts()

        def mut(d):
            d = dict(d or {})
            d["history"] = (d.get("history") or [])[-49:] + [{"at": t, "enabled": bool(enabled), "by": by}]
            d.update(enabled=bool(enabled), changed_at=t, by=by)
            return d

        update_json(self.dir / "automation.json", mut,
                    default={"enabled": self.default_auto, "changed_at": None, "by": "默认值", "history": []})
        return self.auto_state()

    # ---- 一键停止 ----
    def halt_state(self) -> dict:
        return read_json(self.dir / "halt.json", None) or {"halted": False}

    def halted(self) -> bool:
        return bool(self.halt_state().get("halted"))

    def halt(self, reason: str, by: str) -> dict:
        return update_json(self.dir / "halt.json",
                           lambda d: {"halted": True, "reason": reason or "手动停止", "at": ts(), "by": by},
                           default={"halted": False})

    def resume(self, by: str) -> dict:
        return update_json(self.dir / "halt.json",
                           lambda d: {"halted": False, "resumed_at": ts(), "by": by, "previous": d or {"halted": False}},
                           default={"halted": False})

    # ---- 持仓获取方式：自动（下单程序快照）/ 手动填写 ----
    def _pos(self) -> dict:
        return read_json(self.dir / "positions_mode.json", None) or {"auto": False, "changed_at": None, "by": "默认值", "history": []}

    def positions_auto(self) -> bool:
        return bool(self._pos()["auto"])

    def positions_state(self) -> dict:
        d = self._pos()
        return {"auto": bool(d["auto"]), "changed_at": d.get("changed_at"), "by": d.get("by"), "history": (d.get("history") or [])[-10:]}

    def set_positions_auto(self, auto: bool, by: str) -> dict:
        t = ts()

        def mut(d):
            d = dict(d or {})
            d["history"] = (d.get("history") or [])[-49:] + [{"at": t, "auto": bool(auto), "by": by}]
            d.update(auto=bool(auto), changed_at=t, by=by)
            return d

        update_json(self.dir / "positions_mode.json", mut,
                    default={"auto": False, "changed_at": None, "by": "默认值", "history": []})
        return self.positions_state()
