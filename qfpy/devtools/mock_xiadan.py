"""按 /home/sprite/xiadan/接口文档.md 写的下单程序模拟服务（只用于开发/测试，不碰任何真实券商）。

    python -m qfpy.devtools.mock_xiadan --port 18765 --password htfs

实现了：X-Auth 口令（403）、GET /api/status、/api/snapshot（only / refresh / no_render）、/api/refresh、
/api/requery、POST /api/order、/api/cancel（只记录，模拟委托表变化）、503（客户端未连接）、400（参数错误）。
/api/command、/api/client/restart、/api/connect 会返回 200 但记入 forbidden_hits，测试用它断言从没被调用。

控制接口（仅模拟服务有，不需要口令）：
    POST /mock/fill     {"entrust_no": "5301", "qty": 100, "price": 10.0}   让一笔委托成交（可部分）
    POST /mock/set      {"connected": false, "order_mode": "ok|timeout|fail|no_confirm", "positions": [...], ...}
    POST /mock/external {"side": "sell", "code": "600000", "qty": 100, "price": 10.2}  模拟你在同花顺里手动下单并成交
    GET  /mock/state
"""
from __future__ import annotations

import argparse
import base64
import threading
import time
from datetime import datetime, timedelta, timezone

CN = timezone(timedelta(hours=8))   # 下单程序跑在北京时间的 Windows 上


def _now():
    return datetime.now(CN)

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

CONFIRM = "我确认这是股票交易委托"


class MockState:
    def __init__(self, password: str = "htfs"):
        self.password = password
        self.status_needs_auth = True     # False：像真实下单程序那样 /api/status 不验口令、其他接口验（403）
        self.connected = True
        self.order_mode = "ok"
        self.next_no = 5301
        self.balance = 100000.0
        self.positions: dict[str, dict] = {}
        self.orders: list[dict] = []
        self.deals: list[dict] = []
        self.calls: list[dict] = []
        self.forbidden_hits: list[str] = []
        self.trade_calls: list[dict] = []
        self.stale_positions = False      # 模拟“持仓表没重绘”：持仓返回旧副本
        self._stale_copy: list[dict] | None = None
        self.hide_orders = False          # 模拟“委托表被释放”：只留最新一单
        self.empty_positions = False      # 模拟“持仓表读不到”：返回空列表（市值不为 0）
        self.lock = threading.Lock()

    def now(self) -> str:
        return _now().strftime("%H:%M:%S")

    def add_position(self, code, name, qty, cost, price=None, available=None):
        self.positions[code] = {"code": code, "name": name, "qty": int(qty), "available": int(qty if available is None else available),
                                "cost": float(cost), "price": float(price or cost)}

    def pos_rows(self) -> list[dict]:
        out = []
        for p in self.positions.values():
            if p["qty"] <= 0:
                continue
            mv = round(p["qty"] * p["price"], 2)
            pnl = round((p["price"] - p["cost"]) * p["qty"], 2)
            out.append({**p, "market_value": mv, "pnl": pnl,
                        "pnl_pct": round((p["price"] / p["cost"] - 1) * 100, 2) if p["cost"] else 0, "code_verified": True})
        return out

    def snapshot(self, only: str | None = None) -> dict:
        pos = self.pos_rows()
        mv = round(sum(p["market_value"] for p in pos), 2)
        if self.stale_positions and self._stale_copy is not None:
            pos = self._stale_copy
        if self.empty_positions:
            pos = []
        orders = self.orders[-1:] if self.hide_orders else list(self.orders)
        money = {"balance": f"{self.balance:.2f}", "available": f"{self.balance:.2f}", "frozen": "0.00",
                 "market_value": f"{mv:.2f}", "total_asset": f"{self.balance + mv:.2f}",
                 "position_pnl": f"{sum(p['pnl'] for p in self.pos_rows()):.2f}", "today_pnl": "0.00"}
        cancels = [o for o in orders if int(o["撤单数量"]) > 0 or "撤" in o["备注"]]
        snap = {"ok": True, "ts": _now().strftime("%Y-%m-%d %H:%M:%S"), "pid": 4276, "source": "memory+xml",
                "cached": False, "elapsed_ms": 12, "money": money, "positions": pos, "orders": orders,
                "deals": list(self.deals), "cancels": cancels,
                "totals": {"positions": len(pos), "market_value": mv, "orders": len(orders), "deals": len(self.deals),
                           "cancels": len(cancels)}}
        if only:
            return {"ok": True, "ts": snap["ts"], only: snap[only]}
        return snap

    def fill(self, no: str, qty: int, price: float) -> dict:
        o = next(o for o in self.orders if o["合同编号"] == no)
        q0 = int(o["成交数量"])
        qty = min(int(qty), int(o["委托数量"]) - q0 - int(o["撤单数量"]))
        if qty <= 0:
            return o
        avg = ((float(o["成交均价"]) * q0) + price * qty) / (q0 + qty)
        o["成交数量"] = str(q0 + qty)
        o["成交均价"] = f"{avg:.3f}"
        o["备注"] = "全部成交" if q0 + qty >= int(o["委托数量"]) else "部分成交"
        self._apply_deal(o["证券代码"], o["证券名称"], "买入" if o["操作"] == "买入" else "卖出", qty, price, no)
        return o

    def _apply_deal(self, code, name, op, qty, price, no):
        if self.stale_positions and self._stale_copy is None:
            self._stale_copy = self.pos_rows()
        self.deals.append({"成交时间": self.now(), "证券代码": code, "证券名称": name, "操作": op, "成交数量": str(qty),
                           "成交均价": f"{price:.3f}", "成交金额": f"{qty * price:.2f}", "合同编号": no})
        p = self.positions.get(code)
        if op == "买入":
            if p:
                p["cost"] = (p["cost"] * p["qty"] + price * qty) / (p["qty"] + qty)
                p["qty"] += qty
            else:
                self.add_position(code, name, qty, price, price, available=0)
            self.balance -= qty * price
        else:
            if p:
                p["qty"] -= qty
                p["available"] = max(0, p["available"] - qty)
                if p["qty"] <= 0:
                    del self.positions[code]
            self.balance += qty * price

    def new_order(self, side, code, name, qty, price) -> dict:
        no = str(self.next_no)
        self.next_no += 1
        o = {"委托时间": self.now(), "证券代码": code, "证券名称": name or code, "操作": "买入" if side == "buy" else "卖出",
             "委托数量": str(qty), "成交数量": "0", "撤单数量": "0", "委托价格": f"{price:.3f}", "成交均价": "0.000",
             "合同编号": no, "备注": "未成交"}
        self.orders.append(o)
        return o


