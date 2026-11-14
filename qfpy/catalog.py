"""因子分类与标签：只在这里定义一份，网页（因子库、公式字段点选、形态信号、打分）都从 /api/catalog 读。

每个因子带三个标签：
  update        realtime 盘中实时 / eod 盘后更新
  backtest      yes 回测可用 / partial 看具体字段 / no 回测不可用
  experimental  实验性（数据源不稳定、没有历史）
"""
from __future__ import annotations

CATEGORIES: list[dict] = [
    {"id": "price", "name": "价格涨跌", "desc": "当天涨幅、高开、N 日涨幅、距高点回撤"},
    {"id": "volume", "name": "量能换手", "desc": "成交额、量比、换手率、换手放大、市值"},
    {"id": "ma", "name": "均线与分时", "desc": "均线、均价线、日内冲高回落"},
    {"id": "tech", "name": "技术指标", "desc": "MACD、KDJ、RSI、布林带"},
    {"id": "limit", "name": "涨停板", "desc": "连板、炸板、距涨停"},
    {"id": "signal", "name": "形态信号", "desc": "突破MA5、60日新高、放量，以及自己拼的信号。点选拼条件（每条「字段 比较符 字段/数字」，全部满足），适合不想写公式的人；能保存成信号文件反复用"},
    {"id": "auction_fund", "name": "竞价与资金（实验性）", "desc": "竞价、封单、主力资金、龙虎榜；没有历史数据，回测不可用"},
    {"id": "pool", "name": "股池来源", "desc": "来自哪个股池、同时命中几个池"},
    {"id": "fundamental", "name": "基本面与排雷", "desc": "ST、板块、上市天数、次新股、行业"},
    {"id": "position", "name": "持仓与卖出", "desc": "止盈止损、持有天数、到点卖出等，只在卖出策略里出现", "sides": ["sell"]},
    {"id": "advanced", "name": "高级/自定义公式", "desc": "任意字段区间、自定义公式。公式能写 and/or/括号和四则运算、close[1] 这样的 N 天前，比形态信号灵活；形态信号能表达的公式都能表达"},
    {"id": "external", "name": "外部因子（绑定数据源）", "desc": "来自外部数据源：先点选数据源，再点选它提供的因子或字段；数据源不可用按数据缺失"},
]
CATEGORY_IDS = [c["id"] for c in CATEGORIES]

UPDATE_NAMES = {"realtime": "盘中实时", "eod": "盘后更新"}
BACKTEST_NAMES = {"yes": "回测可用", "partial": "回测看字段", "no": "回测不可用"}

# 旧因子（没有写 cat 的）的分类与标签
_LEGACY: dict[str, dict] = {
    "exclude_board": {"cat": "fundamental", "update": "eod", "example": "排除北交所和科创板（没开权限时）"},
    "exclude_st": {"cat": "fundamental", "update": "eod", "example": "名称里带 ST 的一律不要"},
    "range": {"cat": "advanced", "backtest": "partial", "example": "当前涨幅 change_rate 在 2%~7%"},
    "not_limit_up": {"cat": "limit", "example": "距涨停不到 0.5 个点就不追（大概率买不进）"},
    "min_hits": {"cat": "pool", "backtest": "no", "backtest_note": "回测不重放股池命中次数",
                 "example": "同时出现在 2 个及以上股池里"},
    "above_vwap": {"cat": "ma", "backtest": "no", "backtest_note": "需要分时，日K回测无法重现",
                   "example": "现价至少高于分时均价线 0%"},
    "max_drawdown": {"cat": "ma", "backtest": "no", "backtest_note": "需要分时，日K回测无法重现",
                     "example": "从日内最高点回落不超过 3%"},
    "min_amount": {"cat": "volume", "backtest": "no", "backtest_note": "需要分时，日K回测无法重现",
                   "example": "到现在累计成交额 ≥ 1 亿"},
    "expr": {"cat": "advanced", "backtest": "partial", "example": "close > ma5 and vol_ratio_5d >= 1.5"},
    "signal": {"cat": "signal"},
    "stop_loss": {"cat": "position", "example": "浮亏达到 5% 就卖"},
    "take_profit": {"cat": "position", "example": "浮盈达到 8% 就卖"},
    "hold_days": {"cat": "position", "example": "持有满 1 个交易日就卖（黄金两点半的用法）"},
    "time_reached": {"cat": "position", "example": "到 14:45 就卖"},
    "below_vwap": {"cat": "position", "backtest": "no", "backtest_note": "需要分时", "example": "10:00 以后跌破均价线 1% 就卖"},
    "drawdown_from_high": {"cat": "position", "example": "从买入后最高点回撤 4% 就卖（移动止盈）"},
    "below_ma": {"cat": "position", "example": "收盘跌破 MA5 就卖"},
    "external": {"cat": "external", "backtest": "no", "backtest_note": "外部因子没有历史结果",
                 "example": "数据源 my_push 推送的 hot_rank：pass=true 才留下"},
    "http": {"cat": "external", "backtest": "no", "backtest_note": "外部因子没有历史结果",
             "example": "数据源 my_http 的 ai_score：score ≥ 0.6 才留下"},
}


