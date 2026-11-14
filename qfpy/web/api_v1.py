"""对外 REST API：/api/v1/*，token 鉴权。接口文档见 docs/API.md，OpenAPI 在 /api/v1/openapi.json。

鉴权：请求头 Authorization: Bearer <token>（或 X-API-Token: <token>）。token 优先读环境变量 QF_API_TOKEN，
其次 [api].token；两处都空时整个 /api/v1 返回 401（不配 token 就不开放）。

这套 API 故意没有任何下单、撤单、改开关、改风控、改实盘配置的接口：委托只会由内部交易层在定时任务里发出，
并且要过自动交易开关、一键停止、实盘三重开关和全部风控。POST /api/v1/runs 只选股不交易。
"""
from __future__ import annotations

import hmac
import os
import threading
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query

from .. import configsvc
from ..datasources import source_catalog
from ..external import check_name
from ..factors import factor_catalog
from ..funnel import template_list
from ..market import FIELD_DOCS

# 对外永远不暴露的能力（测试会扫描路由，确保没有这些关键词）
FORBIDDEN_WORDS = ("order", "cancel", "trade", "halt", "resume", "auto_trade", "risk", "live", "broker", "xiadan", "command")


def api_token(cfg: dict) -> str:
    a = cfg.get("api") or {}
    return (os.environ.get(a.get("token_env") or "QF_API_TOKEN") or a.get("token") or "").strip()


def make_auth(engine):
    def auth(authorization: str | None = Header(default=None), x_api_token: str | None = Header(default=None)):
        cfg = engine.cfg
        if not (cfg.get("api") or {}).get("enabled", True):
            raise HTTPException(404, "对外 API 未启用（[api].enabled = false）")
        tok = api_token(cfg)
        if not tok:
            raise HTTPException(401, "未配置 API token：设置环境变量 QF_API_TOKEN 或 [api].token 后才能使用 /api/v1")
        got = x_api_token or ""
        if authorization and authorization.lower().startswith("bearer "):
            got = authorization[7:].strip()
        if not got or not hmac.compare_digest(got.encode(), tok.encode()):
            raise HTTPException(401, "token 不对", headers={"WWW-Authenticate": "Bearer"})
    return auth


