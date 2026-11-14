"""同花顺下单程序（交易工作台 xiadan）的 HTTP 客户端。按 /home/sprite/xiadan/接口文档.md 实现。

- 所有 /api/* 都带口令头 X-Auth = base64(UTF-8(口令))。口令来自环境变量（默认 QF_XIADAN_PASSWORD）
  或配置 [live].auth_password，代码里不写死任何口令。口令为空时不带这个头（对应下单程序 --trade-password ""）。
- 只允许调用白名单里的接口：读接口 GET /api/status、/api/snapshot、/api/refresh、/api/requery；
  交易接口 POST /api/order、/api/cancel（只有 ThsLiveBroker 在实盘三重开关全开时才会走到）。
  /api/command、/api/client/restart、/api/connect 不在白名单里，调用会直接抛错。
- 各接口超时按文档里的耗时分别设置，并有最小调用间隔，避免把下单程序（切页渲染是互斥的）堵住。
- 读接口失败只抛 XiadanError，绝不重试交易接口。
"""
from __future__ import annotations

import base64
import os
import re
import threading
import time
from typing import Any, Callable

import httpx

READ_PATHS = ("/api/status", "/api/snapshot", "/api/refresh", "/api/requery")
TRADE_PATHS = ("/api/order", "/api/cancel")
# 永远不调用的接口（FORBIDDEN）：原始命令、重启客户端、重连。代码里只在这里出现，用来拦截。
FORBIDDEN_PATHS = ("/api/command", "/api/client/restart", "/api/connect")  # FORBIDDEN

# 默认超时（秒）。依据文档“耗时参考”：status 最轻；snapshot 命中缓存几十毫秒、无缓存兜底渲染 8~10 秒；
# refresh 默认约 2 秒、需要切页时 12~17 秒；requery 子进程脚本超时 45 秒。
DEFAULT_TIMEOUTS = {"status": 5.0, "snapshot": 15.0, "snapshot_sync": 6.0, "refresh": 25.0, "requery": 50.0,
                    "order": 20.0, "cancel": 15.0}
# 同一接口两次调用的最小间隔（秒）。下单/撤单后的同步（snapshot_sync）不限。
DEFAULT_MIN_INTERVAL = {"status": 2.0, "snapshot": 3.0, "snapshot_sync": 0.0, "refresh": 30.0, "requery": 120.0}

ERROR_LABELS = {"auth": "口令错误或未提供（403）", "not_connected": "下单程序没连上同花顺交易客户端（503）",
                "bad_request": "参数错误（400）", "timeout": "请求超时", "network": "连不上下单程序",
                "http": "HTTP 错误", "bad_json": "返回内容不是 JSON", "rate_limited": "调用太频繁",
                "forbidden": "禁止调用的接口", "not_ok": "下单程序返回 ok=false"}


class XiadanError(RuntimeError):
    def __init__(self, kind: str, message: str, http_status: int | None = None, body: Any = None):
        super().__init__(f"{ERROR_LABELS.get(kind, kind)}：{message}" if message else ERROR_LABELS.get(kind, kind))
        self.kind, self.http_status, self.body = kind, http_status, body

    def to_dict(self) -> dict:
        return {"kind": self.kind, "label": ERROR_LABELS.get(self.kind, self.kind), "message": str(self),
                "http_status": self.http_status}


def auth_password(live_cfg: dict) -> str:
    """口令：环境变量优先（默认 QF_XIADAN_PASSWORD），其次 [live].auth_password。都没有则为空（不带 X-Auth）。"""
    env = live_cfg.get("auth_env") or "QF_XIADAN_PASSWORD"
    v = os.environ.get(env)
    if v is not None and v != "":
        return v
    return str(live_cfg.get("auth_password") or "")


def auth_header_value(password: str) -> str:
    return base64.b64encode(password.encode("utf-8")).decode("ascii")


def auth_source(live_cfg: dict) -> str:
    env = live_cfg.get("auth_env") or "QF_XIADAN_PASSWORD"
    if os.environ.get(env):
        return f"环境变量 {env}"
    if live_cfg.get("auth_password"):
        return "配置 [live].auth_password"
    return "未设置（不带 X-Auth 头）"


