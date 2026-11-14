"""实盘自动对账：本地持仓（四种状态）+ 在途委托 ↔ 券商持仓、委托、成交（下单程序 /api/snapshot）。

两步：
1. 委托级：每笔在途委托（已提交 / 结果未知）先按合同号、再按「代码 + 方向 + 数量 + 限价 + 时间」在券商委托里找。
   找到就按委托状态（已成 / 部成 / 已撤 / 部成部撤 / 废单）更新本地持仓。
   找不到且超过宽限时间，在有新鲜数据时用「券商持仓数量的变化」推断：没变 → 没报上/已失效；
   刚好变了委托数量 → 已成交；其余情况请人工处理。
2. 持仓级：没有在途委托的本地持仓，和券商持仓比数量。一致 → 确认（并同步成本/可卖）；
   不一致 → 连续 mismatch_confirmations 次（默认 2 次、间隔至少 60 秒）都不一致才标记“结果未知”并推送，
   避免下单程序持仓缓存（最长 120 秒）造成误报。本地有、券商没有而当天成交里有等量卖出 → 认为你在
   同花顺里手动卖了，自动平仓。

所有判断只改本地账本，不发任何委托。返回的 events 里 notify=True 的要推送（铉钉等）。
"""
from __future__ import annotations

from datetime import datetime

from .book import STATE_LABELS, Book

DEFAULTS = {"not_found_grace_secs": 180, "mismatch_confirmations": 2, "mismatch_min_gap_secs": 60,
            "price_tolerance": 0.006, "adopt_broker_cost": True, "order_time_slack_secs": 120}


def _secs(hms: str) -> int | None:
    try:
        parts = [int(x) for x in str(hms).strip()[-8:].split(":")]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
    except ValueError:
        return None
    return None


def _parse_ts(s: str) -> datetime | None:
    try:
        return datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def positions_trustworthy(snap: dict) -> bool:
    """券商持仓列表能不能拿来判断“没有这只票”。下单程序读不到持仓表时会返回空列表，
    所以空列表只在资金里的总市值为 0 时才算数；有代码没核实（code_verified=false）时也不算。"""
    if not snap.get("ok", True):
        return False
    if snap.get("unverified"):
        return False
    if snap.get("positions"):
        return True
    mv = (snap.get("money") or {}).get("market_value")
    try:
        return mv is not None and float(str(mv).replace(",", "")) == 0.0
    except ValueError:
        return False


def match_order(rec: dict, orders: list[dict], claimed: set[str], tol: float, slack: int) -> dict | None:
    """在券商委托里找本地这笔委托。先按合同号；没有合同号时按代码/方向/数量/限价/时间，且不能是别的本地委托已认领的合同号。"""
    no = rec.get("entrust_no")
    if no:
        for o in orders:
            if o["entrust_no"] == no:
                return o
        return None
    sub = _secs(str(rec.get("submitted_at", ""))[11:19])
    cands = []
    for o in orders:
        if o["symbol"] != rec["symbol"] or o["side"] != rec["side"] or o["quantity"] != rec["quantity"]:
            continue
        if abs(o["price"] - float(rec["price"])) > tol:
            continue
        if o["entrust_no"] and o["entrust_no"] in claimed:
            continue
        ot = _secs(o["time"])
        if sub is not None and ot is not None and ot < sub - slack:
            continue
        cands.append((ot if ot is not None else 10 ** 6, o))
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])
    return cands[0][1]