def make_router(engine) -> APIRouter:
    run_lock = threading.Lock()
    state: dict[str, Any] = {"run": None}
    auth = make_auth(engine)

    def writable():
        if not (engine.cfg.get("api") or {}).get("allow_config_write", True):
            raise HTTPException(403, "对外 API 只读（[api].allow_config_write = false）")

    def bad(e: Exception):
        raise HTTPException(400, str(e))

    r = APIRouter(prefix="/api/v1", dependencies=[Depends(auth)], tags=["v1"])

    # 策略、模板、因子目录、形态信号、卖出检查结果、选股结果按层/按票、股池走势详情、公式校验和预览。
    # 写操作同样受 [api].allow_config_write 控制；这里没有任何下单接口。
    from .strategy_api import make_strategy_router
    r.include_router(make_strategy_router(engine, "", write_deps=[Depends(writable)]))

    # ---------------- 读取 ----------------
    @r.get("/status", summary="系统状态（只读）")
    def status():
        sw = engine.switches
        return {"mode": engine.mode, "mode_label": engine.broker.label, "auto_trade": sw.auto_enabled(), "halted": sw.halted(),
                "config_version": engine.store.version, "plugins": len(engine.plugins)}

    @r.get("/funnel/latest", summary="最新一次运行的买入漏斗结果")
    def funnel_latest(detail: bool = False):
        rec = engine.get_run("latest")
        if rec is None:
            raise HTTPException(404, "还没有运行记录")
        return _run_view(rec, detail)

    @r.get("/runs", summary="运行记录列表")
    def runs(limit: int = Query(50, ge=1, le=500)):
        return engine._run_index()[:limit]

    @r.get("/runs/{run_id}", summary="某次运行的漏斗结果")
    def run_detail(run_id: str, detail: bool = True):
        rec = engine.get_run(run_id)
        if rec is None:
            raise HTTPException(404, "没有这次运行")
        return _run_view(rec, detail)

    @r.post("/runs", summary="触发一次只选股不交易的运行（后台执行）")
    def run_now(body: dict = Body(default={})):
        """永远 trade=False：不会生成委托，也不会记信号。外部程序推完因子结果/股池后可以调它看效果。"""
        if body.get("trade"):
            raise HTTPException(400, "对外 API 不能触发交易，只能只选股")
        if not run_lock.acquire(blocking=False):
            raise HTTPException(409, "上一次通过 API 触发的运行还没结束")
        state["run"] = {"running": True, "error": None, "run_id": None}

        def go():
            try:
                rec = engine.run(date=body.get("date") or None, pools=body.get("pools") or None, trade=False, origin="对外API")
                state["run"] = {"running": False, "error": None, "run_id": rec.get("id")}
            except Exception as e:  # noqa: BLE001
                state["run"] = {"running": False, "error": str(e), "run_id": None}
            finally:
                run_lock.release()
        threading.Thread(target=go, daemon=True, name="api-run").start()
        return {"started": True, "trade": False, "poll": "/api/v1/runs/current/state"}

    @r.get("/runs/current/state", summary="通过 API 触发的运行是否结束")
    def run_current():
        return state["run"] or {"running": False, "run_id": None, "error": None}

    @r.get("/positions", summary="本地持仓（含状态、保留标记）")
    def positions():
        a = engine.account()
        return {"mode": a["mode"], "summary": a["summary"], "positions": a["positions"]}

    @r.get("/signals", summary="买卖信号和委托记录（只读）")
    def signals(limit: int = Query(100, ge=1, le=300), side: str | None = None, status: str | None = None):
        rows = engine.journal.list(300)
        if side:
            rows = [x for x in rows if x.get("side") == side]
        if status:
            rows = [x for x in rows if x.get("status") == status]
        return rows[:limit]

    @r.get("/sell/preview", summary="卖出因子链对每只持仓的逐层结果（后台计算，返回上次结果）")
    def sell_preview(refresh: bool = False):
        engine.preview_job.kick(force=refresh)
        return {"job": engine.preview_job.view(), "results": engine.preview_job.result or []}

    # ---------------- 因子和漏斗 ----------------
    @r.get("/factors", summary="可用因子类型（含插件）")
    def factors(side: str | None = Query(None, pattern="^(buy|sell)$")):
        return {"factors": factor_catalog(side), "fields": FIELD_DOCS}

    @r.get("/funnels/{side}", summary="读取买入漏斗 / 卖出因子链")
    def get_funnel(side: str):
        try:
            return configsvc.get_funnel(engine, side)
        except configsvc.ConfigError as e:
            bad(e)

    @r.put("/funnels/{side}", summary="整体替换漏斗层级（校验通过才写入配置）", dependencies=[Depends(writable)])
    def put_funnel(side: str, body: dict = Body(...)):
        try:
            return configsvc.put_funnel(engine, side, body.get("layers"), dry_run=bool(body.get("dry_run")))
        except configsvc.ConfigError as e:
            bad(e)

    @r.post("/funnels/{side}/validate", summary="只校验不保存")
    def validate_funnel(side: str, body: dict = Body(...)):
        try:
            configsvc.put_funnel(engine, side, body.get("layers"), dry_run=True)
            return {"ok": True, "errors": []}
        except configsvc.ConfigError as e:
            return {"ok": False, "errors": str(e).split("；")}

    @r.get("/templates", summary="漏斗模板")
    def templates(side: str = Query("buy", pattern="^(buy|sell)$")):
        return template_list(side)

    @r.post("/funnels/{side}/apply-template", summary="套用模板（写入配置，之后可继续改）", dependencies=[Depends(writable)])
    def apply_template(side: str, body: dict = Body(...)):
        try:
            return configsvc.apply_template(engine, side, str(body.get("key") or ""), dry_run=bool(body.get("dry_run")))
        except configsvc.ConfigError as e:
            bad(e)

    # ---------------- 外部因子结果（推） ----------------
    @r.post("/factor-results/{name}", summary="推送外部因子结果（打分或通过/淘汰）")
    def put_factor_results(name: str, body: dict = Body(...)):
        try:
            return engine.ext_store.put_factor_results(name, body.get("results", body.get("items")), body.get("date"),
                                                       source=str(body.get("source") or "api"), replace=not body.get("merge"))
        except (ValueError, TypeError) as e:
            bad(e)

    @r.get("/factor-results", summary="已推送的外部因子")
    def list_factor_results():
        return engine.ext_store.list_factor_results()

    @r.get("/factor-results/{name}", summary="读取某天的外部因子结果")
    def get_factor_results(name: str, date: str | None = None):
        try:
            rec = engine.ext_store.get_factor_results(name, date)
        except ValueError as e:
            bad(e)
        if rec is None:
            raise HTTPException(404, "没有这天的结果")
        return rec

    # ---------------- 数据源 ----------------
    @r.get("/source-types", summary="可用数据源类型（含插件）")
    def source_types():
        return source_catalog()

    @r.get("/sources", summary="已配置的数据源（密钥掩码）")
    def sources():
        masked = engine.store.masked().get("sources", {})
        return [{**s.info(), "config": masked.get(n, {})} for n, s in engine.sources().items()]

    @r.put("/sources/{name}", summary="新增或修改数据源配置", dependencies=[Depends(writable)])
    def put_source(name: str, body: dict = Body(...)):
        try:
            return configsvc.put_source(engine, name, body, (body or {}).get("role"))
        except (configsvc.ConfigError, ValueError) as e:
            bad(e)

    @r.delete("/sources/{name}", summary="删除数据源配置", dependencies=[Depends(writable)])
    def delete_source(name: str):
        try:
            return configsvc.delete_source(engine, name)
        except KeyError:
            raise HTTPException(404, "没有这个数据源") from None
        except configsvc.ConfigError as e:
            bad(e)

    @r.put("/sources/{name}/pools/{pool}", summary="推送股池（数据源 type = push）")
    def push_pool(name: str, pool: str, body: dict = Body(...)):
        _need_push(name)
        try:
            return engine.ext_store.put_pool(name, pool, body.get("stocks"), body.get("date"), str(body.get("name") or ""))
        except (ValueError, TypeError, AttributeError) as e:
            bad(e)

    @r.get("/sources/{name}/pools", summary="某数据源的股池列表")
    def source_pools(name: str):
        s = engine.sources().get(name)
        if s is None:
            raise HTTPException(404, "没有这个数据源")
        try:
            return s.list_pools()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"获取失败：{e}") from None

    @r.post("/sources/{name}/fields", summary="推送行情/自定义字段（数据源 type = push）")
    def push_fields(name: str, body: dict = Body(...)):
        _need_push(name)
        try:
            return engine.ext_store.put_fields(name, body.get("items"), body.get("date"), replace=bool(body.get("replace")))
        except (ValueError, TypeError, AttributeError) as e:
            bad(e)

    def _need_push(name: str):
        try:
            check_name(name, "数据源名")
        except ValueError as e:
            bad(e)
        sc = (engine.cfg.get("sources") or {}).get(name)
        if not sc or sc.get("type") != "push":
            raise HTTPException(404, f"没有名为 {name} 的 push 数据源：先 PUT /api/v1/sources/{name} {{\"type\": \"push\"}}")

    # ---------------- 插件 ----------------
    @r.get("/plugins", summary="已加载的插件")
    def plugins():
        return engine.plugins

    @r.post("/plugins/reload", summary="重新扫描 plugins/ 目录", dependencies=[Depends(writable)])
    def reload_plugins():
        return engine.reload_plugins()

    return r


