"""网页和对外 API 共用的只读视图：选股结果按票看、股池走势里一只票的详情、公式校验和预览、形态信号预览。

这里只读数据，不下单、不改配置。拿不到的数据一律给 None（页面显示「—」），不当成 0。
"""
from __future__ import annotations

import time
from datetime import date as _date, timedelta

from .model import Candidate
from .util import today_str


# ---------------- 运行记录的显示：旧层名、运行方式 ----------------
# 第六轮以前的记录第 0 层叫「前池（股池合并去重）」，读出来时一律换成新叫法，原记录文件不改。
ALL_POOLS_STAGE = "进入策略（全部股池）"
OLD_STAGE_LABELS = {"前池（股池合并去重）": ALL_POOLS_STAGE, "前池": ALL_POOLS_STAGE, "股池合并去重": ALL_POOLS_STAGE}

# 运行方式：记录里存的是 [execution].mode（paper / dry_run / live）。也认中文和几个常见别名，认不出就写「未记录」，
# 绝不把认不出的值当成实盘。
MODE_LABELS = {"paper": "模拟盘", "dry_run": "演练", "live": "实盘"}
_MODE_ALIASES = {"paper": "paper", "sim": "paper", "simulate": "paper", "simulation": "paper", "模拟盘": "paper", "模拟": "paper",
                 "dry_run": "dry_run", "dryrun": "dry_run", "dry-run": "dry_run", "演练": "dry_run",
                 "live": "live", "实盘": "live"}


def zh_stage_label(label):
    if not isinstance(label, str):
        return label
    return OLD_STAGE_LABELS.get(label.strip(), label)


def norm_mode(value) -> str | None:
    """把记录里的运行方式归一成 paper / dry_run / live；认不出给 None。"""
    if value is None:
        return None
    return _MODE_ALIASES.get(str(value).strip().lower()) or _MODE_ALIASES.get(str(value).strip())


def run_mode(rec: dict) -> str | None:
    """这次运行的运行方式：先看记录顶层的 mode，认不出再看这次委托上记的 mode。"""
    m = norm_mode(rec.get("mode"))
    if m:
        return m
    for o in rec.get("orders") or []:
        m = norm_mode((o or {}).get("mode")) if isinstance(o, dict) else None
        if m:
            return m
    return None


def normalize_run(rec: dict | None) -> dict | None:
    """读运行记录时统一换成现在的写法（只改返回给页面的副本）：旧层名换掉、运行方式给中文、
    合并层标出每个池各贡献几只。"""
    if rec is None:
        return rec
    rec = dict(rec)
    m = run_mode(rec)
    rec["mode"] = m or rec.get("mode")
    rec["mode_label"] = MODE_LABELS.get(m, "未记录")
    stages = []
    for st in rec.get("stages") or []:
        st = dict(st)
        old = st.get("label")
        st["label"] = zh_stage_label(old)
        if st["label"] == ALL_POOLS_STAGE and st.get("type") == "pool" and not st.get("pool"):
            by: dict[str, int] = {}
            for row in st.get("kept") or []:
                for s in row.get("sources") or []:
                    by[s] = by.get(s, 0) + 1
            st["by_pool"] = by
        stages.append(st)
    if "stages" in rec:
        rec["stages"] = stages
    return rec


# ---------------- 选股结果：按层 / 按票 ----------------
def run_layers(rec: dict) -> list[dict]:
    """按层看：每层进几只、留几只、淘汰几只（含数据缺失几只）。"""
    out = []
    for st in rec.get("stages") or []:
        dropped = st.get("dropped") or []
        miss = sum(1 for d in dropped if any(c.get("missing") for c in d.get("checks") or []))
        out.append({"index": st.get("index"), "type": st.get("type"), "label": zh_stage_label(st.get("label")), "mode": st.get("mode"),
                    "input": st.get("input"), "kept": len(st.get("kept") or []), "dropped": len(dropped), "missing": miss,
                    "skipped": st.get("skipped")})
    return out


