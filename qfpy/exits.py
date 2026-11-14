"""卖出：卖出因子链 + 退出规则 + 人工保留。

卖出因子链（[sell_funnel]，见 qfpy/funnel.py）对每只持仓逐层检查，第一个触发的层就是卖出原因（decision = 层 id）。
没写 [sell_funnel] 时按 [exits] 生成「止损 → 止盈 → 到期退出」，和原来的固定规则等价：
  stop_loss    浮亏 ≥ stop_loss_pct：每次检查都看，立即卖
  take_profit  浮盈 ≥ take_profit_pct：每次检查都看，立即卖
  time         持有满 max_hold_days 个交易日：到当天 exit_time 卖
规则参数（全局 [exits]，可按股池在 [exits.by_pool."数据源:股池"] 覆盖）是卖出因子参数留空时的取值。

始终生效的规则（写在引擎和风控里，和卖出链怎么配无关）：
  - 含「止损」因子的层永远最先检查，默认不能被人工保留跳过（hold_can_skip_stop_loss = false）
  - T+1：当天买的不卖；实盘只卖「已确认」持仓，卖出数量不超过券商可卖
  - 人工保留：下一次本该自动卖出时跳过并清掉标记（只管一次）；某层可设 hold_skippable = false 禁止跳过
"""
from __future__ import annotations

from pathlib import Path

from .config import EXIT_RULE_KEYS
from .factors.base import FactorContext
from .model import Candidate

DECISION_LABELS = {"stop_loss": "止损", "take_profit": "止盈", "time": "到期退出"}


_UNSET = object()


def rule_for(cfg: dict, pos: dict, pool=_UNSET) -> dict:
    """一只持仓的卖出参数。cfg 是这只票所用卖出策略的配置（引擎用 poolbind.Resolver 选好后传进来）。
    pool 不传时按买入时记下的来源股池、[exits.by_pool] 的配置顺序取第一个配了覆盖的（老行为）；
    pool 传了就只看这个池的覆盖（None = 不套覆盖）。"""
    ex = cfg["exits"]
    rule = {k: ex.get(k) for k in EXIT_RULE_KEYS}
    rule["from"] = "全局"
    by_pool = ex.get("by_pool") or {}
    if pool is _UNSET:
        srcs = set(pos.get("sources") or [])
        pool = next((k for k, ov in by_pool.items() if k in srcs and ov and any(x in EXIT_RULE_KEYS for x in ov)), None)
    ov = by_pool.get(pool) if pool else None
    if ov and any(k in EXIT_RULE_KEYS for k in ov):
        rule.update({k: v for k, v in ov.items() if k in EXIT_RULE_KEYS})
        rule["from"] = pool
    return rule


def hm_to_min(hm: str) -> float:
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


def position_candidate(pos: dict, price: float | None, held_days: int, sellable: int, now_hm: str,
                       fields: dict | None = None) -> Candidate:
    """把一只持仓做成卖出链的候选：持仓字段 + 调用方补的分时/日K字段。"""
    c = Candidate(pos["symbol"], pos.get("name") or pos["symbol"], list(pos.get("sources") or []), dict(fields or {}))
    f = c.fields
    f.update(held_days=float(held_days), avg_cost=pos.get("avg_cost"), quantity=float(pos.get("quantity") or 0),
             sellable=float(sellable), now_min=hm_to_min(now_hm))
    if price:
        f["price"] = float(price)
        if pos.get("avg_cost"):
            f["pnl_pct"] = (price / pos["avg_cost"] - 1) * 100
    return c


