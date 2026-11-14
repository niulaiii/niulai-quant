"""简单回测：用日K回放「因子漏斗 + 退出规则」。

做法（只用日K，尽量避免未来函数）：
- 股票范围：你给的代码列表（默认：自选股 + 本地缓存里出现过的股池成分）。gushi.in 只给最近两天的股池，
  所以回测不重放“当天是否入池”，只回放因子和退出规则。
- 第 D 天收盘后计算因子（price=收盘价、change_rate=当日涨幅、open_rise=开盘涨幅，日K字段正常可用），
  打分取前 top_n，D+1 开盘价买入（一字涨停买不进则放弃）。
- 需要分时的因子（均价线、回落、成交额等）日K无法重现，默认跳过并在结果里列出；市值字段没有历史值，同样跳过。
- 退出：T+1 起每天先看止损（最低价触及，按 min(开盘, 止损价) 成交），再看止盈（最高价触及，按 max(开盘, 止盈价)），
  持有满 max_hold_days 按当天收盘卖。同一天既碰止损又碰止盈按止损算（保守）。人工保留无法回测。
- 费用、滑点用 [paper] 的设置（Alpha 算法）。
- 可以自选买入策略和卖出策略（已保存的策略或模板），默认用当前生效的那套；结果里记下用的是哪两套，方便对比。
- 实验性因子（竞价、封单、主力资金、龙虎榜等）没有历史数据，标「回测不可用」并跳过。
"""
from __future__ import annotations

from datetime import date as _date, timedelta

from .datasources.watchlist import WatchlistSource
from .exits import rule_for
from .factors.base import FactorContext, apply_missing, factor_label
from .funnel import build_layers, combine, eval_layer, layers_of
from .market import Market, limit_info
from .model import Candidate
from .util import round2, round_buy_qty
from .vendor.alpha_fees import calc_buy_cost, calc_sell_cost, closed_trade_stats

MAX_CODES = 60


def default_universe(engine) -> list[str]:
    codes: list[str] = []
    for s in engine.sources().values():
        if isinstance(s, WatchlistSource):
            codes += [c for c, _ in s.entries()]
    cache = engine.data_dir / "cache"
    import json
    for f in sorted(cache.glob("*/pools/*/*.json"))[-40:]:
        try:
            v = json.loads(f.read_text(encoding="utf-8"))
            rows = v if isinstance(v, list) else ((v.get("data") or {}).get("stock_list") or [])
            for r in rows:
                codes.append(str(r.get("code") or r.get("stock_code")))
        except Exception:  # noqa: BLE001
            continue
    out = []
    for c in codes:
        if c and len(c) == 6 and c not in out:
            out.append(c)
    return out[:MAX_CODES]


def strategy_choices(engine) -> dict:
    """回测页的下拉：每一侧的已保存策略（生效的排第一）和模板，默认选当前生效的。"""
    out = {}
    for side in ("buy", "sell"):
        lst = engine.strategies.list(side)
        act = (lst.get("active") or {}).get("id")
        opts = [{"ref": f"s:{it['id']}", "kind": "strategy", "id": it["id"],
                 "name": it["name"] + ("（正在生效）" if it["active"] else ""), "active": it["active"]} for it in lst["items"]]
        opts += [{"ref": f"t:{t['id']}", "kind": "template", "id": t["id"], "name": f"模板：{t['title']}", "active": False}
                 for t in engine.strategies.templates(side)]
        out[side] = {"default": f"s:{act}" if act else (opts[0]["ref"] if opts else ""), "options": opts}
    return out


def resolve_strategy(engine, side: str, ref: str | None) -> tuple[dict, dict]:
    """ref：空 = 当前生效的；s:<id> 或 <id> = 已保存的策略；t:<id> = 模板。返回 (策略内容, 记录用的描述)。"""
    from .strategies import normalize_content
    S = engine.strategies
    ref = (ref or "").strip()
    if not ref:
        act = S.active_names()[side]
        if not act.get("id"):
            from .strategies import content_from_cfg
            return content_from_cfg(engine.cfg, side), {"kind": "config", "id": None, "name": "当前配置"}
        ref = f"s:{act['id']}"
    kind, _, rid = ref.partition(":") if ref[:2] in ("s:", "t:") else ("s", ":", ref)
    try:
        if kind == "t":
            t = S.template(side, rid)
            return normalize_content(side, t["content"], content_from_active(engine, side)), \
                {"kind": "template", "id": rid, "name": f"模板：{t['title']}"}
        d = S.get(side, rid)
    except KeyError:
        raise ValueError(f"找不到{'买入' if side == 'buy' else '卖出'}{'模板' if kind == 't' else '策略'}：{rid}") from None
    return d["content"], {"kind": "strategy", "id": rid, "name": d["name"], "active": bool(d.get("active"))}