def run_by_stock(rec: dict, pools: list[str] | None = None) -> list[dict]:
    """按票看：每只票走到哪一层、在哪一层被淘汰、为什么、打分明细、最后有没有入选。

    第六轮起漏斗按股池分开：同一只票在几个池里会各出一行（pool 字段标明是哪个池）。pools 给了就只看这些池。
    旧记录（没有 pool 字段）照旧一只票一行。
    """
    stocks: dict[tuple, dict] = {}
    order: list[tuple] = []
    stages = rec.get("stages") or []
    want = set(pools or [])
    for st in stages:
        pool = st.get("pool") or ""
        if want and pool not in want:
            continue
        for row, ok in [(r, True) for r in st.get("kept") or []] + [(r, False) for r in st.get("dropped") or []]:
            code = row.get("code")
            if not code:
                continue
            k = (pool, code)
            s = stocks.get(k)
            if s is None:
                s = stocks[k] = {"code": code, "name": row.get("name"), "pool": pool or None, "sources": row.get("sources") or [],
                                 "fields": row.get("fields") or {}, "path": [], "reached": None, "dropped_at": None,
                                 "reason": None, "missing": False, "picked": False, "score": None, "score_parts": None,
                                 "returned": False, "_depth": -1}
                order.append(k)
            if row.get("returned"):
                s["returned"] = True
            label = zh_stage_label(st.get("label"))
            step = {"stage": label, "type": st.get("type"), "passed": ok, "note": row.get("note") or "",
                    "checks": row.get("checks") or []}
            s["path"].append(step)
            if ok:
                s["reached"] = label
                s["_depth"] = st.get("index", 0)
            elif s["dropped_at"] is None:
                s["dropped_at"] = label
                s["reason"] = row.get("note") or ""
                s["missing"] = any(c.get("missing") for c in row.get("checks") or [])
            if st.get("type") == "select":
                s["score"] = row.get("value")
                s["score_parts"] = row.get("score_parts")
    picked = {(p.get("pool") or "", p.get("code")) for p in rec.get("picks") or []}
    picked_codes = {p.get("code") for p in rec.get("picks") or []}
    has_pool = any(st.get("pool") for st in stages)
    out = []
    for k in order:
        s = stocks[k]
        s["picked"] = (k in picked) if has_pool else (k[1] in picked_codes)
        s["status"] = "入选" if s["picked"] else (f"在「{s['dropped_at']}」被淘汰" if s["dropped_at"] else "走完全程")
        out.append(s)
    out.sort(key=lambda s: (not s["picked"], -s["_depth"], -(s["score"] or 0)))
    for s in out:
        s.pop("_depth", None)
    return out


def stock_in_run(rec: dict | None, code: str, pool: str | None = None) -> dict | None:
    if not rec:
        return None
    rows = [s for s in run_by_stock(rec) if s["code"] == code]
    if pool:
        rows = sorted(rows, key=lambda s: s.get("pool") != pool)
    for s in rows:
        return {**s, "run_id": rec.get("id"), "trading_date": rec.get("trading_date"),
                "strategy": (rec.get("strategy") or {}).get("buy")}
    return None


# ---------------- 股池走势：一只票的详情 ----------------
def _vwap_points(points: list[dict], preclose: float | None) -> list[dict]:
    """分时 + 均价线（累计成交额 / 累计成交量；没有成交量时用已有 avg 或留空）。"""
    out, sv, sa = [], 0.0, 0.0
    for p in points or []:
        px = p.get("p")
        if px is None:
            continue
        v, a = p.get("vol"), p.get("amt")
        if isinstance(v, (int, float)) and isinstance(a, (int, float)) and v > 0:
            sv += v
            sa += a
        avg = p.get("avg") or ((sa / sv) if sv > 0 else None)
        out.append({"t": p.get("t"), "p": px, "avg": round(avg, 4) if avg else None,
                    "pct": round((px / preclose - 1) * 100, 3) if preclose else None})
    return out


def _entry_of(engine, code: str, pool: str | None, max_runs: int = 120) -> dict:
    """入池日：最近运行记录里这只票第一次出现在该股池的交易日和当时的价格。"""
    first = None
    for item in engine._run_index()[:max_runs]:
        if pool and not any(p.get("key") == pool for p in item.get("pools") or []):
            continue
        rec = engine.get_run(item["id"])
        if not rec:
            continue
        st0 = next((s for s in rec.get("stages") or [] if s.get("type") == "pool"), None)
        for row in (st0 or {}).get("kept") or []:
            if row.get("code") == code and (not pool or pool in (row.get("sources") or [])):
                d = rec.get("trading_date")
                if d and (first is None or d <= first["date"]):
                    first = {"date": d, "price": (row.get("fields") or {}).get("price"), "run_id": rec.get("id")}
                break
    return first or {}


def _add_trading_days(engine, m, start: str, n: int) -> tuple[str | None, bool]:
    """start 之后第 n 个交易日。已知交易日历不够时按工作日推算（返回是否估算）。"""
    if n <= 0:
        return start, False
    try:
        known = sorted(d for d in m.known_trade_dates() if d > start)
    except Exception:  # noqa: BLE001
        known = []
    if len(known) >= n:
        return known[n - 1], False
    d = _date.fromisoformat(known[-1] if known else start)
    left = n - len(known)
    while left > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            left -= 1
    return d.isoformat(), True