def make_app(state: MockState | None = None) -> FastAPI:
    st = state or MockState()
    app = FastAPI(title="mock xiadan")
    app.state.mock = st

    def auth_ok(req: Request) -> bool:
        if not st.password:
            return True
        want = base64.b64encode(st.password.encode("utf-8")).decode()
        return req.headers.get("x-auth") == want

    def guard():
        return {"state": "connected" if st.connected else "disconnected", "running": st.connected,
                "window": st.connected, "connected": st.connected}

    @app.get("/_mock/calls")
    def mock_calls():
        """开发用：模拟服务收到过哪些请求（不在 /api/ 下，不记入调用记录）。"""
        return {"calls": st.calls, "forbidden_hits": st.forbidden_hits, "trade_calls": st.trade_calls}

    @app.middleware("http")
    async def gate(req: Request, call_next):
        path = req.url.path
        if path.startswith("/api/"):
            st.calls.append({"method": req.method, "path": path, "query": str(req.url.query), "at": st.now()})
            if not auth_ok(req) and (path != "/api/status" or st.status_needs_auth):
                return JSONResponse({"ok": False, "message": "口令错误"}, status_code=403)
            if path in ("/api/command", "/api/client/restart", "/api/connect"):
                st.forbidden_hits.append(path)
            if path not in ("/api/status",) and not st.connected and path.startswith("/api/"):
                return JSONResponse({"ok": False, "message": "未连接交易客户端"}, status_code=503)
        return await call_next(req)

    @app.get("/api/status")
    def status():
        return {"ok": st.connected, "win32_available": True, "pid": 4276,
                "message": "交易系统已连接" if st.connected else "未找到交易窗口", "executor": "同花顺股票交易系统（模拟）",
                "guard": guard()}

    @app.get("/api/snapshot")
    def snapshot(only: str | None = None, refresh: int = 0, no_render: int = 0):
        with st.lock:
            s = st.snapshot(only)
            s["cached"] = not refresh
            return s

    @app.get("/api/refresh")
    def refresh(fast: int = 1):
        with st.lock:
            s = st.snapshot()
            s["refresh"] = {"mode": "memory", "rendered": False}
            return s

    @app.get("/api/requery")
    def requery(page: str = "资金股票"):
        with st.lock:
            st.stale_positions, st._stale_copy = False, None
            s = st.snapshot()
            s["requery"] = {"page": page, "clicked": True, "error": ""}
            return s

    @app.post("/api/order")
    async def order(req: Request):
        b = await req.json()
        st.trade_calls.append({"path": "/api/order", "body": b})
        if b.get("confirm") != CONFIRM:
            return JSONResponse({"ok": False, "message": "缺少确认语"}, status_code=400)
        if b.get("side") not in ("buy", "sell") or not str(b.get("symbol", "")).isdigit():
            return JSONResponse({"ok": False, "message": "参数错误"}, status_code=400)
        mode = st.order_mode
        if mode == "timeout":
            time.sleep(float(b.get("_sleep", 3)))
        if mode == "fail":
            return {"ok": False, "status": "failed", "confirmed_count": 0, "message": "委托失败：可用资金不足"}
        with st.lock:
            st.new_order(b["side"], b["symbol"], b.get("_name", ""), int(b["quantity"]), float(b["price"]))
        cmd = f"{b['side']} {b['symbol']} {b['price']} {b['quantity']} -notip"
        if mode == "no_confirm":
            return {"ok": False, "command": cmd, "commands": [cmd], "confirmed_count": 0, "status": "unconfirmed",
                    "message": "已发送，未拿到合同号"}
        return {"ok": True, "command": cmd, "commands": [cmd], "confirmed_count": 1, "message": "已确认 1/1 笔委托",
                "status": "confirmed"}

    @app.post("/api/cancel")
    async def cancel(req: Request):
        b = await req.json()
        st.trade_calls.append({"path": "/api/cancel", "body": b})
        if b.get("confirm") != CONFIRM or b.get("kind") not in ("all", "last", "buy", "sell", "stock", "order"):
            return JSONResponse({"ok": False, "message": "参数错误"}, status_code=400)
        with st.lock:
            for o in st.orders:
                if b["kind"] == "order" and o["合同编号"] != str(b.get("entrust_no")):
                    continue
                rest = int(o["委托数量"]) - int(o["成交数量"]) - int(o["撤单数量"])
                if rest > 0:
                    o["撤单数量"] = str(rest)
                    o["备注"] = "部成部撤" if int(o["成交数量"]) else "已撤单"
        return {"ok": True, "message": f"您的撤单委托已成功提交，合同编号：{b.get('entrust_no', '')}。"}

    @app.post("/api/command")
    def command():
        return {"ok": True, "message": "mock：这个接口不应该被调用"}

    @app.post("/api/client/restart")
    def restart():
        return {"ok": True, "message": "mock：这个接口不应该被调用", "guard": guard()}

    @app.post("/api/connect")
    def connect():
        return {"ok": True, "message": "mock：这个接口不应该被调用"}

    # ---------- 控制接口 ----------
    @app.post("/mock/fill")
    async def mock_fill(req: Request):
        b = await req.json()
        with st.lock:
            return st.fill(str(b["entrust_no"]), int(b["qty"]), float(b["price"]))

    @app.post("/mock/set")
    async def mock_set(req: Request):
        b = await req.json()
        with st.lock:
            for k in ("connected", "order_mode", "stale_positions", "hide_orders", "empty_positions", "password"):
                if k in b:
                    setattr(st, k, b[k])
            for p in b.get("positions") or []:
                st.add_position(p["code"], p.get("name", p["code"]), p["qty"], p["cost"], p.get("price"), p.get("available"))
        return {"ok": True}

    @app.post("/mock/external")
    async def mock_external(req: Request):
        b = await req.json()
        with st.lock:
            o = st.new_order(b["side"], b["code"], b.get("name", b["code"]), int(b["qty"]), float(b["price"]))
            st.fill(o["合同编号"], int(b["qty"]), float(b["price"]))
        return o

    @app.get("/mock/state")
    def mock_state():
        return {"calls": st.calls[-100:], "trade_calls": st.trade_calls, "forbidden_hits": st.forbidden_hits,
                "orders": st.orders, "positions": st.pos_rows()}

    return app


