"""可自定义层级的买入漏斗和卖出因子链。

配置（config/pipeline.toml）：

    [[buy_funnel.layers]]
    name = "硬性过滤"
    mode = "all"            # all 全部满足 / any 任一满足
    enabled = true
    [[buy_funnel.layers.factors]]
    kind = "exclude_st"
    [[buy_funnel.layers.factors]]
    kind = "range"
    params = { field = "market_value", min = 30, max = 800, unit = "亿", missing = "pass" }

    [[sell_funnel.layers]]   # 卖出链：对持仓逐只按顺序检查，第一层触发就是卖出原因
    id = "stop_loss"
    name = "止损"
    [[sell_funnel.layers.factors]]
    kind = "stop_loss"

买入漏斗：候选逐层过滤，每层按 mode 组合本层开启的因子，没通过的票在这一层淘汰。
卖出链：每只持仓逐层检查，某层「成立」（all = 本层因子都说该卖；any = 任一说该卖）就是卖出信号。
含「止损」因子的层永远排在最前，并且默认不能被人工保留跳过（exits.hold_can_skip_stop_loss）。
没写 [buy_funnel] 时用旧的平铺 [[factors]]（每个因子单独一层）；没写 [sell_funnel] 时按 [exits] 生成
「止损 → 止盈 → 到期退出」三层，和原来的固定退出规则等价。
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .factors import FACTOR_TYPES, build_factor
from .factors.base import factor_label
from .factors.base import FactorContext
from .model import Candidate

MODES = {"all": "全部满足", "any": "任一满足"}
SIDES = {"buy": "买入漏斗", "sell": "卖出因子链"}
ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,32}$")
MAX_LAYERS, MAX_FACTORS = 30, 20


# ---------------------------------------------------------------- 配置读取

def legacy_buy_layers(factors: list[dict]) -> list[dict]:
    out = []
    for i, spec in enumerate(factors or []):
        f = {k: v for k, v in spec.items() if k != "enabled"}
        out.append({"id": f"L{i + 1}", "name": spec.get("label") or spec.get("kind", "?"), "mode": "all",
                    "enabled": spec.get("enabled", True), "factors": [f]})
    return out


def default_sell_layers() -> list[dict]:
    """和原来固定退出规则等价：参数留空，都按 [exits]（含按股池覆盖）取值。"""
    return [
        {"id": "stop_loss", "name": "止损", "mode": "any", "enabled": True, "factors": [{"kind": "stop_loss"}]},
        {"id": "take_profit", "name": "止盈", "mode": "any", "enabled": True, "factors": [{"kind": "take_profit"}]},
        {"id": "time", "name": "到期退出", "mode": "all", "enabled": True,
         "factors": [{"kind": "hold_days"}, {"kind": "time_reached"}]},
    ]


def layers_of(cfg: dict, side: str) -> list[dict]:
    if side == "buy":
        bf = cfg.get("buy_funnel") or {}
        raw = bf.get("layers") if "layers" in bf else legacy_buy_layers(cfg.get("factors") or [])
    else:
        sf = cfg.get("sell_funnel") or {}
        raw = sf.get("layers") if "layers" in sf else default_sell_layers()
    return normalize_layers(raw or [])


def normalize_layers(raw: list) -> list[dict]:
    """补默认值（不做合法性检查，检查见 validate_layers）。"""
    out, used = [], set()
    for i, l in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(l, dict):
            continue
        lid = str(l.get("id") or "")
        if not lid or lid in used:
            lid = f"L{i + 1}"
            while lid in used:
                lid += "x"
        used.add(lid)
        layer = {"id": lid, "name": str(l.get("name") or f"第 {i + 1} 层"), "mode": l.get("mode", "all"),
                 "enabled": bool(l.get("enabled", True)),
                 "factors": [dict(f) for f in (l.get("factors") or []) if isinstance(f, dict)]}
        for k in ("hold_skippable", "note"):
            if k in l:
                layer[k] = l[k]
        out.append(layer)
    return out


def validate_layers(raw, side: str, root: Path) -> list[str]:
    if side not in SIDES:
        return [f"未知的链：{side}"]
    if not isinstance(raw, list):
        return [f"{SIDES[side]}的 layers 必须是列表"]
    if len(raw) > MAX_LAYERS:
        return [f"{SIDES[side]}最多 {MAX_LAYERS} 层"]
    errs, ids = [], set()
    for i, l in enumerate(raw):
        where = f"{SIDES[side]}第 {i + 1} 层"
        if not isinstance(l, dict):
            errs.append(f"{where}必须是对象")
            continue
        if l.get("id") not in (None, ""):
            if not ID_RE.match(str(l["id"])):
                errs.append(f"{where}：id 只能用字母数字 _ -，最多 32 个字符")
            elif l["id"] in ids:
                errs.append(f"{where}：id {l['id']} 重复")
            ids.add(l.get("id"))
        if l.get("mode", "all") not in MODES:
            errs.append(f"{where}：mode 只能是 all（全部满足）或 any（任一满足）")
        if "enabled" in l and not isinstance(l["enabled"], bool):
            errs.append(f"{where}：enabled 必须是 true/false")
        if len(str(l.get("name") or "")) > 40:
            errs.append(f"{where}：名字最多 40 个字")
        fs = l.get("factors", [])
        if not isinstance(fs, list):
            errs.append(f"{where}：factors 必须是列表")
            continue
        if len(fs) > MAX_FACTORS:
            errs.append(f"{where}：一层最多 {MAX_FACTORS} 个因子")
        for j, spec in enumerate(fs):
            w = f"{where}第 {j + 1} 个因子"
            if not isinstance(spec, dict) or not spec.get("kind"):
                errs.append(f"{w}：缺少 kind")
                continue
            cls = FACTOR_TYPES.get(spec["kind"])
            if cls is None:
                errs.append(f"{w}：未知因子类型 {spec['kind']}（插件没加载？）")
                continue
            if side not in cls.sides:
                errs.append(f"{w}：{cls.title}（{spec['kind']}）不能用在{SIDES[side]}里")
                continue
            if "params" in spec and not isinstance(spec["params"], dict):
                errs.append(f"{w}：params 必须是对象")
                continue
            try:
                build_factor(spec, root)
            except Exception as e:  # noqa: BLE001
                errs.append(f"{w}（{spec['kind']}）：{e}")
    return errs


def config_problems(cfg: dict, root: Path) -> list[str]:
    """校验买入漏斗和卖出因子链（以及旧的平铺 [[factors]]）。"""
    errs: list[str] = []
    if "layers" in (cfg.get("buy_funnel") or {}):
        errs += validate_layers(cfg["buy_funnel"]["layers"], "buy", root)
    else:
        errs += validate_layers(legacy_buy_layers(cfg.get("factors") or []), "buy", root)
    if "layers" in (cfg.get("sell_funnel") or {}):
        errs += validate_layers(cfg["sell_funnel"]["layers"], "sell", root)
    return errs


# ---------------------------------------------------------------- 构建和评估

@dataclass
class BuiltFactor:
    spec: dict
    fac: object | None
    error: str | None
    label: str

    @property
    def active(self) -> bool:
        return self.fac is not None and self.spec.get("enabled", True)


@dataclass
class BuiltLayer:
    spec: dict
    factors: list[BuiltFactor] = field(default_factory=list)

    @property
    def id(self):
        return self.spec["id"]

    @property
    def mode(self):
        return self.spec.get("mode", "all")

    def active(self) -> list[BuiltFactor]:
        return [f for f in self.factors if f.active]

    def needs(self) -> set[str]:
        out: set[str] = set()
        for f in self.active():
            out |= f.fac.needs()
        return out

    @property
    def is_stop_loss(self) -> bool:
        return any(f.active and f.spec.get("kind") == "stop_loss" for f in self.factors)

    def has_time_gate(self) -> bool:
        return any(f.active and f.spec.get("kind") == "time_reached" for f in self.factors)

    def info(self) -> dict:
        return {"id": self.id, "name": self.spec["name"], "mode": self.mode, "mode_label": MODES.get(self.mode, self.mode),
                "enabled": self.spec.get("enabled", True),
                "factors": [{"kind": f.spec.get("kind"), "label": f.label, "enabled": f.spec.get("enabled", True),
                             "error": f.error, "needs": sorted(f.fac.needs()) if f.fac else [],
                             "description": getattr(f.fac, "description", "") if f.fac else ""} for f in self.factors]}


def build_layers(layers: list[dict], root: Path, side: str) -> list[BuiltLayer]:
    out = []
    for l in normalize_layers(layers):
        bl = BuiltLayer(l)
        for spec in l.get("factors", []):
            try:
                cls = FACTOR_TYPES.get(spec.get("kind"))
                if cls is not None and side not in cls.sides:
                    raise ValueError(f"{cls.title} 不能用在{SIDES[side]}里")
                fac = build_factor(spec, root)
                bl.factors.append(BuiltFactor(spec, fac, None, factor_label(spec, fac)))
            except Exception as e:  # noqa: BLE001
                bl.factors.append(BuiltFactor(spec, None, str(e), spec.get("label") or str(spec.get("kind"))))
        out.append(bl)
    return out


def combine(mode: str, results: list[bool]) -> bool:
    if not results:            # 空层：买入链放行，卖出链不触发（调用方处理）
        return True
    return all(results) if mode == "all" else any(results)


def eval_layer(layer: BuiltLayer, c: Candidate, ctx: FactorContext) -> tuple[bool, list[dict]]:
    from .factors.base import Outcome, apply_missing
    side = (ctx.extra or {}).get("side", "buy")
    checks = []
    for i, f in enumerate(layer.factors):
        if not f.active:
            continue
        error = False
        try:
            o = f.fac.evaluate(c, ctx)
        except Exception as e:  # noqa: BLE001
            o = Outcome.fail(None, f"因子出错：{type(e).__name__}: {e}")
            error = True
        missing = bool(getattr(o, "missing", False))
        # 统一口径：数据缺失时买入漏斗按 missing 参数处理（默认淘汰）；卖出链一律不触发，由引擎提醒
        o2 = apply_missing(o, f.spec.get("params"), side)
        passed, note, let_through = bool(o2.passed), o2.note, (missing and bool(o2.passed))
        checks.append({"i": i, "kind": f.spec.get("kind"), "label": f.label, "passed": passed,
                       "value": None if o.value is None else round(float(o.value), 4), "note": note,
                       **({"missing": True} if missing else {}),
                       **({"missing_passed": True} if let_through else {}),
                       **({"error": True} if error else {}),
                       **({"na": True} if getattr(o, "na", False) else {})})
    return combine(layer.mode, [x["passed"] for x in checks]), checks


def prepare_layer(layer: BuiltLayer, cands: list[Candidate], ctx: FactorContext, warnings: list[str] | None = None):
    for f in layer.active():
        try:
            f.fac.prepare(cands, ctx)
        except Exception as e:  # noqa: BLE001
            if warnings is not None:
                warnings.append(f"{f.label} 预取失败：{e}")


def run_buy_funnel(layers: list[BuiltLayer], cands: list[Candidate], ctx: FactorContext,
                   loader: Callable[[list[Candidate], set[str]], None], brief: Callable[[Candidate], dict],
                   warnings: list[str]) -> tuple[list[Candidate], list[dict]]:
    """逐层过滤。loader(cands, needs) 负责给到达这一层的票补分时/日K。返回（剩下的票，每层记录）。"""
    stages = []
    for li, layer in enumerate(layers):
        info = layer.info()
        if not layer.spec.get("enabled", True):
            continue
        errs = [f for f in layer.factors if f.spec.get("enabled", True) and f.fac is None]
        for f in errs:
            warnings.append(f"「{layer.spec['name']}」里的因子 {f.label} 配置有误，已跳过：{f.error}")
        if not layer.active():
            stages.append({**info, "index": li, "type": "layer", "label": info["name"], "input": len(cands),
                           "kept": [brief(c) for c in cands], "dropped": [], "skipped": "这一层没有可用的因子，全部放行"})
            continue
        n_in = len(cands)
        needs = layer.needs()
        if needs and cands:
            loader(cands, needs)
        prepare_layer(layer, cands, ctx, warnings)
        kept, dropped, kept_c = [], [], []
        for c in cands:
            ok, checks = eval_layer(layer, c, ctx)
            shown = [x for x in checks if x["passed"] == ok] or checks
            row = {**brief(c), "value": shown[0]["value"] if shown else None,
                   "note": "；".join(f"{x['label']}：{x['note']}" if len(checks) > 1 else x["note"] for x in shown),
                   "checks": checks}
            (kept if ok else dropped).append(row)
            if ok:
                kept_c.append(c)
        stages.append({**info, "index": li, "type": "layer", "label": info["name"], "input": n_in, "kept": kept, "dropped": dropped})
        cands = kept_c
    return cands, stages


def eval_sell(layers: list[BuiltLayer], c: Candidate, ctx: FactorContext, hold_can_skip_stop_loss: bool) -> dict:
    """对一只持仓跑卖出链。含止损的层排最前；返回第一个触发的层。

    due_at：含「到点卖出」的层如果除了时刻以外都满足，就是今天到点要卖（用来提前提醒）。
    """
    order = sorted([l for l in layers if l.spec.get("enabled", True)], key=lambda l: 0 if l.is_stop_loss else 1)
    out_layers, decision, due = [], None, []
    for layer in order:
        act = layer.active()
        skippable = hold_can_skip_stop_loss if layer.is_stop_loss else bool(layer.spec.get("hold_skippable", True))
        if not act:
            out_layers.append({**layer.info(), "triggered": False, "checks": [], "stop_loss": layer.is_stop_loss,
                               "hold_skippable": skippable, "note": "没有可用的因子"})
            continue
        ok, checks = eval_layer(layer, c, ctx)
        rec = {**layer.info(), "triggered": ok, "checks": checks, "stop_loss": layer.is_stop_loss, "hold_skippable": skippable,
               "note": "；".join(x["note"] for x in checks if x["passed"] == ok) if ok else ""}
        out_layers.append(rec)
        if ok and decision is None:
            decision = rec
        if not ok and layer.has_time_gate():
            others = [x for x in checks if x["kind"] != "time_reached"]
            ats = [f.fac.at(ctx) for f in act if f.spec.get("kind") == "time_reached"]
            # 把时刻门槛当作「已到」再组合一次
            sim = others + [{"passed": True}] * (len(checks) - len(others))
            if combine(layer.mode, [x["passed"] for x in sim]) and ats:
                due.append({"layer": layer.id, "name": layer.spec["name"], "at": max(ats), "hold_skippable": skippable})
    return {"decision": decision["id"] if decision else None, "decision_label": decision["name"] if decision else None,
            "reason": decision["note"] if decision else "", "stop_loss": bool(decision and decision["stop_loss"]),
            "hold_skippable": bool(decision["hold_skippable"]) if decision else True,
            "layers": out_layers, "due": due}


# ---------------------------------------------------------------- 模板

def _f(kind, label=None, enabled=True, **params):
    d = {"kind": kind}
    if label:
        d["label"] = label
    if not enabled:
        d["enabled"] = False
    if params:
        d["params"] = params
    return d


def _l(lid, name, factors, mode="all", **kw):
    return {"id": lid, "name": name, "mode": mode, "enabled": True, "factors": factors, **kw}


_HARD = _l("hard", "硬性过滤", [_f("exclude_st"), _f("exclude_board", boards=["star", "bse"]),
                                   _f("range", "市值 30~800 亿", field="market_value", min=30, max=800, unit="亿")])

BUY_TEMPLATES: dict[str, dict] = {
    "auction_strong": {
        "title": "竞价强势", "description": "适合 9:25~9:40 跑：高开 1%~6%、开盘后不跳水、站上均价线、未封板、成交足够。搭配竞价类股池。",
        "layers": [
            copy.deepcopy(_HARD),
            _l("auction", "竞价高开", [_f("range", "高开 1%~6%", field="open_rise", min=1, max=6, unit="%"),
                                       _f("expr", "开盘后不跳水", expr="change_rate >= open_rise - 1")]),
            _l("strength", "盘中强度", [_f("above_vwap", min_dev_pct=0.0), _f("max_drawdown", max_pct=3.0)]),
            _l("room", "未涨停且有成交", [_f("not_limit_up", buffer_pct=1.0), _f("min_amount", min_yi=0.5)]),
        ]},
    "gold_230": {
        "title": "尾盘黄金两点半", "description": "适合 14:30 跑：全天站稳均价线、涨幅 2%~7%、距涨停还有空间、回撤小。搭配「黄金两点半」股池和「隔日尾盘」卖出模板。",
        "layers": [
            copy.deepcopy(_HARD),
            _l("trend", "日内趋势", [_f("range", "涨幅 2%~7%", field="change_rate", min=2, max=7, unit="%"),
                                     _f("above_vwap", min_dev_pct=0.3), _f("max_drawdown", max_pct=2.5)]),
            _l("room", "距涨停有空间", [_f("not_limit_up", buffer_pct=2.0), _f("min_amount", min_yi=1.0)]),
        ]},
    "steady_dip": {
        "title": "稳健低吸", "description": "均线多头的票回踩 5 日线附近、当天小涨小跌、没放量杀跌。第二层任一满足：贴近 5 日线或 10 日线都算。",
        "layers": [
            copy.deepcopy(_HARD),
            _l("trend", "均线多头", [_f("expr", "ma5 > ma10 > ma20", expr="ma5 > ma10 and ma10 > ma20")]),
            _l("dip", "回踩均线（任一）", [_f("expr", "贴近 5 日线（±1.5%）", expr="abs(close / ma5 - 1) * 100 <= 1.5"),
                                            _f("expr", "贴近 10 日线（±1.5%）", expr="abs(close / ma10 - 1) * 100 <= 1.5")], mode="any"),
            _l("calm", "当天平稳", [_f("range", "涨跌 -2%~2%", field="change_rate", min=-2, max=2, unit="%"),
                                    _f("range", "量比 ≤ 1.5", field="vol_ratio_5d", max=1.5),
                                    _f("min_amount", min_yi=1.0)]),
        ]},
    "expr_only": {
        "title": "只用表达式（示例）", "description": "每层都是表达式，演示字段、回看 close[1] 和任一满足。字段表在「买卖策略 → 表达式可用字段」里。",
        "layers": [
            _l("base", "基础条件", [_f("expr", "涨幅 0%~7%、市值 ≥ 30 亿", expr="change_rate > 0 and change_rate < 7 and market_value >= 30")]),
            _l("breakout", "突破（任一）", [_f("expr", "今天站上 5 日线、昨天还在线下", expr="close > ma5 and close[1] <= ma5[1]"),
                                         _f("expr", "放量上涨", expr="vol_ratio_5d >= 1.8 and change_rate > 2")], mode="any"),
            _l("guard", "风险排除", [_f("expr", "离涨停 > 1% 且价在均价上", expr="limit_gap_pct > 1 and vwap_dev_pct >= 0")]),
        ]},
}

SELL_TEMPLATES: dict[str, dict] = {
    "classic": {
        "title": "经典三件套（按退出规则）", "description": "止损 → 止盈 → 持有满 N 天到点卖。参数留空，按 [exits] 和按股池覆盖取值，和原来的固定规则完全一样。",
        "layers": default_sell_layers()},
    "next_day_close": {
        "title": "隔日尾盘卖", "description": "给黄金两点半用：止损 3%、冲高 5% 止盈、日内高点回撤 3% 且曾浮盈 2% 以上就走，否则第二天 14:45 卖。",
        "layers": [
            _l("stop_loss", "止损", [_f("stop_loss", pct=3.0)], mode="any"),
            _l("take_profit", "止盈或回撤止盈", [_f("take_profit", pct=5.0),
                                                 _f("drawdown_from_high", pct=3.0, basis="intraday", min_profit_pct=2.0)], mode="any"),
            _l("time", "隔日尾盘", [_f("hold_days", min_days=1), _f("time_reached", at="14:45")]),
        ]},
    "trend_follow": {
        "title": "趋势跟踪", "description": "止损 5%；跌破 5 日线或买入以来高点回撤 8% 就走；最长拿 10 个交易日。",
        "layers": [
            _l("stop_loss", "止损", [_f("stop_loss", pct=5.0)], mode="any"),
            _l("trend_break", "趋势破坏（任一）", [_f("below_ma", ma=5, buffer_pct=0.5),
                                                 _f("drawdown_from_high", pct=8.0, basis="since_buy")], mode="any"),
            _l("time", "最长持有", [_f("hold_days", min_days=10), _f("time_reached", at="14:50")]),
        ]},
    "intraday_guard": {
        "title": "盘中走弱就卖", "description": "止损 3%；10:00 后跌破均价线 1% 且浮盈不到 1% 就卖（表达式示例）；到期按退出规则。",
        "layers": [
            _l("stop_loss", "止损", [_f("stop_loss", pct=3.0)], mode="any"),
            _l("weak", "走弱", [_f("below_vwap", min_below_pct=1.0, after="10:00"), _f("expr", "浮盈 < 1%", expr="pnl_pct < 1")]),
            _l("time", "到期退出", [_f("hold_days"), _f("time_reached")]),
        ]},
}


def templates(side: str) -> dict[str, dict]:
    return BUY_TEMPLATES if side == "buy" else SELL_TEMPLATES


def template_list(side: str) -> list[dict]:
    return [{"key": k, "title": v["title"], "description": v["description"], "side": side,
             "layers": copy.deepcopy(v["layers"])} for k, v in templates(side).items()]


def template_layers(side: str, key: str) -> list[dict]:
    t = templates(side).get(key)
    if t is None:
        raise KeyError(f"没有这个模板：{key}")
    return copy.deepcopy(t["layers"])