def factor_meta(kind: str, cls) -> dict:
    """分类与标签。新因子在类上写 cat/update/backtest_ok/experimental；旧因子查 _LEGACY。"""
    leg = _LEGACY.get(kind, {})
    cat = getattr(cls, "cat", "") or leg.get("cat")
    if not cat:
        cat = "position" if tuple(getattr(cls, "sides", ())) == ("sell",) else "advanced"
    if cat not in CATEGORY_IDS:
        cat = "advanced"
    if "backtest" in leg:
        bt = leg["backtest"]
    else:
        bt = "yes" if getattr(cls, "backtest_ok", True) else "no"
    upd = leg.get("update") or getattr(cls, "update", "realtime")
    if upd not in UPDATE_NAMES:
        upd = "eod"
    exp = bool(getattr(cls, "experimental", False))
    ext = any(getattr(p, "type", "") == "ext_source" for p in getattr(cls, "params", []))
    tags = [UPDATE_NAMES[upd], BACKTEST_NAMES[bt]] + (["实验性"] if exp else []) + (["外部数据源"] if ext else [])
    return {"cat": cat, "update": upd, "backtest": bt, "experimental": exp, "external_source": ext,
            "backtest_note": leg.get("backtest_note") or getattr(cls, "backtest_note", ""),
            "example": getattr(cls, "example", "") or leg.get("example", ""), "tags": tags}


# ---- 公式字段：同一套分类 ----
_FIELD_CAT = {
    "price": "price", "open_price": "price", "open_rise": "price", "change_rate": "price", "preclose": "price",
    "open": "price", "high": "price", "low": "price", "close": "price", "pct_chg": "price", "amplitude": "price",
    "high_20d": "price", "low_20d": "price", "high_60d": "price", "low_60d": "price",
    "chg_3d": "price", "chg_5d": "price", "chg_10d": "price", "chg_20d": "price", "chg_60d": "price",
    "drawdown_20d": "price", "drawdown_60d": "price",
    "volume": "volume", "amount": "volume", "vol_ma5": "volume", "vol_ratio_5d": "volume", "amount_yi": "volume",
    "turnover": "volume", "turnover_5d_avg": "volume", "turnover_10d_avg": "volume", "turnover_ratio_5d": "volume",
    "float_mv": "volume", "market_value": "volume", "total_mv": "volume",
    "ma5": "ma", "ma10": "ma", "ma20": "ma", "ma60": "ma", "ma_bull": "ma", "ma_bull_60": "ma",
    "vwap": "ma", "vwap_dev_pct": "ma", "day_high": "ma", "day_low": "ma", "drawdown_pct": "ma",
    "minute_last": "ma", "minute_points": "ma",
    "macd_dif": "tech", "macd_dea": "tech", "macd_hist": "tech", "macd_golden": "tech", "macd_death": "tech",
    "kdj_k": "tech", "kdj_d": "tech", "kdj_j": "tech", "kdj_golden": "tech", "kdj_death": "tech",
    "rsi6": "tech", "rsi12": "tech", "rsi24": "tech", "boll_mid": "tech", "boll_up": "tech", "boll_low": "tech",
    "boll_pos": "tech",
    "limit_pct": "limit", "limit_up": "limit", "limit_down": "limit", "limit_gap_pct": "limit", "no_limit": "limit",
    "limit_up_close": "limit", "touched_limit_up": "limit", "broke_board": "limit", "board_count": "limit",
    "prev_board_count": "limit", "limit_up_count_5d": "limit", "limit_up_count_10d": "limit",
    "limit_up_count_20d": "limit", "broke_count_5d": "limit", "no_limit_day": "limit",
    "hit_count": "pool", "listing_days": "fundamental", "is_st": "fundamental",
    "pnl_pct": "position", "held_days": "position", "avg_cost": "position", "quantity": "position",
    "sellable": "position", "now_min": "position", "high_since_buy": "position", "drawdown_since_buy_pct": "position",
    "score": "advanced",
}
_POSITION_DOCS = {
    "pnl_pct": "持仓浮动盈亏 %", "held_days": "已持有交易日数", "avg_cost": "持仓成本价", "quantity": "持仓数量",
    "sellable": "今天可卖数量", "now_min": "现在是几点（分钟数，如 14:45 = 885）",
    "high_since_buy": "买入以来最高价", "drawdown_since_buy_pct": "从买入后最高点回撤 %",
}