def evaluate_chain(cfg: dict, layers, pos: dict, price: float | None, today: str, now_hm: str, held_days: int, sellable: int,
                   cand: Candidate | None = None, root: Path | None = None, extra: dict | None = None,
                   rule: dict | None = None, sell_by: dict | None = None) -> dict:
    """用卖出因子链评估一只持仓。layers 是 funnel.build_layers(..., "sell") 的结果。

    decision 为本次应执行的卖出原因（层 id；None = 不卖）；due_at_exit_time 表示今天到点会卖。
    problems 列出这一次没法正常判断的地方（没有最新价、因子数据缺失、因子出错），引擎会记日志并推钉钉，
    不会悄悄跳过。sell_by 写明按哪个股池的哪套策略在卖。
    """
    from .funnel import eval_sell
    rule = rule or rule_for(cfg, pos)
    out = {"symbol": pos["symbol"], "name": pos["name"], "quantity": pos["quantity"], "sellable": sellable,
           "avg_cost": pos["avg_cost"], "price": price, "held_days": held_days, "rule": rule, "decision": None,
           "decision_label": None, "reason": "", "due_at_exit_time": False, "due": [], "layers": [],
           "hold_skippable": True, "hold": pos.get("hold"), "state": pos.get("state", "confirmed"),
           "problems": [], **({"sell_by": sell_by} if sell_by else {})}
    if not price or not pos.get("avg_cost"):
        out["reason"] = "没有最新价，这一轮没法判断止损/止盈/到点卖出" if not price else "没有成本价，这一轮没法判断卖出"
        out["problems"].append({"kind": "no_price" if not price else "no_cost", "stop_loss": True, "note": out["reason"]})
        return out
    c = cand or position_candidate(pos, price, held_days, sellable, now_hm)
    pnl = c.fields.get("pnl_pct")
    out["pnl_pct"] = round(pnl, 2) if pnl is not None else None
    ctx = FactorContext(today, root, {"side": "sell", "rule": rule, "now_hm": now_hm, "pos": pos, **(extra or {})})
    r = eval_sell(layers, c, ctx, bool(cfg["exits"].get("hold_can_skip_stop_loss", False)))
    out.update(decision=r["decision"], decision_label=r["decision_label"], layers=r["layers"], due=r["due"],
               hold_skippable=r["hold_skippable"], stop_loss=r["stop_loss"])
    for l in r["layers"]:
        for chk in l.get("checks") or []:
            if chk.get("error") or chk.get("missing"):
                out["problems"].append({"kind": "error" if chk.get("error") else "missing", "layer": l.get("name") or l.get("id"),
                                        "stop_loss": bool(l.get("stop_loss")), "factor": chk.get("label"), "note": chk.get("note")})
    out["time_ats"] = sorted({f.fac.at(ctx) for bl in layers if bl.spec.get("enabled", True)
                              for f in bl.active() if f.spec.get("kind") == "time_reached"})
    if r["decision"]:
        out["reason"] = r["reason"]
    elif r["due"]:
        d = min(r["due"], key=lambda x: x["at"])
        out["due_at_exit_time"] = True
        out["due_at"] = d["at"]
        out["reason"] = f"已持有 {held_days} 个交易日，今天 {d['at']} 到期卖出（{d['name']}）"
    else:
        mh = int(rule.get("max_hold_days") or 0)
        out["reason"] = f"浮盈 {pnl:+.2f}%，持有 {held_days}/{mh or '不限'} 个交易日，卖出链未触发"
    return out


def evaluate(cfg: dict, pos: dict, price: float | None, today: str, now_hm: str, held_days: int, sellable: int) -> dict:
    """用配置里的卖出链评估一只持仓（不拉分时/日K，只用价格和持仓字段；引擎里用 evaluate_chain）。"""
    from .funnel import build_layers, layers_of
    return evaluate_chain(cfg, build_layers(layers_of(cfg, "sell"), None, "sell"), pos, price, today, now_hm, held_days, sellable)


def hold_can_skip(cfg: dict, decision: str) -> bool:
    """旧接口：按默认卖出链的层 id 判断。引擎里用评估结果的 hold_skippable。"""
    return decision != "stop_loss" or bool(cfg["exits"].get("hold_can_skip_stop_loss", False))


def exit_times(cfg: dict, extra_contents: list[dict] | None = None) -> list[str]:
    """定时任务要做卖出检查和提醒的时刻：卖出链里「到点卖出」的时刻；参数留空时按 [exits] 的 exit_time（含按股池覆盖）。
    extra_contents：股池绑定的其他卖出策略内容，它们的到点时刻也要排进定时任务。"""
    from .funnel import layers_of
    out_all: set[str] = set(_exit_times_one(cfg))
    for content in extra_contents or []:
        try:
            from .poolbind import sell_cfg
            out_all |= set(_exit_times_one(sell_cfg(cfg, content)))
        except Exception:  # noqa: BLE001
            pass
    return sorted(out_all)


def _exit_times_one(cfg: dict) -> list[str]:
    from .funnel import layers_of
    ex = cfg["exits"]
    rule_times = {ex.get("exit_time", "14:50")}
    for ov in (ex.get("by_pool") or {}).values():
        if ov.get("exit_time"):
            rule_times.add(ov["exit_time"])
    out: set[str] = set()
    for l in layers_of(cfg, "sell"):
        if not l.get("enabled", True):
            continue
        for f in l.get("factors", []):
            if f.get("kind") == "time_reached" and f.get("enabled", True):
                at = (f.get("params") or {}).get("at")
                if at:
                    out.add(str(at))
                else:
                    out |= rule_times
    return sorted(out)
