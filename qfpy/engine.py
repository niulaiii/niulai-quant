"""引擎：选股 → 因子漏斗 → 打分 → 下单；退出检查与人工保留；退出前提醒。

自动交易开关在这里生效：关闭时一切照常（选股、漏斗、风控预检、推送），只是不把委托交给券商适配层，
委托记录的状态是 signal_only（本应下单）。
"""
from __future__ import annotations

import threading
import time
import traceback
from pathlib import Path

from .config import ConfigStore
from .datasources import NotSupported, build_sources
from .exec.book import STATE_LABELS, Book
from .exec.brokerlink import BrokerLink
from .exec.brokers import live_gate, make_broker
from .exec.journal import OrderJournal
from .exec.risk import Risk
from .exec.state import Switches
from .exits import DECISION_LABELS, evaluate_chain, exit_times, position_candidate, rule_for
from .bg import BgJob
from .external import ExternalStore
from .factors.base import FactorContext
from .funnel import build_layers, layers_of, run_buy_funnel
from .market import Market, limit_info, summarize_minute
from . import extsources
from .model import Candidate
from .notify import Notifier
from .plugins import HOLD_POLICIES, load_plugins
from .tiers import Tiers
from .util import (append_jsonl, in_sessions, log, now_cn, read_json, read_jsonl, round2, round_buy_qty, today_str,
                   ts, write_json)
from .vendor import tsp_price_limits as pl

STATUS_LABELS = {"filled": "模拟成交", "unfilled": "模拟未成交", "dry_run": "演练（未发送）", "submitted": "已提交",
                 "unknown": "结果未知", "rejected": "被拒绝", "risk_rejected": "风控拦截", "signal_only": "仅信号（自动交易关闭）",
                 "hold_skipped": "保留跳过"}


