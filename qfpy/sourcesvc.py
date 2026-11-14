"""两类数据源的状态：股池数据源（股池成分股从哪里拉）、行情数据源（实时价、分时、日K、指数、市场情绪）。

给「设置 → 股池数据源 / 行情数据源」显示：用在哪里、是否可用、最近一次成功和最近的报错（本次启动以来）。
"""
from __future__ import annotations

from .market import SOURCE_HEALTH

TYPE_CN = {"gushi": "gushi.in 策略股池", "watchlist": "自选股", "akshare": "AKShare 免费行情", "demo": "演示数据",
           "push": "外部程序推送", "http": "外部 HTTP 接口", "tencent": "腾讯行情", "eastmoney": "东方财富"}
MARKET_KEYS = {"minute_sources": "分时", "daily_sources": "日K", "quote_sources": "实时行情 / 指数 / 市场情绪"}


def _state(h: dict, enabled: bool) -> tuple[str, str]:
    if not enabled:
        return "off", "已停用"
    if not h:
        return "unknown", "本次启动后还没用过"
    ok, bad = h.get("ok_at") or "", h.get("err_at") or ""
    if bad and bad >= ok:
        return "error", "最近一次失败"
    return "ok", "可用"


def status(engine) -> dict:
    cfg = engine.cfg
    pools = [str(p) for p in (cfg.get("pool") or {}).get("pools", [])]
    mk = cfg.get("market") or {}
    out = {"pool": [], "market": []}
    for role, sec in (("pool", "pool_sources"), ("market", "market_sources")):
        for name, sc in (cfg.get(sec) or {}).items():
            enabled = sc.get("enabled", True) is not False
            h = dict(SOURCE_HEALTH.get(name) or {})
            st, label = _state(h, enabled)
            used_m = [cn for k, cn in MARKET_KEYS.items() if name in (mk.get(k) or [])]
            out[role].append({"name": name, "type": sc.get("type"), "type_cn": TYPE_CN.get(str(sc.get("type")), sc.get("type")),
                              "enabled": enabled, "state": st, "state_cn": label, "health": h,
                              "pools": [p for p in pools if p.split(":", 1)[0] == name],
                              "market_use": used_m, "shared": role == "pool" and bool(used_m)})
    return out


def pool_names(engine) -> dict:
    """股池中文显示名：设置里起的别名（[pool].aliases）> 数据源给的名字 > 最近一次运行记下的名字 > 代码本身。"""
    cfg = engine.cfg
    aliases = dict((cfg.get("pool") or {}).get("aliases") or {})
    out: dict[str, dict] = {}
    from .datasources.gushi import STRATEGIES
    gs = dict(STRATEGIES)
    seen: dict[str, str] = {}
    try:
        idx = engine._run_index()[:5]
        for it in idx:
            r = engine.get_run(it.get("run_id") or it.get("id")) or {}
            for p in r.get("pools") or []:
                if p.get("name") and p["name"] != p.get("key"):
                    seen.setdefault(p["key"], p["name"])
    except Exception:  # noqa: BLE001
        pass
    srcs = {**(cfg.get("market_sources") or {}), **(cfg.get("pool_sources") or {}), **(cfg.get("sources") or {})}
    # 除了 [pool].pools，定时时段、股池绑定、按股池卖出覆盖、最近的运行记录里出现的股池也要有名字
    # （例如 gushi:gold_230、gushi:half_past_ten 默认只写在时段里）
    keys: list[str] = [str(p) for p in (cfg.get("pool") or {}).get("pools", [])]
    for r in (cfg.get("schedule") or {}).get("runs") or []:
        keys += [str(p) for p in (r.get("pools") or [])]
    keys += [str(k) for k in ((cfg.get("pool") or {}).get("bind") or {})]
    keys += [str(k) for k in ((cfg.get("exits") or {}).get("by_pool") or {})]
    keys += list(seen) + list(aliases)
    try:
        for it in engine._run_index()[:50]:
            for p in it.get("pools") or []:
                if isinstance(p, dict) and p.get("key"):
                    keys.append(str(p["key"]))
    except Exception:  # noqa: BLE001
        pass
    for k in dict.fromkeys(k for k in keys if k and ":" in k):
        sname, _, pkey = k.partition(":")
        typ = str((srcs.get(sname) or {}).get("type") or "")
        auto = gs.get(pkey) if (typ == "gushi" or (not typ and sname == "gushi")) else None
        if typ == "watchlist" and not auto:
            auto = "自选股" + ("" if pkey in ("all", "") else f"·{pkey}")
        auto = auto or seen.get(k)
        out[k] = {"name": aliases.get(k) or auto or k, "alias": aliases.get(k) or "", "auto": auto or ""}
    return out
