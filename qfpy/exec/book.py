"""持仓账本。

- paper：模拟盘账户（现金、持仓、成交、已实现盈亏、收益曲线），费用按 Alpha 的算法，T+1 按 tick-stock-panel 的批次记账。
- dry_run / live：本地记录的持仓。实盘时由 reconcile.py 用下单程序的 /api/snapshot、/api/refresh
  （持仓、委托、成交）自动对账；对不上才推送请你人工处理。
  每只持仓有 state：
    confirmed  已确认（演练模式自动确认；实盘由自动对账确认，也可在网页人工核对）
    unconfirmed_buy  买入委托已提交、是否成交未知
    selling    卖出委托已提交、是否成交未知
    uncertain  委托结果未知（超时/断线/没拿到合同号）
  只有 confirmed 的持仓会自动卖出；其余状态推送请人工处理。
"""
from __future__ import annotations

from pathlib import Path

from ..util import StateTransaction, lock_for, read_json, round2, ts, write_json
from ..vendor.alpha_fees import calc_buy_cost, calc_sell_cost, closed_trade_stats
from ..vendor.tsp_lots import available_of, consume_lots

STATE_LABELS = {"confirmed": "已确认", "unconfirmed_buy": "买入待确认", "selling": "卖出待确认", "uncertain": "结果未知"}