def field_catalog(side: str | None = None) -> list[dict]:
    """公式/形态信号/打分可用的字段，每个字段一行：{name, desc, cat, daily(能否取几天前), source, backtest}。"""
    from .expr import POSITION_FIELDS
    from .market import DAILY_FIELDS, FIELD_DOCS, MINUTE_FIELDS
    out, seen = [], set()
    for key, doc in FIELD_DOCS:
        for name in key.split("/"):
            if name in seen:
                continue
            seen.add(name)
            out.append({"name": name, "desc": doc, "cat": _FIELD_CAT.get(name, "advanced"),
                        "daily": name in DAILY_FIELDS, "minute": name in MINUTE_FIELDS,
                        "backtest": name in DAILY_FIELDS})
    if side in (None, "sell"):
        for name in POSITION_FIELDS:
            if name not in seen:
                out.append({"name": name, "desc": _POSITION_DOCS.get(name, ""), "cat": "position",
                            "daily": False, "minute": False, "backtest": False})
    return out


OPERATORS = [
    {"op": ">", "desc": "大于"}, {"op": ">=", "desc": "大于等于"}, {"op": "<", "desc": "小于"},
    {"op": "<=", "desc": "小于等于"}, {"op": "==", "desc": "等于"}, {"op": "!=", "desc": "不等于"},
    {"op": "and", "desc": "并且"}, {"op": "or", "desc": "或者"}, {"op": "not", "desc": "不是"},
    {"op": "+", "desc": "加"}, {"op": "-", "desc": "减"}, {"op": "*", "desc": "乘"}, {"op": "/", "desc": "除"},
    {"op": "( )", "desc": "括号"}, {"op": "[1]", "desc": "几天前（只用在日K字段后面，如 close[1] = 昨收）"},
    {"op": "abs()", "desc": "绝对值"}, {"op": "max(,)", "desc": "取大"}, {"op": "min(,)", "desc": "取小"},
]


def catalog(side: str | None = None, root=None, data_dir=None, ext_sources: list | None = None) -> dict:
    from .factors.base import factor_catalog
    from . import signals as sig
    from .limits import rule_table
    cats = [c for c in CATEGORIES if side is None or side in c.get("sides", ["buy", "sell"])]
    facs = factor_catalog(side)
    if side == "buy":
        facs = [f for f in facs if f["cat"] != "position"]
    out = {"categories": cats, "factors": facs, "fields": field_catalog(side), "operators": OPERATORS,
           "update_names": UPDATE_NAMES, "backtest_names": BACKTEST_NAMES, "limit_rules": rule_table()}
    if root is not None:
        out["signals"] = sig.list_signals(root, data_dir)
    if ext_sources is not None:
        out["ext_sources"] = ext_sources      # 外部因子点选用：数据源 → 它提供的因子/字段 + 是否可用
    return out
