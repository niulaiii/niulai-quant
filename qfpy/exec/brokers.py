"""三种券商适配：模拟盘 paper / 演练 dry_run / 实盘 live（同花顺下单程序，接口见 /home/sprite/xiadan/接口文档.md）。

实盘安全设计（沿用 Rust 版 src/exec/ths.rs）：
- 是否实盘只看 [execution].mode = "live"（设置页直接切换，不再有确认文字/三重开关门槛；
  旧配置里的 [live].enabled、QF_LIVE_ARMED 不再起作用）。风控（单笔买入上限、每日买入金额等）照常生效。
- 只发数字限价（两位小数字符串）、整数股数、batch_count=1；不发价格关键字、all、money:、position:；
  永远不调 /api/command、/api/client/restart、/api/connect（xiadan.py 里直接拦截）。
- 撤单只用 kind=order + 合同号。
- 超时、断线、没拿到确认 → “结果未知”，绝不自动重试；照样计入当天额度。之后由自动对账在券商委托里找这笔单，
  找不到/对不上再推送请人工核对。
"""
from __future__ import annotations

import os
import re

from ..util import log, ts
from .xiadan import FORBIDDEN_PATHS, XiadanClient, XiadanError, auth_password  # noqa: F401

CONFIRM_PHRASE = "我确认这是股票交易委托"


class LiveNotArmed(RuntimeError):
    pass


def live_gate(cfg: dict) -> tuple[bool, list[str]]:
    """是否处在实盘模式。返回 (是否实盘, 缺少什么)。只看 [execution].mode。"""
    if cfg["execution"].get("mode") != "live":
        return False, ["[execution].mode 不是 live"]
    return True, []


def build_order_body(it: dict) -> dict:
    """构造发给下单程序的请求体。任何不合规的参数直接抛错。"""
    side = it["side"]
    if side not in ("buy", "sell"):
        raise ValueError(f"非法方向 {side}")
    code = str(it["symbol"])
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError(f"非法代码 {code}")
    price, qty = it["price"], it["quantity"]
    if not isinstance(price, (int, float)) or price <= 0:
        raise ValueError("只允许数字限价")
    if not isinstance(qty, int) or qty <= 0:
        raise ValueError("只允许正整数股数")
    price_s, qty_s = f"{price:.2f}", str(int(qty))
    assert re.fullmatch(r"\d+\.\d{2}", price_s) and qty_s.isdigit()
    return {"side": side, "symbol": code, "price": price_s, "quantity": qty_s, "batch_count": 1, "confirm": CONFIRM_PHRASE}


def interpret_order_response(http_status: int, v: dict) -> tuple[str, str, str | None]:
    """解读 POST /api/order 的响应。返回 (status, message, entrust_no)。

    新版下单程序的成功响应是 {"ok": true, "confirmed_count": 1, "status": "confirmed", "message": "已确认 1/1 笔委托"}，
    不一定带合同号（合同号由对账时从券商委托里找）。旧版带 entrust_nos。
    - 400 参数错误 / 403 口令错误 / 503 没连上客户端：委托没有发出去 → rejected。
    - ok=true 且 confirmed_count≥1（或带合同号）→ submitted。
    - status=failed 且没有确认 → rejected。其余一律 unknown（需对账/人工核对），绝不重试。"""
    v = v if isinstance(v, dict) else {}
    ok = v.get("ok") is True
    status = str(v.get("status") or "")
    msg = str(v.get("message") or "")
    nos = v.get("entrust_nos") or []
    no = str(nos[0]) if nos and isinstance(nos, list) and nos[0] else None
    if not no:
        m = re.search(r"合同编号[：:]\s*(\d+)", msg)
        no = m.group(1) if m else None
    try:
        confirmed = int(v.get("confirmed_count") or 0)
    except (TypeError, ValueError):
        confirmed = 0
    if http_status == 400:
        return "rejected", f"下单程序拒绝参数：{msg}", None
    if http_status == 403:
        return "rejected", f"口令错误，委托未发出：{msg}", None
    if http_status == 503:
        return "rejected", f"下单程序没连上同花顺客户端，委托未发出：{msg}", None
    if http_status == 200 and ok and (confirmed >= 1 or no):
        return "submitted", msg, no
    if status == "failed" and confirmed == 0 and not no:
        return "rejected", f"委托失败：{msg}", None
    return "unknown", f"未确认（HTTP {http_status} {status}）：{msg}。系统不会自动重试，会用券商委托自动对账", no