class Book:
    def __init__(self, data_dir: Path, mode: str, paper_cfg: dict):
        self.mode = mode
        self.paper_cfg = paper_cfg
        name = "paper/account.json" if mode == "paper" else f"ledger/{mode}.json"
        self.path = Path(data_dir) / name
        self.s = read_json(self.path, None) or self._fresh()
        # 事务锁：每次「读-改-写」最外层先拿跨进程锁并从磁盘重载，避免两个进程各持一份
        # 旧内存快照、后写的把先写的整份覆盖（保留标记/持仓状态被冲掉）。
        self.lock = StateTransaction(self.path, lock_for(self.path), self.reload_from_disk, self.save)

    def _fresh(self) -> dict:
        cash = float(self.paper_cfg.get("initial_cash", 100000)) if self.mode == "paper" else None
        return {"mode": self.mode, "created_at": ts(), "initial_cash": cash, "cash": cash, "positions": {},
                "closed": [], "nav": []}

    def reload_from_disk(self) -> None:
        self.s = read_json(self.path, None) or self._fresh()

    def save(self) -> None:
        # 不套 self.lock：单独调用时保持「把当前内存写盘」的语义，不会触发事务重载
        # 而把调用方刚改的东西丢掉；在事务内调用时外层已持有跨进程锁。
        write_json(self.path, self.s)

    def reset(self) -> None:
        with self.lock:
            self.s = self._fresh()
            self.save()

    # ---------- 查询 ----------
    def positions(self) -> dict:
        return self.s["positions"]

    def get(self, code: str) -> dict | None:
        return self.s["positions"].get(code)

    def sellable(self, code: str, day: str) -> int:
        p = self.get(code)
        return available_of(p, day) if p else 0

    # ---------- 记账 ----------
    def paper_fill_buy(self, code, name, price, qty, day, sources) -> tuple[float, float]:
        """模拟买入成交。返回 (成交价, 费用)。资金不足抛 ValueError。"""
        with self.lock:
            fill, fee = calc_buy_cost(price, qty, self.paper_cfg)
            cost = fill * qty + fee
            if cost > self.s["cash"] + 1e-6:
                raise ValueError(f"模拟账户资金不足：需要 {cost:.2f}，可用 {self.s['cash']:.2f}")
            self.s["cash"] = round2(self.s["cash"] - cost)
            self._add_lot(code, name, fill, qty, day, sources, cost, "confirmed")
            self.save()
            return fill, fee

    def paper_fill_sell(self, code, price, qty, day, reason) -> tuple[float, float, float]:
        """模拟卖出成交。返回 (成交价, 费用, 已实现盈亏)。"""
        with self.lock:
            p = self.get(code)
            fill, fee = calc_sell_cost(price, qty, self.paper_cfg)
            proceeds = fill * qty - fee
            cost_part = p["cost"] * qty / p["quantity"]
            pnl = round2(proceeds - cost_part)
            self.s["cash"] = round2(self.s["cash"] + proceeds)
            self._remove_qty(code, qty, day, cost_part)
            self.s["closed"].append({"symbol": code, "name": p["name"], "quantity": qty, "sell_price": fill, "fee": fee,
                                     "cost": round2(cost_part), "pnl": pnl, "date": day, "at": ts(), "reason": reason,
                                     "buy_date": p["first_buy_date"]})
            self.save()
            return fill, fee, pnl

    def ledger_buy(self, code, name, price, qty, day, sources, state: str, order_id: str) -> None:
        with self.lock:
            self._add_lot(code, name, price, qty, day, sources, price * qty, state, order_id)
            self.save()

    def ledger_mark(self, code, state: str, note: str, order_id: str | None = None) -> None:
        with self.lock:
            p = self.get(code)
            if p:
                p["state"] = state
                p["state_note"] = note
                p["state_at"] = ts()
                if order_id:
                    p.setdefault("orders", []).append(order_id)
                self.save()

    def ledger_sell_done(self, code, qty, price, day, reason) -> float | None:
        """演练模式卖出（虚拟成交）或你在网页确认实盘已卖出。"""
        with self.lock:
            p = self.get(code)
            if not p:
                return None
            qty = min(qty, p["quantity"])
            cost_part = p["cost"] * qty / p["quantity"]
            pnl = round2(price * qty - cost_part) if price else None
            self._remove_qty(code, qty, day, cost_part, any_lot=True)
            self.s["closed"].append({"symbol": code, "name": p["name"], "quantity": qty, "sell_price": price, "fee": None,
                                     "cost": round2(cost_part), "pnl": pnl, "date": day, "at": ts(), "reason": reason,
                                     "buy_date": p["first_buy_date"]})
            self.save()
            return pnl

    def confirm(self, code: str, quantity: int, avg_cost: float, buy_date: str | None = None) -> dict:
        """人工核对后确认持仓（按同花顺里的实际数量和成本）。quantity=0 表示实际没有持仓，删除。"""
        with self.lock:
            p = self.get(code)
            if not p:
                raise KeyError(code)
            if quantity <= 0:
                del self.s["positions"][code]
                self.save()
                return {"removed": True}
            d = buy_date or p["first_buy_date"]
            p.update(quantity=int(quantity), avg_cost=float(avg_cost), cost=round2(quantity * avg_cost),
                     lots=[{"date": d, "qty": int(quantity), "price": float(avg_cost)}], state="confirmed",
                     state_note="已人工核对确认", state_at=ts())
            self.save()
            return p

    # ---------- 持仓来源：手动填写 / 从下单程序快照自动获取 ----------
    MANUAL_LOT_DATE = "1970-01-01"        # 「可卖」部分记成早已买入的批次，其余记成今天买入（T+1 明天才能卖）

    @staticmethod
    def _lots_for(quantity: int, sellable: int, cost: float, day: str) -> list[dict]:
        lots = []
        if sellable > 0:
            lots.append({"date": Book.MANUAL_LOT_DATE, "qty": int(sellable), "price": float(cost)})
        if quantity - sellable > 0:
            lots.append({"date": day, "qty": int(quantity - sellable), "price": float(cost)})
        return lots

    def busy_codes(self) -> set[str]:
        """有在途委托或状态未确认的代码：手动编辑和自动获取都不碰，交给对账。"""
        codes = {r.get("symbol") for r in self.pending().values()}
        codes |= {c for c, p in self.positions().items() if p.get("state", "confirmed") != "confirmed"}
        return {c for c in codes if c}

    def manual_upsert(self, code: str, name: str, quantity: int, avg_cost: float, sellable: int, day: str) -> dict:
        """网页手动填写一只持仓（数量、成本、可卖数量），记为已确认。
        模拟盘也可以手填：当作把这些股票按成本「转入」模拟账户，初始资金同步加上成本差，收益率不会因此跳变。"""
        import re as _re
        code = str(code).strip()
        if not _re.fullmatch(r"\d{6}", code):
            raise ValueError("代码应为 6 位数字")
        try:
            quantity, sellable, avg_cost = int(quantity), int(sellable), float(avg_cost)
        except (TypeError, ValueError):
            raise ValueError("数量、可卖数量必须是整数，成本必须是数字") from None
        if quantity <= 0:
            raise ValueError("数量必须大于 0（要删除请点删除）")
        if not 0 <= sellable <= quantity:
            raise ValueError("可卖数量必须在 0 到持仓数量之间")
        if not avg_cost > 0:
            raise ValueError("成本价必须大于 0")
        with self.lock:
            if code in self.busy_codes():
                raise ValueError(f"{code} 有在途委托或待确认状态，等对账完成后再改")
            p = self.get(code)
            old_cost = float(p["cost"]) if p else 0.0
            if not p:
                p = self.s["positions"][code] = {"symbol": code, "name": name or code, "first_buy_date": day, "sources": ["手动填写"],
                                                 "hold": None, "orders": [], "last_price": avg_cost, "last_mark_at": ts(),
                                                 "sell_strategy_id": ""}
            p.update(name=name or p.get("name") or code, quantity=quantity, avg_cost=round(avg_cost, 4),
                     cost=round2(quantity * avg_cost), lots=self._lots_for(quantity, sellable, avg_cost, day),
                     state="confirmed", state_note="手动填写", state_at=ts(), origin="manual",
                     manual={"quantity": quantity, "sellable": sellable, "avg_cost": avg_cost, "day": day, "at": ts()})
            self._adjust("position", code, old_cost, p["cost"])
            self.save()
            return p

    def manual_remove(self, code: str) -> None:
        with self.lock:
            if code in self.busy_codes():
                raise ValueError(f"{code} 有在途委托或待确认状态，等对账完成后再改")
            if code not in self.positions():
                raise KeyError(code)
            old_cost = float(self.s["positions"][code]["cost"])
            del self.s["positions"][code]
            self._adjust("position", code, old_cost, 0.0)
            self.save()

    def _adjust(self, kind: str, what: str, old: float | None, new: float | None) -> None:
        """记一笔手工调整。模拟盘：手工调资金/持仓视作转入转出，初始资金同步调整。"""
        delta = round2((new or 0) - (old or 0))
        if self.mode == "paper" and delta:
            self.s["initial_cash"] = round2(float(self.s.get("initial_cash") or 0) + delta)
        adj = self.s.setdefault("adjustments", [])
        adj.append({"at": ts(), "kind": kind, "what": what, "old": old, "new": new, "delta": delta})
        del adj[:-200]

    # ---------- 手动资金 ----------
    def funds(self) -> dict:
        f = self.s.get("funds") or {}
        return {"available": self.s.get("cash"), "balance": f.get("balance"), "updated_at": f.get("updated_at")}

    def set_funds(self, available, balance=None) -> dict:
        """手动填写可用资金（必填）和资金余额（选填，含冻结资金；不填就按可用资金算）。"""
        try:
            available = float(available)
            balance = None if balance in (None, "") else float(balance)
        except (TypeError, ValueError):
            raise ValueError("可用资金、资金余额必须是数字") from None
        if available < 0 or (balance is not None and balance < 0):
            raise ValueError("资金不能是负数")
        if balance is not None and balance + 1e-6 < available:
            raise ValueError("资金余额不能小于可用资金（余额 = 可用 + 冻结）")
        with self.lock:
            old = self.s.get("cash")
            self.s["cash"] = round2(available)
            self.s["funds"] = {"balance": None if balance is None else round2(balance), "updated_at": ts()}
            self._adjust("funds", "可用资金", old, self.s["cash"])
            self.save()
            return self.funds()

    def adjust_cash(self, delta: float) -> None:
        """成交引起的资金变化（演练虚拟成交、手动补记成交）。没填过资金时不动。"""
        if self.s.get("cash") is None:
            return
        self.s["cash"] = round2(self.s["cash"] + delta)
        f = self.s.get("funds") or {}
        if f.get("balance") is not None:
            f["balance"] = round2(f["balance"] + delta)

    def manual_fill(self, side: str, code: str, name: str, price: float, qty: int, fee: float, day: str, note: str) -> dict:
        """把一笔手动记下的成交记进账本。买入：加一批 day 买入的持仓（T+1 才可卖），扣可用资金；
        卖出：减持仓、记已实现盈亏、加可用资金。返回 {side, symbol, name, quantity, price, fee, cash_delta, pnl}。"""
        qty, price, fee = int(qty), float(price), float(fee or 0)
        if qty <= 0 or price <= 0:
            raise ValueError("成交数量和价格必须大于 0")
        with self.lock:
            p = self.get(code)
            if side == "buy":
                cost = price * qty + fee
                if self.mode == "paper" and cost > float(self.s.get("cash") or 0) + 1e-6:
                    raise ValueError(f"模拟账户可用资金不足：需要 {cost:.2f}，可用 {float(self.s.get('cash') or 0):.2f}（先改可用资金）")
                self._add_lot(code, name or (p or {}).get("name") or code, price, qty, day, ["手动成交"], cost, "confirmed")
                self.get(code).setdefault("origin", "manual")
                self.adjust_cash(-cost)
                out = {"cash_delta": round2(-cost), "pnl": None}
            else:
                if not p or int(p["quantity"]) < qty:
                    raise ValueError(f"{code} 持仓不足 {qty} 股，不能记卖出成交（先在持仓里补上这只股票）")
                cost_part = p["cost"] * qty / p["quantity"]
                proceeds = price * qty - fee
                pnl = round2(proceeds - cost_part)
                self._remove_qty(code, qty, day, cost_part, any_lot=True)
                self.s["closed"].append({"symbol": code, "name": p["name"], "quantity": qty, "sell_price": price, "fee": fee,
                                         "cost": round2(cost_part), "pnl": pnl, "date": day, "at": ts(), "reason": note,
                                         "buy_date": p["first_buy_date"]})
                self.adjust_cash(proceeds)
                out = {"cash_delta": round2(proceeds), "pnl": pnl}
                name = name or p["name"]
            self.save()
            return {"side": side, "symbol": code, "name": name or (self.get(code) or {}).get("name") or code,
                    "quantity": qty, "price": price, "fee": round2(fee), **out}

    def adopt_broker(self, snap: dict, day: str, trustworthy: bool, skip: set | None = None) -> list[dict]:
        """自动获取持仓：本地持仓照搬券商快照（数量、成本、可卖、市价）。有在途委托/未确认的代码不碰；
        券商没有、本地已确认的持仓只在持仓表可信时删除。返回变化列表。"""
        if self.mode == "paper":
            return []
        changes = []
        with self.lock:
            busy = self.busy_codes() | set(skip or ())     # skip：对账判定券商持仓表还是成交前的旧数
            seen = set()
            for b in snap.get("positions") or []:
                code = b["symbol"]
                seen.add(code)
                if code in busy or b.get("code_verified") is False or int(b.get("qty") or 0) <= 0:
                    continue
                qty, avail = int(b["qty"]), max(0, min(int(b.get("available") or 0), int(b["qty"])))
                cost = float(b.get("cost") or 0) or float(b.get("price") or 0)
                if cost <= 0:
                    continue
                p = self.get(code)
                same = p and p["quantity"] == qty and self.sellable(code, day) == avail and abs(p["avg_cost"] - cost) < 1e-4
                if not p:
                    p = self.s["positions"][code] = {"symbol": code, "name": b.get("name") or code, "first_buy_date": day,
                                                     "sources": ["券商持仓"], "hold": None, "orders": []}
                    changes.append({"symbol": code, "kind": "added", "message": f"{code} {b.get('name', '')} 新增 {qty} 股（券商持仓）"})
                elif not same:
                    changes.append({"symbol": code, "kind": "updated",
                                    "message": f"{code} 按券商更新：{p['quantity']}→{qty} 股，可卖 {avail}，成本 {cost:g}"})
                p.update(name=b.get("name") or p.get("name") or code, quantity=qty, avg_cost=round(cost, 4), cost=round2(qty * cost),
                         lots=self._lots_for(qty, avail, cost, day), state="confirmed", state_note="自动获取（券商持仓）",
                         state_at=ts(), origin="broker",
                         broker={"qty": qty, "available": avail, "cost": b.get("cost"), "price": b.get("price"), "at": ts()})
                if b.get("price"):
                    p["last_price"], p["last_mark_at"] = float(b["price"]), ts()
            if trustworthy:
                for code in [c for c in list(self.positions()) if c not in seen and c not in busy]:
                    changes.append({"symbol": code, "kind": "removed", "message": f"{code} 券商已无持仓，本地删除"})
                    del self.s["positions"][code]
            if changes:
                self.save()
        return changes

    # ---------- 实盘：在途委托（等待对账） ----------
    def pending(self) -> dict:
        return self.s.setdefault("pending", {})

    def add_pending(self, rec: dict) -> None:
        with self.lock:
            self.pending()[rec["order_id"]] = rec
            self.save()

    def update_pending(self, order_id: str, **kw) -> dict | None:
        with self.lock:
            rec = self.pending().get(order_id)
            if rec is not None:
                rec.update(kw)
                self.save()
            return rec

    def close_pending(self, order_id: str, outcome: str, note: str = "") -> None:
        """在途委托已有结论：移到 pending_done（保留最近 200 条，网页可查）。"""
        with self.lock:
            rec = self.pending().pop(order_id, None)
            if rec is None:
                return
            rec.update(outcome=outcome, outcome_note=note, closed_at=ts())
            done = self.s.setdefault("pending_done", [])
            done.append(rec)
            del done[:-200]
            if rec.get("entrust_no"):
                claimed = self.s.setdefault("claimed_entrust", [])
                if rec["entrust_no"] not in claimed:
                    claimed.append(rec["entrust_no"])
                    del claimed[:-500]
            self.save()

    def claimed_entrust(self) -> set[str]:
        s = set(self.s.get("claimed_entrust") or [])
        s |= {r["entrust_no"] for r in self.pending().values() if r.get("entrust_no")}
        return s

    def set_state(self, code: str, state: str, note: str) -> None:
        with self.lock:
            p = self.get(code)
            if p:
                p.update(state=state, state_note=note, state_at=ts())
                self.save()

    def fill_buy(self, code: str, order_id: str, order_qty: int, filled: int, avg_price: float | None, day: str,
                 final: bool, note: str) -> None:
        """实盘买单有了成交结论：把下单时按委托数量记下的批次改成实际成交数量/均价。
        final=True 表示委托已终结（全成/部成部撤/已撤/废单），否则只更新备注。"""
        with self.lock:
            p = self.get(code)
            if not p:
                return
            if not final:
                p["state_note"] = note
                p["state_at"] = ts()
                self.save()
                return
            lots = p["lots"]
            idx = next((i for i in range(len(lots) - 1, -1, -1) if lots[i].get("order_id") == order_id), None)
            if idx is None:   # 旧版本记账没有 order_id：取当天、数量一致的最后一笔
                idx = next((i for i in range(len(lots) - 1, -1, -1) if lots[i]["date"] == day and lots[i]["qty"] == order_qty), None)
            if idx is not None:
                old = lots[idx]
                old_cost = old["qty"] * old["price"]
                if filled <= 0:
                    lots.pop(idx)
                    new_cost = 0.0
                else:
                    px = float(avg_price) if avg_price else old["price"]
                    lots[idx] = {**old, "qty": int(filled), "price": px}
                    new_cost = filled * px
                p["quantity"] = int(sum(l["qty"] for l in lots))
                p["cost"] = round2(p["cost"] - old_cost + new_cost)
            if p["quantity"] <= 0:
                del self.s["positions"][code]
            else:
                p["avg_cost"] = round(p["cost"] / p["quantity"], 4)
                p.update(state="confirmed", state_note=note, state_at=ts())
            self.save()

    def apply_broker(self, code: str, b: dict, adopt_cost: bool) -> None:
        """把券商持仓的数量/可卖/成本/市价记到本地（对账一致时）。"""
        with self.lock:
            p = self.get(code)
            if not p:
                return
            p["broker"] = {"qty": b["qty"], "available": b["available"], "cost": b["cost"], "price": b["price"], "at": ts()}
            if b.get("price"):
                p["last_price"] = float(b["price"])
                p["last_mark_at"] = ts()
            if adopt_cost and b.get("cost") and p["quantity"]:
                ratio = float(b["cost"]) / p["avg_cost"] if p["avg_cost"] else 1.0
                p["avg_cost"] = round(float(b["cost"]), 4)
                p["cost"] = round2(p["avg_cost"] * p["quantity"])
                p["lots"] = [{**l, "price": round(l["price"] * ratio, 4)} for l in p["lots"]]
            self.save()

    def _add_lot(self, code, name, price, qty, day, sources, cost, state, order_id=None):
        ps = self.s["positions"]
        p = ps.get(code)
        if not p:
            p = ps[code] = {"symbol": code, "name": name, "lots": [], "quantity": 0, "cost": 0.0, "avg_cost": 0.0,
                            "first_buy_date": day, "sources": list(sources or []), "hold": None, "state": state,
                            "last_price": price, "last_mark_at": ts(), "orders": [], "sell_strategy_id": ""}
        lot = {"date": day, "qty": int(qty), "price": price}
        if order_id and self.mode != "paper":
            lot["order_id"] = order_id
        p["lots"].append(lot)
        p["quantity"] += int(qty)
        p["cost"] = round2(p["cost"] + cost)
        p["avg_cost"] = round(p["cost"] / p["quantity"], 4)
        if state != "confirmed":
            p["state"] = state
        if order_id:
            p.setdefault("orders", []).append(order_id)
        for s in sources or []:
            if s not in p["sources"]:
                p["sources"].append(s)

    def _remove_qty(self, code, qty, day, cost_part, any_lot=False):
        p = self.s["positions"][code]
        if any_lot:
            p["lots"] = consume_lots(p["lots"], qty, "9999-12-31")
        else:
            p["lots"] = consume_lots(p["lots"], qty, day)
        p["quantity"] -= qty
        p["cost"] = round2(p["cost"] - cost_part)
        if p["quantity"] <= 0:
            del self.s["positions"][code]
        else:
            p["avg_cost"] = round(p["cost"] / p["quantity"], 4)

    # ---------- 人工保留 ----------
    def set_hold(self, code: str, hold: bool, note: str = "", mode: str | None = None) -> dict:
        with self.lock:
            p = self.get(code)
            if not p:
                raise KeyError(code)
            p["hold"] = {"set_at": ts(), "note": note or "人工保留",
                         "mode": mode or "until_next_check"} if hold else None
            self.save()
            return p

    def set_sell_strategy(self, code: str, sid: str, by: str = "网页") -> dict:
        """给一只持仓单独指定卖出策略（个股级，优先级高于股池绑定）。sid 为空 = 恢复跟随股池/生效策略。"""
        with self.lock:
            p = self.get(code)
            if not p:
                raise KeyError(code)
            sid = str(sid or "").strip()
            old = p.get("sell_strategy_id") or ""
            p["sell_strategy_id"] = sid
            p["sell_strategy_set"] = {"at": ts(), "by": by, "from": old, "to": sid} if sid else None
            self.save()
            return p

    def consume_hold(self, code: str) -> dict | None:
        with self.lock:
            p = self.get(code)
            if not p or not p.get("hold"):
                return None
            h = p["hold"]
            p["hold"] = None
            p.setdefault("hold_history", []).append({**h, "used_at": ts()})
            self.save()
            return h

    # ---------- 估值 ----------
    def mark(self, prices: dict[str, float], day: str) -> None:
        with self.lock:
            for code, px in prices.items():
                p = self.get(code)
                if p and px:
                    p["last_price"] = float(px)
                    p["last_mark_at"] = ts()
            if self.mode == "paper":
                eq = self.equity()
                nav = self.s.setdefault("nav", [])
                pt = {"t": ts(), "date": day, "equity": eq["equity"], "cash": eq["cash"], "market_value": eq["market_value"]}
                if nav and nav[-1]["date"] == day:
                    nav[-1] = pt     # 每个交易日保留最后一个点
                else:
                    nav.append(pt)
            self.save()

    def equity(self) -> dict:
        mv = sum(p["quantity"] * (p.get("last_price") or p["avg_cost"]) for p in self.positions().values())
        cash = self.s.get("cash") or 0.0
        return {"cash": round2(cash), "market_value": round2(mv), "equity": round2(cash + mv)}

    def summary(self) -> dict:
        eq = self.equity()
        stats = closed_trade_stats([c["pnl"] for c in self.s["closed"] if c.get("pnl") is not None])
        unreal = sum(p["quantity"] * (p.get("last_price") or p["avg_cost"]) - p["cost"] for p in self.positions().values())
        out = {**eq, **stats, "unrealized_pnl": round2(unreal), "position_count": len(self.positions())}
        f = self.s.get("funds") or {}
        out["funds_set"] = self.s.get("cash") is not None
        out["balance"] = f.get("balance") if f.get("balance") is not None else (eq["cash"] if out["funds_set"] else None)
        out["frozen"] = round2(out["balance"] - eq["cash"]) if out["funds_set"] else None
        out["funds_updated_at"] = f.get("updated_at")
        if not out["funds_set"]:
            out["cash"] = None
            out["equity"] = None       # 没填可用资金时总资产算不出来
        if self.mode == "paper":
            init = self.s["initial_cash"]
            out["initial_cash"] = init
            out["total_return_pct"] = round((eq["equity"] / init - 1) * 100, 2) if init else 0
            peak, mdd = 0.0, 0.0
            for pt in self.s.get("nav", []):
                peak = max(peak, pt["equity"])
                if peak:
                    mdd = max(mdd, (peak - pt["equity"]) / peak * 100)
            out["max_drawdown_pct"] = round(mdd, 2)
        return out