def _default_transport(method: str, url: str, headers: dict, params: dict | None, json_body: dict | None,
                       timeout: float) -> tuple[int, Any]:
    if method == "GET":
        r = httpx.get(url, headers=headers, params=params, timeout=timeout)
    else:
        r = httpx.post(url, headers=headers, json=json_body, timeout=timeout)
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        body = {"_raw": (getattr(r, "text", "") or "")[:500], "_bad_json": True}
    return r.status_code, body


Transport = Callable[[str, str, dict, "dict | None", "dict | None", float], "tuple[int, Any]"]


class XiadanClient:
    """下单程序客户端。transport 可注入（测试里用内存版模拟服务，不走网络）。"""

    def __init__(self, live_cfg: dict, transport: Transport | None = None, clock: Callable[[], float] = time.monotonic):
        self.live = live_cfg
        self.base = str(live_cfg.get("base_url") or "http://127.0.0.1:8765").rstrip("/")
        self.transport = transport or _default_transport
        self.clock = clock
        self.timeouts = {**DEFAULT_TIMEOUTS, **(live_cfg.get("timeouts") or {})}
        if live_cfg.get("timeout_secs") and "order" not in (live_cfg.get("timeouts") or {}):
            self.timeouts["order"] = max(float(live_cfg["timeout_secs"]), 5.0)
        self.min_interval = {**DEFAULT_MIN_INTERVAL, **(live_cfg.get("min_interval_secs") or {})}
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()      # 串行化：下单程序的渲染本身是互斥的，并发只会排队
        self.calls: list[dict] = []        # 最近调用记录（网页显示、测试断言）

    # ---------- 底层 ----------
    def headers(self) -> dict:
        pw = auth_password(self.live)
        h = {"Content-Type": "application/json"}
        if pw:
            h["X-Auth"] = auth_header_value(pw)
        return h

    def _call(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None,
              timeout_key: str, rate_key: str | None = None, force: bool = False) -> dict:
        if path in FORBIDDEN_PATHS or path not in READ_PATHS + TRADE_PATHS:
            raise XiadanError("forbidden", path)
        if method == "GET" and path not in READ_PATHS:
            raise XiadanError("forbidden", f"GET {path}")
        if method == "POST" and path not in TRADE_PATHS:
            raise XiadanError("forbidden", f"POST {path}")
        rk = rate_key or timeout_key
        with self._lock:
            now = self.clock()
            gap = float(self.min_interval.get(rk, 0) or 0)
            last = self._last.get(rk)
            if not force and gap and last is not None and now - last < gap:
                raise XiadanError("rate_limited", f"{path} 距上次调用不到 {gap:.0f} 秒，{gap - (now - last):.0f} 秒后再试")
            self._last[rk] = now
            timeout = float(self.timeouts.get(timeout_key, 10))
            t0 = time.monotonic()
            rec = {"at": time.strftime("%H:%M:%S"), "method": method, "path": path, "params": params or {}}
            try:
                status, data = self.transport(method, self.base + path, self.headers(), params, body, timeout)
            except httpx.TimeoutException as e:
                rec.update(result="timeout", ms=int((time.monotonic() - t0) * 1000))
                self._remember(rec)
                raise XiadanError("timeout", f"{path} 超过 {timeout:.0f} 秒没有响应（{type(e).__name__}）") from e
            except TimeoutError as e:
                rec.update(result="timeout", ms=int((time.monotonic() - t0) * 1000))
                self._remember(rec)
                raise XiadanError("timeout", f"{path} 超过 {timeout:.0f} 秒没有响应") from e
            except XiadanError:
                raise
            except Exception as e:  # noqa: BLE001
                rec.update(result="network", ms=int((time.monotonic() - t0) * 1000))
                self._remember(rec)
                raise XiadanError("network", f"{self.base}{path}：{type(e).__name__}: {e}") from e
            rec.update(result=status, ms=int((time.monotonic() - t0) * 1000))
            self._remember(rec)
        if not isinstance(data, dict):
            raise XiadanError("bad_json", f"{path} 返回的不是 JSON 对象", status, data)
        msg = str(data.get("message") or "")
        if status == 403:
            raise XiadanError("auth", msg or "请检查 [live].auth_password 或环境变量 QF_XIADAN_PASSWORD", status, data)
        if status == 503:
            raise XiadanError("not_connected", msg, status, data)
        if status == 400:
            raise XiadanError("bad_request", msg, status, data)
        if status != 200:
            raise XiadanError("http", f"HTTP {status} {msg}", status, data)
        if data.get("_bad_json"):
            raise XiadanError("bad_json", str(data.get("_raw"))[:200], status, data)
        return data

    def _remember(self, rec: dict) -> None:
        self.calls.append(rec)
        del self.calls[:-50]

    # ---------- 读接口 ----------
    def status(self, force: bool = False) -> dict:
        """GET /api/status：连接状态（最轻量）。ok=false 不抛错，由调用方看 guard。"""
        return self._call("GET", "/api/status", timeout_key="status", force=force)

    def snapshot(self, only: str | None = None, refresh: bool = False, force: bool = False) -> dict:
        """GET /api/snapshot：资金+持仓+委托+成交+撤单。命中缓存毫秒级，缓存过期约 0.8 秒。"""
        params: dict = {}
        if only:
            if only not in ("money", "positions", "orders", "deals", "cancels"):
                raise ValueError(f"only 不支持 {only}")
            params["only"] = only
        if refresh:
            params["refresh"] = 1
        return self._call("GET", "/api/snapshot", params=params or None, timeout_key="snapshot", force=force)

    def snapshot_after_trade(self) -> dict:
        """下单/撤单后的同步：文档推荐 /api/snapshot?refresh=1&no_render=1，只扫内存、绝不切页，约 0.7~0.9 秒。"""
        return self._call("GET", "/api/snapshot", params={"refresh": 1, "no_render": 1}, timeout_key="snapshot_sync",
                          rate_key="snapshot_sync", force=True)

    def refresh(self, force: bool = False) -> dict:
        """GET /api/refresh：全量刷新（默认 mode=memory 约 2 秒；委托为空时会切页补渲染 12~17 秒）。"""
        return self._call("GET", "/api/refresh", timeout_key="refresh", force=force)

    def requery(self, page: str = "资金股票", force: bool = False) -> dict:
        """GET /api/requery：让客户端点一次查询（会还原并激活窗口），最慢，只在手动点按钮时用。"""
        return self._call("GET", "/api/requery", params={"page": page}, timeout_key="requery", force=force)

    # ---------- 交易接口（只给 ThsLiveBroker 用） ----------
    def post_order(self, body: dict) -> tuple[int, dict]:
        """POST /api/order。返回 (http 状态, 响应)。400/403/503 不抛错，交给 interpret_order_response 判断。
        超时/断线抛 XiadanError（调用方记为“结果未知”，绝不重试）。"""
        return self._post_trade("/api/order", body, "order")

    def post_cancel(self, body: dict) -> tuple[int, dict]:
        return self._post_trade("/api/cancel", body, "cancel")

    def _post_trade(self, path: str, body: dict, key: str) -> tuple[int, dict]:
        try:
            return 200, self._call("POST", path, body=body, timeout_key=key, rate_key=key, force=True)
        except XiadanError as e:
            if e.kind in ("auth", "not_connected", "bad_request", "http") and e.http_status:
                return e.http_status, e.body if isinstance(e.body, dict) else {}
            raise