class PaperBroker:
    mode = "paper"
    label = "模拟盘"

    def __init__(self, cfg, book):
        self.cfg, self.book = cfg, book

    def place(self, it: dict) -> dict:
        """限价单：买入限价 ≥ 参考价 / 卖出限价 ≤ 参考价才成交，按参考价 + 滑点成交（Alpha 费用算法）。"""
        ref = it.get("ref_price") or 0
        side, price, qty = it["side"], it["price"], it["quantity"]
        if side == "buy":
            if price + 1e-9 < ref:
                return {"status": "unfilled", "message": f"限价 {price:.2f} 低于参考价 {ref:.2f}，模拟未成交"}
            try:
                fill, fee = self.book.paper_fill_buy(it["symbol"], it["name"], ref, qty, it["trading_date"], it.get("sources"))
            except ValueError as e:
                return {"status": "rejected", "message": str(e)}
            return {"status": "filled", "fill_price": fill, "fee": fee, "message": f"模拟成交 {qty} 股 @ {fill:.3f}，费用 {fee:.2f}"}
        if price - 1e-9 > ref:
            return {"status": "unfilled", "message": f"限价 {price:.2f} 高于参考价 {ref:.2f}，模拟未成交"}
        fill, fee, pnl = self.book.paper_fill_sell(it["symbol"], ref, qty, it["trading_date"], it.get("reason", ""))
        return {"status": "filled", "fill_price": fill, "fee": fee, "pnl": pnl,
                "message": f"模拟卖出 {qty} 股 @ {fill:.3f}，费用 {fee:.2f}，盈亏 {pnl:+.2f}"}


class DryRunBroker:
    mode = "dry_run"
    label = "演练"

    def __init__(self, cfg, book):
        self.cfg, self.book = cfg, book

    def place(self, it: dict) -> dict:
        body = build_order_body(it)
        url = self.cfg["live"]["base_url"].rstrip("/") + "/api/order"
        # 演练：只生成请求，不发送。本地记账按“虚拟成交”处理，用来验证卖出/保留流程。
        if it["side"] == "buy":
            self.book.ledger_buy(it["symbol"], it["name"], it["price"], it["quantity"], it["trading_date"], it.get("sources"),
                                 "confirmed", it["id"])
            with self.book.lock:     # 填过可用资金时，虚拟成交同样扣/加资金（不计费用）
                self.book.adjust_cash(-it["price"] * it["quantity"])
                self.book.save()
        else:
            p = self.book.get(it["symbol"])
            q = min(int(it["quantity"]), int(p["quantity"])) if p else 0
            self.book.ledger_sell_done(it["symbol"], it["quantity"], it["price"], it["trading_date"], it.get("reason", ""))
            with self.book.lock:
                self.book.adjust_cash(it["price"] * q)
                self.book.save()
        headers = {"Content-Type": "application/json"}
        if auth_password(self.cfg["live"]):
            headers["X-Auth"] = "***（base64 口令，已隐藏）"
        return {"status": "dry_run", "message": "演练：请求已生成，未发送", "request": body, "would_post": url, "headers": headers}


