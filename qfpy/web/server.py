"""网页与 API（FastAPI）。默认只监听 127.0.0.1，用 SSH 端口转发访问；没有登录鉴权。"""
from __future__ import annotations

import copy
import threading
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware

from ..exec.brokers import live_gate
from fastapi.responses import JSONResponse, Response

from ..backtest import default_universe, pool_choices, run_backtest, run_pool_backtest, strategy_choices
from ..config import API_LOCKED_KEYS, LIVE_LOCKED_KEYS, WEB_PAGE_READONLY
from ..datasources import SOURCE_TYPES
from ..datasources.watchlist import read_watchlist_csv
from .. import configsvc
from ..bg import BgJob
from ..datasources import source_catalog
from ..factors import factor_catalog
from ..funnel import template_list
from .api_v1 import make_router
from ..market import FIELD_DOCS
from ..notify import CHANNEL_TYPES
from ..notify.base import EVENT_LABELS
from ..scheduler import jobs_for
from ..util import append_jsonl, log, read_jsonl, today_str, ts

HERE = Path(__file__).parent

SWITCH_DOCS = [
    {"name": "自动交易开关", "what": "系统要不要自己发买卖委托。关闭时照常定时选股、跑漏斗、做退出判断和推送，但委托只记录为「本应下单」的信号；关闭的同时撤掉本系统今天还没成交的自动委托。",
     "when": "日常开关，也是紧急刹车。想先观察策略、或发现异常时关掉。", "scope": "只管系统自动发的委托，手动下单和手动撤单不受影响；持久保存，重启后保持；切换时推送钉钉。"},
    {"name": "运行模式", "what": "模拟盘 / 演练 / 实盘。实盘时委托发给下单程序，模拟盘在本地模拟成交，演练只生成委托请求。",
     "when": "在设置页「运行模式」里切换，保存后立即生效。", "scope": "切换时记日志、推钉钉；风控参数在设置页「风控」里改。"},
    {"name": "自动获取持仓", "what": "打开时资金和持仓从下单程序读取；关闭时完全不读取券商数据，资金、持仓、委托在账户页手动填写。",
     "when": "在账户页切换；只有实盘能打开。", "scope": "持久保存，重启后保持；切换时推送钉钉。"},
]


# ---------- 防 DNS 重绑定和跨站表单 ----------
# 网页只监听 127.0.0.1，但浏览器里别的网站仍可能：
#   1) 用 DNS 重绑定把自己的域名指到 127.0.0.1，再调本机接口 —— 所以只认 Host 是本机的请求；
#   2) 用跨站表单 / fetch 直接 POST 到 http://127.0.0.1:端口 —— 所以写操作（POST/PUT/PATCH/DELETE）要求
#      Origin（或 Referer）是本机；浏览器标了 Sec-Fetch-Site: cross-site 的写请求一律拒绝。
# 命令行、脚本、外部程序调用 /api/v1 时不带 Origin，不受影响（/api/v1 另有 token）。
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", "testserver"}   # testserver 是测试客户端用的主机名
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _host_only(h: str) -> str:
    h = (h or "").strip().lower()
    if h.startswith("["):
        return h[: h.find("]") + 1] if "]" in h else h
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def check_origin(method: str, headers) -> str | None:
    """返回拒绝原因；None = 放行。"""
    from urllib.parse import urlsplit
    host = _host_only(headers.get("host", ""))
    if host and host not in LOCAL_HOSTS:
        return f"只接受本机地址访问（Host: {host}）。请用 http://127.0.0.1:端口 打开"
    if method.upper() in _WRITE_METHODS:
        if (headers.get("sec-fetch-site") or "").lower() == "cross-site":
            return "拒绝跨站请求"
        src = headers.get("origin") or headers.get("referer")
        if src and src != "null":
            oh = _host_only(urlsplit(src).netloc)
            if oh not in LOCAL_HOSTS:
                return f"拒绝来自 {oh} 的写操作（只接受本机网页）"
        elif src == "null":
            return "拒绝来源不明（Origin: null）的写操作"
    return None


