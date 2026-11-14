"""和 Windows 上下单程序的连接：连接状态、券商持仓/委托/成交快照、实盘自动对账。

调用频率（按接口文档的耗时定）：
- 交易时段每 poll_secs（默认 20 秒）查一次 /api/status；非交易时段每 idle_poll_secs（300 秒）。
- 连上时每 reconcile_secs（60 秒）读一次 /api/snapshot（命中缓存几十毫秒）做对账。
- 有在途委托或上次对账有疑点时，每 full_refresh_secs（120 秒）最多一次 /api/snapshot?refresh=1 强制重扫，
  只有这种“新鲜”数据才会触发“找不到委托 / 数量不一致”的判定。
- 下单/撤单后立刻按文档推荐调 /api/snapshot?refresh=1&no_render=1（只扫内存、不切页，约 1 秒）。
- /api/refresh（可能切页 12~17 秒）和 /api/requery（会激活客户端窗口）只在网页上手动点时调。
只有实盘模式才对账（改本地账本）；模拟盘/演练模式下只显示券商数据。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from ..util import log, now_cn, read_json, today_str, ts, write_json
from .reconcile import reconcile
from .xiadan import XiadanClient, XiadanError, auth_source, normalize_snapshot


class BrokerLink:
    def __init__(self, cfg: dict, book, notifier, data_dir: Path, transport=None,
                 clock: Callable[[], float] = time.monotonic, now_fn=now_cn):
        self.cfg, self.book, self.notifier = cfg, book, notifier
        self.mode = cfg["execution"]["mode"]
        self.live_cfg = cfg["live"]
        self.client = XiadanClient(self.live_cfg, transport=transport, clock=clock)
        self._transport = transport
        self.clock, self.now_fn = clock, now_fn
        self.path = Path(data_dir) / "state" / "broker.json"
        saved = read_json(self.path, {}) or {}
        self.mismatch: dict = saved.get("mismatch", {})
        self.events: list[dict] = saved.get("events", [])
        self.snapshot: dict | None = saved.get("snapshot")
        self.snapshot_at: str | None = saved.get("snapshot_at")
        self.snapshot_via: str | None = saved.get("snapshot_via")
        self.report: dict | None = saved.get("report")
        self.auto_positions_fn: Callable[[], bool] = lambda: False    # 引擎注入：「自动获取持仓」开关
        self.adopt_log: list[dict] = saved.get("adopt_log", [])
        self.conn: dict = {"state": "unknown", "checked_at": None}
        self.lock = threading.RLock()
        self._next_status = 0.0
        self._next_recon = 0.0
        self._next_full = 0.0
        self._was_connected: bool | None = None
        self._snap_mono: float | None = None
        from ..bg import BgJob
        # 网页触发的同步一律放到后台线程，请求线程不等待下单程序（不可达时连接超时要好几秒）
        self.sync_job = BgJob("下单程序同步", self._do_sync, max_age=0)

    def reconfigure(self, cfg: dict, book) -> None:
        """网页改了运行模式 / 下单程序地址、口令、超时、对账参数：原地替换（后台轮询线程继续用这个对象）。"""
        with self.lock:
            self.cfg, self.book = cfg, book
            self.mode = cfg["execution"]["mode"]
            self.live_cfg = cfg["live"]
            self.client = XiadanClient(self.live_cfg, transport=self._transport, clock=self.clock)
            self.conn = {"state": "unknown", "checked_at": None}
            self._next_status = 0.0

    @property
    def reconciling(self) -> bool:
        return self.mode == "live"

    def _save(self) -> None:
        write_json(self.path, {"mismatch": self.mismatch, "events": self.events[-100:], "snapshot": self.snapshot,
                               "snapshot_at": self.snapshot_at, "snapshot_via": self.snapshot_via, "report": self.report,
                               "adopt_log": self.adopt_log[-50:]})

    # ---------- 连接状态 ----------
    def check_status(self, force: bool = False) -> dict:
        t0 = time.monotonic()
        try:
            v = self.client.status(force=force)
            g = v.get("guard") or {}
            ok = bool(v.get("ok"))
            st = "connected" if ok else ("reachable" if v else "error")
            self.conn = {"state": st, "ok": ok, "message": v.get("message"), "executor": v.get("executor"),
                         "pid": v.get("pid"), "guard": g, "checked_at": ts(), "latency_ms": int((time.monotonic() - t0) * 1000),
                         "error": None}
        except XiadanError as e:
            if e.kind == "rate_limited":
                return self.conn
            self.conn = {"state": {"auth": "auth_failed", "not_connected": "reachable"}.get(e.kind, "unreachable"),
                         "ok": False, "checked_at": ts(), "error": e.to_dict(),
                         "latency_ms": int((time.monotonic() - t0) * 1000)}
        connected = self.conn.get("state") == "connected"
        if self._was_connected is not None and connected != self._was_connected and self.reconciling:
            self.notifier.notify("broker_link", "下单程序已连上" if connected else "下单程序连接中断",
                                 f"- {self.client.base}\n- {self.conn.get('message') or (self.conn.get('error') or {}).get('message', '')}",
                                 dedupe_key=f"link:{connected}", dedupe_secs=600)
        self._was_connected = connected
        return self.conn

    # ---------- 快照 ----------
    def fetch(self, how: str = "cached") -> dict:
        """how: cached / fresh（refresh=1）/ after_trade（refresh=1&no_render=1）/ refresh（/api/refresh）/ requery。"""
        c = self.client
        raw = {"cached": lambda: c.snapshot(), "fresh": lambda: c.snapshot(refresh=True),
               "after_trade": c.snapshot_after_trade, "refresh": lambda: c.refresh(), "requery": lambda: c.requery()}[how]()
        snap = normalize_snapshot(raw)
        with self.lock:
            self.snapshot, self.snapshot_at, self.snapshot_via = snap, ts(), how
            self._snap_mono = self.clock()
            self._save()
        return snap

    def snapshot_age(self) -> float | None:
        return None if self._snap_mono is None else self.clock() - self._snap_mono

    # ---------- 对账 ----------
    def reconcile_now(self, how: str = "fresh") -> dict:
        """读一次快照并对账。只有实盘模式改本地账本。下单程序不可用时抛 XiadanError。"""
        snap = self.fetch(how)
        if not self.reconciling:
            return {"skipped": f"当前是 {self.mode} 模式，只显示券商数据，不对账（自动获取持仓也只在实盘模式写入本地）"}
        fresh = how in ("fresh", "refresh", "requery")
        with self.book.lock, self.lock:
            rep = reconcile(self.book, snap, self.now_fn().replace(tzinfo=None), today_str(), fresh,
                            self.live_cfg.get("reconcile") or {}, self.mismatch)
            rep["via"] = how
            if self.auto_positions_fn():
                from .reconcile import positions_trustworthy
                ch = self.book.adopt_broker(snap, today_str(), positions_trustworthy(snap) and not rep.get("stale_positions"),
                                           set(rep.get("stale_positions") or []))
                rep["adopted"] = ch
                for c in ch:
                    self.adopt_log.append({**c, "at": rep["at"]})
            self.report = rep
            for e in rep["events"]:
                if e["kind"] == "positions_stale" and any(x.get("kind") == "positions_stale" and x["symbol"] == e["symbol"]
                                                          and x.get("at", "")[:10] == rep["at"][:10] for x in self.events[-20:]):
                    continue
                self.events.append({**e, "at": rep["at"]})
            self._save()
        for e in rep["events"]:
            if e.get("notify"):
                self.notifier.notify("reconcile", f"实盘对账：{e['symbol']}", f"- {e['message']}",
                                     dedupe_key=f"recon:{e['kind']}:{e['symbol']}:{e['message']}", dedupe_secs=3600)
        if rep["events"]:
            log.info("对账（%s）：%s", how, "；".join(e["message"] for e in rep["events"])[:800])
        return rep

    def after_trade(self) -> dict | None:
        """下单/撤单后的同步。失败只记日志（委托本身已经记为待确认/未知，后台轮询会继续对账）。
        自动获取持仓关闭时不读券商数据（委托成交由你在委托记录里补记）。"""
        if not self.auto_positions_fn():
            return None
        try:
            return self.reconcile_now("after_trade")
        except Exception as e:  # noqa: BLE001
            log.warning("下单后同步失败：%s（后台对账会继续）", e)
            return None
        finally:
            self._next_full = min(self._next_full, self.clock() + 30)

    def sync_before_sell(self) -> bool:
        """自动卖出前对一次账，让卖出用上券商最新的可卖数量。返回是否拿到了新数据。自动获取持仓关闭时不读。"""
        if not self.auto_positions_fn():
            return False
        try:
            self.reconcile_now("fresh")
            return True
        except XiadanError as e:
            if e.kind == "rate_limited" and (self.snapshot_age() or 1e9) < 60:
                return True
            log.warning("卖出前对账失败：%s", e)
            return False

    def broker_available(self, code: str, max_age: float = 120) -> int | None:
        """券商可卖数量（快照足够新且持仓表可信时）。不可用返回 None。自动获取持仓关闭时不用券商数据。"""
        if not self.auto_positions_fn():
            return None
        age = self.snapshot_age()
        if not self.snapshot or age is None or age > max_age:
            return None
        for p in self.snapshot.get("positions") or []:
            if p["symbol"] == code:
                return int(p["available"])
        from .reconcile import positions_trustworthy
        return 0 if positions_trustworthy(self.snapshot) else None

    # ---------- 后台轮询 ----------
    def tick(self, in_session: bool) -> None:
        # 自动获取持仓关闭时后台什么都不拉：页面只显示手动填写的资金、持仓和委托
        if not self.live_cfg.get("monitor", True) or not self.auto_positions_fn():
            return
        now = self.clock()
        if now < self._next_status:
            return
        gap = float(self.live_cfg.get("poll_secs", 20) if in_session else self.live_cfg.get("idle_poll_secs", 300))
        self._next_status = now + gap
        st = self.check_status()
        if st.get("state") != "connected":
            return
        need_full = bool(self.book.pending()) or bool(self.mismatch) or bool(self.report and self.report.get("stale_positions"))
        try:
            if self.reconciling and need_full and now >= self._next_full:
                self._next_full = now + float(self.live_cfg.get("full_refresh_secs", 120))
                self._next_recon = now + float(self.live_cfg.get("reconcile_secs", 60))
                self.reconcile_now("fresh")
            elif now >= self._next_recon:
                self._next_recon = now + float(self.live_cfg.get("reconcile_secs", 60)) * (1 if in_session else 5)
                self.reconcile_now("cached")
        except XiadanError as e:
            if e.kind != "rate_limited":
                log.warning("对账读取失败：%s", e)

    def start(self, in_session_fn: Callable[[], bool]) -> None:
        if not self.live_cfg.get("monitor", True):
            return

        def loop():
            while True:
                try:
                    self.tick(in_session_fn())
                except Exception as e:  # noqa: BLE001
                    log.warning("下单程序轮询异常：%s", e)
                time.sleep(2)

        threading.Thread(target=loop, name="broker-link", daemon=True).start()

    # ---------- 网页触发的同步（后台） ----------
    def _do_sync(self, how: str) -> dict:
        if how != "status" and not self.auto_positions_fn():
            return {"how": how, "sync_error": "自动获取持仓已关闭，不读取券商数据"}
        st = self.check_status(force=True)
        if how == "status":
            return {"how": how, "sync_error": None}
        if st.get("state") not in ("connected", "reachable"):
            return {"how": how, "sync_error": (st.get("error") or {}).get("message") or "下单程序不可用"}
        try:
            self.reconcile_now(how)
        except XiadanError as e:
            return {"how": how, "sync_error": str(e)}
        return {"how": how, "sync_error": None}

    def request_sync(self, how: str = "status") -> bool:
        """在后台做一次同步，立刻返回（是否新启动了）。结果通过 view()['sync'] 和 /api/broker 轮询拿到。"""
        return self.sync_job.kick(how, force=True)

    def sync_view(self) -> dict:
        v = self.sync_job.view()
        r = self.sync_job.result or {}
        v["how"] = (self.sync_job.args or (r.get("how"),))[0]
        v["sync_error"] = None if v["running"] else (v["error"] or r.get("sync_error"))
        return v

    def invalidate(self) -> None:
        """之前读到的快照作废（比如刚打开自动获取持仓）：在拿到新数据之前，页面不显示旧数据。"""
        with self.lock:
            self._snap_mono = None

    def freshness(self) -> dict:
        """券商数据是不是最新：本次运行里读到、且不超过 live.stale_secs（默认 600 秒）。旧数据只给时间，不给内容。"""
        age = self.snapshot_age()
        limit = float(self.live_cfg.get("stale_secs", 600))
        fresh = bool(self.snapshot) and age is not None and age <= limit
        dt = (self.snapshot or {}).get("ts")
        if isinstance(dt, (int, float)) and dt > 0:
            from datetime import datetime
            dt = datetime.fromtimestamp(dt / 1000 if dt > 1e11 else dt).strftime("%Y-%m-%d %H:%M:%S")
        return {"fresh": fresh, "age_secs": None if age is None else round(age, 1), "stale_secs": limit,
                "fetched_at": self.snapshot_at, "data_time": dt or self.snapshot_at, "via": self.snapshot_via,
                "broker_cached": bool((self.snapshot or {}).get("cached"))}

    def account_view(self) -> dict:
        """账户页（自动获取持仓打开时）用：只有最新数据才给资金和持仓。"""
        with self.lock:
            fr = self.freshness()
            snap = self.snapshot if fr["fresh"] else None
            return {"conn": self.conn, "sync": self.sync_view(), "base_url": self.client.base, "freshness": fr,
                    "money": (snap or {}).get("money"), "positions": (snap or {}).get("positions"),
                    "stale_positions": bool(self.report and self.report.get("stale_positions")) if snap else False,
                    "pending": list(self.book.pending().values()), "adopt_log": list(reversed(self.adopt_log[-10:]))}

    def diag_view(self) -> dict:
        """设置页「下单程序」的连接诊断：连接状态、最近调用、对账事件。不含券商资金和持仓。"""
        with self.lock:
            return {"sync": self.sync_view(), "base_url": self.client.base, "auth": auth_source(self.live_cfg),
                    "monitor": self.live_cfg.get("monitor", True), "mode": self.mode, "conn": self.conn,
                    "events": list(reversed(self.events[-30:])), "calls": list(reversed(self.client.calls[-15:])),
                    "pending_done": list(reversed((self.book.s.get("pending_done") or [])[-20:]))}

    # ---------- 网页 ----------
    def view(self) -> dict:
        with self.lock:
            return {"sync": self.sync_view(), "base_url": self.client.base, "auth": auth_source(self.live_cfg), "monitor": self.live_cfg.get("monitor", True),
                    "mode": self.mode, "reconciling": self.reconciling, "conn": self.conn, "snapshot": self.snapshot,
                    "snapshot_at": self.snapshot_at, "snapshot_via": self.snapshot_via, "report": self.report,
                    "events": list(reversed(self.events[-30:])), "pending": list(self.book.pending().values()),
                    "pending_done": list(reversed((self.book.s.get("pending_done") or [])[-20:])),
                    "calls": list(reversed(self.client.calls[-15:])), "adopt_log": list(reversed(self.adopt_log[-20:]))}