# ---------------- 数据规整（委托/成交表的列名随券商版本变化） ----------------

def _pick(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _num(v, default=0.0) -> float:
    if v is None or v == "":
        return default
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[,\s元股]", "", str(v))
    try:
        return float(s)
    except ValueError:
        return default


def norm_code(v) -> str:
    m = re.search(r"\d{6}", str(v or ""))
    return m.group(0) if m else ""


ORDER_STATUS_LABELS = {"pending": "未成交", "partial": "部分成交", "filled": "已成交", "cancelled": "已撤单",
                       "partial_cancelled": "部成部撤", "rejected": "废单", "unknown": "未知"}


def norm_order(row: dict) -> dict:
    """把一行委托（表头 → 值）规整成固定字段。"""
    code = norm_code(_pick(row, "证券代码", "代码", "股票代码", "code", "symbol"))
    side_raw = str(_pick(row, "操作", "买卖标志", "买卖方向", "委托类别", "方向", "side", default=""))
    side = "buy" if ("买" in side_raw or side_raw.lower() == "buy") else ("sell" if ("卖" in side_raw or side_raw.lower() == "sell") else "")
    qty = int(_num(_pick(row, "委托数量", "数量", "qty", "quantity")))
    filled = int(_num(_pick(row, "成交数量", "已成交数量", "filled")))
    cancelled = int(_num(_pick(row, "撤单数量", "已撤数量", "cancelled")))
    price = _num(_pick(row, "委托价格", "价格", "price"))
    avg = _num(_pick(row, "成交均价", "成交价格", "avg_price"))
    no = str(_pick(row, "合同编号", "委托编号", "合同号", "entrust_no", default="")).strip()
    remark = str(_pick(row, "备注", "状态", "委托状态", "状态说明", "status", default=""))
    t = str(_pick(row, "委托时间", "时间", "time", default=""))
    if "废" in remark:
        st = "rejected"
    elif cancelled > 0 or "撤" in remark and "待撤" not in remark:
        st = "partial_cancelled" if filled > 0 else "cancelled"
    elif qty and filled >= qty:
        st = "filled"
    elif "已成" in remark and filled == 0 and qty:
        st, filled = "filled", qty
    elif filled > 0 or "部成" in remark:
        st = "partial"
    else:
        st = "pending"
    return {"symbol": code, "name": str(_pick(row, "证券名称", "名称", "name", default="")), "side": side, "quantity": qty,
            "filled": filled, "cancelled": cancelled, "price": price, "avg_price": avg, "entrust_no": no,
            "remark": remark, "time": t, "status": st, "status_label": ORDER_STATUS_LABELS[st],
            "final": st in ("filled", "cancelled", "partial_cancelled", "rejected")}


def norm_deal(row: dict) -> dict:
    side_raw = str(_pick(row, "操作", "买卖标志", "买卖方向", "方向", "side", default=""))
    return {"symbol": norm_code(_pick(row, "证券代码", "代码", "code", "symbol")),
            "name": str(_pick(row, "证券名称", "名称", "name", default="")),
            "side": "buy" if "买" in side_raw or side_raw == "buy" else ("sell" if "卖" in side_raw or side_raw == "sell" else ""),
            "quantity": int(_num(_pick(row, "成交数量", "数量", "qty"))),
            "price": _num(_pick(row, "成交均价", "成交价格", "价格", "price")),
            "entrust_no": str(_pick(row, "合同编号", "委托编号", "合同号", "entrust_no", default="")).strip(),
            "time": str(_pick(row, "成交时间", "时间", "委托时间", "time", default=""))}


def norm_position(row: dict) -> dict:
    return {"symbol": norm_code(_pick(row, "code", "证券代码")), "name": str(_pick(row, "name", "证券名称", default="")),
            "qty": int(_num(_pick(row, "qty", "股票余额", "持仓数量"))),
            "available": int(_num(_pick(row, "available", "可用余额", "可卖数量"))),
            "cost": _num(_pick(row, "cost", "成本价")), "price": _num(_pick(row, "price", "市价")),
            "market_value": _num(_pick(row, "market_value", "市值")), "pnl": _num(_pick(row, "pnl", "盈亏")),
            "pnl_pct": _num(_pick(row, "pnl_pct", "盈亏比例")),
            "code_verified": bool(row.get("code_verified", True))}


def normalize_snapshot(snap: dict) -> dict:
    """规整整份快照：positions/orders/deals 都变成固定字段；保留原始 money、ts、cached 等。"""
    pos = [norm_position(r) for r in snap.get("positions") or [] if isinstance(r, dict)]
    orders = [norm_order(r) for r in snap.get("orders") or [] if isinstance(r, dict)]
    deals = [norm_deal(r) for r in snap.get("deals") or [] if isinstance(r, dict)]
    return {"ok": snap.get("ok", True), "ts": snap.get("ts"), "cached": snap.get("cached"), "source": snap.get("source"),
            "elapsed_ms": snap.get("elapsed_ms"), "money": snap.get("money") or {},
            "positions": [p for p in pos if p["symbol"]], "orders": [o for o in orders if o["symbol"]],
            "deals": [d for d in deals if d["symbol"]], "refresh": snap.get("refresh"), "requery": snap.get("requery"),
            "unverified": [p["symbol"] for p in pos if not p["code_verified"]]}