def openapi_route(app, engine) -> None:
    """/api/v1/openapi.json：只含 /api/v1 的 OpenAPI 文档（同样要 token）。网页内部接口不对外。"""
    from fastapi.openapi.utils import get_openapi
    dep = make_auth(engine)

    @app.get("/api/v1/openapi.json", include_in_schema=False, dependencies=[Depends(dep)])
    def v1_openapi():
        routes = [r for r in app.routes if getattr(r, "path", "").startswith("/api/v1") and r.path != "/api/v1/openapi.json"]
        spec = get_openapi(title="牛来量化 对外 API", version="1", routes=routes,
                           description="自用 A 股选股→因子→交易系统的对外接口。token 鉴权；没有下单/撤单/改开关/改风控的接口。中文说明见 docs/API.md")
        spec.setdefault("components", {})["securitySchemes"] = {"bearer": {"type": "http", "scheme": "bearer"}}
        spec["security"] = [{"bearer": []}]
        return spec


def _run_view(rec: dict, detail: bool) -> dict:
    stages = []
    for st in rec.get("stages", []):
        s = {k: st.get(k) for k in ("index", "type", "id", "label", "mode", "input", "skipped")}
        s["kept"] = len(st.get("kept", []))
        s["dropped"] = len(st.get("dropped", []))
        if detail:
            s["kept_list"] = st.get("kept", [])
            s["dropped_list"] = st.get("dropped", [])
            s["factors"] = st.get("factors")
        stages.append(s)
    return {"id": rec.get("id"), "trading_date": rec.get("trading_date"), "started_at": rec.get("started_at"),
            "origin": rec.get("origin"), "picks": [{"code": p["code"], "name": p["name"], "score": p.get("value")} for p in rec.get("picks", [])],
            "stages": stages, "warnings": rec.get("warnings", [])}