def asgi_transport(app: FastAPI):
    """把模拟服务包成 XiadanClient 的 transport：进程内直接调用 ASGI 应用，不走网络（测试用）。"""
    import asyncio
    import json as _json

    import httpx

    tr = httpx.ASGITransport(app=app)

    async def _go(method, url, headers, params, json_body):
        content = _json.dumps(json_body, ensure_ascii=False).encode("utf-8") if (method == "POST" and json_body is not None) else b""
        req = httpx.Request(method, url, headers=headers, params=params, content=content)
        resp = await tr.handle_async_request(req)
        body = await resp.aread()
        return resp.status_code, body

    def transport(method, url, headers, params, json_body, timeout):
        code, body = asyncio.run(_go(method, url, headers, params, json_body))
        try:
            return code, _json.loads(body.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return code, {"_raw": body[:500].decode("utf-8", "replace"), "_bad_json": True}

    return transport


def main():
    ap = argparse.ArgumentParser(description="下单程序模拟服务（开发/测试用）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18765)
    ap.add_argument("--password", default="htfs")
    ap.add_argument("--demo", action="store_true", help="预置几只持仓和委托，网页截图用")
    a = ap.parse_args()
    st = MockState(a.password)
    if a.demo:
        st.add_position("300825", "阿尔特", 400, 13.62, 14.08)
        st.add_position("513970", "恒生消费ETF", 23400, 0.8397, 0.777)
        st.add_position("600519", "贵州茅台", 100, 1452.3, 1478.0)
        o = st.new_order("buy", "002230", "科大讯飞", 200, 48.50)
        st.fill(o["合同编号"], 200, 48.47)
        st.new_order("sell", "513970", "恒生消费ETF", 5000, 0.79)
    import uvicorn
    uvicorn.run(make_app(st), host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