def position_view(engine, m, code: str, price: float | None, day: str) -> dict | None:
    """有持仓时：成本、浮盈、离止盈/止损还差多少、哪天到期卖出（按买入来源股池的覆盖参数）。"""
    from .exits import rule_for
    pos = engine.book.get(code)
    if not pos or not pos.get("quantity"):
        return None
    sell_by = None
    try:      # 按股池绑定的卖出策略（没绑定 = 生效策略），再套这套策略对这个池的覆盖参数
        res = engine.binder().resolve_sell(pos.get("sources"))
        rule = rule_for(res["cfg"], pos, res["pool"])
        sell_by = {k: res[k] for k in ("pool", "strategy_id", "strategy", "by", "bound", "override")}
    except Exception:  # noqa: BLE001
        rule = rule_for(engine.cfg, pos)
    cost = pos.get("avg_cost")
    pnl = (price / cost - 1) * 100 if price and cost else None
    tp, sl = rule.get("take_profit_pct"), rule.get("stop_loss_pct")
    buy = pos.get("first_buy_date")
    try:
        held = m.trading_days_between(buy, day) if buy else None
    except Exception:  # noqa: BLE001
        held = None
    n = int(rule.get("max_hold_days") or 0)
    due, est = (None, False)
    if buy and n > 0:
        due, est = _add_trading_days(engine, m, buy, n)
    return {"quantity": pos.get("quantity"), "avg_cost": cost, "buy_date": buy, "sources": pos.get("sources") or [],
            "hold": pos.get("hold"), "pnl_pct": round(pnl, 3) if pnl is not None else None, "held_days": held,
            "rule": rule, "rule_from": rule.get("from"), "sell_by": sell_by,
            "take_profit_price": round(cost * (1 + tp / 100), 3) if cost and tp else None,
            "stop_loss_price": round(cost * (1 - sl / 100), 3) if cost and sl else None,
            "to_take_profit_pct": round(tp - pnl, 3) if tp and pnl is not None else None,
            "to_stop_loss_pct": round(pnl + sl, 3) if sl and pnl is not None else None,
            "due_date": due, "due_estimated": est, "exit_time": rule.get("exit_time"),
            "due_text": (f"{due}{'（按工作日估算）' if est else ''} {rule.get('exit_time') or ''} 到期卖出" if due
                         else "不按时间卖出（最多持有天数为 0）")}