def reconcile(book: Book, snap: dict, now: datetime, day: str, fresh: bool, opts: dict | None = None,
              mismatch_memory: dict | None = None) -> dict:
    """snap 是 normalize_snapshot() 的结果。fresh=True 表示数据是强制刷新拿到的（refresh=1 或 /api/refresh），
    只有 fresh 时才会做“找不到委托”和“数量不一致”这类需要否定证据的判断。
    mismatch_memory：跨次调用保存的不一致计数（由 BrokerLink 持久化）。"""
    o = {**DEFAULTS, **(opts or {})}
    mem = mismatch_memory if mismatch_memory is not None else {}
    events: list[dict] = []
    ev = lambda kind, sym, msg, notify=False, **kw: events.append(  # noqa: E731
        {"kind": kind, "symbol": sym, "message": msg, "notify": notify, **kw})
    orders = snap.get("orders") or []
    deals = snap.get("deals") or []
    bpos = {p["symbol"]: p for p in snap.get("positions") or []}
    trust_pos = positions_trustworthy(snap)
    claimed = book.claimed_entrust()
    busy: set[str] = set()   # 仍有在途委托的代码：本轮不做持仓级比对

    for oid, rec in sorted(book.pending().items(), key=lambda kv: kv[1].get("submitted_at", "")):
        sym, side, qty = rec["symbol"], rec["side"], int(rec["quantity"])
        name = rec.get("name") or sym
        m = match_order(rec, orders, claimed - ({rec.get("entrust_no")} if rec.get("entrust_no") else set()),
                        o["price_tolerance"], o["order_time_slack_secs"])
        if m is not None:
            if not rec.get("entrust_no") and m["entrust_no"]:
                book.update_pending(oid, entrust_no=m["entrust_no"])
                claimed.add(m["entrust_no"])
            if rec.get("status") == "unknown" and not rec.get("found_at"):
                book.update_pending(oid, found_at=now.strftime("%Y-%m-%d %H:%M:%S"))
                ev("unknown_found", sym, f"{name} 结果未知的{_side(side)}单在券商委托里找到了（合同 {m['entrust_no'] or '-'}），已按委托状态跟踪")
            _apply_order_state(book, rec, m, day, ev, busy, deals)
            continue
        # 没找到
        busy.add(sym)
        sub = _parse_ts(rec.get("submitted_at", ""))
        age = (now - sub).total_seconds() if sub else 10 ** 9
        crossed_day = rec.get("trading_date", day) < day
        if not fresh or (age < o["not_found_grace_secs"] and not crossed_day):
            continue
        if not trust_pos:
            if not rec.get("notfound_notified"):
                book.update_pending(oid, notfound_notified=True)
                ev("pending_not_found", sym, f"{name} {_side(side)} {qty} 股的委托在券商委托里找不到，持仓表也没读到，请到同花顺核对",
                   True, order_id=oid)
            continue
        base = int(rec.get("baseline_qty") or 0)
        bq = int(bpos.get(sym, {}).get("qty", 0))
        if side == "buy":
            if bq == base:
                book.fill_buy(sym, oid, qty, 0, None, day, True, "券商里没有这笔委托、持仓也没变，判定未成交")
                book.close_pending(oid, "not_placed", "券商无此委托，持仓未变")
                ev("buy_not_placed", sym, f"{name} 买入 {qty} 股：券商里没有这笔委托、持仓也没变，判定没有成交，已从本地持仓移除",
                   rec.get("status") == "unknown")
                busy.discard(sym)
            elif bq - base == qty:
                px = _deal_avg(deals, sym, "buy") or float(rec["price"])
                book.fill_buy(sym, oid, qty, qty, px, day, True, "委托表里没找到，但券商持仓刚好多了委托数量，判定已成交")
                book.close_pending(oid, "filled_inferred", "按持仓变化推断已成交")
                ev("buy_filled_inferred", sym, f"{name} 买入 {qty} 股：委托表里没找到，但券商持仓刚好多了 {qty} 股，判定已成交（价 {px:.3f}）")
                busy.discard(sym)
            elif not rec.get("notfound_notified"):
                book.update_pending(oid, notfound_notified=True)
                book.set_state(sym, "uncertain", f"券商委托里找不到，券商持仓 {bq} 股、下单前 {base} 股，对不上")
                ev("pending_not_found", sym, f"{name} 买入 {qty} 股：券商委托里找不到，券商持仓 {bq} 股（下单前 {base} 股），对不上，请人工核对",
                   True, order_id=oid)
        else:
            if bq >= base:
                book.set_state(sym, "confirmed", "券商里没有这笔卖单、持仓未减少，判定未卖出")
                book.close_pending(oid, "not_placed", "券商无此委托，持仓未变")
                ev("sell_not_placed", sym, f"{name} 卖出 {qty} 股：券商里没有这笔委托、持仓没减少，判定没有卖出，恢复为已确认",
                   True)
                busy.discard(sym)
            elif base - bq == qty:
                px = _deal_avg(deals, sym, "sell") or float(rec["price"])
                book.ledger_sell_done(sym, qty, px, day, rec.get("reason") or "实盘卖出")
                book.set_state(sym, "confirmed", "卖出已按持仓变化确认")
                book.close_pending(oid, "filled_inferred", "按持仓变化推断已卖出")
                ev("sell_filled_inferred", sym, f"{name} 卖出 {qty} 股：委托表里没找到，但券商持仓刚好少了 {qty} 股，判定已卖出（价 {px:.3f}）", True)
                busy.discard(sym)
            elif not rec.get("notfound_notified"):
                book.update_pending(oid, notfound_notified=True)
                book.set_state(sym, "uncertain", f"卖单在券商委托里找不到，券商持仓 {bq} 股、下单前 {base} 股")
                ev("pending_not_found", sym, f"{name} 卖出 {qty} 股：券商委托里找不到，券商持仓 {bq} 股（下单前 {base} 股），对不上，请人工核对",
                   True, order_id=oid)

    # ---------- 持仓级 ----------
    nowts = now.timestamp()
    stale: list[str] = []
    for sym, p in list(book.positions().items()):
        if sym in busy:
            continue
        name = p.get("name") or sym
        b = bpos.get(sym)
        lq = int(p["quantity"])
        if b is not None and b["qty"] == lq:
            mem.pop(sym, None)
            if p.get("state") != "confirmed":
                old = STATE_LABELS.get(p.get("state"), p.get("state"))
                book.set_state(sym, "confirmed", f"与券商持仓一致（{lq} 股），自动确认")
                ev("auto_confirmed", sym, f"{name}：本地「{old}」{lq} 股与券商持仓一致，已自动确认")
            book.apply_broker(sym, b, bool(o["adopt_broker_cost"]))
            continue
        if b is None and not trust_pos:
            continue
        bq = int(b["qty"]) if b else 0
        if b is None:
            sold = sum(d["quantity"] for d in deals if d["symbol"] == sym and d["side"] == "sell")
            if sold >= lq > 0:
                px = _deal_avg(deals, sym, "sell")
                book.ledger_sell_done(sym, lq, px, day, "同花顺里手动卖出（对账发现）")
                mem.pop(sym, None)
                ev("manual_sell_detected", sym, f"{name}：券商已无持仓，当天成交里有卖出 {sold} 股，判定你在同花顺里手动卖了，已自动平仓"
                   + (f"（均价 {px:.3f}）" if px else ""), True)
                continue
        if not fresh:
            continue
        net = sum(d["quantity"] if d["side"] == "buy" else -d["quantity"] for d in deals if d["symbol"] == sym)
        if net and bq == lq - net:
            stale.append(sym)
            ev("positions_stale", sym, f"{name}：券商持仓 {bq} 股还是今天成交前的数（本地 {lq} 股、今天净成交 {net:+d} 股），"
               "多半是客户端持仓表没重绘，暂不判为不一致；可在网页点「重查持仓」")
            continue
        m = mem.get(sym)
        if m and m.get("local") == lq and m.get("broker") == bq:
            if nowts - m.get("last", 0) >= o["mismatch_min_gap_secs"]:
                m["count"] += 1
                m["last"] = nowts
        else:
            m = mem[sym] = {"local": lq, "broker": bq, "count": 1, "last": nowts, "notified": False}
        if m["count"] >= o["mismatch_confirmations"] and not m["notified"]:
            m["notified"] = True
            book.set_state(sym, "uncertain", f"与券商持仓不一致：本地 {lq} 股，券商 {bq} 股")
            ev("qty_mismatch", sym, f"{name}：本地记录 {lq} 股，券商持仓 {bq} 股，连续 {m['count']} 次对不上，已标为结果未知、暂停自动卖出，请人工处理",
               True, local=lq, broker=bq)

    external = [b for s, b in bpos.items() if s not in book.positions()]
    for k in [k for k in mem if k not in book.positions()]:
        mem.pop(k, None)
    return {"at": now.strftime("%Y-%m-%d %H:%M:%S"), "fresh": fresh, "positions_trusted": trust_pos,
            "events": events, "external": external, "stale_positions": stale, "pending": list(book.pending().values()),
            "mismatch": {k: v for k, v in mem.items()}}