def content_from_active(engine, side: str) -> dict:
    from .strategies import content_from_cfg
    return content_from_cfg(engine.cfg, side)


def backtest_cfg(engine, buy_ref: str | None, sell_ref: str | None) -> tuple[dict, dict]:
    """在当前配置的副本上套用选中的买入/卖出策略（不写配置文件、不改生效策略）。"""
    import copy
    from .strategies import cfg_patch
    cfg = copy.deepcopy(engine.cfg)
    used = {}
    for side, ref in (("buy", buy_ref), ("sell", sell_ref)):
        content, desc = resolve_strategy(engine, side, ref)
        cfg.update(cfg_patch(cfg, side, content))
        used[side] = desc
    return cfg, used


def _backtest_unusable(spec: dict, fac) -> str | None:
    """因子目录里标了「回测不可用」的（实验性因子等）：返回原因。"""
    from .catalog import factor_meta
    meta = factor_meta(spec.get("kind") or "", type(fac))
    if meta.get("backtest") == "no":
        if meta.get("experimental"):
            return "回测不可用（实验性因子，没有历史数据）"
        return "回测不可用" + (f"：{meta['backtest_note']}" if meta.get("backtest_note") else "")
    return None


def run_backtest(engine, start: str, end: str, codes: list[str] | None = None, names: dict | None = None,
                 buy_strategy: str | None = None, sell_strategy: str | None = None, *,
                 cfg_used: tuple[dict, dict] | None = None, eligible: dict[str, set] | None = None,
                 pool: str | None = None, bars_cache: dict | None = None, max_codes: int = MAX_CODES) -> dict:
    """eligible：按股池回测时每天可选的股票（股池存档里那天的成分股）；某天不在 eligible 里 = 那天没有存档，不选股。
    pool：持仓记成来自这个股池（卖出时套用这个池的覆盖参数）。cfg_used：直接给定 (配置, 策略说明)。"""
    cfg, strategy_used = cfg_used or backtest_cfg(engine, buy_strategy, sell_strategy)
    codes = [c.strip() for c in (codes or default_universe(engine)) if c.strip()][:max_codes]
    names = names or {}
    if not codes:
        raise ValueError("没有可回测的股票：请填代码，或先配置自选股")
    m: Market = engine.market()
    # 多拉一些天数保证均线和退出有数据
    end_fetch = (_date.fromisoformat(end) + timedelta(days=20)).isoformat()
    bars_by: dict[str, list] = {}
    errors = []
    lookback = int(cfg["market"].get("daily_lookback_days", 150))
    for c in codes:
        if bars_cache is not None and c in bars_cache:
            if bars_cache[c]:
                bars_by[c] = bars_cache[c]
            continue
        full = []
        for s in m._ordered(cfg["market"].get("daily_sources", []), "daily"):
            try:
                st = (_date.fromisoformat(start) - timedelta(days=lookback)).isoformat()
                full = [b for b in s.get_daily(c, st, end_fetch) if b.get("close")]
                if full:
                    break
            except Exception as e:  # noqa: BLE001
                errors.append(f"{c}: {str(e)[:120]}")
        if full:
            bars_by[c] = full
        if bars_cache is not None:
            bars_cache[c] = full
    if not bars_by:
        raise RuntimeError("日K都没拉到：" + "；".join(errors[:5]))
    # 买入漏斗：逐层回放，层内跳过日K重现不了的因子，按 all/any 组合；整层都跳过的层放行
    root = engine.root
    used, skipped, layers_bt = [], [], []
    for bl in build_layers(layers_of(cfg, "buy"), root, "buy"):
        if not bl.spec.get("enabled", True):
            continue
        facs = []
        for f in bl.factors:
            spec, fac = f.spec, f.fac
            if not spec.get("enabled", True):
                continue
            if fac is None:
                skipped.append({"label": spec.get("kind"), "reason": f.error})
            elif "minute" in fac.needs():
                skipped.append({"label": f.label, "reason": "需要分时，日K回测无法重现"})
            elif spec.get("kind") == "range" and (spec.get("params") or {}).get("field") == "market_value":
                skipped.append({"label": f.label, "reason": "没有历史市值数据"})
            elif spec.get("kind") == "min_hits":
                skipped.append({"label": f.label, "reason": "回测不重放股池命中次数"})
            elif spec.get("kind") in ("external", "http", "ext_field"):
                skipped.append({"label": f.label, "reason": "外部因子没有历史结果"})
            elif spec.get("kind") == "expr" and (fac.needs() & {"pool"}):
                skipped.append({"label": f.label, "reason": "用到股池字段，回测没有"})
            elif _backtest_unusable(spec, fac):
                skipped.append({"label": f.label, "reason": _backtest_unusable(spec, fac)})
            else:
                facs.append(f)
                used.append((spec, fac))
        if facs:
            layers_bt.append((bl.mode, facs))
    # 卖出因子链：止损/止盈/持有天数按日内高低价精确回放；其余不需要分时的卖出层按收盘价检查
    sell_layers = [bl for bl in build_layers(layers_of(cfg, "sell"), root, "sell") if bl.spec.get("enabled", True)]

    def _sell_param(kind, pname, rule_key, rule):
        for bl in sell_layers:
            for f in bl.active():
                if f.spec.get("kind") == kind:
                    v = f.fac.num(pname)
                    return v if v is not None else rule.get(rule_key)
        return None
    extra_sell = []
    for bl in sell_layers:
        kinds = {f.spec.get("kind") for f in bl.active()}
        if not kinds or kinds <= {"stop_loss", "take_profit", "hold_days", "time_reached"}:
            continue
        bad = [f for f in bl.active() if _backtest_unusable(f.spec, f.fac)]
        if "minute" in bl.needs() or kinds & {"external", "http", "ext_field"}:
            skipped.append({"label": f"卖出层「{bl.spec['name']}」", "reason": "需要分时或外部结果，日K回测无法重现"})
        elif bad:
            skipped.append({"label": f"卖出层「{bl.spec['name']}」", "reason": f"{bad[0].label}：{_backtest_unusable(bad[0].spec, bad[0].fac)}"})
        else:
            extra_sell.append(bl)
    dates = sorted({b["date"] for bs in bars_by.values() for b in bs if start <= b["date"] <= end})
    all_dates = sorted({b["date"] for bs in bars_by.values() for b in bs})
    idx_of = {c: {b["date"]: i for i, b in enumerate(bs)} for c, bs in bars_by.items()}
    paper = cfg["paper"]
    cash = float(paper["initial_cash"])
    positions: dict[str, dict] = {}
    trades, closed, nav, signals = [], [], [], []
    top_n = int(cfg["select"].get("top_n", 1))
    weights = cfg["select"].get("weights") or {}
    pending: list[str] = []
    no_archive: list[str] = []

    for d in [x for x in all_dates if x >= start and x <= end_fetch]:
        in_range = d <= end
        # 1) 按昨天的信号开盘买入
        for code in pending:
            i = idx_of[code].get(d)
            if i is None or code in positions:
                continue
            b, pb = bars_by[code][i], bars_by[code][i - 1] if i > 0 else None
            if not pb:
                continue
            li = limit_info(code, names.get(code, ""), pb["close"], d)
            if li["limit_up"] and b["open"] >= li["limit_up"] - 1e-9 and b["low"] >= li["limit_up"] - 1e-9:
                trades.append({"date": d, "symbol": code, "side": "buy", "skipped": "一字涨停买不进"})
                continue
            qty = round_buy_qty(code, float(cfg["sizing"]["per_stock_amount"]) / b["open"])
            if qty <= 0:
                continue
            fill, fee = calc_buy_cost(b["open"], qty, paper)
            if fill * qty + fee > cash:
                trades.append({"date": d, "symbol": code, "side": "buy", "skipped": "资金不足"})
                continue
            cash -= fill * qty + fee
            positions[code] = {"symbol": code, "name": names.get(code, code), "quantity": qty, "cost": fill * qty + fee,
                               "avg_cost": (fill * qty + fee) / qty, "buy_date": d, "held": 0,
                               "sources": [pool] if pool else []}
            trades.append({"date": d, "symbol": code, "side": "buy", "price": round(fill, 3), "quantity": qty, "fee": fee})
        pending = []
        # 2) 退出（T+1）
        for code, p in list(positions.items()):
            i = idx_of[code].get(d)
            if i is None or d <= p["buy_date"]:
                continue
            p["held"] += 1
            b = bars_by[code][i]
            rule = rule_for(cfg, p)
            sl = _sell_param("stop_loss", "pct", "stop_loss_pct", rule)
            tp = _sell_param("take_profit", "pct", "take_profit_pct", rule)
            mh = int(_sell_param("hold_days", "min_days", "max_hold_days", rule) or 0)
            base = p["avg_cost"]
            px, why = None, ""
            if sl and b["low"] <= base * (1 - sl / 100):
                px, why = min(b["open"], base * (1 - sl / 100)), "止损"
            elif tp and b["high"] >= base * (1 + tp / 100):
                px, why = max(b["open"], base * (1 + tp / 100)), "止盈"
            elif mh and p["held"] >= mh:
                px, why = b["close"], "到期"
            elif extra_sell:
                pc = Candidate(code, p.get("name") or code, p.get("sources") or [], {})
                pc.daily = bars_by[code][: i + 1]
                for k in ("close", "ma5", "ma10", "ma20", "ma60", "vol_ratio_5d"):
                    v = pc.daily_at(k, 0)
                    if v is not None:
                        pc.fields[k] = v
                hi = max(x["high"] for x in bars_by[code][: i + 1] if x["date"] > p["buy_date"])
                pc.fields.update(price=b["close"], pnl_pct=(b["close"] / base - 1) * 100, held_days=float(p["held"]),
                                 avg_cost=base, quantity=float(p["quantity"]), sellable=float(p["quantity"]), now_min=900.0,
                                 high_since_buy=hi, drawdown_since_buy_pct=(hi - b["close"]) / hi * 100)
                sctx = FactorContext(d, root, {"side": "sell", "rule": rule, "now_hm": "15:00"})
                for bl in extra_sell:
                    ok, _ = eval_layer(bl, pc, sctx)
                    if ok:
                        px, why = b["close"], bl.spec["name"]
                        break
            if px is None and not in_range and d == all_dates[-1]:
                px, why = b["close"], "回测结束平仓"
            if px is None:
                continue
            fill, fee = calc_sell_cost(px, p["quantity"], paper)
            proceeds = fill * p["quantity"] - fee
            cash += proceeds
            pnl = round2(proceeds - p["cost"])
            closed.append({"symbol": code, "buy_date": p["buy_date"], "sell_date": d, "reason": why, "pnl": pnl,
                           "pnl_pct": round(pnl / p["cost"] * 100, 2), "held_days": p["held"]})
            trades.append({"date": d, "symbol": code, "side": "sell", "price": round(fill, 3), "quantity": p["quantity"],
                           "fee": fee, "reason": why, "pnl": pnl})
            del positions[code]
        # 3) 收盘后选股（只在回测区间内）
        if in_range and eligible is not None and d not in eligible:
            no_archive.append(d)
        elif in_range:
            cands = []
            allowed = eligible.get(d) if eligible is not None else None
            for code, bs in bars_by.items():
                if allowed is not None and code not in allowed:
                    continue
                i = idx_of[code].get(d)
                if i is None or i == 0:
                    continue
                b, pb = bs[i], bs[i - 1]
                c = Candidate(code, names.get(code, code), [pool] if pool else ["回测"], {})
                c.fields.update(price=b["close"], open_price=b["open"], change_rate=(b["close"] / pb["close"] - 1) * 100,
                                open_rise=(b["open"] / pb["close"] - 1) * 100, hit_count=1.0)
                li = limit_info(code, c.name, pb["close"], d)
                c.fields.update(li)
                if li["limit_up"]:
                    c.fields["limit_gap_pct"] = (li["limit_up"] - b["close"]) / pb["close"] * 100
                c.daily = bs[: i + 1]
                for k in ("close", "ma5", "ma10", "ma20", "ma60", "vol_ratio_5d"):
                    v = c.daily_at(k, 0)
                    if v is not None:
                        c.fields[k] = v
                cands.append(c)
            ctx = FactorContext(d, root, {"side": "buy"})
            for mode, facs in layers_bt:
                cands = [c for c in cands if combine(mode, [bool(apply_missing(f.fac.evaluate(c, ctx), f.spec.get("params")).passed)
                                                            for f in facs])]
            engine._rank_scores(cands, weights)
            cands.sort(key=lambda c: -c.fields.get("score", 0))
            picks = [c for c in cands if c.code not in positions][:top_n]
            if picks:
                signals.append({"date": d, "picks": [c.code for c in picks], "passed": len(cands)})
            pending = [c.code for c in picks]
        # 4) 净值
        mv = 0.0
        for code, p in positions.items():
            i = idx_of[code].get(d)
            last = bars_by[code][i]["close"] if i is not None else p["avg_cost"]
            mv += last * p["quantity"]
        nav.append({"date": d, "equity": round2(cash + mv)})
    init = float(paper["initial_cash"])
    peak, mdd = 0.0, 0.0
    for pt in nav:
        peak = max(peak, pt["equity"])
        if peak:
            mdd = max(mdd, (peak - pt["equity"]) / peak * 100)
    stats = closed_trade_stats([c["pnl"] for c in closed])
    final = nav[-1]["equity"] if nav else init
    return {"start": start, "end": end, "strategy": strategy_used, "pool": pool,
            **({"archive_days": sorted(eligible), "no_archive_days": no_archive} if eligible is not None else {}),
            "codes": list(bars_by), "missing_codes": [c for c in codes if c not in bars_by],
            "factors_used": [factor_label(s, f) for s, f in used], "factors_skipped": skipped,
            "summary": {**stats, "initial_cash": init, "final_equity": final, "total_return_pct": round((final / init - 1) * 100, 2),
                        "max_drawdown_pct": round(mdd, 2), "signal_days": len(signals), "trading_days": len(dates)},
            "nav": nav, "trades": trades, "closed": closed, "signals": signals[-200:], "errors": errors[:20],
            "notes": ["D 日收盘后选股、D+1 开盘买入；日K不含分时，需要分时的因子已跳过",
                      "同一天既触及止损又触及止盈按止损计（保守）；人工保留无法回测",
                      "股票范围固定，存在幸存者偏差；结果只用于比较参数，不代表实盘收益"]}