def pool_stock_detail(engine, code: str, pool: str | None = None, kline: str = "minute") -> dict:
    """股池走势右边的详情：分时（带均价线）/日K、入池日、指标、在买入策略里走到哪一层、持仓。"""
    m = engine.market()
    errors: list[str] = []
    try:
        day = m.latest_trading_date() or today_str()
    except Exception:  # noqa: BLE001
        day = today_str()
    q = {}
    try:
        qs, _, err = m.quotes([code])
        q = (qs or {}).get(code) or {}
        if err and not q:
            errors.append(f"实时行情：{err}")
    except Exception as e:  # noqa: BLE001
        errors.append(f"实时行情：{str(e)[:120]}")
    out: dict = {"code": code, "name": q.get("name") or code, "pool": pool, "date": day, "errors": errors}
    # 分时
    minute = None
    fn = getattr(m, "stock_minute", None)
    if fn is not None:
        try:
            d, src, err = fn(code, day)
        except Exception as e:  # noqa: BLE001
            d, src, err = None, None, str(e)[:160]
        if d:
            pre = d.get("preclose") or q.get("preclose")
            minute = {"date": day, "preclose": pre, "source": src, "points": _vwap_points(d.get("points") or [], pre)}
        else:
            errors.append(f"分时：{err or '没有数据'}")
    out["minute"] = minute
    # 日K
    bars = []
    dfn = getattr(m, "daily", None)
    if dfn is not None:
        try:
            bars, err = dfn(code, day)
        except Exception as e:  # noqa: BLE001
            bars, err = [], str(e)[:160]
        if not bars:
            errors.append(f"日K：{err or '没有数据'}")
    out["daily"] = [{k: b.get(k) for k in ("date", "open", "high", "low", "close", "volume", "amount")} for b in (bars or [])[-60:]]
    # 指标（拿不到的给 None，页面显示「—」）
    price = q.get("price") or (minute["points"][-1]["p"] if minute and minute["points"] else None) or \
        (bars[-1]["close"] if bars else None)
    pre = q.get("preclose") or (minute or {}).get("preclose")
    rows = []
    if bars:
        try:
            from .indicators import series_rows
            rows = series_rows(bars, code, out["name"])
        except Exception:  # noqa: BLE001
            rows = []
    last = rows[-1] if rows else {}
    lim_up = lim_dn = None
    if pre:
        try:
            from .market import limit_info
            li = limit_info(code, out["name"], pre, day)
            lim_up, lim_dn = li.get("limit_up"), li.get("limit_down")
        except Exception:  # noqa: BLE001
            pass
    avg_last = None
    if minute and minute["points"]:
        avg_last = minute["points"][-1].get("avg")
    chg = q.get("change_pct")
    if chg is None and price and pre:
        chg = (price / pre - 1) * 100
    out["indicators"] = [
        {"key": "price", "label": "现价", "value": price},
        {"key": "change_pct", "label": "涨跌幅 %", "value": round(chg, 3) if isinstance(chg, (int, float)) else None},
        {"key": "amount", "label": "成交额（亿）", "value": round(q["amount"] / 1e8, 3) if q.get("amount") else None},
        {"key": "turnover", "label": "换手率 %", "value": last.get("turnover")},
        {"key": "turnover_ratio_5d", "label": "换手是 5 日均值的倍数", "value": last.get("turnover_ratio_5d")},
        {"key": "vwap_dev_pct", "label": "偏离均价 %", "value": round((price / avg_last - 1) * 100, 3) if price and avg_last else None},
        {"key": "limit_up", "label": "涨停价", "value": lim_up},
        {"key": "limit_down", "label": "跌停价", "value": lim_dn},
        {"key": "board_count", "label": "连板数", "value": last.get("board_count")},
        {"key": "rsi", "label": "RSI(6)", "value": last.get("rsi6")},
        {"key": "ma_bull", "label": "均线多头排列", "value": last.get("ma_bull")},
    ]
    for it in out["indicators"]:
        if isinstance(it["value"], float):
            it["value"] = round(it["value"], 4)
    # 入池日、入池后涨跌
    ent = _entry_of(engine, code, pool)
    if ent.get("price") and price:
        ent["since_pct"] = round((price / ent["price"] - 1) * 100, 3)
    out["entry"] = ent or None
    # 在买入策略里走到哪一层（最近一次含这只票的运行）
    found = None
    for item in engine._run_index()[:30]:
        rec = engine.get_run(item["id"])
        found = stock_in_run(rec, code, pool)
        if found:
            break
    out["funnel"] = found
    out["position"] = position_view(engine, m, code, price, day)
    return out


# ---------------- 公式校验 / 预览、形态信号预览 ----------------
def formula_check(expr: str, side: str = "buy") -> dict:
    from .expr import Expr, ExprError, POSITION_FIELDS
    try:
        e = Expr(expr)
    except ExprError as err:
        return {"ok": False, "error": str(err)}
    names = sorted({n for n, _ in e.refs})
    if side == "buy":
        pos = [n for n in names if n in POSITION_FIELDS]
        if pos:
            return {"ok": False, "error": f"持仓字段只能在卖出策略里用：{', '.join(pos)}"}
    return {"ok": True, "fields": e.fields(), "needs": sorted(e.needs())}


def _today_candidates(engine, limit: int) -> tuple[list[Candidate], dict | None, int]:
    rec = engine.get_run("latest")
    if not rec:
        return [], None, 0
    st0 = next((s for s in rec.get("stages") or [] if s.get("type") == "pool"), None)
    rows = (st0 or {}).get("kept") or []
    cands = [Candidate(r["code"], r.get("name") or r["code"], list(r.get("sources") or []), dict(r.get("fields") or {}))
             for r in rows[:limit] if r.get("code")]
    return cands, rec, len(rows)