class ThsLiveBroker:
    """实盘：经 XiadanClient 调下单程序。下单/撤单后按文档推荐调 /api/snapshot?refresh=1&no_render=1 同步并对账。"""
    mode = "live"
    label = "实盘"

    def __init__(self, cfg, book, link=None):
        ok, missing = live_gate(cfg)
        if not ok:
            raise LiveNotArmed("不是实盘模式：" + "；".join(missing))
        self.cfg, self.book = cfg, book
        if link is None:
            raise ValueError("实盘需要 BrokerLink（由 Engine 创建并传入）")
        self.link = link
        self.client = link.client

    def place(self, it: dict) -> dict:
        ok, missing = live_gate(self.cfg)     # 发送前再检查一遍
        if not ok:
            return {"status": "rejected", "message": "不是实盘模式：" + "；".join(missing)}
        body = build_order_body(it)
        p0 = self.book.get(it["symbol"])
        baseline = int(p0["quantity"]) if p0 else 0     # 下单前本地持仓数量，对账时用来判断券商持仓有没有变化
        submitted_at = ts()
        try:
            code, v = self.client.post_order(body)
            status, msg, no = interpret_order_response(code, v)
        except Exception as e:  # noqa: BLE001  超时/断线：可能已报单，绝不重试
            status, msg, no, v = "unknown", f"请求异常：{e}。可能已报单，系统不会自动重试，会用券商委托自动对账", None, None
        log.warning("实盘委托 %s %s %s@%s → %s %s", it["side"], it["symbol"], it["quantity"], it["price"], status, msg)
        if status in ("submitted", "unknown") and not self.link.auto_positions_fn():
            # 自动获取持仓关闭（手动账本）：不读券商、不自动对账；委托记在委托记录里，成交由你补记后再入账
            msg += "。自动获取持仓已关闭：成交后请在「账户 → 委托记录」里补记成交，持仓和可用资金会自动更新"
        elif status in ("submitted", "unknown"):
            st = {"buy": "unconfirmed_buy", "sell": "selling"}[it["side"]] if status == "submitted" else "uncertain"
            if it["side"] == "buy":
                self.book.ledger_buy(it["symbol"], it["name"], it["price"], it["quantity"], it["trading_date"], it.get("sources"), st, it["id"])
            else:
                self.book.ledger_mark(it["symbol"], st, msg, it["id"])
            self.book.add_pending({"order_id": it["id"], "symbol": it["symbol"], "name": it.get("name", ""), "side": it["side"],
                                   "quantity": int(it["quantity"]), "price": float(it["price"]), "trading_date": it["trading_date"],
                                   "submitted_at": submitted_at, "status": status, "entrust_no": no, "baseline_qty": baseline,
                                   "reason": it.get("reason", ""), "message": msg})
            self.link.after_trade()
        return {"status": status, "message": msg, "entrust_no": no, "request": body, "response": v}

    def cancel(self, order_id: str, entrust_no: str | None = None) -> dict:
        """撤掉一笔本系统发出的在途委托（只用 kind=order + 合同号）。超时同样不重试，等对账确认。
        entrust_no：手动账本模式下没有对账记录时，用委托记录里的合同号。"""
        ok, missing = live_gate(self.cfg)
        if not ok:
            return {"status": "rejected", "message": "不是实盘模式：" + "；".join(missing)}
        rec = self.book.pending().get(order_id) or (entrust_no and {"entrust_no": entrust_no})
        if not rec:
            return {"status": "rejected", "message": f"没有在途委托 {order_id}"}
        no = str(rec.get("entrust_no") or "")
        if not re.fullmatch(r"\d{1,20}", no):
            return {"status": "rejected", "message": "这笔委托还没对上合同号，不能按合同号撤单；请在同花顺里撤"}
        body = build_cancel_body(no)
        try:
            code, v = self.client.post_cancel(body)
            okk = code == 200 and v.get("ok") is True
            status = "cancel_submitted" if okk else ("rejected" if code in (400, 403, 503) else "unknown")
            msg = str(v.get("message") or "")
        except Exception as e:  # noqa: BLE001
            status, msg, v = "unknown", f"撤单请求异常：{e}。系统不会重试，会用券商委托自动对账", None
        if order_id in self.book.pending():
            self.book.update_pending(order_id, cancel_status=status, cancel_at=ts(), cancel_message=msg)
        log.warning("实盘撤单 %s 合同 %s → %s %s", order_id, no, status, msg)
        self.link.after_trade()
        return {"status": status, "message": msg, "request": body, "response": v}

    def status(self) -> dict:
        return self.link.check_status(force=True)


def build_cancel_body(entrust_no: str) -> dict:
    """撤单请求体：只允许 kind=order + 纯数字合同号；永远不发 all/last/buy/sell/stock。"""
    no = str(entrust_no)
    if not re.fullmatch(r"\d{1,20}", no):
        raise ValueError("合同号必须是数字")
    return {"kind": "order", "entrust_no": no, "confirm": CONFIRM_PHRASE}


def broker_status(cfg: dict) -> dict:
    """只读：调用下单程序的 /api/status（带 X-Auth）。"""
    c = XiadanClient(cfg["live"])
    try:
        return {"ok": True, "url": c.base + "/api/status", "body": c.status(force=True)}
    except XiadanError as e:
        return {"ok": False, "url": c.base + "/api/status", "error": str(e), "kind": e.kind}


def make_broker(cfg: dict, book, link=None):
    mode = cfg["execution"]["mode"]
    if mode == "paper":
        return PaperBroker(cfg, book)
    if mode == "dry_run":
        return DryRunBroker(cfg, book)
    return ThsLiveBroker(cfg, book, link)