def _side(side: str) -> str:
    return "买" if side == "buy" else "卖"


def _deal_avg(deals: list[dict], sym: str, side: str, entrust_no: str | None = None) -> float | None:
    rows = [d for d in deals if d["symbol"] == sym and d["side"] == side and (not entrust_no or d["entrust_no"] == entrust_no)]
    q = sum(d["quantity"] for d in rows)
    if q <= 0:
        return None
    return round(sum(d["quantity"] * d["price"] for d in rows) / q, 4)


def _apply_order_state(book: Book, rec: dict, m: dict, day: str, ev, busy: set, deals: list[dict]) -> None:
    oid, sym, side, qty = rec["order_id"], rec["symbol"], rec["side"], int(rec["quantity"])
    name = rec.get("name") or sym
    st, filled = m["status"], int(m["filled"])
    avg = m["avg_price"] or _deal_avg(deals, sym, side, m["entrust_no"] or None) or None
    tag = f"（合同 {m['entrust_no'] or '-'}）"
    if st in ("pending", "partial"):
        busy.add(sym)
        note = f"券商委托{m['status_label']} {filled}/{qty}{tag}"
        if rec.get("last_seen") != f"{st}:{filled}":
            book.update_pending(oid, last_seen=f"{st}:{filled}", broker_status=m["status_label"], filled=filled)
            if side == "buy":
                book.fill_buy(sym, oid, qty, filled, avg, day, False, note)
            else:
                p = book.get(sym)
                if p:
                    book.set_state(sym, "selling", note)
        return
    # 终结状态
    if side == "buy":
        if filled > 0:
            book.fill_buy(sym, oid, qty, filled, avg, day, True, f"券商委托{m['status_label']}，成交 {filled} 股{tag}")
            kind = "buy_filled" if st == "filled" else "buy_partial_final"
            ev(kind, sym, f"{name} 买入{m['status_label']}：成交 {filled}/{qty} 股" + (f" 均价 {avg:.3f}" if avg else "") + tag,
               kind != "buy_filled")
        else:
            book.fill_buy(sym, oid, qty, 0, None, day, True, "")
            ev("buy_" + st, sym, f"{name} 买入委托{m['status_label']}，未成交，已从本地持仓移除{tag}", True)
    else:
        if filled > 0:
            book.ledger_sell_done(sym, filled, avg or float(rec["price"]), day, rec.get("reason") or "实盘卖出")
            book.set_state(sym, "confirmed", f"卖出 {filled} 股已成交，剩余持仓已确认")
            kind = "sell_filled" if st == "filled" else "sell_partial_final"
            ev(kind, sym, f"{name} 卖出{m['status_label']}：成交 {filled}/{qty} 股" + (f" 均价 {avg:.3f}" if avg else "") + tag,
               kind != "sell_filled")
        else:
            book.set_state(sym, "confirmed", f"卖出委托{m['status_label']}，持仓不变")
            ev("sell_" + st, sym, f"{name} 卖出委托{m['status_label']}，没有卖出，持仓恢复为已确认{tag}", True)
    book.update_pending(oid, filled=filled, fill_price=avg)
    book.close_pending(oid, st, m["status_label"])
