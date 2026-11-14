"""A 股涨跌停幅度规则表（带生效日期）。

全系统（选股、连板/炸板因子、委托价、回测）都从这里取涨跌幅，规则只写这一份：

  板块              涨跌幅            说明
  沪深主板          10%               ST / *ST：2026-07-06 之前 5%，从这天起 10%
  创业板            20%               2020-08-24 注册制改革前与主板相同（10%，ST 5%）
  科创板            20%
  北交所            30%
  注册制新股        不设涨跌幅限制    上市前 5 个交易日（北交所只有上市首日）

新股无涨跌幅限制的这几天，连板/炸板/涨停类因子没有意义，统一标「不适用」。

板块判断（board_of）、ST 判断（is_st / is_risk_name）、新股无涨跌幅窗口（no_limit_days / is_no_limit_day）
也只在这里写一份；qfpy/util.py 和 vendor/tsp_price_limits.py 里的同名函数都只是转调这里。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class LimitRule:
    board: str            # main / gem / star / bse
    st: bool | None       # True = 只管 ST/*ST；False = 只管非 ST；None = 不区分
    since: date           # 生效日（含）
    until: date | None    # 失效日（不含）；None = 一直有效
    pct: float            # 涨跌幅（小数）
    note: str


ST_10PCT_DATE = date(2026, 7, 6)          # 主板 ST/*ST 涨跌幅由 5% 调整为 10% 的生效日（沪深《交易规则（2026年修订）》，2026-04-24 发布）
GEM_20PCT_DATE = date(2020, 8, 24)        # 创业板注册制改革首日
_EPOCH = date(1990, 1, 1)

RULES: tuple[LimitRule, ...] = (
    LimitRule("main", False, _EPOCH, None, 0.10, "沪深主板 10%"),
    LimitRule("main", True, _EPOCH, ST_10PCT_DATE, 0.05, "主板 ST/*ST 5%（2026-07-06 之前）"),
    LimitRule("main", True, ST_10PCT_DATE, None, 0.10, "主板 ST/*ST 10%（2026-07-06 起）"),
    LimitRule("gem", False, _EPOCH, GEM_20PCT_DATE, 0.10, "创业板 10%（2020-08-24 之前）"),
    LimitRule("gem", True, _EPOCH, GEM_20PCT_DATE, 0.05, "创业板 ST 5%（2020-08-24 之前）"),
    LimitRule("gem", None, GEM_20PCT_DATE, None, 0.20, "创业板 20%"),
    LimitRule("star", None, _EPOCH, None, 0.20, "科创板 20%"),
    LimitRule("bse", None, _EPOCH, None, 0.30, "北交所 30%"),
)

NO_LIMIT_DAYS = {"main": 5, "gem": 5, "star": 5, "bse": 1}
BOARD_NAMES = {"main": "沪深主板", "gem": "创业板", "star": "科创板", "bse": "北交所"}
BOARD_LABELS = {"main": "主板", "gem": "创业板", "star": "科创板", "bse": "北交所"}
# 注册制起始日：这天之前上市的老股没有「上市初期无涨跌幅」窗口
REGISTRATION_DATES = {"main": date(2023, 2, 17), "gem": date(2020, 8, 24), "star": date(2019, 7, 22), "bse": date(2021, 11, 15)}
# 只知道上市日、不知道是第几个交易日时，用日历天兜底（窗口 + 节假日余量）
NO_LIMIT_CALENDAR_MARGIN = {"main": 15, "gem": 15, "star": 15, "bse": 3}


def board_of(code: str) -> str:
    c = str(code).split(".")[0]
    if str(code).endswith(".BJ") or c.startswith(("4", "8", "92")):
        return "bse"
    if c.startswith(("300", "301", "302")):
        return "gem"
    if c.startswith(("688", "689")):
        return "star"
    return "main"


def is_st(name: str | None) -> bool:
    """风险警示股（ST / *ST）：决定涨跌幅用哪条规则。"""
    return "ST" in str(name or "").upper()


def is_risk_name(name: str | None) -> bool:
    """排雷用：ST、*ST 或名字带「退」（退市整理）。「排除 ST」和「风险排雷」两个因子都用这一个。"""
    return is_st(name) or "退" in str(name or "")


def _as_date(d) -> date:
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d)[:10])


def rule_for(code: str, trade_date, st: bool = False) -> LimitRule:
    b, d = board_of(code), _as_date(trade_date)
    cands = [r for r in RULES if r.board == b and r.since <= d and (r.until is None or d < r.until)]
    for r in cands:
        if r.st is st:
            return r
    for r in cands:
        if r.st is None:
            return r
    for r in cands:          # 例如 ST 规则不存在时退回普通规则
        if r.st is False:
            return r
    raise ValueError(f"没有 {code} 在 {d} 的涨跌幅规则")


def limit_pct(code: str, trade_date, st: bool = False) -> float:
    return rule_for(code, trade_date, st).pct


def no_limit_days(code: str, listing_date=None) -> int:
    """注册制新股上市初期不设涨跌幅限制的交易日数。给了上市日时，注册制之前上市的老股返回 0。"""
    b = board_of(code)
    if listing_date is not None and _as_date(listing_date) < REGISTRATION_DATES[b]:
        return 0
    return NO_LIMIT_DAYS[b]


def in_no_limit_window(code: str, day_rank: int | None, listing_date=None) -> bool:
    """day_rank = 上市第几个交易日（首日 = 1）。不知道就返回 False。"""
    return day_rank is not None and 1 <= day_rank <= no_limit_days(code, listing_date)


def is_no_limit_day(code: str, listing_date, trade_date, day_rank: int | None = None) -> bool:
    """trade_date 是否落在上市初期无涨跌幅窗口内。有 day_rank 按交易日数精确判断，否则按上市日 + 日历天余量近似。"""
    if listing_date is None:
        return False
    ld, td = _as_date(listing_date), _as_date(trade_date)
    days = no_limit_days(code, ld)
    if days == 0 or td < ld:
        return False
    from datetime import timedelta
    if td > ld + timedelta(days=NO_LIMIT_CALENDAR_MARGIN[board_of(code)]):
        return False
    return day_rank is None or day_rank <= days


def rule_table() -> list[dict]:
    """给网页显示的规则表。"""
    rows = [{"board": BOARD_NAMES[r.board], "st": {True: "ST/*ST", False: "非 ST", None: "全部"}[r.st],
             "since": None if r.since == _EPOCH else r.since.isoformat(), "until": r.until.isoformat() if r.until else None,
             "pct": round(r.pct * 100, 2), "note": r.note} for r in RULES]
    rows += [{"board": BOARD_NAMES[b], "st": "新股", "since": None, "until": None, "pct": None,
              "note": f"注册制新股上市前 {n} 个交易日不设涨跌幅限制" if n > 1 else "注册制新股上市首日不设涨跌幅限制"}
             for b, n in NO_LIMIT_DAYS.items()]
    return rows