def _pools_for_factor(engine, spec: dict) -> list[str]:
    """试算用的股池：先找「包含这个因子的那套买入策略」显式绑定的股池；
    没有显式绑定（说明它按全局生效策略作用于所有池）时，退回当前生效策略的那一组。"""
    all_pools = [str(p) for p in ((engine.cfg.get("pool") or {}).get("pools") or [])]
    # 只在 pool.bind 里出现、没进 pool.pools 的股池（例如竞价母池）也算，不然试算看不到它
    for k in ((engine.cfg.get("pool") or {}).get("bind") or {}):
        if str(k) not in all_pools:
            all_pools.append(str(k))
    if not all_pools:
        return []
    kind = str((spec or {}).get("kind") or "")
    try:
        binder = engine.binder()
        act = binder.active_doc("buy") or {}
        items = (engine.strategies.list("buy") or {}).get("items") or []
        cands = [it for it in items if it.get("id")]
        # 活跃策略优先，其余按列表顺序
        cands.sort(key=lambda it: 0 if it.get("id") == act.get("id") else 1)
        for it in cands:
            doc = engine.strategies.get("buy", it["id"]) or {}
            kinds = {f.get("kind") for l in (doc.get("content") or {}).get("layers") or []
                     for f in l.get("factors") or []}
            if kind not in kinds:
                continue
            # 只认「明确绑定到这套策略」的池；没显式绑定的池会跟着全局生效策略走，不算它的专属股池
            bind = (engine.cfg.get("pool") or {}).get("bind") or {}
            explicit = [k for k in all_pools
                        if str((bind.get(k) or {}).get("buy") or "") == it["id"]]
            if explicit:
                return explicit
            break
        order = binder.buy_priority(all_pools)
        for g in binder.buy_groups(order):
            if g.get("active"):
                return [p for p in g["pools"] if p in all_pools] or all_pools
    except Exception:  # noqa: BLE001
        pass
    return all_pools


def _active_buy_pools(engine) -> list[str]:
    """当前生效买入策略对应的股池：绑定了它的池优先；一个都没绑就用配置里的全部池。"""
    all_pools = [str(p) for p in ((engine.cfg.get("pool") or {}).get("pools") or [])]
    if not all_pools:
        return []
    try:
        binder = engine.binder()
        order = binder.buy_priority(all_pools)
        for g in binder.buy_groups(order):
            if g.get("active"):
                return [p for p in g["pools"] if p in all_pools] or all_pools
    except Exception:  # noqa: BLE001
        pass
    return all_pools


def _fresh_candidates(engine, pool_keys: list[str], day: str, limit: int):
    """按股池现拉候选（不用上一次运行的记录，避免换了策略还在拿旧股池试算）。"""
    srcs = engine.sources()
    merged: dict[str, Candidate] = {}
    used: list[dict] = []
    for key in pool_keys:
        sname, _, pkey = key.partition(":")
        src = srcs.get(sname)
        if src is None:
            used.append({"key": key, "count": 0, "error": f"数据源 {sname} 没启用或不存在"})
            continue
        try:
            pr = src.get_pool(pkey, day)
        except Exception as e:  # noqa: BLE001
            used.append({"key": key, "count": 0, "error": str(e)[:120]})
            continue
        if pr.error:
            used.append({"key": key, "count": 0, "error": pr.error})
            continue
        used.append({"key": key, "name": pr.name, "count": len(pr.stocks)})
        for s in pr.stocks:
            code = s.get("code")
            if not code:
                continue
            c = merged.get(code)
            if c is None:
                c = merged[code] = Candidate(code, s.get("name") or "", [], {})
            if key not in c.sources:
                c.sources.append(key)
            if not c.name and s.get("name"):
                c.name = s["name"]
            for k, v in (s.get("fields") or {}).items():
                c.fields.setdefault(k, v)
    total = len(merged)
    return list(merged.values())[:limit], used, total