def install_origin_guard(app: FastAPI) -> None:
    from starlette.responses import JSONResponse

    @app.middleware("http")
    async def _guard(request, call_next):
        why = check_origin(request.method, request.headers)
        if why:
            return JSONResponse({"detail": why}, status_code=403)
        return await call_next(request)


def create_app(engine, scheduler=None) -> FastAPI:
    app = FastAPI(title="牛来量化", docs_url="/api/docs", redoc_url=None)
    store = engine.store
    bt_lock = threading.Lock()

    def err(status: int, msg: str):
        raise HTTPException(status_code=status, detail=msg)

    install_origin_guard(app)
    # 首页是 260KB 的单文件（内联 CSS+JS），不压缩时每次打开都要重新传一遍；gzip 后约 50KB。
    # 通过 SSH 端口转发从别的设备访问时差别最明显。
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    app.include_router(make_router(engine))   # 对外 REST API /api/v1（token 鉴权，没有下单接口）
    from .strategy_api import make_strategy_router
    app.include_router(make_strategy_router(engine, "/api"))   # 本机网页用：策略/模板/因子库/信号/结果查看（没有下单接口）

    from .api_v1 import openapi_route
    openapi_route(app, engine)

    def _list_pools():
        out = {}
        for name, s in engine.sources().items():
            try:
                out[name] = s.list_pools()
            except Exception as e:  # noqa: BLE001
                out[name] = [{"key": "?", "name": f"列表获取失败：{e}"}]
        return out
    pools_job = BgJob("股池列表", _list_pools, max_age=600)   # 股池列表要请求数据源，后台取、缓存 10 分钟

    from ..overview import Overview
    overview = Overview(engine)
    app.state.overview = overview
    engine.overview_svc = overview      # 股池走势列表（/api/pool-trend）复用首页股池行情

    @app.get("/api/plugins")
    def local_plugins():
        """插件列表页：每个插件注册了什么、是否加载成功、失败或重名被拒的原因。"""
        return {"items": engine.plugins, "dir": "plugins/",
                "doc": "docs/GUIDE.md",
                "note": "插件是放在 plugins/ 目录里的 Python 文件或文件夹，可以注册因子、数据源、推送渠道。"
                        "不能占用内置因子的名字，重名的不注册。写法见 docs/GUIDE.md 的「插件」一节。"}

    @app.post("/api/plugins/reload")
    def local_plugins_reload():
        """重新扫描 plugins/ 目录，返回和 /api/plugins 一样的列表；股池列表也跟着刷新（插件可能注册了新数据源）。"""
        engine.reload_plugins()
        pools_job.kick(force=True)
        return local_plugins()

    # ---------- 首页：大盘行情 + 股池走势（后台计算并缓存，接口立即返回上次结果） ----------
    @app.get("/api/overview")
    def get_overview(refresh: int = 0):
        return overview.view(force=bool(refresh))

    @app.get("/api/overview/pool")
    def get_overview_pool(key: str):
        d = overview.pool_detail(key)
        if d is None:
            return {"key": key, "pending": True, "members": [], "series": []}
        return d

    @app.get("/api/dataproto/openapi.json")
    def dataproto_openapi():
        """标准数据接口 qfdp v1 的 OpenAPI 描述（第三方行情 / 股池提供方照着实现）。"""
        from ..dataproto import openapi_spec
        return openapi_spec()

    @app.exception_handler(Exception)
    async def _any(_req, exc):  # noqa: ANN001
        log.exception("API 异常")
        return JSONResponse({"detail": f"{type(exc).__name__}: {exc}"}, status_code=500)

    @app.get("/")
    def index(request: Request):
        # no-cache（不是 no-store）：允许浏览器存副本但每次必须回源校验。
        # Starlette 的 FileResponse 不处理 If-None-Match，所以自己算 ETag 并回 304。
        # 首页由多个源码片段拼成，ETag 必须覆盖全部片段，否则改了 parts/*.js
        # 而首页指纹没变，浏览器会一直拿旧缓存。
        from .uisource import fingerprint, read_ui_source
        etag = f'"{fingerprint()}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"Cache-Control": "no-cache", "ETag": etag})
        return Response(read_ui_source(), media_type="text/html",
                        headers={"Cache-Control": "no-cache", "ETag": etag})

    # ---------- 状态与开关 ----------
    @app.get("/api/status")
    def status():
        s = engine.status()
        s["switch_docs"] = SWITCH_DOCS
        s["schedule"] = scheduler.view() if scheduler else {"jobs": jobs_for(engine.cfg, engine.bound_sell_contents()), "note": "未启用定时（命令行模式）"}
        return s

    @app.post("/api/auto_trade")
    def auto_trade(body: dict = Body(...)):
        if "enabled" not in body:
            err(400, "缺少 enabled")
        return engine.set_auto(bool(body["enabled"]), by="网页")

    @app.post("/api/trading")
    def trading(body: dict = Body(...)):
        """顶栏唯一的「自动交易」开关：{on: true|false}。关闭 = 不再发委托 + 撤本系统未成交委托（实盘、按合同号）。"""
        if "on" not in body:
            err(400, "缺少 on")
        return engine.set_trading(bool(body["on"]), by="网页")

    @app.post("/api/halt")
    def halt(body: dict = Body(default={})):
        """旧接口：等同于关闭自动交易。"""
        return {**engine.set_trading(False, by="网页"), "halted": True}

    @app.post("/api/resume")
    def resume():
        """旧接口：只解除停止状态，不打开自动交易。"""
        return engine.resume(by="网页")

    @app.get("/api/broker_status")
    def broker_status():
        return _broker_out(engine.broker_status())

    # ---------- 下单程序（只读）+ 实盘对账 ----------
    @app.get("/api/broker/diag")
    def broker_diag():
        """设置页「下单程序」的连接诊断：连接状态、最近调用、对账事件（不含券商资金和持仓）。"""
        return engine.link.diag_view()

    def _broker_out(v: dict) -> dict:
        """自动获取持仓关闭时，不返回任何券商数据（快照、对账报告、接管记录），只留连接状态。"""
        if engine.auto_pos():
            return v
        return {**v, "snapshot": None, "snapshot_at": None, "snapshot_via": None, "report": None,
                "adopt_log": [], "positions_auto": False}

    @app.get("/api/broker")
    def broker_view():
        """网页轮询用：返回上次的连接状态和券商快照，不主动调用下单程序。自动获取持仓关闭时不含券商数据。"""
        return _broker_out(engine.link.view())

    @app.post("/api/broker/sync")
    def broker_sync(body: dict = Body(default={})):
        """手动同步：how = status / cached / fresh / refresh / requery。在后台执行、立刻返回，
        页面轮询 GET /api/broker 看 sync.running 和结果（refresh 切页可能 12~17 秒，不可达时要等连接超时）。"""
        how = body.get("how") or "fresh"
        if how not in ("status", "cached", "fresh", "refresh", "requery"):
            err(400, "how 不支持")
        if how != "status" and not engine.auto_pos():
            err(400, "自动获取持仓已关闭，不读取券商数据；要读取请先在账户页打开「自动获取持仓」")
        started = engine.link.request_sync(how)
        return {**_broker_out(engine.link.view()), "sync_started": started}

    # ---------- 持仓获取方式：自动（下单程序快照）/ 手动填写 ----------
    @app.get("/api/positions_mode")
    def positions_mode():
        return {**engine.switches.positions_state(), "auto": engine.auto_pos(), "mode": engine.mode}

    @app.post("/api/positions_mode")
    def set_positions_mode(body: dict = Body(...)):
        if "auto" not in body:
            err(400, "缺少 auto")
        try:
            return {**engine.set_positions_auto(bool(body["auto"]), by="网页"), "mode": engine.mode}
        except ValueError as e:
            err(400, str(e))

    @app.post("/api/positions/manual")
    def manual_position(body: dict = Body(...)):
        """手动填写/修改一只持仓：{code, name?, quantity, avg_cost, sellable}。只在「自动获取持仓」关闭时可用。"""
        try:
            return engine.manual_position(str(body.get("code", "")), body)
        except ValueError as e:
            err(400, str(e))

    @app.post("/api/funds/manual")
    def manual_funds(body: dict = Body(...)):
        """手动填写资金：{available, balance?}。只在「自动获取持仓」关闭时可用（模拟盘也可以）。"""
        try:
            return engine.manual_funds(body)
        except ValueError as e:
            err(400, str(e))

    @app.get("/api/orders")
    def orders(limit: int = 200, signals: int = 1):
        """交易委托记录（当前运行模式的那一本）：系统委托 + 手动补记。"""
        if engine.mode == "live" and engine.auto_pos():
            engine.journal.sync_from_book(engine.book)
        return {"mode": engine.mode, "orders": engine.journal.list(limit, bool(signals)), "counts": engine.journal.counts()}

    @app.get("/api/trade/quote/{code}")
    def trade_quote(code: str):
        """手动下单表单：名称、现价、涨跌停价、持仓和可卖数量。"""
        try:
            return engine.manual_quote(code)
        except ValueError as e:
            err(400, str(e))

    @app.post("/api/trade/order")
    def trade_order(body: dict = Body(...)):
        """手动下单：{symbol, side, price_type: limit|market, price, quantity}。照样过风控，不受自动交易开关限制。
        实盘发给下单程序 /api/order，模拟盘走模拟成交，演练只生成请求。"""
        try:
            return engine.manual_place(body, by="网页")
        except ValueError as e:
            err(400, str(e))

    @app.post("/api/trade/cancel/{oid}")
    def trade_cancel(oid: str):
        """撤一笔未成交 / 部分成交的委托（实盘，按合同号调下单程序 /api/cancel），然后刷新委托状态。"""
        try:
            return engine.manual_cancel(oid, by="网页")
        except KeyError:
            err(404, "没有这笔委托")
        except ValueError as e:
            err(400, str(e))

    @app.post("/api/orders/refresh")
    def orders_refresh():
        """刷新委托状态（实盘）。手动账本模式下只读一次下单程序的委托表，不读持仓和资金。"""
        r = engine.refresh_orders()
        if not r.get("ok"):
            err(502, r.get("error") or "刷新失败")
        return r

    @app.post("/api/orders/manual")
    def manual_order(body: dict = Body(...)):
        """补记一笔委托（不带 id）或修改一笔（带 id）：symbol/side/price/quantity/status/filled_qty/fill_price/fee/time/strategy。
        手动账本模式下，成交数量比已入账的多出来的部分自动记进持仓和可用资金。"""
        try:
            return engine.manual_order(body, by="网页")
        except KeyError:
            err(404, "没有这笔委托")
        except ValueError as e:
            err(400, str(e))

    @app.delete("/api/orders/manual/{oid}")
    def manual_order_delete(oid: str):
        try:
            engine.manual_order_delete(oid)
        except KeyError:
            err(404, "没有这笔委托")
        except ValueError as e:
            err(400, str(e))
        return {"ok": True}

    @app.delete("/api/positions/manual/{code}")
    def manual_remove(code: str):
        try:
            engine.manual_remove(code)
        except KeyError:
            err(404, f"没有 {code} 的持仓")
        except ValueError as e:
            err(400, str(e))
        return {"ok": True}

    @app.post("/api/broker/cancel")
    def broker_cancel(body: dict = Body(...)):
        """撤掉本系统发出的一笔实盘在途委托（只在实盘模式可用，只按合同号撤）。"""
        if engine.mode != "live":
            err(400, "只有实盘模式才有委托可撤")
        oid = str(body.get("order_id") or "")
        if not oid:
            err(400, "缺少 order_id")
        return engine.cancel_order(oid, by="网页")

    # ---------- 运行 ----------
    @app.post("/api/run")
    def run(body: dict = Body(default={})):
        try:
            return engine.run(date=body.get("date") or None, pools=body.get("pools") or None,
                              trade=bool(body.get("trade", False)), origin="网页手动")
        except RuntimeError as e:
            err(409, str(e))

    @app.get("/api/runs")
    def runs():
        return engine._run_index()[:100]

    @app.get("/api/results")
    def results(pools: str | None = None, date: str | None = None, run: str | None = None,
                tier: str | None = None, offset: int = 0, limit: int = 100):
        """选股结果 · 全部结果：所有运行记录合成一张表（只读）。pools 逗号分隔；tier=focus 只看重点及以上。"""
        from .. import viewsvc
        return viewsvc.all_results(engine, [p for p in (pools or "").split(",") if p] or None, date or None,
                                   run or None, tier or None, max(0, offset), max(1, min(limit, 500)))

    @app.get("/api/results/stock/{code}")
    def result_stock(code: str):
        """点一只票：每次运行走到哪、跨天的分级变化。"""
        from .. import viewsvc
        return viewsvc.stock_history(engine, code)

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        r = engine.get_run(run_id)
        if r is None:
            err(404, "没有运行记录")
        return r

    # ---------- 持仓、委托、收益 ----------
    @app.get("/api/account")
    def account():
        """账户页（资金、持仓、委托记录、自动获取持仓开关）一次拿全。
        - 自动获取持仓关闭：只给本地账本（手动填写 / 模拟盘）的资金和持仓，broker 为 null，后台也不去读下单程序。
        - 打开：broker 里是券商最新数据和数据时间；数据不够新时 money/positions 为空并给出提示，不拿旧数据冒充。
        卖出预览在后台算（要拉行情），这里只拿上次的结果，不等待。"""
        auto = engine.auto_pos()
        a = engine.account()
        engine.preview_job.kick()
        a["preview"] = engine.preview_job.view()
        prev = {p["symbol"]: p for p in (engine.preview_job.result or [])}
        for p in a["positions"]:
            ev = prev.get(p["symbol"])
            p["exit_preview"] = None if ev is None else {k: ev.get(k) for k in (
                "decision", "decision_label", "reason", "due_at_exit_time", "due_at", "pnl_pct", "held_days", "sellable", "rule",
                "price", "note", "hold_skippable", "stop_loss", "layers")}
        a["hold_can_skip_stop_loss"] = bool(engine.cfg["exits"].get("hold_can_skip_stop_loss"))
        a["auto_positions"] = auto
        a["positions_state"] = engine.switches.positions_state()
        a["funds"] = engine.book.funds()
        if auto:
            if engine.mode == "live":
                engine.journal.sync_from_book(engine.book)
            a["broker"] = engine.link.account_view()
        else:
            a["broker"] = None
        a["orders"] = engine.journal.list(200)
        a["order_counts"] = engine.journal.counts()
        a["trading_on"] = engine.trading_on()
        return a

    @app.get("/api/sell/preview")
    def sell_preview(refresh: int = 0):
        """卖出因子链对每只持仓的逐层结果（可视化用）。后台计算，立刻返回上次结果；refresh=1 强制重算。"""
        engine.preview_job.kick(force=bool(refresh))
        return {"job": engine.preview_job.view(), "results": engine.preview_job.result or [],
                "layers": [bl.info() for bl in engine._sell_layers()]}

    @app.post("/api/positions/{code}/hold")
    def hold(code: str, body: dict = Body(...)):
        try:
            return engine.set_hold(code, bool(body.get("hold", True)), body.get("note", ""),
                                   by="网页", mode=body.get("mode"))
        except KeyError:
            err(404, f"没有 {code} 的持仓")

    @app.post("/api/positions/{code}/sell_strategy")
    def set_position_sell_strategy(code: str, body: dict = Body(...)):
        """给单只持仓指定卖出策略（优先级高于股池绑定）。strategy_id 传空 = 恢复跟随股池/生效策略。"""
        try:
            return engine.set_position_sell_strategy(code, body.get("strategy_id") or "", by="网页")
        except KeyError:
            err(404, f"没有 {code} 的持仓")
        except ValueError as e:
            err(400, str(e))

    @app.post("/api/positions/{code}/confirm")
    def confirm(code: str, body: dict = Body(...)):
        if engine.mode == "paper":
            err(400, "模拟盘不需要确认")
        try:
            qty = int(body.get("quantity"))
            cost = float(body.get("avg_cost") or 0)
        except (TypeError, ValueError):
            err(400, "quantity 须为整数、avg_cost 须为数字")
        if qty > 0 and cost <= 0:
            err(400, "请填写成本价")
        try:
            return engine.book.confirm(code, qty, cost, body.get("buy_date"))
        except KeyError:
            err(404, f"没有 {code} 的持仓")

    @app.post("/api/positions/{code}/sold")
    def sold(code: str, body: dict = Body(...)):
        """实盘/演练：你在同花顺确认已卖出后，在这里登记。"""
        if engine.mode == "paper":
            err(400, "模拟盘由系统记账")
        p = engine.book.get(code)
        if not p:
            err(404, f"没有 {code} 的持仓")
        qty = int(body.get("quantity") or p["quantity"])
        pnl = engine.book.ledger_sell_done(code, qty, float(body.get("price") or 0) or None, today_str(), "人工登记已卖出")
        return {"ok": True, "pnl": pnl}

    @app.post("/api/exits/check")
    def exits_check(body: dict = Body(default={})):
        try:
            return engine.check_exits(origin="网页手动", now_hm=body.get("now_hm"))
        except RuntimeError as e:
            err(409, str(e))

    @app.post("/api/exits/remind")
    def exits_remind(body: dict = Body(default={})):
        return engine.remind_exits(body.get("exit_time") or engine.cfg["exits"]["exit_time"])

    @app.post("/api/paper/reset")
    def paper_reset(body: dict = Body(...)):
        if engine.mode != "paper" or body.get("confirm") != "RESET":
            err(400, "只有模拟盘可以重置，且需要 confirm=RESET")
        engine.book.reset()
        return {"ok": True}

    # ---------- 分级股池 ----------
    @app.get("/api/tiers")
    def tiers(date: str | None = None):
        idx = engine._run_index()
        day = date or (idx[0]["trading_date"] if idx else today_str())
        return engine.tiers.view(day)

    @app.post("/api/tiers/move")
    def tiers_move(body: dict = Body(...)):
        r = engine.tiers.move(body["date"], body["code"], body["target"])
        if not r["ok"]:
            err(400, r["message"])
        return r

    # ---------- 配置 ----------
    @app.get("/api/config")
    def get_config():
        return {"config": store.masked(), "readonly": list(WEB_PAGE_READONLY), "path": str(store.path),
                "locked": {"live": list(LIVE_LOCKED_KEYS), "api": list(API_LOCKED_KEYS)},
                "mode": engine.mode,
                "live_gate": dict(zip(("ok", "missing"), live_gate(engine.cfg)))}

    @app.get("/api/meta")
    def meta():
        """因子库、字段、数据源、推送渠道。股池列表在后台取（数据源可能慢），这里不等。"""
        pools_job.kick()
        pools = pools_job.result or {}
        srcs = [{**s.info(), "pools": pools.get(name, [])} for name, s in engine.sources().items()]
        return {"factors": factor_catalog(), "fields": FIELD_DOCS, "sources": srcs, "pools_job": pools_job.view(),
                "source_types": [{**t, "fields": t["config_fields"]} for t in source_catalog()],
                "channels": [{"type": k, "title": c.title, "fields": c.fields, "help": c.help} for k, c in CHANNEL_TYPES.items()],
                "events": EVENT_LABELS, "plugins": engine.plugins, "templates": {"buy": template_list("buy"), "sell": template_list("sell")}}

    def _validate(new: dict) -> list[str]:
        return configsvc.problems(new, engine.root)

    # ---------- 买入漏斗 / 卖出因子链 ----------
    @app.get("/api/funnel/{side}")
    def get_funnel(side: str):
        try:
            return configsvc.get_funnel(engine, side)
        except configsvc.ConfigError as e:
            err(400, str(e))

    @app.put("/api/funnel/{side}")
    def put_funnel(side: str, body: dict = Body(...)):
        try:
            return configsvc.put_funnel(engine, side, body.get("layers"), dry_run=bool(body.get("dry_run")))
        except configsvc.ConfigError as e:
            err(400, str(e))

    @app.get("/api/templates")
    def templates(side: str = "buy"):
        if side not in ("buy", "sell"):
            err(400, "side 只能是 buy 或 sell")
        return template_list(side)

    @app.post("/api/config/validate")
    def validate_config(body: dict = Body(...)):
        """只校验不保存；返回会改哪些项。"""
        try:
            r = configsvc.web_apply(engine, body, dry_run=True)
        except configsvc.ConfigError as e:
            return {"ok": False, "errors": [str(e)]}
        return {"ok": True, "errors": [], "changes": r["changes"]}

    @app.put("/api/config")
    def put_config(body: dict = Body(...)):
        """保存设置（任意段的部分提交）。切实盘直接保存，照样记日志、推钉钉。"""
        try:
            return configsvc.web_apply(engine, body, by="网页")
        except configsvc.ConfigError as e:
            err(400, str(e))

    # ---------- 测试连接（用表单当前的值，不需要先保存，不写配置） ----------
    @app.get("/api/pools/names")
    def pools_names():
        from .. import sourcesvc
        return sourcesvc.pool_names(engine)

    @app.put("/api/pools/alias")
    def pools_alias(body: dict = Body(...)):
        """给股池起中文别名：{pool, alias}，alias 留空 = 用数据源给的名字。"""
        pool = str(body.get("pool") or "").strip()
        alias = str(body.get("alias") or "").strip()[:30]
        if pool not in [str(p) for p in engine.cfg["pool"]["pools"]]:
            err(400, f"没有这个股池：{pool}")
        al = dict((engine.cfg["pool"].get("aliases") or {}))
        if alias:
            al[pool] = alias
        else:
            al.pop(pool, None)
        try:
            configsvc.web_apply(engine, {"pool": {**engine.cfg["pool"], "aliases": al}}, by="网页")
        except configsvc.ConfigError as e:
            err(400, str(e))
        from .. import sourcesvc
        return sourcesvc.pool_names(engine)

    @app.get("/api/sources/status")
    def sources_status():
        from .. import sourcesvc
        return sourcesvc.status(engine)

    @app.post("/api/test/source")
    def test_source(body: dict = Body(...)):
        try:
            return configsvc.test_source(engine, str(body.get("name") or "test"), body.get("config") or {})
        except configsvc.ConfigError as e:
            err(400, str(e))

    @app.post("/api/test/xiadan")
    def test_xiadan(body: dict = Body(default={})):
        """只调用下单程序的只读接口：GET /api/status 和 GET /api/snapshot?only=money（验证口令）。"""
        try:
            return configsvc.test_xiadan(engine, body.get("live") or {}, transport=app.state.xiadan_test_transport)
        except configsvc.ConfigError as e:
            err(400, str(e))

    app.state.xiadan_test_transport = None      # 测试注入

    @app.get("/api/watchlist")
    def get_watchlist():
        cfg = engine.cfg
        out = {}
        for name, s in cfg["sources"].items():
            if s.get("type") == "watchlist":
                csv_rows = read_watchlist_csv(store.resolve_path(s["csv"])) if s.get("csv") else []
                out[name] = {"codes": s.get("codes", []), "csv": s.get("csv"), "csv_rows": csv_rows}
        return out

    @app.put("/api/watchlist/{name}")
    def put_watchlist(name: str, body: dict = Body(...)):
        """整体替换自选股 CSV 内容（rows: [[code, name], ...]）。"""
        s = engine.cfg["sources"].get(name)
        if not s or s.get("type") != "watchlist" or not s.get("csv"):
            err(404, "没有这个自选股列表或未配置 csv")
        rows = body.get("rows") or []
        lines = ["code,name"] + [f"{str(r[0]).strip()},{(r[1] if len(r) > 1 else '').strip()}" for r in rows
                                 if str(r[0]).strip().isdigit() and len(str(r[0]).strip()) == 6]
        store.resolve_path(s["csv"]).write_text("\n".join(lines) + "\n", encoding="utf-8")
        return {"ok": True, "count": len(lines) - 1}

    # ---------- 推送 ----------
    @app.post("/api/notify/test")
    def notify_test(body: dict = Body(default={})):
        """发测试消息。带 config（表单里当前填的值）时直接用这组值测，不保存、不改已保存的设置；
        不带 config 时按已保存的配置发（渠道名为空 = 所有已启用渠道）。"""
        ch = body.get("channel")
        if isinstance(body.get("config"), dict):
            if not ch:
                err(400, "用表单值测试时需要指定 channel")
            saved = (store.get().get("notify") or {}).get(ch) or {}
            try:
                return {"results": engine.notifier.test_with(ch, body["config"], saved, store._unmask), "unsaved": True}
            except ValueError as e:
                err(400, str(e))
        return {"results": engine.notifier.send_sync("test", "测试消息", "- 这是牛来量化的测试推送\n- 收到就说明配置正确", only=ch)}

    @app.get("/api/notify/recent")
    def notify_recent():
        return engine.notifier.recent(80)

    @app.get("/api/logs")
    def logs(n: int = 300, q: str = ""):
        """运行日志 data/qf.log 的最后 n 行（只读文件尾部，不会因为日志大而变慢）；q 为可选的关键字过滤。"""
        n = max(1, min(int(n), 2000))
        path = engine.data_dir / "qf.log"
        if not path.exists():
            return {"path": str(path), "lines": [], "size": 0}
        size = path.stat().st_size
        with path.open("rb") as f:
            f.seek(max(0, size - 512 * 1024))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
        if size > 512 * 1024:
            lines = lines[1:]
        if q:
            lines = [x for x in lines if q.lower() in x.lower()]
        return {"path": str(path), "lines": lines[-n:], "size": size}

    # ---------- 回测 ----------
    @app.get("/api/backtest/universe")
    def bt_universe():
        return {"codes": default_universe(engine)}

    @app.get("/api/backtest/strategies")
    def bt_strategies():
        """回测可选的买入/卖出策略（已保存的策略 + 模板），默认是当前生效的。"""
        return strategy_choices(engine)

    @app.get("/api/backtest/pools")
    def bt_pools():
        """按股池回测可选的股池：存档天数、绑定的策略。"""
        return pool_choices(engine)

    @app.get("/api/pool_archive")
    def pool_archive_summary():
        return engine.pool_archive.summary()

    @app.get("/api/pool_archive/{day}")
    def pool_archive_day(day: str):
        try:
            return engine.pool_archive.load(day)
        except ValueError as e:
            err(400, str(e))

    @app.post("/api/pool_archive/run")
    def pool_archive_run():
        """立刻拉一遍所有股池并存档（只读数据源，不下单）。"""
        return engine.archive_pools(origin="手动")

    @app.get("/api/backtest/history")
    def bt_history(limit: int = 20):
        """最近几次回测的摘要（含用的买入/卖出策略），方便对比。"""
        return {"items": list(reversed(read_jsonl(engine.data_dir / "backtests.jsonl", limit=limit)))}

    @app.post("/api/backtest")
    def backtest(body: dict = Body(...)):
        if not bt_lock.acquire(blocking=False):
            err(409, "已有回测在跑")
        try:
            if body.get("pools"):      # 按股池回测：用存档的历史成分股，每个池用它绑定的策略（或手动指定）
                res = run_pool_backtest(engine, body["start"], body["end"], list(body["pools"]),
                                        body.get("pool_strategies") or None)
                try:
                    append_jsonl(engine.data_dir / "backtests.jsonl",
                                 {"at": ts(), "mode": "pools", "start": res["start"], "end": res["end"],
                                  "pools": [{"pool": p["pool"], "strategy": p["strategy"], "summary": p["summary"]}
                                            for p in res["pools"]], "summary": res["summary"]})
                except Exception:  # noqa: BLE001
                    pass
                return res
            codes = body.get("codes")
            if isinstance(codes, str):
                codes = [c for c in codes.replace("，", ",").replace(" ", ",").split(",") if c]
            res = run_backtest(engine, body["start"], body["end"], codes or None,
                               buy_strategy=body.get("buy_strategy") or None, sell_strategy=body.get("sell_strategy") or None)
            try:   # 记一条摘要，方便对比不同策略
                append_jsonl(engine.data_dir / "backtests.jsonl",
                             {"at": ts(), "start": res["start"], "end": res["end"], "strategy": res["strategy"],
                              "codes": len(res["codes"]), "summary": res["summary"],
                              "skipped": len(res.get("factors_skipped") or [])})
            except Exception:  # noqa: BLE001
                pass
            return res
        except (ValueError, RuntimeError) as e:
            err(400, str(e))
        finally:
            bt_lock.release()

    return app
