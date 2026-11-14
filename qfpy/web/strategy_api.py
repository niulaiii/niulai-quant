"""策略、模板、因子目录、形态信号、卖出检查结果、选股结果按层/按票、股池走势详情、公式校验和预览的接口。

同一套路由挂两次：
  - /api/...    给本机网页用（和现有 /api/config 一样不加 token）；
  - /api/v1/... 对外 REST API（token 鉴权，写操作受 [api].allow_config_write 控制）。
这里没有任何下单/撤单/发命令的接口，也不调用下单程序。
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from .. import signals as sigmod
from .. import viewsvc
from ..catalog import catalog as factor_catalog_full
from ..settings_docs import docs as settings_docs
from ..strategies import StrategyError

SIDE = "^(buy|sell)$"


def _signal_users(engine, file: str) -> list[str]:
    """哪些策略还在用这个形态信号（按 params.file 匹配）。"""
    used = []
    for side in ("buy", "sell"):
        try:
            items = engine.strategies.list(side)["items"]
        except Exception:  # noqa: BLE001
            continue
        for it in items:
            try:
                doc = engine.strategies.get(side, it["id"])
            except Exception:  # noqa: BLE001
                continue
            for layer in doc["content"].get("layers") or []:
                for f in layer.get("factors") or []:
                    if f.get("kind") == "signal" and (f.get("params") or {}).get("file") == file:
                        used.append(f"{doc['name']}「{layer.get('name')}」")
    return sorted(set(used))


def make_strategy_router(engine, prefix: str, write_deps: list | None = None, deps: list | None = None) -> APIRouter:
    r = APIRouter(prefix=prefix, dependencies=deps or [])
    W = write_deps or []
    S = engine.strategies

    def bad(e: Exception, code: int = 400):
        raise HTTPException(code, str(e))

    def call(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except KeyError as e:
            bad(f"找不到：{e.args[0] if e.args else ''}", 404)
        except (StrategyError, ValueError) as e:
            bad(e)

    # ---------------- 策略 ----------------
    @r.get("/strategies-active", summary="正在生效的买入/卖出策略和启用时间")
    def strategies_active():
        return {**S.active_names(), "auto_trade": bool(engine.switches.auto_enabled()),
                "note": "自动交易开关和生效策略是两回事：生效策略决定怎么选、怎么卖；开关关着时照样运行，只是不下单。"}

    @r.get("/strategies/{side}", summary="某一侧的全部策略（生效的排第一）")
    def strategies_list(side: str):
        return call(S.list, side)

    @r.get("/strategies/{side}/{sid}", summary="读取一套策略")
    def strategy_get(side: str, sid: str):
        return call(S.get, side, sid)

    @r.post("/strategies/{side}", summary="新建策略（空白 / 复制自某套 / 来自模板）", dependencies=W)
    def strategy_create(side: str, body: dict = Body(...)):
        return call(S.create, side, str(body.get("name") or "").strip(), body.get("content"), note=str(body.get("note") or ""),
                    from_id=body.get("from_id"), from_template=body.get("from_template"))

    @r.post("/strategies/{side}/{sid}/copy", summary="复制为新策略", dependencies=W)
    def strategy_copy(side: str, sid: str, body: dict = Body(default={})):
        src = call(S.get, side, sid)
        name = str(body.get("name") or "").strip() or f"{src['name']} 副本"
        return call(S.create, side, name, None, note=str(body.get("note") or src.get("note") or ""), from_id=sid)

    @r.put("/strategies/{side}/{sid}", summary="保存一套策略（dry_run = 只列差异）", dependencies=W)
    def strategy_save(side: str, sid: str, body: dict = Body(...)):
        return call(S.save, side, sid, body.get("content") or {}, name=body.get("name"), note=body.get("note"),
                    dry_run=bool(body.get("dry_run")))

    @r.post("/strategies/{side}/{sid}/rename", summary="改名", dependencies=W)
    def strategy_rename(side: str, sid: str, body: dict = Body(...)):
        return call(S.rename, side, sid, str(body.get("name") or ""), note=body.get("note"))

    @r.delete("/strategies/{side}/{sid}", summary="删除（正在生效的不能删）", dependencies=W)
    def strategy_delete(side: str, sid: str):
        call(S.delete, side, sid)
        return {"ok": True}

    @r.post("/strategies/{side}/{sid}/activate", summary="设为生效（必须带 confirm: true）", dependencies=W)
    def strategy_activate(side: str, sid: str, body: dict = Body(default={})):
        if body.get("confirm") is not True:
            bad("设为生效会立即改变自动选股/卖出的做法，请带 confirm: true 确认")
        return call(S.activate, side, sid, by=str(body.get("by") or ("网页" if prefix == "/api" else "API")))

    @r.post("/strategies/{side}/validate", summary="只校验一套策略内容，不保存")
    def strategy_validate(side: str, body: dict = Body(...)):
        from ..strategies import normalize_content
        try:
            content = normalize_content(side, body.get("content") or {})
        except StrategyError as e:
            return {"ok": False, "errors": [str(e)]}
        errs = call(S.validate, side, content)
        return {"ok": not errs, "errors": errs}

    @r.get("/strategies/{side}/{sid}/export", summary="导出（自动去掉密钥口令）")
    def strategy_export(side: str, sid: str):
        return call(S.export, side, sid)

    @r.post("/strategies/{side}/import/preview", summary="导入前先列差异（不保存）")
    def strategy_import_preview(side: str, body: dict = Body(...)):
        return call(S.import_preview, side, body.get("text") if body.get("text") is not None else body.get("data"),
                    compare_to=body.get("compare_to"))

    @r.post("/strategies/{side}/import", summary="导入为新的一套（不会自动生效）", dependencies=W)
    def strategy_import(side: str, body: dict = Body(...)):
        return call(S.import_save, side, body.get("text") if body.get("text") is not None else body.get("data"),
                    name=body.get("name"))

    # ---------------- 策略模板 ----------------
    @r.get("/strategy-templates/{side}", summary="策略模板（自带 + 自存）")
    def tpl_list(side: str):
        return call(S.templates, side)

    @r.get("/strategy-templates/{side}/{tid}", summary="读取一个模板")
    def tpl_get(side: str, tid: str):
        return call(S.template, side, tid)

    @r.post("/strategy-templates/{side}", summary="另存为模板（content 或 from_id）", dependencies=W)
    def tpl_save(side: str, body: dict = Body(...)):
        content = body.get("content")
        if content is None and body.get("from_id"):
            content = call(S.get, side, str(body["from_id"]))["content"]
        return call(S.save_template, side, str(body.get("title") or ""), str(body.get("description") or ""), content or {})

    @r.put("/strategy-templates/{side}/{tid}", summary="改模板名字/说明（自带的不能改）", dependencies=W)
    def tpl_update(side: str, tid: str, body: dict = Body(...)):
        return call(S.update_template, side, tid, title=body.get("title"), description=body.get("description"))

    @r.delete("/strategy-templates/{side}/{tid}", summary="删模板（自带的不能删）", dependencies=W)
    def tpl_delete(side: str, tid: str):
        call(S.delete_template, side, tid)
        return {"ok": True}

    @r.post("/strategy-templates/{side}/{tid}/preview", summary="套用前预览差异（不保存）")
    def tpl_preview(side: str, tid: str, body: dict = Body(default={})):
        return call(S.template_preview, side, tid, sid=body.get("sid"), draft=body.get("draft"))

    @r.post("/strategy-templates/{side}/default", summary="设为默认模板（id 为空 = 恢复默认）", dependencies=W)
    def tpl_default(side: str, body: dict = Body(default={})):
        return call(S.set_default_template, side, body.get("id") or None)

    @r.get("/strategy-templates/{side}/{tid}/export", summary="导出模板")
    def tpl_export(side: str, tid: str):
        return call(S.export, side, None, template=tid)

    # ---------------- 因子目录、设置说明 ----------------
    @r.get("/catalog", summary="因子库：12 类因子、字段说明、运算符、涨跌停规则、形态信号")
    def catalog(side: str | None = Query(None, pattern=SIDE)):
        from .. import extsources
        try:
            srcs = extsources.sources(engine.cfg, engine.data_dir, engine.ext_store)
        except Exception:  # noqa: BLE001
            srcs = []
        return factor_catalog_full(side, engine.root, engine.data_dir, srcs)

    @r.get("/settings-docs", summary="难懂设置的说明（干什么、何时生效、留空会怎样）")
    def docs():
        return settings_docs()

    # ---------------- 外部数据源（外部因子绑定的数据源） ----------------
    @r.get("/ext-sources", summary="已配置的外部数据源：各自提供的因子和字段、现在是否可用")
    def ext_sources():
        from .. import extsources
        return {"sources": extsources.sources(engine.cfg, engine.data_dir, engine.ext_store),
                "note": "外部因子先点选数据源，再点选它提供的因子或字段；数据源不可用或数据没拿到时按数据缺失处理"}

    # ---------------- 形态信号 ----------------
    @r.get("/pattern-signals", summary="形态信号（自带只读 + 自定义）")
    def sig_list():
        return sigmod.list_signals(engine.root, engine.data_dir)

    @r.post("/pattern-signals/validate", summary="校验条件，返回规范化后的条件和中文描述")
    def sig_validate(body: dict = Body(...)):
        try:
            c = sigmod.validate(body.get("conditions") or [])
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "conditions": c, "text": sigmod.describe(c)}

    @r.post("/pattern-signals", summary="保存为自定义信号", dependencies=W)
    def sig_create(body: dict = Body(...)):
        return call(sigmod.save_signal, engine.root, engine.data_dir, str(body.get("name") or ""), body.get("conditions") or [])

    @r.put("/pattern-signals/{sid}", summary="修改自定义信号", dependencies=W)
    def sig_update(sid: str, body: dict = Body(...)):
        return call(sigmod.save_signal, engine.root, engine.data_dir, str(body.get("name") or ""), body.get("conditions") or [], sid)

    @r.post("/pattern-signals/{sid}/rename", summary="改名（文件名不变，引用不失效）", dependencies=W)
    def sig_rename(sid: str, body: dict = Body(...)):
        return call(sigmod.rename_signal, engine.root, engine.data_dir, sid, str(body.get("name") or ""))

    @r.delete("/pattern-signals/{sid}", summary="删除自定义信号（还在用的不能删）", dependencies=W)
    def sig_delete(sid: str):
        file = next((s["file"] for s in sigmod.list_signals(engine.root, engine.data_dir)
                     if s["id"] == sid and not s["builtin"]), None)
        call(sigmod.delete_signal, engine.root, engine.data_dir, sid, _signal_users(engine, file) if file else None)
        return {"ok": True}

    @r.post("/pattern-signals/preview", summary="用当天数据试算这组条件能筛出几只（不保存）")
    def sig_preview(body: dict = Body(...)):
        if body.get("file"):
            spec = {"kind": "signal", "params": {"file": body["file"]}}
        else:
            try:
                c = sigmod.validate(body.get("conditions") or [])
            except ValueError as e:
                return {"ok": False, "error": str(e)}
            spec = {"kind": "signal", "params": {"conditions": c}}
        return viewsvc.preview_factor(engine, "buy", spec, limit=int(body.get("limit") or 200))

    # ---------------- 公式校验、预览；单个因子预览 ----------------
    @r.post("/formula/check", summary="校验自定义公式")
    def formula_check(body: dict = Body(...)):
        return viewsvc.formula_check(str(body.get("expr") or ""), str(body.get("side") or "buy"))

    @r.post("/formula/preview", summary="用当天数据预览公式能筛出几只（不保存）")
    def formula_preview(body: dict = Body(...)):
        chk = viewsvc.formula_check(str(body.get("expr") or ""), "buy")
        if not chk["ok"]:
            return chk
        return viewsvc.preview_factor(engine, "buy", {"kind": "expr", "params": {"expr": body["expr"]}},
                                      limit=int(body.get("limit") or 200))

    @r.post("/factor/preview", summary="用当天数据试算一个买入因子（不保存）")
    def factor_preview(body: dict = Body(...)):
        spec = body.get("factor") or {}
        if not isinstance(spec, dict) or not spec.get("kind"):
            bad("factor.kind 不能为空")
        return viewsvc.preview_factor(engine, "buy", spec, limit=int(body.get("limit") or 200))

    # ---------------- 卖出检查结果、选股结果 ----------------
    @r.get("/exit-checks", summary="卖出检查结果（每次检查的每只持仓明细，新的在前）")
    def exit_checks(limit: int = Query(50, ge=1, le=500), day: str | None = None):
        return engine.exit_checks(limit=limit, day=day)

    def _run(run_id: str) -> dict:
        rec = engine.get_run(run_id)
        if not rec:
            bad("没有这次运行记录", 404)
        return rec

    @r.get("/runs/{run_id}/layers", summary="选股结果按层看（run_id 可以是 latest）")
    def run_layers(run_id: str):
        rec = _run(run_id)
        return {"id": rec.get("id"), "trading_date": rec.get("trading_date"), "strategy": rec.get("strategy"),
                "layers": viewsvc.run_layers(rec), "picks": rec.get("picks") or []}

    @r.get("/runs/{run_id}/stocks", summary="选股结果按票看：每只票走到哪一层、为什么被淘汰、打分明细")
    def run_stocks(run_id: str, q: str | None = None, pools: str | None = None):
        rec = _run(run_id)
        rows = viewsvc.run_by_stock(rec, [p for p in (pools or "").split(",") if p] or None)
        if q:
            rows = [s for s in rows if q in s["code"] or q in (s.get("name") or "")]
        return {"id": rec.get("id"), "trading_date": rec.get("trading_date"), "strategy": rec.get("strategy"), "stocks": rows}

    @r.get("/runs/{run_id}/stocks/{code}", summary="某只票在这次运行里的逐层结果")
    def run_stock(run_id: str, code: str):
        s = viewsvc.stock_in_run(_run(run_id), code)
        if not s:
            bad("这次运行里没有这只票", 404)
        return s

    # ---------------- 股池绑定策略 ----------------
    @r.get("/pool-bind", summary="每个股池用哪套买入/卖出策略（没绑定 = 生效策略）、覆盖参数、判定规则")
    def pool_bind_get():
        b = engine.binder()
        return {"rows": b.table(list(engine.cfg["pool"]["pools"])), "sell_priority": b.sell_priority(),
                "bind_order": list((engine.cfg["pool"].get("bind") or {}).keys()),
                "buy_priority": b.buy_priority(list(engine.cfg["pool"]["pools"])),
                "priority": b.priority(),
                "rule": "一只票同时在几个池里时，按配置顺序取第一个：先看绑定表里的顺序，再看卖出策略「按股池覆盖」的顺序。"
                        "买入看选股时的来源股池，卖出看买入时记下的来源股池。池绑定的策略决定用哪套漏斗/卖出链，"
                        "这套策略里对这个池的覆盖参数再改止盈止损等数字。"}

    @r.put("/pool-bind", summary="设置一个股池绑定的策略：{pool, buy, sell}，留空 = 用生效策略", dependencies=W)
    def pool_bind_put(body: dict = Body(...)):
        from ..configsvc import ConfigError, apply_patch
        from ..poolbind import validate
        pool = str(body.get("pool") or "").strip()
        bind = {k: dict(v) for k, v in ((engine.cfg.get("pool") or {}).get("bind") or {}).items()}
        if pool:
            if ":" not in pool:
                bad("pool 格式应为 数据源:股池，例如 gushi:auction_long")
            # 只改提交了的那一边（买入页只提交 buy，卖出页只提交 sell）；两边都提交时整条替换（旧写法）
            cur = dict(bind.get(pool) or {})
            if "buy" not in body and "sell" not in body and "order" not in body:
                cur = {}                                 # 只给 pool：两边都解除绑定（旧写法）
            for s_ in ("buy", "sell"):
                if s_ in body:
                    v = str(body.get(s_) or "").strip()
                    if v:
                        cur[s_] = v
                    else:
                        cur.pop(s_, None)
            if cur:
                bind[pool] = cur
            else:
                bind.pop(pool, None)
        elif not isinstance(body.get("order"), list):
            bad("pool 格式应为 数据源:股池，例如 gushi:auction_long")
        new_pool = {**engine.cfg["pool"]}
        if isinstance(body.get("order"), list):        # 可选：调整优先顺序（网页拖动股池标签）
            order = [k for k in body["order"] if k in bind] + [k for k in bind if k not in body["order"]]
            bind = {k: bind[k] for k in order}
            pools = [str(k) for k in engine.cfg["pool"]["pools"]]
            new_pool["priority"] = [k for k in body["order"] if k in pools] + [k for k in pools if k not in body["order"]]
        new_pool["bind"] = bind

        def exists(side, sid):
            try:
                S.get(side, sid)
                return True
            except KeyError:
                return False
        errs = validate({"pool": new_pool}, exists)
        if errs:
            bad("；".join(errs))
        try:
            apply_patch(engine, {"pool": new_pool})
        except ConfigError as e:
            bad(e)
        return pool_bind_get()

    @r.get("/positions-sell-by", summary="每只持仓按哪个股池的哪套卖出策略在卖（含持仓手动指定的）")
    def positions_sell_by():
        b = engine.binder()
        out = []
        for code, pos in engine.book.positions().items():
            res = b.resolve_sell(pos.get("sources"), pos.get("sell_strategy_id"))
            from ..exits import rule_for
            rule = rule_for(res["cfg"], pos, res["pool"])
            out.append({"symbol": code, "name": pos.get("name"), "sources": pos.get("sources") or [], "pool": res["pool"],
                        "strategy": res["strategy"], "strategy_id": res["strategy_id"], "by": res["by"],
                        "override": res["override"], "forced": bool(res.get("forced")), "broken": res.get("broken"),
                        "sell_strategy_id": pos.get("sell_strategy_id") or "",
                        "rule": {k: rule.get(k) for k in
                                 ("take_profit_pct", "stop_loss_pct", "max_hold_days", "exit_time")}})
        return {"items": out}

    @r.get("/pool-stock/{code}", summary="股池走势里一只票的详情：分时/日K、入池日、指标、走到哪一层、持仓")
    def pool_stock(code: str, pool: str | None = None):
        if not code.isdigit() or len(code) != 6:
            bad("代码应为 6 位数字")
        return viewsvc.pool_stock_detail_cached(engine, code, pool)

    @r.get("/pool-trend", summary="股池走势左侧列表：成分股、现价、涨跌幅、入池日、入池以来涨跌")
    def pool_trend(key: str):
        return viewsvc.pool_trend(engine, getattr(engine, "overview_svc", None), key)

    @r.get("/industries", summary="行业列表（本地缓存的个股资料里出现过的行业，供行业筛选点选）")
    def industries():
        return viewsvc.industries(engine)

    return r
