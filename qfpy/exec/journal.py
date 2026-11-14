"""交易委托记录（每个运行模式一本：data/journal/<mode>.json）。

- 系统发出的每笔委托（含只记信号、风控拦截）在 Engine.submit 里自动记下：时间、代码、方向、价格、数量、状态、
  成交情况、来源策略。
- 用户可以在网页上手动补记一笔委托，或修改已有委托的状态和成交（成交数量、成交均价、费用）。
- 「入账」：成交数量比已入账的多出来的部分，按成交均价记进本地账本（买入加持仓、扣可用资金；卖出减持仓、
  加可用资金）。模拟盘和演练的系统委托在下单时已经记过账（applied_qty = 成交数量），不会重复入账。
  自动获取持仓打开时（实盘），持仓和资金以券商为准，这里只记录，不入账。
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

from ..util import StateTransaction, lock_for, read_json, read_jsonl, today_str, ts, write_json

STATUS_LABELS = {
    "signal_only": "只记信号", "risk_rejected": "风控拦截", "submitted": "已报", "unknown": "结果未知",
    "partial": "部分成交", "filled": "已成交", "partial_cancelled": "部成部撤", "cancelled": "已撤单",
    "rejected": "废单/被拒", "unfilled": "未成交", "not_placed": "未报", "dry_run": "演练成交",
}
OPEN_STATUSES = {"submitted", "unknown", "partial"}
NOT_SENT = {"signal_only", "risk_rejected"}
MANUAL_STATUSES = ("submitted", "partial", "filled", "partial_cancelled", "cancelled", "rejected", "unfilled")


def _num(v, kind=float, name=""):
    if v is None or v == "":
        return None
    try:
        return kind(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name}必须是{'整数' if kind is int else '数字'}") from None


class OrderJournal:
    def __init__(self, data_dir: Path, mode: str):
        self.mode = mode
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "journal" / f"{mode}.json"
        s = read_json(self.path, None)
        if s is None:
            s = {"mode": mode, "created_at": ts(), "orders": self._migrate()}
            write_json(self.path, s)
        self.s = s
        self.lock = StateTransaction(self.path, lock_for(self.path), self.reload_from_disk, self.save)

    def reload_from_disk(self) -> None:
        s = read_json(self.path, None)
        self.s = s if s is not None else {"mode": self.mode, "created_at": ts(), "orders": self._migrate()}

    def _migrate(self) -> list[dict]:
        """第一次建本子时，把旧的 orders.jsonl 里本模式的委托搬进来（最多 500 条）。"""
        out = []
        for r in read_jsonl(self.data_dir / "orders.jsonl", 500) or []:
            if r.get("mode") == self.mode and r.get("id"):
                out.append(self._from_rec(r))
        return out

    def save(self) -> None:
        # 同 Book.save：不在事务外触发重载，单独调用时保持「写当前内存」的语义
        write_json(self.path, self.s)

    # ---------- 系统委托 ----------
    def _from_rec(self, rec: dict) -> dict:
        st = rec.get("status") or "unknown"
        qty = int(rec.get("quantity") or 0)
        e = {"id": rec["id"], "created_at": rec.get("created_at") or ts(), "trading_date": rec.get("trading_date") or today_str(),
             "symbol": rec.get("symbol"), "name": rec.get("name", ""), "side": rec.get("side"), "price": rec.get("price"),
             "quantity": qty, "status": st, "filled_qty": 0, "fill_price": None, "fee": None,
             "strategy": "、".join(rec.get("sources") or []) or rec.get("run_label") or "", "reason": rec.get("reason", ""),
             "origin": "手动" if rec.get("manual") else "系统", "message": rec.get("message", ""), "entrust_no": rec.get("entrust_no"),
             "applied_qty": 0, "applied_fee": 0.0, "updated_at": rec.get("created_at") or ts(), "edits": []}
        if st == "filled":       # 模拟盘：已在账本里成交
            e.update(filled_qty=qty, fill_price=rec.get("fill_price") or rec.get("price"), fee=rec.get("fee"),
                     applied_qty=qty, applied_fee=float(rec.get("fee") or 0))
        elif st == "dry_run":    # 演练：虚拟成交，已在账本里记过
            e.update(filled_qty=qty, fill_price=rec.get("price"), applied_qty=qty)
        return e

    def record_system(self, rec: dict) -> dict:
        with self.lock:
            e = self._from_rec(rec)
            self._orders().append(e)
            del self._orders()[:-2000]
            self.save()
            return e

    def _orders(self) -> list[dict]:
        return self.s.setdefault("orders", [])

    def get(self, oid: str) -> dict | None:
        return next((o for o in self._orders() if o["id"] == oid), None)

    def update_system(self, oid: str, **kw) -> dict | None:
        with self.lock:
            e = self.get(oid)
            if e is not None:
                e.update(kw)
                e["updated_at"] = ts()
                self.save()
            return e

    def sync_from_book(self, book) -> None:
        """实盘自动对账的结果同步到委托记录（状态、合同号、成交数量）。只改系统委托，不入账（账本已由对账更新）。"""
        changed = False
        with self.lock:
            pend = book.pending()
            done = {r["order_id"]: r for r in book.s.get("pending_done") or []}
            for e in self._orders():
                if e.get("origin") not in ("系统", "手动") or e["status"] not in OPEN_STATUSES:
                    continue
                r = pend.get(e["id"])
                if r is not None:
                    f = int(r.get("filled") or 0)
                    st = "partial" if f > 0 else e["status"]
                    new = {"entrust_no": r.get("entrust_no") or e.get("entrust_no"), "filled_qty": f, "status": st,
                           "broker_status": r.get("broker_status")}
                else:
                    r = done.get(e["id"])
                    if r is None:
                        continue
                    out = r.get("outcome") or ""
                    st = {"filled_inferred": "filled", "pending": "submitted"}.get(out, out)
                    if st not in STATUS_LABELS:
                        st = "filled" if int(r.get("filled") or 0) >= e["quantity"] else "unknown"
                    f = int(r.get("filled") or (e["quantity"] if st == "filled" else 0))
                    new = {"entrust_no": r.get("entrust_no") or e.get("entrust_no"), "filled_qty": f, "status": st,
                           "fill_price": r.get("fill_price") or e.get("fill_price"), "broker_status": r.get("outcome_note")}
                if any(e.get(k) != v for k, v in new.items()):
                    e.update(new)
                    e["applied_qty"] = e["filled_qty"]     # 对账已把成交记进账本
                    e["updated_at"] = ts()
                    changed = True
            if changed:
                self.save()

    # ---------- 查询 ----------
    def list(self, limit: int = 200, include_signals: bool = True) -> list[dict]:
        rows = [o for o in self._orders() if include_signals or o["status"] not in NOT_SENT]
        rows = sorted(rows, key=lambda o: o.get("created_at") or "", reverse=True)[:limit]
        return [{**o, "status_label": STATUS_LABELS.get(o["status"], o["status"]),
                 "side_label": "买入" if o.get("side") == "buy" else "卖出", "open": o["status"] in OPEN_STATUSES,
                 "unapplied_qty": max(0, int(o.get("filled_qty") or 0) - int(o.get("applied_qty") or 0))} for o in rows]

    def list_one(self, oid: str) -> dict | None:
        e = self.get(oid)
        if e is None:
            return None
        return next(o for o in self.list(10 ** 6) if o["id"] == oid)

    def open_orders(self, side: str | None = None) -> list[dict]:
        return [o for o in self._orders() if o["status"] in OPEN_STATUSES and (side is None or o.get("side") == side)]

    def open_qty(self, code: str, side: str) -> int:
        """某只股票还没成交完的委托数量（手动持仓模式下防止重复卖出）。"""
        return sum(max(0, int(o["quantity"]) - int(o.get("filled_qty") or 0)) for o in self.open_orders(side) if o["symbol"] == code)

    def counts(self) -> dict:
        out = {"total": 0, "open": 0, "today": 0}
        day = today_str()
        for o in self._orders():
            if o["status"] in NOT_SENT:
                continue
            out["total"] += 1
            out["open"] += o["status"] in OPEN_STATUSES
            out["today"] += o.get("trading_date") == day
        return out

    # ---------- 手动补记 / 修改 ----------
    def manual_save(self, body: dict, book, apply_to_book: bool, by: str = "网页") -> dict:
        """新建（没有 id）或修改一笔委托。返回 {order, applied: {...} | None}。
        apply_to_book：成交数量比已入账的多时，把多出来的部分记进账本（手动持仓模式 / 模拟盘）。"""
        with self.lock:
            oid = str(body.get("id") or "").strip()
            e = self.get(oid) if oid else None
            if oid and e is None:
                raise KeyError(oid)
            if e is None:
                code = str(body.get("symbol") or "").strip()
                if not re.fullmatch(r"\d{6}", code):
                    raise ValueError("代码应为 6 位数字")
                side = body.get("side")
                if side not in ("buy", "sell"):
                    raise ValueError("方向只能是买入或卖出")
                e = {"id": "M" + uuid.uuid4().hex[:11], "created_at": ts(), "trading_date": today_str(), "symbol": code,
                     "name": "", "side": side, "price": None, "quantity": 0, "status": "submitted", "filled_qty": 0,
                     "fill_price": None, "fee": None, "strategy": "手动", "reason": "", "origin": "手动补记", "message": "",
                     "entrust_no": None, "applied_qty": 0, "applied_fee": 0.0, "updated_at": ts(), "edits": []}
                new_entry = True
            else:
                new_entry = False
            cand = dict(e)
            for k in ("name", "strategy", "message", "entrust_no"):
                if k in body and body[k] is not None:
                    cand[k] = str(body[k]).strip()
            if body.get("time"):
                t = str(body["time"]).strip().replace("T", " ")
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}( \d{2}:\d{2}(:\d{2})?)?", t):
                    raise ValueError("时间格式应为 2026-11-11 09:31")
                cand["created_at"] = t if len(t) > 16 else (t + ":00" if len(t) == 16 else t + " 00:00:00")
                cand["trading_date"] = t[:10]
            if "price" in body:
                cand["price"] = _num(body["price"], float, "委托价")
            if "quantity" in body:
                cand["quantity"] = _num(body["quantity"], int, "委托数量") or 0
            if "filled_qty" in body:
                cand["filled_qty"] = _num(body["filled_qty"], int, "成交数量") or 0
            if "fill_price" in body:
                cand["fill_price"] = _num(body["fill_price"], float, "成交均价")
            if "fee" in body:
                cand["fee"] = _num(body["fee"], float, "费用")
            if body.get("status"):
                if body["status"] not in STATUS_LABELS:
                    raise ValueError(f"未知状态 {body['status']}")
                cand["status"] = body["status"]
            q, f = int(cand["quantity"] or 0), int(cand["filled_qty"] or 0)
            if q <= 0:
                raise ValueError("委托数量必须大于 0")
            if cand["price"] is not None and cand["price"] <= 0:
                raise ValueError("委托价必须大于 0")
            if not 0 <= f <= q:
                raise ValueError("成交数量必须在 0 到委托数量之间")
            if cand["fee"] is not None and cand["fee"] < 0:
                raise ValueError("费用不能是负数")
            if not body.get("status"):     # 没指定状态时按成交数量推断
                if f >= q:
                    cand["status"] = "filled"
                elif f > 0 and cand["status"] in ("submitted", "unknown"):
                    cand["status"] = "partial"
            if f > 0 and not (cand["fill_price"] or cand["price"]):
                raise ValueError("有成交时要填成交均价（或委托价）")
            if f > 0 and not cand["fill_price"]:
                cand["fill_price"] = cand["price"]
            if f > 0 and cand["fill_price"] is not None and cand["fill_price"] <= 0:
                raise ValueError("成交均价必须大于 0")
            applied = int(e.get("applied_qty") or 0)
            delta = f - applied
            app = None
            if apply_to_book and delta < 0:
                raise ValueError(f"这笔已有 {applied} 股成交记进了持仓和资金，成交数量不能改小；要更正请直接改持仓和可用资金")
            if apply_to_book and delta > 0:
                fee_total = float(cand["fee"] or 0)
                fee_part = max(0.0, fee_total - float(e.get("applied_fee") or 0))
                app = book.manual_fill(cand["side"], cand["symbol"], cand.get("name") or "", float(cand["fill_price"]), delta,
                                       fee_part, cand["trading_date"], f"委托 {cand['id']}")
                cand["applied_qty"] = f
                cand["applied_fee"] = round(float(e.get("applied_fee") or 0) + fee_part, 2)
                if not cand.get("name") and app.get("name"):
                    cand["name"] = app["name"]
            changes = {k: [e.get(k), cand.get(k)] for k in ("status", "price", "quantity", "filled_qty", "fill_price", "fee",
                                                              "created_at", "name", "strategy", "message")
                       if e.get(k) != cand.get(k)}
            if not new_entry and not changes and app is None:
                return {"order": e, "applied": None, "changed": False}
            cand["updated_at"] = ts()
            cand.setdefault("edits", []).append({"at": ts(), "by": by, "changes": changes,
                                                 **({"applied_qty": delta} if app else {})})
            del cand["edits"][:-20]
            if new_entry:
                self._orders().append(cand)
            else:
                e.clear()
                e.update(cand)
            self.save()
            return {"order": cand, "applied": app, "changed": True}

    def delete_manual(self, oid: str) -> dict:
        """只能删除手动补记、且没有入账的委托。"""
        with self.lock:
            e = self.get(oid)
            if e is None:
                raise KeyError(oid)
            if e.get("origin") != "手动补记":
                raise ValueError("本系统发出的委托不能删除，只能修改状态和成交")
            if int(e.get("applied_qty") or 0) > 0:
                raise ValueError("这笔的成交已经记进持仓和资金，不能删除；要更正请直接改持仓和可用资金")
            self._orders().remove(e)
            self.save()
            return e