def preview_factor(engine, side: str, spec: dict, limit: int = 200, time_budget: float = 20.0) -> dict:
    """试算一个因子能筛出几只。只算不存，不影响任何记录。

    候选来源：**当前生效买入策略对应的股池，现拉**（换了策略就换股池，不再沿用上一次运行的记录）；
    股池一个都拉不到时，才退回最近一次运行的前池。
    """
    from .factors.base import FactorContext
    from .funnel import build_layers, run_buy_funnel, validate_layers
    layer = {"id": "preview", "name": "预览", "mode": "all", "enabled": True, "factors": [dict(spec, enabled=True)]}
    errs = validate_layers([layer], "buy", engine.root)
    if errs:
        return {"ok": False, "error": "；".join(errs)}
    day = today_str()
    pools = _pools_for_factor(engine, spec)
    cands, used, total = _fresh_candidates(engine, pools, day, limit) if pools else ([], [], 0)
    rec = engine.get_run("latest")
    if cands:
        ok_pools = [u for u in used if not u.get("error")]
        basis = "用当前生效买入策略的股池现拉：" + ("、".join(f"{u['key']} {u['count']} 只" for u in ok_pools) or "无")
        if total > len(cands):
            basis += f"（取前 {len(cands)} 只试算）"
        failed_pools = [u for u in used if u.get("error")]
    else:
        cands, rec, total = _today_candidates(engine, limit)
        if rec is None:
            return {"ok": False, "error": "当前股池没拉到票，也没有运行记录：先运行一次（只选股不交易也行），再预览"}
        day = rec.get("trading_date") or day
        basis = f"股池没拉到，退用 {day} 最近一次运行的前池试算（{len(cands)}/{total} 只）"
        failed_pools = []
    warnings: list[str] = [f"{u['key']} 拉取失败：{u['error']}" for u in failed_pools]
    layers = build_layers([layer], engine.root, "buy")
    m = engine.market()
    t0 = time.monotonic()

    def loader(cs, needs):
        for c in cs:
            if time.monotonic() - t0 > time_budget:
                c.daily_error = c.daily_error or "预览超时，没加载"
                c.minute_error = c.minute_error or "预览超时，没加载"
                continue
            try:
                if "info" in needs and not c.info:
                    c.info, _ = m.stock_info(c.code)
                if "minute" in needs:
                    engine._load_minute(c, m, day)
                if "daily" in needs:
                    engine._load_daily(c, m, day)
            except Exception as e:  # noqa: BLE001
                c.daily_error = c.daily_error or str(e)[:120]
    for c in cands:
        try:
            engine._derive(c, m, day, warnings)
        except Exception:  # noqa: BLE001
            pass
    try:
        engine._enrich_fields(cands, day, warnings)
    except Exception:  # noqa: BLE001
        pass
    ctx = FactorContext(day, engine.root, {"side": "buy", **engine.ext_ctx(day)})
    kept, stages = run_buy_funnel(layers, cands, ctx, loader, lambda c: c.brief(), warnings)
    st = stages[0] if stages else {"kept": [], "dropped": []}
    dropped = st.get("dropped") or []
    miss = [d for d in dropped if any(x.get("missing") for x in d.get("checks") or [])]
    return {"ok": True, "date": day, "run_id": (rec or {}).get("id"), "checked": len(cands), "pool_total": total,
            "passed": len(kept), "failed": len(dropped) - len(miss), "missing": len(miss),
            "examples": [f"{r['code']} {r.get('name') or ''}".strip() for r in (st.get("kept") or [])[:12]],
            "missing_examples": [f"{r['code']}：{r.get('note') or ''}"[:80] for r in miss[:5]],
            "basis": f"{basis}，只算不存",
            "pools": used,
            "warnings": warnings[:5]}


# ---------------- 股池走势左侧列表（入池以来涨跌） ----------------
def _archive_entry(arch, key: str, days_desc: list[str]) -> dict[str, dict]:
    """按存档往回找每只票最近一段连续在池的第一天：{code: {date, price}}。"""
    out: dict[str, dict] = {}
    alive: set[str] | None = None
    for d in days_desc:
        pool = (arch.load(d).get("pools") or {}).get(key)
        if pool is None:
            break      # 这天这个池没有存档：连续段断了
        codes = {s.get("code"): s for s in pool.get("stocks") or []}
        if alive is None:
            alive = set(codes)
        alive = {c for c in alive if c in codes}
        if not alive:
            break
        for c in alive:
            px = ((codes[c].get("fields") or {}).get("price"))
            out[c] = {"date": d, "price": px if isinstance(px, (int, float)) and px > 0 else None}
    return out


def pool_trend(engine, overview, key: str) -> dict:
    """股池走势左侧列表：成分股、现价、涨跌幅、入池日、入池以来涨跌。现价来自首页股池行情；入池日来自股池存档。"""
    if overview is not None and hasattr(overview, "focus"):
        overview.focus(key)      # 告诉后台「现在看的是这个池」，只给它算分时/回看曲线
    d = overview.pool_detail(key) if overview is not None else None
    members = list((d or {}).get("members") or [])
    if not members:
        try:
            arch = engine.pool_archive
            days = arch.days()
            if days:
                pool = (arch.load(days[-1]).get("pools") or {}).get(key) or {}
                members = [{"code": s.get("code"), "name": s.get("name"), "price": None, "change_pct": None}
                           for s in pool.get("stocks") or []]
        except Exception:  # noqa: BLE001
            members = []
    try:
        arch = engine.pool_archive
        entry = _archive_entry(arch, key, list(reversed(arch.days()))[:60])
    except Exception:  # noqa: BLE001
        entry = {}
    rows = []
    for mb in members:
        code = mb.get("code")
        e = entry.get(code) or {}
        px, ep = mb.get("price"), e.get("price")
        since = round((px / ep - 1) * 100, 3) if isinstance(px, (int, float)) and px and ep else None
        rows.append({"code": code, "name": mb.get("name") or "", "price": px, "change_pct": mb.get("change_pct"),
                     "entry_date": e.get("date"), "entry_price": ep, "since_entry_pct": since})
    return {"key": key, "name": (d or {}).get("name") or key, "trading_date": (d or {}).get("trading_date"),
            "pending": d is None, "rows": rows,
            "series": (d or {}).get("series") or [], "intraday": (d or {}).get("intraday"),
            "note": "入池日按股池存档往回找最近一段连续在池的第一天；存档从换包后才开始记，之前的日子没有。拿不到的显示「—」。"}