# ---------------- 按股池回测 ----------------
def pool_strategy_cfg(engine, pool: str, buy_ref: str | None = None, sell_ref: str | None = None) -> tuple[dict, dict]:
    """某个股池回测用的配置：手动指定的优先，否则用这个池绑定的策略（[pool.bind]），都没有就用生效的那套。"""
    import copy
    from .poolbind import buy_cfg, sell_cfg
    binder = engine.binder()
    cfg = copy.deepcopy(engine.cfg)
    used = {}
    for side, ref in (("buy", buy_ref), ("sell", sell_ref)):
        if ref:
            content, desc = resolve_strategy(engine, side, ref)
            desc = {**desc, "by": "手动指定"}
        else:
            st = binder.strategy_for_pool(side, pool)
            content = st["content"]
            desc = {"kind": "strategy" if st.get("id") else "config", "id": st.get("id"), "name": st.get("name"),
                    "by": st["by"], "bound": st["bound"]}
        cfg = buy_cfg(cfg, content) if side == "buy" else sell_cfg(cfg, content)
        used[side] = desc
    return cfg, used


def pool_choices(engine) -> dict:
    """回测页：可以按哪些股池回测（有存档的，加上配置里的），每个池绑定的策略和存档天数。"""
    arc = engine.pool_archive.summary()
    by = {p["pool"]: p for p in arc["pools"]}
    binder = engine.binder()
    keys = list(dict.fromkeys(list(engine.cfg["pool"]["pools"]) + list(by)))
    out = []
    for k in keys:
        a = by.get(k) or {}
        out.append({"pool": k, "title": a.get("title") or "", "archive_days": a.get("days", 0), "first": a.get("first"),
                    "last": a.get("last"), "configured": k in engine.cfg["pool"]["pools"],
                    "buy": binder.strategy_for_pool("buy", k).get("name"), "sell": binder.strategy_for_pool("sell", k).get("name")})
    return {"pools": out, "days": arc["days"]}