class Engine:
    def __init__(self, store: ConfigStore, offline: bool = False):
        self.store = store
        self.offline = offline
        cfg = store.get()
        self.root = store.root
        self.data_dir = store.data_dir
        self.plugins = load_plugins(self.root / "plugins")
        self.switches = Switches(self.data_dir, cfg["automation"].get("default_enabled", False))
        self.notifier = Notifier(store.get, self.data_dir)
        self.risk = Risk(store.get, self.data_dir, self.switches)
        self.mode = cfg["execution"]["mode"]          # 网页改运行模式/实盘设置后由 _reconfigure 热切换
        self.book = Book(self.data_dir, self.mode, cfg["paper"])
        self.journal = OrderJournal(self.data_dir, self.mode)                   # 交易委托记录（系统委托 + 手动补记）
        self.link = BrokerLink(cfg, self.book, self.notifier, self.data_dir)   # 下单程序连接/券商快照/实盘对账
        self.link.auto_positions_fn = self.auto_pos
        self.broker = make_broker(cfg, self.book, self.link)      # 实盘三重开关不全时这里直接抛错
        self.tiers = Tiers(self.data_dir)
        from .poolentry import PoolEntry
        self.pool_entry = PoolEntry(self.data_dir)
        self.ext_store = ExternalStore(self.data_dir)    # 外部推送的因子结果/股池/字段
        from .strategies import Strategies
        self.strategies = Strategies(self)               # 多套买入/卖出策略、生效策略、模板（data/strategies、data/templates）
        # 网页持仓页的卖出预览要拉行情（冷缓存时几秒），放后台算，页面先显示上次的结果
        self.preview_job = BgJob("卖出预览", self.preview_exits, max_age=20)
        self.run_lock = threading.Lock()
        self._sources = None
        self._sources_ver = -1
        store.listeners.append(self._on_config)

    def reload_plugins(self) -> list[dict]:
        """重新扫描 plugins/：新放进去的文件会注册，改过的文件会重新执行。"""
        self.plugins = load_plugins(self.root / "plugins")
        self._sources = None
        return self.plugins

    # ---------- 基础 ----------
    @property
    def cfg(self) -> dict:
        return self.store.get()

    def _on_config(self, cfg):
        self.book.paper_cfg = cfg["paper"]
        if cfg["execution"]["mode"] != self.mode or cfg["live"] != self.link.live_cfg:
            self._reconfigure(cfg)
        self.broker.cfg = {**cfg, "execution": self.broker.cfg["execution"], "live": self.broker.cfg["live"]}

    def _reconfigure(self, cfg):
        """网页改了运行模式或实盘设置：换账本、下单程序客户端和券商适配。实盘三重开关照旧在 make_broker 里检查
        （网页保存前已预检，条件不全时根本不会保存）；这里再失败就退回原设置并报错。"""
        if not self.run_lock.acquire(timeout=60):
            raise RuntimeError("有运行尚未结束，稍后再改运行模式")
        try:
            old = (self.mode, self.book, self.broker, dict(self.link.cfg))
            new_mode = cfg["execution"]["mode"]
            book = self.book if new_mode == self.mode else Book(self.data_dir, new_mode, cfg["paper"])
            self.link.reconfigure(cfg, book)
            try:
                broker = make_broker(cfg, book, self.link)
            except Exception:
                self.link.reconfigure(old[3], old[1])
                raise
            self.mode, self.book, self.broker = new_mode, book, broker
            if new_mode != old[0]:
                self.journal = OrderJournal(self.data_dir, new_mode)
            log.info("运行设置已切换：模式 %s → %s（%s）", old[0], new_mode, broker.label)
        finally:
            self.run_lock.release()

    # ---------- 持仓获取方式（自动 / 手动） ----------
    def auto_pos(self) -> bool:
        """「自动获取持仓」是否生效：只有实盘才从下单程序读持仓和资金；模拟盘和演练永远是本地账本（可手动改）。"""
        return self.mode == "live" and self.switches.positions_auto()

    def set_positions_auto(self, auto: bool, by: str) -> dict:
        if auto and self.mode != "live":
            raise ValueError(f"当前是{self.broker.label}：资金和持仓是本地账本，只有实盘才能从下单程序自动获取")
        before = self.switches.positions_auto()
        st = self.switches.set_positions_auto(auto, by)
        if before != auto:
            self.notifier.notify("positions_mode", f"持仓改为{'自动获取' if auto else '手动填写'}",
                                 f"- 模式：{self.broker.label}\n- 操作来源：{by}\n"
                                 + ("- 之后本地持仓按下单程序快照自动更新（只在实盘模式写入）" if auto else "- 之后持仓在网页上手动填写和编辑"))
        if auto:
            self.link.invalidate()          # 打开时旧快照作废：拿到最新数据前页面不显示旧数据
            self.link.request_sync("fresh")
        return st

    def manual_position(self, code: str, body: dict) -> dict:
        if self.auto_pos():
            raise ValueError("当前是「自动获取持仓」，不能手动编辑；先关掉开关")
        from .util import today_str
        p = self.book.manual_upsert(code, str(body.get("name") or ""), body.get("quantity"), body.get("avg_cost"),
                                    body.get("sellable", 0), today_str())
        log.info("手动填写持仓：%s %s 股 成本 %s 可卖 %s", code, p["quantity"], p["avg_cost"], body.get("sellable"))
        self.preview_job.kick(force=True)
        return p

    def manual_remove(self, code: str) -> None:
        if self.auto_pos():
            raise ValueError("当前是「自动获取持仓」，不能手动编辑；先关掉开关")
        self.book.manual_remove(code)
        log.info("手动删除持仓：%s", code)
        # 演示/临时分时缓存随宿主一起回收（只认 demo 标记，不碰正常行情缓存）
        try:
            from .cacheclean import prune_for_engine
            prune_for_engine(self)
        except Exception as ex:  # noqa: BLE001
            log.warning("演示缓存回收失败（不影响删除持仓）：%s", ex)
        self.preview_job.kick(force=True)

    def manual_funds(self, body: dict) -> dict:
        if self.auto_pos():
            raise ValueError("当前是「自动获取持仓」，资金以券商为准；先关掉开关")
        f = self.book.set_funds(body.get("available"), body.get("balance"))
        log.info("手动填写资金（%s）：可用 %s 余额 %s", self.mode, f["available"], f["balance"])
        return f

    # ---------- 手动下单 / 撤单（用户亲自操作，不受自动交易开关限制，但照样过风控） ----------
    def manual_quote(self, code: str) -> dict:
        """手动下单表单用：名称、现价、昨收、涨跌停价、可卖数量（T+1）。"""
        import re as _re
        code = str(code).strip()
        if not _re.fullmatch(r"\d{6}", code):
            raise ValueError("代码应为 6 位数字")
        day = today_str()
        q, src, qerr = self.market().quotes([code])
        q = (q or {}).get(code) or {}
        pos = self.book.get(code)
        name = q.get("name") or (pos or {}).get("name") or ""
        price, pre = q.get("price"), q.get("preclose")
        out = {"symbol": code, "name": name, "price": price, "preclose": pre, "source": src, "error": None if q else (qerr or "没取到行情"),
               "held": int(pos["quantity"]) if pos else 0, "sellable": self._manual_sellable(code, day) if pos else 0,
               "mode": self.mode, "mode_label": self.broker.label}
        if pre:
            out.update(limit_info(code, name, float(pre), day))
        return out

    def _manual_sellable(self, code: str, day: str) -> int:
        n = self.book.sellable(code, day)
        if self.mode == "live":
            if self.auto_pos():
                bav = self.link.broker_available(code)
                if bav is not None:
                    n = min(n, bav)
            n = max(0, n - self.journal.open_qty(code, "sell"))
        return n

    def manual_place(self, body: dict, by: str = "网页") -> dict:
        """手动下单：{symbol, side, price_type: limit|market, price, quantity}。
        市价单不直接发「市价」：按现价加/减保护幅度（risk.market_protect_pct，默认 1%，不超过涨跌停）换成数字限价，
        这样下单程序那边仍只收数字限价。照样过风控（买入看单笔上限、每日金额；卖出看可卖数量/T+1；都看涨跌停、交易时段）。"""
        side = body.get("side")
        if side not in ("buy", "sell"):
            raise ValueError("方向只能是买入或卖出")
        ptype = body.get("price_type") or "limit"
        if ptype not in ("limit", "market"):
            raise ValueError("价格类型只能是限价或市价")
        try:
            qty = int(body.get("quantity"))
        except (TypeError, ValueError):
            raise ValueError("数量必须是整数") from None
        if qty <= 0:
            raise ValueError("数量必须大于 0")
        qd = self.manual_quote(body.get("symbol"))
        code, day = qd["symbol"], today_str()
        ref = qd.get("price")
        if not ref:
            raise ValueError(f"取不到 {code} 的现价（{qd.get('error')}），不能下单")
        if ptype == "market":
            pct = float(self.cfg["risk"].get("market_protect_pct", 1.0)) / 100
            price = round2(ref * (1 + pct)) if side == "buy" else round2(ref * (1 - pct))
            if side == "buy" and qd.get("limit_up"):
                price = min(price, qd["limit_up"])
            if side == "sell" and qd.get("limit_down"):
                price = max(price, qd["limit_down"])
        else:
            try:
                price = round(float(body.get("price")), 2)
            except (TypeError, ValueError):
                raise ValueError("限价单要填价格") from None
        m = self.market()
        it = {"id": f"MAN-{now_cn():%Y%m%d%H%M%S}-{code}-{side[0].upper()}{int(time.time() * 1000) % 1000:03d}", "side": side,
              "symbol": code, "name": qd.get("name") or code, "trading_date": day, "ref_price": ref, "price": price, "quantity": qty,
              "sources": ["手动"], "reason": f"手动下单（{'市价，保护限价 ' + format(price, '.2f') if ptype == 'market' else '限价'}）",
              "is_trading_day": m.is_trading_day(day), "limit_up": qd.get("limit_up"),
              "limit_down": qd.get("limit_down"), "manual": True, "price_type": ptype}
        if side == "sell":
            it["sellable"] = qd["sellable"]
        rec = self.submit(it, auto=False)
        if rec["status"] == "risk_rejected":
            log.warning("手动下单被风控拦截（%s）：%s %s %s@%s：%s", by, side, code, qty, price, rec["message"])
        if self.mode == "live" and rec["status"] in ("submitted", "unknown") and not self.auto_pos():
            self.refresh_orders()
        if rec["status"] in ("filled", "dry_run"):
            self.preview_job.kick(force=True)
        return {"order": self.journal.list_one(rec["id"]), "result": rec}

    def manual_cancel(self, oid: str, by: str = "网页") -> dict:
        """撤一笔未成交/部分成交的委托。实盘调下单程序 /api/cancel（只按合同号），然后刷新委托状态。"""
        e = self.journal.get(oid)
        if e is None:
            raise KeyError(oid)
        if e["status"] not in ("submitted", "unknown", "partial"):
            raise ValueError(f"这笔委托是「{e['status']}」，不能撤")
        if self.mode != "live":
            raise ValueError("只有实盘委托能撤单（模拟盘和演练的委托下单时就已成交或未成交）")
        if e.get("origin") == "手动补记":
            raise ValueError("手动补记的委托不是本系统发出的，请在同花顺里撤，再在这里改状态")
        if self.journal.mode != "live":
            raise ValueError("这不是实盘的委托")
        if not e.get("entrust_no") and not self.auto_pos():
            self.refresh_orders()          # 先读一次委托表，对上合同号
            e = self.journal.get(oid)
        r = self.broker.cancel(oid, e.get("entrust_no"))
        st = r.get("status")
        msg = r.get("message") or ""
        self.journal.update_system(oid, message=(e.get("message") or "") + f"；撤单：{st} {msg}".rstrip(),
                                   cancel_status=st, cancel_at=ts())
        side_txt = "买入" if e["side"] == "buy" else "卖出"
        self.notifier.notify("order", f"[{self.broker.label}] 撤单{'已提交' if st == 'cancel_submitted' else '失败'}：{side_txt} {e['symbol']} {e.get('name', '')}",
                             f"- 委托 {oid}（合同 {e.get('entrust_no') or '无'}）\n- {e['quantity']} 股 @ {e.get('price')}\n- 结果：{msg}\n- 操作来源：{by}")
        log.warning("手动撤单（%s）%s %s → %s %s", by, oid, e["symbol"], st, msg)
        if st == "cancel_submitted":
            if self.auto_pos():
                self.journal.sync_from_book(self.book)
            else:
                self.refresh_orders()
        return {"order": self.journal.list_one(oid), "result": r}

    def refresh_orders(self) -> dict:
        """刷新委托状态（实盘）。自动获取持仓打开：用对账结果。关闭（手动账本）：只读一次下单程序的委托表
        （GET /api/snapshot?only=orders，只读、不读持仓和资金），按合同号或代码/方向/数量/价格对上本系统的委托，
        更新状态和成交；新增的成交按成交均价记进手动账本（持仓和可用资金）。"""
        if self.mode != "live":
            return {"ok": True, "updated": 0, "note": "模拟盘和演练的委托下单时就有结果，不需要刷新"}
        if self.auto_pos():
            self.journal.sync_from_book(self.book)
            return {"ok": True, "updated": None, "note": "自动获取持仓已打开：委托状态跟随自动对账"}
        from .exec.reconcile import match_order
        from .exec.xiadan import XiadanError, norm_order
        try:
            raw = self.link.client.snapshot(only="orders", refresh=True, force=True)
        except XiadanError as e:
            hint = "口令错误或未提供（403）：设置 → 下单程序里的口令要和下单程序那边一致" if e.kind == "auth" else str(e)[:200]
            return {"ok": False, "updated": 0, "error": hint}
        orders = [norm_order(r) for r in raw.get("orders") or [] if isinstance(r, dict)]
        orders = [o for o in orders if o["symbol"]]
        updated, applied = 0, []
        with self.journal.lock:
            claimed = {o["entrust_no"] for o in self.journal._orders() if o.get("entrust_no")}
            for e in self.journal._orders():
                if e.get("origin") not in ("系统", "手动") or e["status"] not in ("submitted", "unknown", "partial"):
                    continue
                if e.get("trading_date") != today_str():
                    continue
                bo = match_order({"symbol": e["symbol"], "side": e["side"], "quantity": e["quantity"], "price": e.get("price") or 0,
                                  "entrust_no": e.get("entrust_no"), "submitted_at": e.get("created_at")}, orders, claimed, 0.006, 120)
                if not bo:
                    continue
                if bo["entrust_no"]:
                    claimed.add(bo["entrust_no"])
                st = {"pending": "submitted"}.get(bo["status"], bo["status"])
                new = {"entrust_no": bo["entrust_no"] or e.get("entrust_no"), "status": st, "filled_qty": int(bo["filled"]),
                       "fill_price": bo.get("avg_price") or e.get("fill_price"), "broker_status": bo.get("remark")}
                if any(e.get(k) != v for k, v in new.items()):
                    e.update(new)
                    e["updated_at"] = ts()
                    updated += 1
                delta = int(e["filled_qty"]) - int(e.get("applied_qty") or 0)
                if delta > 0 and e.get("fill_price"):
                    try:
                        a = self.book.manual_fill(e["side"], e["symbol"], e.get("name") or "", float(e["fill_price"]), delta, 0.0,
                                                  e["trading_date"], f"委托 {e['id']}")
                        e["applied_qty"] = int(e["filled_qty"])
                        applied.append(a)
                    except ValueError as ex:
                        e["message"] = (e.get("message") or "") + f"；成交入账失败：{ex}"
            if updated or applied:
                self.journal.save()
        if applied:
            log.info("委托成交记进手动账本：%s", "；".join(f"{a['side']} {a['symbol']} {a['quantity']}@{a['price']}" for a in applied))
            self.preview_job.kick(force=True)
        return {"ok": True, "updated": updated, "applied": applied}

    def manual_order(self, body: dict, by: str = "网页") -> dict:
        """手动补记 / 修改一笔委托。手动账本（自动获取持仓关闭）时，新增的成交记进持仓和可用资金。"""
        apply = not self.auto_pos()
        r = self.journal.manual_save(body, self.book, apply_to_book=apply, by=by)
        o, a = r["order"], r["applied"]
        if r["changed"]:
            log.info("手动%s委托 %s：%s %s %s 股，成交 %s 股，状态 %s%s", "修改" if body.get("id") else "补记", o["id"], o["side"],
                     o["symbol"], o["quantity"], o["filled_qty"], o["status"],
                     f"；入账 {a['quantity']} 股 @ {a['price']}，资金变动 {a['cash_delta']}" if a else "")
        if a:
            self.preview_job.kick(force=True)
        return {**r, "auto_positions": not apply,
                "note": None if apply else "自动获取持仓已打开：持仓和资金以券商为准，这里只记录不入账"}

    def manual_order_delete(self, oid: str) -> dict:
        e = self.journal.delete_manual(oid)
        log.info("删除手动补记的委托 %s %s", oid, e.get("symbol"))
        return e

    def sources(self):
        if self._sources is None or self._sources_ver != self.store.version:
            self._sources = build_sources(self.cfg, self.data_dir, self.offline, self.root)
            self._sources_ver = self.store.version
        return self._sources

    def market(self) -> Market:
        return Market(self.sources(), self.cfg, self.data_dir)

    def set_auto(self, enabled: bool, by: str) -> dict:
        before = self.switches.auto_enabled()
        st = self.switches.set_auto(enabled, by)
        if before != enabled:
            self.notifier.notify("switch", f"自动交易已{'开启' if enabled else '关闭'}",
                                 f"- 模式：{self.broker.label}\n- 操作来源：{by}\n- 时间：{st['changed_at']}\n"
                                 + ("- 之后到点会自动发买卖委托" if enabled else "- 之后照常选股和推送，只记录“本应下单”的信号，不发委托"))
        return st

    def set_trading(self, on: bool, by: str) -> dict:
        """顶栏唯一的「自动交易」开关（合并了原来的一键停止）。
        打开：允许按策略自动发委托（同时解除停止状态）。
        关闭：不再发任何新委托（定时任务照常选股、只记信号）；同时撤掉本系统发出、还没成交完的委托（只在实盘、
        只按合同号撤），没有合同号的列出来请你在同花顺里撤。"""
        cancelled: list[dict] = []
        if on:
            if self.switches.halted():
                self.switches.resume(by)
            st = self.set_auto(True, by)
        else:
            was = self.switches.auto_enabled()
            self.switches.set_auto(False, by)
            if self.mode == "live" and hasattr(self.broker, "cancel"):
                if not self.auto_pos():
                    try:
                        self.refresh_orders()      # 手动账本：先读一次委托表对上合同号
                    except Exception as e:  # noqa: BLE001
                        log.error("关闭自动交易时刷新委托失败：%s", e)
                ids = {k for k in self.book.pending() if not str(k).startswith("MAN-")}     # 手动下的单不动
                for o in self.journal.open_orders():
                    if o.get("origin") == "系统" and o.get("trading_date") == today_str():
                        ids.add(o["id"])
                for oid in sorted(ids):
                    jo = self.journal.get(oid) or {}
                    r = self.broker.cancel(oid, jo.get("entrust_no"))
                    cancelled.append({"id": oid, "symbol": jo.get("symbol") or (self.book.pending().get(oid) or {}).get("symbol"),
                                      "status": r.get("status"), "message": r.get("message")})
                    if r.get("status") == "cancel_submitted" and jo:
                        self.journal.update_system(oid, message=(jo.get("message") or "") + "；关闭自动交易时已申请撤单")
            st = self.switches.auto_state()
            lines = [f"- 模式：{self.broker.label}", f"- 操作来源：{by}", "- 之后不再发任何新委托（定时任务照常选股，只记信号）"]
            if cancelled:
                lines.append("- 撤未成交委托：" + "；".join(f"{c['symbol']} {c['status']} {c['message'] or ''}" for c in cancelled))
            if was or cancelled:
                self.notifier.notify("switch", "自动交易已关闭", "\n".join(lines))
        log.warning("自动交易%s（%s）%s", "打开" if on else "关闭", by, f"，撤单 {len(cancelled)} 笔" if cancelled else "")
        return {"on": self.trading_on(), "auto_trade": st, "cancelled": cancelled}

    def trading_on(self) -> bool:
        return self.switches.auto_enabled() and not self.switches.halted()

    def halt(self, reason: str, by: str) -> dict:
        d = self.switches.halt(reason, by)
        self.notifier.notify("switch", "一键停止已生效", f"- 原因：{d['reason']}\n- 操作来源：{by}\n- 所有新委托（含手动、含卖出）都会被风控拦截")
        return d

    def resume(self, by: str) -> dict:
        d = self.switches.resume(by)
        self.notifier.notify("switch", "已解除一键停止", f"- 操作来源：{by}")
        return d

    def status(self) -> dict:
        cfg = self.cfg
        gate_ok, missing = live_gate(cfg)
        return {
            "mode": self.mode, "mode_label": self.broker.label, "auto_trade": self.switches.auto_state(),
            "halt": self.switches.halt_state(), "live_gate": {"armed": gate_ok and self.mode == "live", "missing": missing},
            "trading_on": self.trading_on(), "positions_auto": self.auto_pos(),
            "broker_conn": self.link.conn if self.auto_pos() else None,
            "risk_usage": self.risk.usage(today_str()), "now": ts(), "offline": self.offline,
            "sources": [s.info() for s in self.sources().values()], "plugins": self.plugins,
            "last_run": self._run_index()[:1],
        }

    # ---------- 委托（自动交易开关、风控、券商） ----------
    def submit(self, it: dict, auto: bool = True) -> dict:
        day = it["trading_date"]
        checks = self.risk.check(it, self.mode, day)
        fail = Risk.first_fail(checks)
        rec = {**it, "mode": self.mode, "created_at": ts(), "checks": [{"name": n, "ok": o, "note": t} for n, o, t in checks]}
        if not it.get("manual") and "strategy" not in rec:
            sn = self.strategy_names()
            if sn:
                rec["strategy"] = sn.get("buy" if it["side"] == "buy" else "sell")
        side_txt = "买入" if it["side"] == "buy" else "卖出"
        head = f"{side_txt} {it['symbol']} {it['name']} {it['quantity']} 股 @ {it['price']:.2f}"
        if auto and not self.switches.auto_enabled():
            rec.update(status="signal_only", message="自动交易已关闭，只记录信号、不发委托（风控预检："
                       + ("通过" if not fail else f"会被拦截——{fail}") + "）")
            self.notifier.notify("signal_only", f"本应{side_txt}（自动交易关闭）", f"- {head}\n- 原因：{it.get('reason', '')}\n- {rec['message']}")
        elif fail:
            rec.update(status="risk_rejected", message=fail)
            self.notifier.notify("risk_reject", f"风控拦截：{side_txt} {it['symbol']} {it['name']}", f"- {head}\n- {fail}")
        else:
            self.risk.record(it, day)      # 先计入额度再发送：发送中途崩溃也不会多发
            try:
                res = self.broker.place(it)
            except Exception as e:  # noqa: BLE001
                res = {"status": "unknown" if self.mode == "live" else "rejected", "message": f"下单异常：{e}"}
            rec.update(res)
            st = rec["status"]
            ev = "unknown" if st == "unknown" else "order"
            self.notifier.notify(ev, f"[{self.broker.label}] {STATUS_LABELS.get(st, st)}：{side_txt} {it['symbol']} {it['name']}",
                                 f"- {head}\n- 原因：{it.get('reason', '')}\n- 结果：{rec.get('message', '')}")
        rec["status_label"] = STATUS_LABELS.get(rec["status"], rec["status"])
        append_jsonl(self.data_dir / "orders.jsonl", rec)
        try:
            self.journal.record_system(rec)
        except Exception as e:  # noqa: BLE001
            log.error("写委托记录失败：%s", e)
        return rec

    # ---------- 选股漏斗 ----------
    def _resolve_date(self, m: Market, pools: list[str], date: str | None, warnings: list) -> str:
        if date:
            return date
        d = m.latest_trading_date()
        if d != today_str():
            warnings.append(f"数据日期 {d} 不是今天（{today_str()}），本次是回放")
        return d

    def _derive(self, c: Candidate, m: Market, day: str, warnings: list) -> None:
        f = c.fields
        f["hit_count"] = float(len(c.sources))
        price, chg = f.get("price"), f.get("change_rate")
        pre = None
        if price and chg is not None and chg > -100:
            pre = price / (1 + chg / 100)
        if pre is None and f.get("minute_preclose"):
            pre = f["minute_preclose"]
        if not pre:
            return
        info = limit_info(c.code, c.name, pre, day)
        f.update(info)
        p = c.price()
        if p and info["limit_up"]:
            f["limit_gap_pct"] = (info["limit_up"] - p) / pre * 100
            # 涨幅超过板块限制或贴近涨停时，用日K确认是不是注册制新股上市初期（无涨跌幅限制）
            if f["limit_gap_pct"] <= 2.0 and not self.offline:
                listing, rank = m.listing_info(c.code, day)
                from datetime import date as _d
                if listing and pl.is_no_limit_day(c.code, listing, _d.fromisoformat(day), day_rank=rank):
                    f.update(limit_info(c.code, c.name, pre, day, no_limit=True))
                    f.pop("limit_gap_pct", None)

    def _enrich_fields(self, cands: list[Candidate], day: str, warnings: list) -> None:
        if not cands:
            return
        codes = [c.code for c in cands]
        for name, src in self.sources().items():
            if "fields" not in src.capabilities:
                continue
            try:
                got = src.get_fields(codes, day)
            except NotSupported:
                continue
            except Exception as e:  # noqa: BLE001
                warnings.append(f"数据源 {name} 补字段失败：{str(e)[:200]}")
                extsources.record(self.data_dir, name, False, str(e)[:200])
                continue
            seen: set[str] = set()
            for c in cands:
                for k, v in (got.get(c.code) or {}).items():
                    c.fields.setdefault(k, v)
                    c.src_fields.setdefault(name, {})[k] = v
                    seen.add(k)
            extsources.record(self.data_dir, name, True, fields=sorted(seen))

    def ext_ctx(self, day: str | None = None) -> dict:
        """给因子用的外部数据源信息：外部因子按它判断绑定的数据源是否可用。"""
        try:
            snap = extsources.snapshot(self.cfg, self.data_dir, self.ext_store, day)
        except Exception as e:  # noqa: BLE001
            log.warning("读取外部数据源状态失败：%s", e)
            snap = None
        return {"ext_store": self.ext_store, "ext_sources": snap, "data_dir": self.data_dir}

    def _load_minute(self, c: Candidate, m: Market, day: str) -> None:
        if c.minute_loaded or c.minute_error:
            return
        d, err = m.minute(c.code, day)
        if d is None:
            c.minute_error = err
            return
        c.fields.update(summarize_minute(d))
        c.minute_loaded = True
        f = c.fields
        # 自选股等没有价格字段的票，用分时补全
        if f.get("price") is None and f.get("minute_last"):
            f["price"] = f["minute_last"]
            pre, op = f.get("minute_preclose"), f.get("minute_open")
            if pre:
                f["change_rate"] = (f["minute_last"] / pre - 1) * 100
                if op:
                    f["open_price"] = op
                    f["open_rise"] = (op / pre - 1) * 100

    def _load_daily(self, c: Candidate, m: Market, day: str) -> None:
        if c.daily is not None or c.daily_error:
            return
        bars, err, synth = m.daily_series(c.code, day, c.fields)
        if not bars:
            c.daily_error = err or "无日K"
            return
        c.daily, c.daily_synth = bars, synth
        for k in ("close", "ma5", "ma10", "ma20", "ma60", "vol_ratio_5d", "high_60d"):
            v = c.daily_at(k, 0)
            if v is not None:
                c.fields[k] = v

    def _rank_scores(self, cands: list[Candidate], weights: dict) -> None:
        """各字段在候选里的百分位排名 × 权重 加总（与 Rust 版一致）。"""
        for c in cands:
            c.fields["score"] = 0.0
            c.score_parts = {}
        n = len(cands)
        for field, w in weights.items():
            vals = sorted((c.get(field) for c in cands if c.get(field) is not None))
            for c in cands:
                v = c.get(field)
                if v is None:
                    continue
                if n <= 1 or len(vals) <= 1:
                    pct = 1.0
                else:
                    pct = sum(1 for x in vals if x < v) / (len(vals) - 1)
                c.fields["score"] += float(w) * pct
                c.score_parts[field] = {"value": round(float(v), 4), "pct": round(pct, 4), "weight": float(w),
                                        "points": round(float(w) * pct, 4)}

    def industry_of(self, m: Market, code: str) -> str | None:
        info, _ = m.stock_info(code)
        return (info or {}).get("industry") or None

    def _pick_with_industry_cap(self, m: Market, ranked: list[Candidate], top_n: int, warnings: list,
                                already: list[Candidate] | None = None):
        """按分数取前 top_n；风控「同一行业最多买几只」（risk.max_per_industry，0 = 不限）会跳过超额行业。
        行业拿不到的票按「数据缺失」处理，不买（不当成通过）。"""
        cap = int(self.cfg["risk"].get("max_per_industry", 0) or 0)
        if cap <= 0:
            return ranked[:top_n], []
        held: dict[str, int] = {}
        for code in self.book.positions():
            ind = self.industry_of(m, code)
            if ind:
                held[ind] = held.get(ind, 0) + 1
        for c in already or []:          # 同一轮里别的股池分组已经选中的，也算进去
            ind = c.info.get("industry")
            if ind:
                held[ind] = held.get(ind, 0) + 1
        picks, dropped = [], []
        for c in ranked:
            if len(picks) >= top_n:
                break
            ind = c.info.get("industry") or self.industry_of(m, c.code)
            if not ind:
                dropped.append((c, "数据缺失：拿不到所属行业，「同一行业最多买几只」无法判断，不买"))
                continue
            c.info["industry"] = ind
            if held.get(ind, 0) >= cap:
                dropped.append((c, f"同一行业最多买 {cap} 只：{ind} 已有 {held[ind]} 只（含持仓）"))
                continue
            held[ind] = held.get(ind, 0) + 1
            picks.append(c)
        return picks, dropped

    # ---------- 股池绑定策略 ----------
    def binder(self, cfg: dict | None = None):
        """按股池找策略（qfpy/poolbind.py）。生效策略的内容以配置文件为准。"""
        from .poolbind import Resolver
        from .strategies import content_from_cfg
        cfg = cfg or self.cfg
        names = self.strategy_names()

        def get_doc(side, sid):
            if sid == names.get(f"{side}_id"):
                return {"id": sid, "name": names.get(side), "content": content_from_cfg(cfg, side)}
            try:
                d = self.strategies.get(side, sid)
            except KeyError:
                return None
            return {"id": d["id"], "name": d["name"], "content": d["content"]}

        def active_doc(side):
            return {"id": names.get(f"{side}_id"), "name": names.get(side) or "当前配置", "content": content_from_cfg(cfg, side)}

        return Resolver(cfg, get_doc, active_doc)

    # ---------- 股池成分股存档（按股池回测用） ----------
    @property
    def pool_archive(self):
        from .poolarchive import PoolArchive
        if getattr(self, "_pool_archive", None) is None:
            self._pool_archive = PoolArchive(self.data_dir)
        return self._pool_archive

    def _archive_pool(self, day: str, key: str, pr, origin: str, past: bool = False) -> None:
        """把拉到的股池成分股记进 data/pool_archive。补录/指定日期时数据源返回的也是那天的股池，照样记，但写明来源。"""
        if not (self.cfg.get("pool") or {}).get("archive", True):
            return
        try:
            self.pool_archive.record(day, key, [{"code": s["code"], "name": s.get("name") or "", "fields": s.get("fields") or {}}
                                                for s in pr.stocks], title=pr.name or "", origin=origin + ("（补录）" if past else ""))
        except Exception:  # noqa: BLE001
            log.exception("股池 %s 存档失败", key)

    def archive_pools(self, origin: str = "手动", date: str | None = None) -> dict:
        """拉一遍所有股池并存档（定时任务在收盘后调用）。拉取失败的池当天就没有存档，不会编数据。"""
        m = self.market()
        day = date or m.latest_trading_date()
        if date is None and day != today_str():
            return {"date": day, "skipped": f"今天不是交易日，不存档（最近交易日 {day}）", "pools": []}
        out = []
        srcs = self.sources()
        for key in self.cfg["pool"]["pools"]:
            sname, _, pkey = key.partition(":")
            src = srcs.get(sname)
            if src is None:
                out.append({"pool": key, "ok": False, "error": f"数据源 {sname} 未启用或不存在"})
                continue
            try:
                pr = src.get_pool(pkey, day)
            except Exception as e:  # noqa: BLE001
                out.append({"pool": key, "ok": False, "error": str(e)[:200]})
                continue
            self._archive_pool(day, key, pr, origin)
            out.append({"pool": key, "ok": True, "count": len(pr.stocks)})
        bad = [x for x in out if not x["ok"]]
        if bad:
            log.warning("股池存档 %s：%d 个池拉取失败：%s", day, len(bad), "；".join(f"{x['pool']} {x['error']}" for x in bad))
        return {"date": day, "pools": out}

    def bound_sell_contents(self) -> list[dict]:
        """所有会用到的卖出策略**内容**：生效策略 + 各股池绑定的 + 持仓页手动指定的。
        定时任务据此排「到点卖出」的时刻，所以持仓指定的那套也必须收进来。"""
        try:
            b = self.binder()
            out = b.sell_contents()
            used = {b.active_doc("sell").get("id")} | {v.get("sell") for v in b.bind.values() if v.get("sell")}
            for pos in self.book.positions().values():
                sid = str(pos.get("sell_strategy_id") or "").strip()
                if not sid or sid in used:
                    continue
                try:
                    d = self.strategies.get("sell", sid)
                except KeyError:
                    continue          # 指定的策略已被删除：卖出时按回退逻辑走，这里不排它的到点时刻
                used.add(sid)
                out.append(d["content"])
            return out
        except Exception:  # noqa: BLE001
            log.exception("读取股池绑定的卖出策略失败")
            return []

    def set_position_sell_strategy(self, code: str, sid: str, by: str = "网页") -> dict:
        """持仓页给单只票指定卖出策略。sid 为空 = 恢复跟随来源股池 / 生效策略。"""
        sid = str(sid or "").strip()
        doc = None
        if sid:
            try:
                doc = self.strategies.get("sell", sid)
            except KeyError:
                raise ValueError(f"卖出策略 {sid} 不存在") from None
        p = self.book.set_sell_strategy(code, sid, by)
        log.info("持仓 %s 的卖出策略改为：%s（%s）", code, doc["name"] if doc else "跟随来源股池 / 生效策略", by)
        self.preview_job.kick(force=True)
        return {"position": p, "strategy": {"id": sid, "name": doc["name"]} if doc else None}

    def strategy_names(self) -> dict:
        try:
            a = self.strategies.active_names()
            return {"buy": a["buy"]["name"], "sell": a["sell"]["name"],
                    "buy_id": a["buy"]["id"], "sell_id": a["sell"]["id"]}
        except Exception:  # noqa: BLE001
            return {}

    def run(self, date: str | None = None, pools: list[str] | None = None, trade: bool = True, origin: str = "手动",
            backfill: bool = False, entry: str | None = None) -> dict:
        if not self.run_lock.acquire(timeout=60 if backfill else 1):
            raise RuntimeError("上一次运行还没结束")
        try:
            return self._run(date, pools, trade, origin, backfill, entry)
        finally:
            self.run_lock.release()

    def _pool_problem(self, key: str, e: Exception | str) -> dict:
        """股池拉取失败：给网页一条中文说明（哪个池、什么问题、怎么处理的），原始报错放 detail。"""
        from .nethelp import NetError
        if isinstance(e, NetError):
            return {"key": key, "name": key, "count": 0, "error": e.problem, "problem": e.problem, "hint": e.hint,
                    "detail": e.detail, "handling": "这个池这一次跳过，没有新入池；没有可用的缓存，不会编数据。"}
        msg = str(e)[:300]
        return {"key": key, "name": key, "count": 0, "error": msg, "problem": msg, "hint": "", "detail": msg,
                "handling": "这个池这一次跳过，没有新入池。"}

    def _run(self, date, pools, trade, origin, backfill=False, entry=None) -> dict:
        cfg = self.cfg
        if entry in ("all", "new"):
            cfg = {**cfg, "pool": {**cfg["pool"], "entry_mode": entry}}
        run_id = now_cn().strftime("%Y%m%d-%H%M%S")
        warnings: list[str] = []
        rec: dict = {"id": run_id, "started_at": ts(), "origin": origin, "mode": self.mode, "trade": trade,
                     "auto_trade": self.switches.auto_enabled(), "warnings": warnings, "pools": [], "stages": [],
                     "picks": [], "orders": [], "exits": [], "errors": [], "backfill": bool(backfill),
                     "strategy": self.strategy_names()}
        try:
            m = self.market()
            pool_keys = pools or cfg["pool"]["pools"]
            day = self._resolve_date(m, pool_keys, date, warnings)
            rec["trading_date"] = day
            rec["is_trading_day"] = m.is_trading_day(day) if day == today_str() else False
            srcs = self.sources()
            merged: dict[str, Candidate] = {}
            members: dict[str, list[str]] = {}      # 股池 → 这次拉到的成分股代码
            fresh: dict[str, bool] = {}             # 股池 → 这次的成分股能不能用来判断新入池
            for key in pool_keys:
                sname, _, pkey = key.partition(":")
                src = srcs.get(sname)
                if src is None:
                    rec["pools"].append(self._pool_problem(key, f"股池数据源 {sname} 没启用或不存在，请到「设置 → 股池数据源」检查"))
                    continue
                from .market import note_health
                try:
                    pr = src.get_pool(pkey, day)
                except Exception as e:  # noqa: BLE001
                    # 只记在 rec["pools"] 里，网页只显示一次（不再同时写进 warnings）
                    rec["pools"].append(self._pool_problem(key, e))
                    note_health(sname, "pool", False, getattr(e, "problem", None) or str(e))
                    log.warning("股池 %s 拉取失败：%s", key, getattr(e, "detail", "") or e)
                    continue
                info = {"key": key, "name": pr.name, "count": len(pr.stocks), "error": None}
                note_health(sname, "pool", not pr.stale_at, pr.problem)
                if pr.stale_at:
                    same_day = pr.trading_date == day
                    info.update({"stale_at": pr.stale_at, "stale_date": pr.trading_date, "problem": pr.problem,
                                 "hint": pr.hint, "detail": pr.detail,
                                 "handling": (f"改用 {pr.stale_at} 成功拉到的缓存（同一交易日），按缓存里的成分股判断新入池"
                                              if same_day else
                                              f"改用 {pr.stale_at} 拉到的缓存（那是 {pr.trading_date} 的股池，不是今天的），"
                                              "只显示、不送进策略")})
                    fresh[key] = same_day
                else:
                    fresh[key] = True
                    self._archive_pool(day, key, pr, f"选股 {now_cn().strftime('%H:%M')}", backfill or date is not None)
                rec["pools"].append(info)
                members[key] = [s["code"] for s in pr.stocks]
                for s in pr.stocks:
                    c = merged.get(s["code"])
                    if c is None:
                        c = merged[s["code"]] = Candidate(s["code"], s.get("name") or "", [], {})
                    if key not in c.sources:
                        c.sources.append(key)
                    if not c.name and s.get("name"):
                        c.name = s["name"]
                    for k, v in (s.get("fields") or {}).items():
                        c.fields.setdefault(k, v)
                        c.src_fields.setdefault(key.split(":", 1)[0], {})[k] = v
            cands = list(merged.values())
            by_code = {c.code: c for c in cands}
            # 自选股等没有价格的票，先用分时补全价格字段
            for c in cands:
                if c.fields.get("price") is None:
                    self._load_minute(c, m, day)
                    if not c.name:
                        c.name = c.code
                self._derive(c, m, day, warnings)
            pool_all = list(cands)
            # 外部数据源补字段（能力 fields：外部推送/HTTP 拉取的行情或自定义字段）
            self._enrich_fields(cands, day, warnings)
            # 买入：以股池为单位。每个池只把「新入池」的票送进它绑定的买入策略（规则见 poolentry.py）。
            # 一只票同时在几个池里时，归绑定顺序靠前的那个池（沿用 poolbind 的规则），只走那个池的策略。
            ctx = FactorContext(day, self.root, {"side": "buy", **self.ext_ctx(day)})

            def _loader(cs, needs):
                for c in cs:
                    if "info" in needs and not c.info:
                        c.info, _ = m.stock_info(c.code)
                    if "minute" in needs:
                        self._load_minute(c, m, day)
                    if "daily" in needs:
                        self._load_daily(c, m, day)

            from .poolbind import buy_cfg
            from .poolentry import entry_mode
            binder = self.binder()
            order = [k for k in binder.buy_priority(pool_keys) if k in members]
            groups = binder.buy_groups(order) if order else []
            grp_of = {k: g for g in groups for k in g["pools"]}
            for g in groups:
                for note in g.get("notes") or []:
                    warnings.append(note)
            sell_names = {k: binder.strategy_for_pool("sell", k).get("name") for k in pool_keys}
            owner = {c.code: binder.pool_of(c.sources, order) for c in cands}
            entry_state = self.pool_entry.load(day)
            picked_today = dict(entry_state.get("_picked") or {})
            splits = {k: (self.pool_entry.split(cfg, day, k, members[k]) if fresh.get(k) else
                          {"new": [], "seen": {}, "returned": [], "mode": entry_mode(cfg), "scope": "", "stale_other_day": True})
                      for k in order}
            rec["entry"] = {"mode": entry_mode(cfg), "formal": bool(trade or backfill)}
            picks: list[Candidate] = []
            focus: list[Candidate] = []
            pick_meta: dict[str, dict] = {}
            rec["groups"] = []
            rec["pool_runs"] = []
            for key in order:
                g = grp_of[key]
                sp = splits[key]
                new_set = set(sp["new"])
                gcands, dropped0 = [], []
                for code in members[key]:
                    c = by_code[code]
                    if sp.get("stale_other_day"):
                        dropped0.append(c.brief(None, "缓存不是今天的股池，只显示、不送进策略"))
                    elif code not in new_set:
                        at = (sp["seen"].get(code) or {}).get("at", "")
                        dropped0.append(c.brief(None, f"{at[11:16] or '之前'} 已评估过、一直在池里，不重复进入"))
                    elif owner.get(code) != key:
                        dropped0.append(c.brief(None, f"同时在 {owner.get(code)}（绑定顺序靠前），归那个池的策略"))
                    elif code in picked_today and picked_today[code] != key:
                        dropped0.append(c.brief(None, f"今天已由 {picked_today[code]} 入选过，不重复买入"))
                    else:
                        gcands.append(c)
                active = g["active"]
                gcfg = cfg if active else buy_cfg(cfg, g["content"])
                base_idx = len(rec["stages"])
                label0 = "新入池" if sp["mode"] == "new" else "股池全部成分股"
                rec["stages"].append({"index": base_idx, "type": "pool", "pool": key, "group": g["id"] or "@active",
                                      "label": label0, "input": len(members[key]),
                                      "kept": [{**c.brief(), **({"returned": True} if c.code in sp["returned"] else {})}
                                               for c in gcands],
                                      "dropped": dropped0})
                layers = build_layers(layers_of(gcfg, "buy"), self.root, "buy")
                kept, stages = run_buy_funnel(layers, gcands, ctx, _loader, lambda c: c.brief(), warnings) if gcands else ([], [])
                for st in stages:
                    st["index"] = len(rec["stages"])
                    st["pool"] = key
                    st["group"] = g["id"] or "@active"
                    rec["stages"].append(st)
                focus += kept
                sel = gcfg["select"]
                self._rank_scores(kept, sel.get("weights") or {})
                kept.sort(key=lambda c: -c.fields.get("score", 0))
                gp, ind_dropped = self._pick_with_industry_cap(m, kept, int(sel.get("top_n", 1)), warnings, already=picks)
                dropped_codes = {c.code for c, _ in ind_dropped}
                rec["stages"].append({"index": len(rec["stages"]), "type": "select", "pool": key, "group": g["id"] or "@active",
                                      "label": f"打分取前 {sel.get('top_n', 1)} 名",
                                      "weights": sel.get("weights"), "input": len(kept),
                                      "kept": [{**c.brief(round(c.fields['score'], 4), "入选"), "score_parts": c.score_parts}
                                               for c in gp],
                                      "dropped": [{**c.brief(round(c.fields['score'], 4), why), "score_parts": c.score_parts}
                                                  for c, why in ind_dropped] +
                                                 [{**c.brief(round(c.fields['score'], 4), "分数排名靠后"), "score_parts": c.score_parts}
                                                  for c in kept if c not in gp and c.code not in dropped_codes]})
                for c in gp:
                    pick_meta[c.code] = {"pool": key, "buy_strategy": g["name"], "buy_strategy_id": g["id"],
                                         "sell_strategy": sell_names.get(key)}
                picks += gp
                pi = next((p for p in rec["pools"] if p["key"] == key), {})
                rec["pool_runs"].append({"pool": key, "name": pi.get("name", key), "strategy": g["name"], "strategy_id": g["id"],
                                         "active": active, "members": len(members[key]), "new": len(sp["new"]),
                                         "returned": sp["returned"], "input": len(gcands), "kept": len(kept),
                                         "picks": [c.code for c in gp], "mode": sp["mode"], "scope": sp.get("scope"),
                                         "stale_at": pi.get("stale_at"), "stages": list(range(base_idx, len(rec["stages"])))})
            # 兼容旧字段：按策略汇总
            for g in groups:
                runs = [r for r in rec["pool_runs"] if r["pool"] in g["pools"]]
                rec["groups"].append({"id": g["id"], "name": g["name"], "pools": g["pools"], "active": g["active"],
                                      "input": sum(r["input"] for r in runs), "kept": sum(r["kept"] for r in runs),
                                      "picks": [c for r in runs for c in r["picks"]]})
            rec["picks"] = [{**c.brief(round(c.fields["score"], 4)), "score_parts": c.score_parts, **pick_meta.get(c.code, {})}
                            for c in picks]
            # 正式运行（会下单的运行、定时任务、补录）才记「已评估」和这次的成分股；只看不下单的试跑不记，免得把新票用掉
            if trade or backfill:
                when = ts()
                for key in order:
                    if fresh.get(key):
                        self.pool_entry.commit(day, key, members[key], splits[key]["new"], when, run_id)
                if picks:
                    self.pool_entry.mark_picked(day, {c.code: pick_meta[c.code]["pool"] for c in picks})
            self.tiers.apply_run(day, pool_all, focus, picks, run_id)
            # 交易：先检查退出，再买入
            if trade and cfg["exits"].get("enabled", True):
                rec["exits"] = self._check_exits(m, day, trade=True, origin=origin, run_id=run_id)
            if trade:
                for i, c in enumerate(picks, 1):
                    rec["orders"].append(self._buy(c, day, rec["is_trading_day"], f"{run_id}-B{i}", pick_meta.get(c.code)))
            self._mark_book(m, day)
            self._notify_run(rec)
        except Exception as e:  # noqa: BLE001
            log.exception("运行失败")
            rec["errors"].append(f"{type(e).__name__}: {e}")
            rec["traceback"] = traceback.format_exc()[-3000:]
            self.notifier.notify("error", "运行失败", f"- 运行 {run_id}（{origin}）\n- {type(e).__name__}: {str(e)[:500]}")
        rec["finished_at"] = ts()
        write_json(self.data_dir / "runs" / f"{run_id}.json", rec)
        idx_list = self._run_index()
        idx_list.insert(0, {"id": run_id, "started_at": rec["started_at"], "trading_date": rec.get("trading_date"),
                            "origin": origin, "backfill": bool(backfill), "pools": [{"key": p["key"], "count": p.get("count", 0)} for p in rec["pools"]],
                            "picks": [p["code"] + " " + p["name"] for p in rec["picks"]],
                            "orders": len(rec["orders"]), "exits": len([e for e in rec["exits"] if e.get("action")]),
                            "errors": len(rec["errors"]),
                            "buy_strategy": (rec.get("strategy") or {}).get("buy"),
                            "sell_strategy": (rec.get("strategy") or {}).get("sell")})
        write_json(self.data_dir / "runs" / "index.json", idx_list[:300])
        return rec

    def _buy(self, c: Candidate, day: str, is_trading_day, oid: str, meta: dict | None = None) -> dict:
        cfg = self.cfg
        ref = c.price()
        meta = meta or {}
        base = {"id": oid, "side": "buy", "symbol": c.code, "name": c.name, "trading_date": day, "ref_price": ref,
                **({"pool": meta.get("pool"), "strategy": meta.get("buy_strategy"), "strategy_id": meta.get("buy_strategy_id"),
                    "sell_strategy": meta.get("sell_strategy")} if meta.get("buy_strategy") else {}),
                "sources": c.sources, "reason": f"漏斗入选（综合分 {c.fields.get('score', 0):.3f}，来自 {'+'.join(c.sources)}）",
                "is_trading_day": is_trading_day, "limit_up": c.get("limit_up"), "limit_down": c.get("limit_down"),
                "no_limit": bool(c.get("no_limit"))}
        if self.book.get(c.code):
            return {**base, "status": "skipped", "status_label": "跳过", "message": "已有持仓，不重复买入", "quantity": 0, "price": 0}
        if not ref:
            return {**base, "status": "skipped", "status_label": "跳过", "message": "没有参考价", "quantity": 0, "price": 0}
        price = round2(ref * (1 + float(cfg["sizing"]["buy_price_offset_pct"]) / 100))
        if c.get("limit_up"):
            price = min(price, c.get("limit_up"))
        qty = round_buy_qty(c.code, float(cfg["sizing"]["per_stock_amount"]) / price)
        if qty <= 0:
            return {**base, "status": "skipped", "status_label": "跳过", "price": price, "quantity": 0,
                    "message": f"每只计划金额 {cfg['sizing']['per_stock_amount']} 元买不到一手（价格 {price}）"}
        return self.submit({**base, "price": price, "quantity": qty})

    # ---------- 退出 ----------
    def _position_price(self, m: Market, code: str, day: str) -> tuple[float | None, str | None]:
        d, err = m.minute(code, day)
        if d and d.get("points"):
            return float(d["points"][-1]["p"]), d.get("preclose")
        return None, None

    def _mark_book(self, m: Market, day: str) -> None:
        prices = {}
        for code in list(self.book.positions()):
            px, _ = self._position_price(m, code, day)
            if px:
                prices[code] = px
        self.book.mark(prices, day)

    def preview_exits(self, now_hm: str | None = None) -> list[dict]:
        """只评估不下单（网页持仓列表用）。"""
        m = self.market()
        day = m.latest_trading_date()
        return self._check_exits(m, day, trade=False, origin="预览", now_hm=now_hm)

    def check_exits(self, origin: str = "手动", now_hm: str | None = None) -> dict:
        if not self.run_lock.acquire(timeout=30):
            raise RuntimeError("有运行尚未结束")
        try:
            m = self.market()
            day = m.latest_trading_date()
            run_id = now_cn().strftime("%Y%m%d-%H%M%S")
            res = self._check_exits(m, day, trade=True, origin=origin, now_hm=now_hm, run_id=run_id)
            self._mark_book(m, day)
            return {"trading_date": day, "results": res}
        finally:
            self.run_lock.release()

    def _sell_layers(self, cfg: dict | None = None):
        return build_layers(layers_of(cfg or self.cfg, "sell"), self.root, "sell")

    def _sell_plan(self, binder, cache: dict, pos: dict) -> dict:
        """一只持仓按哪个股池的哪套卖出策略卖：返回 resolve 结果 + 建好的卖出链（同一套策略只建一次）。
        持仓页手动指定的卖出策略（pos.sell_strategy_id）优先于股池绑定。"""
        res = binder.resolve_sell(pos.get("sources"), pos.get("sell_strategy_id"))
        key = res.get("strategy_id") or "@active"
        if key not in cache:
            layers = self._sell_layers(res["cfg"])
            needs: set[str] = set()
            for l in layers:
                if l.spec.get("enabled", True):
                    needs |= l.needs()
            cache[key] = (layers, needs)
        res["layers"], res["needs"] = cache[key]
        return res

    def _eval_position(self, m: Market, layers, needs: set, code: str, pos: dict, day: str, now_hm: str,
                       sellable: int, plan: dict | None = None) -> tuple[dict, float | None, float | None]:
        """对一只持仓跑卖出因子链，返回（评估结果，现价，昨收）。plan 是 _sell_plan 的结果（按股池绑定的策略）。"""
        cfg_eff, rule, sell_by = self.cfg, None, None
        if plan is not None:
            layers, needs, cfg_eff = plan["layers"], plan["needs"], plan["cfg"]
            rule = rule_for(cfg_eff, pos, plan["pool"])
            sell_by = {"pool": plan["pool"], "strategy_id": plan["strategy_id"], "strategy": plan["strategy"],
                       "by": plan["by"], "bound": plan["bound"], "override": plan["override"]}
        try:
            price, preclose = self._position_price(m, code, day)
        except Exception as e:  # noqa: BLE001
            log.warning("%s 取最新价失败：%s", code, e)
            price, preclose = None, None
        held = m.trading_days_between(pos["first_buy_date"], day)
        c = position_candidate(pos, price, held, sellable, now_hm)
        if price and needs:
            if "minute" in needs:
                self._load_minute(c, m, day)
            if "daily" in needs:
                self._load_daily(c, m, day)
                highs = [b.get("high") for b in (c.daily or []) if b.get("date", "") >= pos["first_buy_date"] and b.get("high")]
                hi = max(highs + [c.get("day_high") or 0, price])
                c.fields["high_since_buy"] = hi
                c.fields["drawdown_since_buy_pct"] = (hi - price) / hi * 100 if hi else None
            self._enrich_fields([c], day, [])
        ev = evaluate_chain(cfg_eff, layers, pos, price, day, now_hm, held, sellable, cand=c, root=self.root,
                            extra=self.ext_ctx(day), rule=rule, sell_by=sell_by)
        return ev, price, preclose

    def _alert_exit_problems(self, ev: dict, pos: dict, day: str, now_hm: str, trade: bool) -> None:
        """卖出检查没法正常判断时（没有最新价、因子出错、止损层数据缺失）：记日志，交易时推钉钉。不会悄悄跳过。"""
        probs = ev.get("problems") or []
        if not probs:
            return
        code, name = pos["symbol"], pos.get("name", "")
        lines = []
        for p in probs:
            where = f"「{p['layer']}」层 {p.get('factor') or ''}".strip() if p.get("layer") else ""
            lines.append(f"- {where + '：' if where else ''}{p.get('note')}")
        log.warning("卖出检查 %s %s 没法正常判断（%s）：%s", code, name, now_hm, "；".join(l[2:] for l in lines))
        urgent = [p for p in probs if p["kind"] in ("no_price", "no_cost", "error") or p.get("stop_loss")]
        if trade and urgent:
            by = ev.get("sell_by") or {}
            self.notifier.notify(
                "exit_problem", f"止损/卖出检查没法判断：{code} {name}",
                "\n".join(lines) + f"\n- 时间 {now_hm}，按{('股池 ' + by['pool'] + ' 的') if by.get('pool') else ''}卖出策略「{by.get('strategy') or '生效策略'}」"
                "\n- 这一轮没有卖出，也不会当成「没触发」。请检查行情/数据源，必要时到同花顺手动处理",
                dedupe_key=f"exitprob-{day}-{code}-{'|'.join(sorted({p['kind'] for p in urgent}))}", dedupe_secs=2 * 3600)

    def _check_exits(self, m: Market, day: str, trade: bool, origin: str, now_hm: str | None = None, run_id: str = "") -> list[dict]:
        """卖出因子链：对每只持仓逐只检查。规则（始终生效）：止损层最先、默认不可被保留跳过；T+1；
        实盘只卖「已确认」持仓、数量不超过券商可卖；人工保留只管一次；自动交易关闭/一键停止时只记信号。"""
        cfg = self.cfg
        now_hm = now_hm or now_cn().strftime("%H:%M")
        is_td = m.is_trading_day(day) if day == today_str() else False
        binder, cache = self.binder(), {}
        out = []
        if self.mode == "live" and trade and self.book.positions():
            self.link.sync_before_sell()        # 卖出前先和券商对一次账（状态、可卖数量）
        for n, (code, pos) in enumerate(list(self.book.positions().items()), 1):
            sellable = self.book.sellable(code, day)
            if self.mode == "live":
                bav = self.link.broker_available(code)
                if bav is not None and bav < sellable:
                    sellable = bav          # 以券商可卖数量为上限
                if not self.auto_pos():   # 手动账本：已报未成交的卖出委托先扣掉，避免重复卖
                    sellable = max(0, sellable - self.journal.open_qty(code, "sell"))
            plan = self._sell_plan(binder, cache, pos)
            ev, price, preclose = self._eval_position(m, None, set(), code, pos, day, now_hm, sellable, plan=plan)
            self._alert_exit_problems(ev, pos, day, now_hm, trade)
            ev["action"] = None
            ev["state_label"] = STATE_LABELS.get(ev["state"], ev["state"])
            out.append(ev)
            label = ev.get("decision_label") or ""
            if self.mode != "paper" and pos.get("state") != "confirmed":
                ev["note"] = f"持仓状态「{ev['state_label']}」，不自动卖出；自动对账确认后恢复，对不上请到同花顺核对后在网页确认"
                if trade and ev["decision"]:
                    ev["action"] = "manual_needed"
                    self.notifier.notify("manual_needed", f"需要人工处理：{code} {pos['name']}",
                                         f"- 卖出因子「{label}」触发：{ev['reason']}\n- 但持仓状态是「{ev['state_label']}」（{pos.get('state_note', '')}）\n"
                                         f"- 系统不会自动卖出。自动对账会继续核对券商持仓/委托；若对账也对不上，请到同花顺核对并手动处理，然后在网页持仓页确认",
                                         dedupe_key=f"manual-{day}-{code}-{ev['decision']}", dedupe_secs=6 * 3600)
                continue
            if not ev["decision"]:
                continue
            if sellable <= 0:
                ev["action"] = "t1_locked"
                ev["note"] = "今天买入的部分 T+1 不能卖"
                continue
            if not trade:
                continue
            note = ""
            if pos.get("hold"):
                if not ev["hold_skippable"]:
                    note = ("已标记保留，但止损不允许被保留跳过（exits.hold_can_skip_stop_loss = false）" if ev.get("stop_loss")
                            else f"已标记保留，但卖出层「{label}」设置了不可被保留跳过")
                elif not (self.switches.auto_enabled() and not self.switches.halted()):
                    note = "已标记保留；自动交易关闭或已一键停止，本次不会真的卖出，保留标记不消耗"
                else:
                    d = {"skip": True, "consume": True, "note": ""}
                    for pname, policy in list(HOLD_POLICIES.items()):
                        decide = getattr(policy, "decide", None)
                        if not decide:
                            continue
                        try:
                            r = decide(code, pos, ev, day, now_hm, str(self.data_dir))
                        except Exception as ex:  # noqa: BLE001
                            log.warning("保留策略 %s 出错，按默认处理：%s", pname, ex)
                            continue
                        if r is not None:
                            d = {"skip": bool(r.get("skip", True)), "consume": bool(r.get("consume", True)),
                                 "note": str(r.get("note") or "")}
                            break
                    h = self.book.consume_hold(code) if d["consume"] else pos.get("hold")
                    if d["skip"]:
                        ev["action"] = "hold_skipped"
                        ev["note"] = d["note"] or "人工保留：跳过这一次自动卖出，保留标记已失效，下次到点照常卖"
                        rec = {"id": f"{run_id}-H{n}", "side": "sell", "symbol": code, "name": pos["name"], "trading_date": day,
                               "quantity": sellable, "price": price, "reason": f"{label}：{ev['reason']}", "status": "hold_skipped",
                               "status_label": STATUS_LABELS["hold_skipped"], "message": ev["note"], "hold": h, "mode": self.mode,
                               "created_at": ts()}
                        append_jsonl(self.data_dir / "orders.jsonl", rec)
                        self.notifier.notify("hold_skip", f"已按保留跳过卖出：{code} {pos['name']}",
                                             f"- 卖出因子「{label}」触发：{ev['reason']}\n- 现价 {price:.2f}，成本 {pos['avg_cost']:.3f}，浮盈 {ev.get('pnl_pct', 0):+.2f}%\n"
                                             f"- {ev['note']}")
                        continue
                    if d["note"]:
                        note = d["note"]
            off = float(cfg["sizing"]["sell_price_offset_pct"]) / 100
            by = ev.get("sell_by") or {}
            lim = limit_info(code, pos["name"], preclose, day) if preclose else {}
            px = round2(price * (1 - off))
            if lim.get("limit_down"):
                px = max(px, lim["limit_down"])
            it = {"id": f"{run_id}-S{n}", "side": "sell", "symbol": code, "name": pos["name"], "trading_date": day,
                  "quantity": int(sellable), "price": px, "ref_price": price, "sellable": int(sellable),
                  "limit_up": lim.get("limit_up"), "limit_down": lim.get("limit_down"), "is_trading_day": is_td,
                  "sources": pos.get("sources"), "reason": f"{label}：{ev['reason']}", "sell_layer": ev["decision"],
                  "pool": by.get("pool"), "strategy": by.get("strategy"), "strategy_id": by.get("strategy_id")}
            r = self.submit(it)
            ev["action"] = r["status"]
            ev["order"] = {k: r.get(k) for k in ("id", "status", "status_label", "message", "price", "quantity")}
            if note:
                ev["note"] = note
        if trade:
            self._log_exit_check(day, origin, now_hm, run_id, out)
        return out

    def _log_exit_check(self, day: str, origin: str, now_hm: str, run_id: str, results: list[dict]) -> None:
        """卖出结果页的数据：每次卖出检查看了哪些持仓、触发了哪一层、有没有被人工保留跳过、下了什么委托。"""
        rows = []
        for ev in results:
            trig = None
            for l in ev.get("layers") or []:
                if l.get("triggered"):
                    trig = l
                    break
            rows.append({"symbol": ev.get("symbol"), "name": ev.get("name"), "price": ev.get("price"),
                         "avg_cost": ev.get("avg_cost"), "pnl_pct": ev.get("pnl_pct"), "held_days": ev.get("held_days"),
                         "rule_from": (ev.get("rule") or {}).get("from"), "sell_by": ev.get("sell_by"),
                         "problems": ev.get("problems") or [], "decision": ev.get("decision"),
                         "decision_label": ev.get("decision_label"), "reason": ev.get("reason"), "action": ev.get("action"),
                         "note": ev.get("note"), "order": ev.get("order"), "state_label": ev.get("state_label"),
                         "hold": bool(ev.get("hold")), "layers": ev.get("layers")})
        try:
            append_jsonl(self.data_dir / "exit_checks.jsonl",
                         {"at": ts(), "id": run_id, "origin": origin, "trading_date": day, "now_hm": now_hm,
                          "strategy": self.strategy_names().get("sell"), "mode": self.mode,
                          "auto": bool(self.switches.auto_enabled() and not self.switches.halted()),
                          "positions": len(rows), "triggered": sum(1 for r in rows if r["decision"]),
                          "orders": sum(1 for r in rows if r.get("order")), "results": rows})
        except Exception:  # noqa: BLE001
            log.exception("写卖出检查记录失败")

    def exit_checks(self, limit: int = 50, day: str | None = None) -> list[dict]:
        from .util import read_jsonl
        rows = read_jsonl(self.data_dir / "exit_checks.jsonl") if (self.data_dir / "exit_checks.jsonl").exists() else []
        if day:
            rows = [r for r in rows if r.get("trading_date") == day]
        return list(reversed(rows))[:limit]

    def remind_exits(self, exit_time: str) -> dict:
        """到点卖出前的提醒：列出到这个时刻会被卖出因子链卖掉的持仓，提示可以去网页点「保留」。"""
        m = self.market()
        day = m.latest_trading_date()
        binder, cache = self.binder(), {}
        lines = []
        for code, pos in self.book.positions().items():
            sellable = self.book.sellable(code, day)
            ev, _, _ = self._eval_position(m, None, set(), code, pos, day, exit_time, sellable,
                                           plan=self._sell_plan(binder, cache, pos))
            if not ev["decision"] or sellable <= 0 or exit_time not in (ev.get("time_ats") or []):
                continue
            hold = pos.get("hold")
            if self.mode != "paper" and pos.get("state") != "confirmed":
                tail = f"持仓状态「{STATE_LABELS.get(pos.get('state'), pos.get('state'))}」，不会自动卖，需人工处理"
            elif hold and ev["hold_skippable"]:
                tail = "已标记保留，将跳过这次"
            elif hold:
                tail = "已标记保留，但这条卖出规则不可跳过，仍会卖出"
            else:
                tail = "未保留，到点卖出"
            lines.append(f"- {code} {pos['name']}：{ev['decision_label']}，浮盈 {ev.get('pnl_pct', 0):+.2f}%，可卖 {sellable} 股；{tail}")
        if lines:
            auto = "" if self.switches.auto_enabled() else "\n\n（自动交易当前关闭，到点只会记录信号）"
            self.notifier.notify("exit_reminder", f"{exit_time} 将自动卖出 {len(lines)} 只",
                                 "\n".join(lines) + "\n\n想继续持有的，打开网页 → 持仓 → 点「保留」，系统会跳过这一次自动卖出。" + auto,
                                 dedupe_key=f"remind-{day}-{exit_time}", dedupe_secs=3600)
        return {"exit_time": exit_time, "trading_date": day, "lines": lines}

    # ---------- 持仓操作 ----------
    def set_hold(self, code: str, hold: bool, note: str = "", by: str = "网页", mode: str | None = None) -> dict:
        p = self.book.set_hold(code, hold, note, mode)
        log.info("%s %s 人工保留：%s（%s）", code, p["name"], "标记" if hold else "取消", by)
        append_jsonl(self.data_dir / "holds.jsonl", {"at": ts(), "symbol": code, "hold": hold, "note": note, "by": by, "mode": mode})
        return p

    def account(self) -> dict:
        b = self.book
        return {"mode": self.mode, "mode_label": self.broker.label, "summary": b.summary(), "positions": list(b.positions().values()),
                "closed": list(reversed(b.s["closed"]))[:200], "nav": b.s.get("nav", []),
                "adjustments": list(reversed(b.s.get("adjustments") or []))[:30]}

    def _run_index(self) -> list:
        return read_json(self.data_dir / "runs" / "index.json", []) or []

    def get_run(self, run_id: str) -> dict | None:
        if run_id == "latest":
            idx = self._run_index()
            if not idx:
                return None
            run_id = idx[0]["id"]
        from .viewsvc import normalize_run          # 旧层名换新叫法、运行方式给中文（只改返回的副本）
        return normalize_run(read_json(self.data_dir / "runs" / f"{Path(run_id).name}.json", None))

    def in_trading_session(self) -> bool:
        now = now_cn()
        day = now.strftime("%Y-%m-%d")
        cache = getattr(self, "_td_cache", None)
        if not cache or cache[0] != day:
            try:
                td = self.market().is_trading_day(day)
            except Exception:  # noqa: BLE001
                td = None
            cache = self._td_cache = (day, now.weekday() < 5 if td is None else td)
        td = cache[1]
        return bool(td) and in_sessions(now, self.cfg["risk"].get("sessions") or [["09:15", "11:30"], ["13:00", "15:00"]])

    def broker_status(self) -> dict:
        """后台检查一次连接，立刻返回上次的状态（不在请求线程里等下单程序）。"""
        self.link.request_sync("status")
        return self.link.view()

    def cancel_order(self, order_id: str, by: str = "web") -> dict:
        """撤掉本系统发出的一笔实盘在途委托（只在实盘三重开关全开时可用）。"""
        if self.mode != "live":
            return {"status": "rejected", "message": f"当前是{self.broker.label}，没有实盘委托可撤"}
        r = self.broker.cancel(order_id)
        append_jsonl(self.data_dir / "orders.jsonl", {"id": f"{order_id}-C", "side": "cancel", "symbol": "", "status": r["status"],
                                                      "status_label": "撤单", "message": r.get("message"), "by": by, "created_at": ts(),
                                                      "mode": self.mode, "target": order_id})
        return r

    def _notify_run(self, rec: dict) -> None:
        st = rec["stages"]
        funnel = " → ".join(str(s["input"]) for s in st) + (f" → {len(rec['picks'])}" if st else "")
        picks = "\n".join(f"- **{p['code']} {p['name']}** 现价 {p['fields'].get('minute_last', p['fields'].get('price', 0)):.2f}，"
                          f"涨幅 {p['fields'].get('change_rate', 0):+.2f}%，分 {p['value']}" for p in rec["picks"]) or "- 无入选"
        orders = "\n".join(f"- {o.get('status_label', o.get('status'))}：{o['symbol']} {o.get('quantity')} 股 @ {o.get('price')}" for o in rec["orders"])
        auto = "开" if rec["auto_trade"] else "关（只记信号）"
        text = (f"- 数据日期 {rec.get('trading_date')}，{rec['origin']}触发，模式 {self.broker.label}，自动交易 {auto}\n"
                f"- 漏斗：{funnel}\n\n{picks}" + (f"\n\n**委托**\n{orders}" if orders else "")
                + ("\n\n⚠ " + "；".join(rec["warnings"][:3]) if rec["warnings"] else ""))
        self.notifier.notify("run_result", f"选股结果 {rec['id']}", text)