def industries(engine) -> dict:
    """行业列表：从本地缓存的个股资料里收集（不联网），再加上策略里已经写过的行业名。"""
    from pathlib import Path
    import json
    seen: dict[str, int] = {}
    d = Path(engine.data_dir) / "cache" / "stock_info"
    if d.exists():
        for f in list(d.glob("*.json"))[:6000]:
            try:
                ind = (json.loads(f.read_text(encoding="utf-8")) or {}).get("industry")
            except Exception:  # noqa: BLE001
                continue
            if ind:
                seen[str(ind)] = seen.get(str(ind), 0) + 1
    used: set[str] = set()
    for side in ("buy",):
        try:
            for it in engine.strategies.list(side)["items"]:
                doc = engine.strategies.get(side, it["id"])
                for layer in doc["content"].get("layers") or []:
                    for f in layer.get("factors") or []:
                        p = f.get("params") or {}
                        for k in ("industries_exclude", "industries_only"):
                            v = p.get(k) or []
                            for x in ([v] if isinstance(v, str) else v):
                                used.add(str(x))
        except Exception:  # noqa: BLE001
            pass
    items = [{"name": k, "count": v} for k, v in sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))]
    for u in sorted(used - set(seen)):
        items.append({"name": u, "count": 0})
    return {"items": items,
            "note": "行业名来自已经取过资料的个股（本地缓存），要和数据源给的行业名完全一致才会命中。列表里没有的行业，可以在下面手动补一个。"}


# ---------------- 选股结果 · 全部结果：所有运行记录合成一张表（第六轮小修） ----------------
def _tier_of(engine, day: str, cache: dict) -> dict:
    if day not in cache:
        try:
            cache[day] = engine.tiers.load(day) or {}
        except Exception:  # noqa: BLE001
            cache[day] = {}
    return cache[day]


def _row_pools(s: dict) -> list[str]:
    return [s["pool"]] if s.get("pool") else list(s.get("sources") or [])


def all_results(engine, pools: list[str] | None = None, day: str | None = None, run: str | None = None,
                min_tier: str | None = None, offset: int = 0, limit: int = 100, max_runs: int = 300) -> dict:
    """全部结果：每次运行 × 每个股池 × 每只票一行，新的在上面。只读，不改任何记录。

    pools：只看这些股池；day / run：可选的按日期、按某一次运行筛选（默认不筛）；
    min_tier：focus = 只看重点及以上（级别取这只票在那个交易日的分级，分级逻辑和数据不变）。
    summary 是筛选后每个池的汇总：出现在几次运行里、一共进入策略几只次、其中新入池几只次、入选几只次。
    """
    from .vendor.alpha_pool_transition import POOL_LEVELS, POOL_TIER_NAMES
    want = set(pools or [])
    need = POOL_LEVELS.get(min_tier) if min_tier else None
    rows: list[dict] = []
    summary: dict[str, dict] = {}
    runs_seen: list[dict] = []
    tcache: dict = {}
    for item in engine._run_index()[:max_runs]:
        if run and item.get("id") != run:
            continue
        if day and item.get("trading_date") != day:
            continue
        rec = engine.get_run(item["id"])
        if not rec:
            continue
        tdate = rec.get("trading_date") or ""
        tiers = _tier_of(engine, tdate, tcache)
        runs_seen.append({"id": rec.get("id"), "trading_date": tdate, "started_at": rec.get("started_at"),
                          "origin": rec.get("origin"), "mode_label": rec.get("mode_label")})
        new_by_pool = {}
        for pr in rec.get("pool_runs") or []:
            new_by_pool[pr.get("pool")] = pr.get("new")
        counted_runs: set = set()
        for s in run_by_stock(rec):
            ps = _row_pools(s)
            if want and not (set(ps) & want):
                continue
            t = (tiers.get(s["code"]) or {}).get("tier")
            if need is not None and (t is None or POOL_LEVELS.get(t, -1) < need):
                continue
            passed = [p for p in s.get("path") or [] if p.get("passed")]
            entered = bool(passed) and (passed[0].get("type") == "pool")
            pool_key = s.get("pool") or (ps[0] if len(ps) == 1 else None)
            rows.append({"run_id": rec.get("id"), "trading_date": tdate, "time": (rec.get("started_at") or "")[11:16],
                         "origin": rec.get("origin"), "mode_label": rec.get("mode_label"),
                         "pool": s.get("pool"), "pools": ps, "code": s["code"], "name": s.get("name"),
                         "layer": len(passed), "layers": len(s.get("path") or []), "reached": s.get("reached"),
                         "dropped_at": s.get("dropped_at"), "reason": s.get("reason"), "entered": entered,
                         "picked": s.get("picked"), "status": s.get("status"), "returned": s.get("returned"),
                         "tier": t, "tier_name": POOL_TIER_NAMES.get(t) if t else None})
            for k in (ps if not want else [p for p in ps if p in want]):
                sm = summary.setdefault(k, {"pool": k, "runs": 0, "entered": 0, "picked": 0, "new": 0, "new_missing": 0})
                if (rec.get("id"), k) not in counted_runs:
                    counted_runs.add((rec.get("id"), k))
                    sm["runs"] += 1
                    # 第六轮之前的旧记录没有按股池的 pool_runs，也就没有「新入池」这一项：不当成 0，单独记次数
                    if new_by_pool.get(k) is not None:
                        sm["new"] += new_by_pool[k] or 0
                    else:
                        sm["new_missing"] += 1
                if entered:
                    sm["entered"] += 1
                if s.get("picked") and (s.get("pool") in (None, k)):
                    sm["picked"] += 1
    for sm in summary.values():
        if sm["new_missing"] >= sm["runs"]:
            sm["new"] = None  # 全是旧记录：没有新入池数据（网页显示「—」），不是 0
    total = len(rows)
    return {"total": total, "offset": offset, "limit": limit, "rows": rows[offset:offset + limit],
            "more": offset + limit < total, "summary": list(summary.values()), "runs": runs_seen,
            "latest_run": runs_seen[0]["id"] if runs_seen else None,
            "tier_names": POOL_TIER_NAMES}