def run_pool_backtest(engine, start: str, end: str, pools: list[str], strategies: dict | None = None,
                      max_codes: int = 120) -> dict:
    """按股池回测：每个池用存档里每天的成分股，按它绑定的策略（或 strategies[池] = {buy, sell} 手动指定）单独回放，
    每个池一份初始资金，结果按股池分开统计。没有存档的日子不选股，写在 no_archive_days 里，不拿别的日子补。"""
    pools = [p for p in (pools or []) if p]
    if not pools:
        raise ValueError("请至少选一个股池")
    strategies = strategies or {}
    arc = engine.pool_archive
    bars_cache: dict = {}
    results, problems = [], []
    for key in pools:
        members = arc.members(key, start, end)
        if not members:
            problems.append({"pool": key, "error": f"{start} ~ {end} 没有这个股池的存档，没法回测（存档从开始记录那天起才有）"})
            continue
        names: dict[str, str] = {}
        count: dict[str, int] = {}
        for d, rows in members.items():
            for r in rows:
                names.setdefault(r["code"], r.get("name") or r["code"])
                count[r["code"]] = count.get(r["code"], 0) + 1
        codes = sorted(count, key=lambda c: -count[c])
        cut = codes[max_codes:]
        codes = codes[:max_codes]
        eligible = {d: {r["code"] for r in rows} & set(codes) for d, rows in members.items()}
        # 新入池规则（和实盘一致，见 poolentry.py）：按日（默认）时每天存档里的成分股都是当天的新入池；
        # 跨日连续时，前一个有存档的交易日已经在池里的票不算新入池，当天不选。
        from .poolentry import entry_mode, entry_scope
        if entry_mode(engine.cfg, key) == "new" and entry_scope(engine.cfg, key) == "continuous":
            prev: set = set()
            for d in sorted(eligible):
                cur = set(eligible[d])
                eligible[d] = cur - prev
                prev = cur
        st = strategies.get(key) or {}
        try:
            cfg_used = pool_strategy_cfg(engine, key, st.get("buy"), st.get("sell"))
            r = run_backtest(engine, start, end, codes, names, cfg_used=cfg_used, eligible=eligible, pool=key,
                             bars_cache=bars_cache, max_codes=max_codes)
        except (ValueError, RuntimeError) as e:
            problems.append({"pool": key, "error": str(e)})
            continue
        if cut:
            r.setdefault("notes", []).append(f"成分股太多，只取入池天数最多的 {max_codes} 只，另有 {len(cut)} 只没回测")
        r["notes"] = [n for n in r.get("notes", []) if not n.startswith("股票范围固定")] + [
            "股票范围用的是股池存档里每天的真实成分股；没有存档的日子不选股"]
        results.append(r)
    if not results:
        raise ValueError("；".join(f"{p['pool']}：{p['error']}" for p in problems) or "没有可回测的股池")
    total_init = sum(r["summary"]["initial_cash"] for r in results)
    total_final = sum(r["summary"]["final_equity"] for r in results)
    closed = sum(r["summary"].get("closed_count", 0) for r in results)
    wins = sum(r["summary"].get("win_count", 0) for r in results)
    return {"start": start, "end": end, "mode": "pools",
            "pools": [{"pool": r["pool"], "strategy": r["strategy"], "summary": r["summary"],
                       "archive_days": len(r.get("archive_days") or []), "no_archive_days": r.get("no_archive_days") or [],
                       "codes": len(r["codes"]), "factors_skipped": r["factors_skipped"]} for r in results],
            "detail": {r["pool"]: r for r in results}, "problems": problems,
            "summary": {"initial_cash": round2(total_init), "final_equity": round2(total_final),
                        "total_return_pct": round((total_final / total_init - 1) * 100, 2) if total_init else 0,
                        "closed_count": closed, "win_count": wins,
                        "win_rate": round(wins / closed * 100, 2) if closed else 0, "pool_count": len(results)},
            "notes": ["每个股池单独一份初始资金、单独回放；合计 = 各池相加",
                      "一只票同时在几个池里时，每个池各算各的（回测里不做跨池去重）",
                      "股池存档从开始记录那天起才有，之前的日子没有数据"]}