def stock_history(engine, code: str, max_runs: int = 300) -> dict:
    """点一只票：它在每次运行里走到哪、跨天的分级变化（哪天升到哪级、哪天降级）、最近一天的级别（可手动调）。"""
    from .vendor.alpha_pool_transition import POOL_LEVELS, POOL_TIER_NAMES
    tdir = engine.tiers.dir
    days = sorted(p.stem for p in tdir.glob("*.json")) if tdir.exists() else []
    hist, prev, name = [], None, None
    for d in days:
        e = (engine.tiers.load(d) or {}).get(code)
        if not e:
            continue
        name = name or e.get("name")
        t = e.get("tier")
        change = None
        if prev is None:
            change = f"进入{POOL_TIER_NAMES.get(t, t)}"
        elif t != prev:
            up = POOL_LEVELS.get(t, 0) > POOL_LEVELS.get(prev, 0)
            change = f"{'升到' if up else '降到'}{POOL_TIER_NAMES.get(t, t)}（原来是{POOL_TIER_NAMES.get(prev, prev)}）"
        hist.append({"date": d, "tier": t, "tier_name": POOL_TIER_NAMES.get(t, t), "change": change,
                     "sources": e.get("sources") or [], "log": e.get("trigger_log") or []})
        prev = t
    runs = []
    for item in engine._run_index()[:max_runs]:
        rec = engine.get_run(item["id"])
        if not rec:
            continue
        for s in run_by_stock(rec):
            if s["code"] == code:
                name = name or s.get("name")
                runs.append({"run_id": rec.get("id"), "trading_date": rec.get("trading_date"),
                             "time": (rec.get("started_at") or "")[11:16], "origin": rec.get("origin"),
                             "pools": _row_pools(s), "status": s.get("status"), "picked": s.get("picked"),
                             "path": s.get("path")})
    last = hist[-1] if hist else None
    return {"code": code, "name": name, "tiers": hist, "runs": runs, "tier_names": POOL_TIER_NAMES,
            "current": {"date": last["date"], "tier": last["tier"]} if last else None}


# 首页自动刷新：同一只票的详情几秒内只算一次，多个页面同时打开时共用，不把行情源打爆
_PS_CACHE: dict = {}
_PS_LOCK = __import__("threading").Lock()


def pool_stock_detail_cached(engine, code: str, pool: str | None, ttl: float = 5.0):
    import time as _t
    key = (code, pool or "")
    with _PS_LOCK:
        hit = _PS_CACHE.get(key)
        if hit and _t.monotonic() - hit[0] < ttl:
            return hit[1]
    d = pool_stock_detail(engine, code, pool)
    with _PS_LOCK:
        if len(_PS_CACHE) > 200:
            _PS_CACHE.clear()
        _PS_CACHE[key] = (_t.monotonic(), d)
    return d
