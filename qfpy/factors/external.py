"""外部因子：把别的程序算好的打分/通过结果接进漏斗。

  external  推：外部程序 POST /api/v1/factor-results/<name> 写入结果，这里按交易日读取
  http      拉：跑到这一层时把本层全部候选一次 POST 给外部地址，按返回结果过滤
两边结果格式相同：{"results": [{"code": "600000", "pass": true, "score": 0.8, "note": "..."}]}。
判定：use="pass" 看 pass；use="score" 看 score ≥ min_score；use="auto"（默认）有 pass 看 pass，否则看 score。
卖出链里 pass=true 表示「该卖」。外部结果里的 score 会写进候选字段 ext_<name>，表达式和打分权重都能用。

外部因子都绑定一个数据源（参数 source，网页上点选，见 qfpy/extsources.py）：
  external  source = push 数据源名，或 @api（REST API 推送、没指定数据源）
  http      source = 配了 factor_url 的 http 数据源（地址、令牌、超时都从数据源取，不用手填）；不填 source 时沿用旧的 url 写法
  ext_field 外部数据源字段：source + field（这个数据源提供的字段）+ 比较
数据源不可用、或者这只票的数据没拿到，一律按「数据缺失」处理（missing=pass 时放行）。
"""
from __future__ import annotations

from .. import external as ext
from ..extsources import API_SOURCE, resolve
from .base import Factor, Outcome, ParamSpec, register_factor


def _missing(f: Factor, note: str) -> Outcome:
    """拿不到数据：一律返回「数据缺失」；放不放行由漏斗按统一的 missing 参数处理（默认淘汰）。"""
    return Outcome.missing_data(note)


def _judge(f: Factor, res: dict | None, field: str, c) -> Outcome:
    if res is None:
        return _missing(f, "外部结果里没有这只票")
    if res.get("score") is not None:
        c.fields[field] = res["score"]
    use, note = f.p.get("use", "auto"), res.get("note") or ""
    if use == "pass" or (use == "auto" and res.get("pass") is not None):
        p = res.get("pass")
        if p is None:
            return Outcome.fail(res.get("score"), "外部结果没有 pass")
        return Outcome(bool(p), res.get("score"), ("通过" if p else "不通过") + (f"：{note}" if note else ""))
    s, ms = res.get("score"), f.num("min_score")
    if s is None:
        return Outcome.fail(None, "外部结果没有 score")
    ms = 0.0 if ms is None else ms
    return Outcome(s >= ms, s, f"score {s:.4g}（阈值 {ms:g}）" + (f"：{note}" if note else ""))


_COMMON = [ParamSpec("use", "auto", "怎么判定：auto 有 pass 看 pass、否则看 score / pass 只看 pass / score 看 score ≥ 阈值", "choice",
                     [("auto", "自动"), ("pass", "看 pass"), ("score", "看 score")]),
           ParamSpec("min_score", None, "use=score 时的阈值"),
           ParamSpec("missing", "drop", "数据源不可用或结果里没有这只票时：drop 按数据缺失淘汰（默认）/ pass 放行", "choice",
                     [("drop", "按数据缺失淘汰（默认）"), ("pass", "放行")])]


def _check_common(f: Factor):
    if f.p.get("use") not in ("auto", "pass", "score"):
        raise ValueError("use 只能是 auto / pass / score")
    if f.p.get("missing") not in ("drop", "fail", "pass"):
        raise ValueError("missing 只能是 drop（淘汰）/ pass（放行）")
    f.num("min_score")


@register_factor
class ExternalPushFactor(Factor):
    kind = "external"
    sides = ("buy", "sell")
    category = "外部"
    title = "外部因子（推送）"
    description = "读取外部程序推送的当日结果（pass 或 score）。先点选数据源，再点选这个数据源下推送过的因子"
    example = "数据源「my_push」的因子 hot_rank：pass=true 才留下"
    params = [ParamSpec("source", API_SOURCE, "数据源（push 数据源，或 @api = REST API 推送没指定数据源）", "ext_source"),
              ParamSpec("name", "", "因子（这个数据源推送过的结果名）", "ext_factor"), *_COMMON]

    def validate(self):
        ext.check_name(str(self.p.get("name") or ""), "name")
        src = str(self.p.get("source") or "")
        if src and src != API_SOURCE:
            ext.check_name(src, "数据源")
        _check_common(self)

    def label(self):
        src = self.p.get("source") or ""
        return f"外部因子 {self.p['name']}" + (f"（{src}）" if src and src != API_SOURCE else "")

    def prepare(self, cands, ctx):
        store = (ctx.extra or {}).get("ext_store")
        _, self._src_err = resolve(ctx, str(self.p.get("source") or ""))
        rec = store.get_factor_results(self.p["name"], ctx.trading_date) if store and not self._src_err else None
        self._items = (rec or {}).get("items")
        self._missing_all = rec is None

    def evaluate(self, c, ctx):
        if not hasattr(self, "_items"):
            self.prepare([c], ctx)
        if self._src_err:
            return _missing(self, self._src_err)
        if self._missing_all:
            return _missing(self, f"{ctx.trading_date} 还没有收到外部因子 {self.p['name']} 的结果")
        return _judge(self, (self._items or {}).get(c.code), f"ext_{self.p['name']}", c)


@register_factor
class HttpPullFactor(Factor):
    kind = "http"
    sides = ("buy", "sell")
    category = "外部"
    title = "外部因子（HTTP 拉取）"
    description = ("跑到这一层时把本层候选（代码、名称、字段）一次 POST 给数据源的因子接口，按返回的 pass/score 过滤。"
                   "先点选数据源（配了 factor_url 的 http 数据源），地址和令牌从数据源取。请求失败按数据缺失处理")
    example = "数据源「my_http」的因子 ai_score：score ≥ 0.6 才留下"
    params = [ParamSpec("source", "", "数据源（配了 factor_url 的 http 数据源）；留空时用下面手填的地址（旧写法）", "ext_source"),
              ParamSpec("name", "", "因子（结果名，写进字段 ext_<name>，可空）", "ext_factor"),
              ParamSpec("url", "", "外部接口地址（只在没选数据源时用）", "string"),
              *_COMMON, ParamSpec("timeout_secs", 10, "超时秒数"),
              ParamSpec("token", "", "可空：作为 Authorization: Bearer 发给外部接口", "string")]

    def validate(self):
        u = str(self.p.get("url") or "")
        if self.p.get("source"):
            ext.check_name(str(self.p["source"]), "数据源")
        elif not u.startswith(("http://", "https://")):
            raise ValueError("先点选数据源；或者 url 必须以 http:// 或 https:// 开头")
        if self.p.get("name"):
            ext.check_name(str(self.p["name"]), "name")
        if self.p.get("on_error") not in (None, "", "fail", "pass"):   # 旧参数名，等同 missing
            raise ValueError("on_error 只能是 fail / pass（新写法用 missing = drop / pass）")
        _check_common(self)

    def label(self):
        src = self.p.get("source") or ""
        return f"外部因子 {self.p.get('name') or src or self.p['url']}" + (f"（{src}）" if src and self.p.get("name") else "")

    def _endpoint(self, ctx):
        """(url, token, timeout, 不可用原因)。选了数据源就从数据源配置取。"""
        src = str(self.p.get("source") or "")
        if not src:
            return self.p.get("url"), self.p.get("token"), float(self.num("timeout_secs") or 10), ""
        s, why = resolve(ctx, src)
        if why:
            return None, None, 0, why
        scfg = (s or {}).get("cfg") or {}
        if s is None:   # 没有数据源信息（单测等）：退回手填地址
            return self.p.get("url"), self.p.get("token"), float(self.num("timeout_secs") or 10), ""
        if not scfg.get("factor_url"):
            return None, None, 0, f"数据源 {src} 没填 factor_url"
        return scfg["factor_url"], scfg.get("token"), float(scfg.get("timeout_secs") or self.num("timeout_secs") or 10), ""

    def prepare(self, cands, ctx):
        self._err, self._items, self._src_err = None, {}, ""
        if not cands:
            return
        url, token, timeout, why = self._endpoint(ctx)
        if why:
            self._src_err = why
            return
        side = (ctx.extra or {}).get("side", "buy")
        body = {"factor": self.p.get("name") or "", "side": side, "trading_date": ctx.trading_date,
                "candidates": [{"code": c.code, "name": c.name, "sources": c.sources,
                                "fields": {k: v for k, v in c.fields.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}}
                               for c in cands]}
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        src = str(self.p.get("source") or "")
        try:
            self._items = ext.normalize_results(ext.http_json("POST", url, body, headers, timeout))
            if src and (ctx.extra or {}).get("data_dir"):
                from ..extsources import record
                record(ctx.extra["data_dir"], src, True)
        except Exception as e:  # noqa: BLE001
            self._err = f"{type(e).__name__}: {str(e)[:200]}"
            if src and (ctx.extra or {}).get("data_dir"):
                from ..extsources import record
                record(ctx.extra["data_dir"], src, False, self._err)

    def evaluate(self, c, ctx):
        if not hasattr(self, "_items"):
            self.prepare([c], ctx)
        if getattr(self, "_src_err", ""):
            return _missing(self, self._src_err)
        if self._err:
            return _missing(self, f"外部接口请求失败：{self._err}")
        return _judge(self, self._items.get(c.code), f"ext_{self.p.get('name') or 'http'}", c)


_OPS = {">": lambda a, b: a > b, ">=": lambda a, b: a >= b, "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
        "==": lambda a, b: a == b, "!=": lambda a, b: a != b}


@register_factor
class ExternalFieldFactor(Factor):
    kind = "ext_field"
    sides = ("buy", "sell")
    category = "外部"
    cat = "external"
    update = "realtime"
    backtest_ok = False
    backtest_note = "外部数据源没有历史数据"
    title = "外部数据源字段"
    description = "先点选数据源，再点选它提供的字段，和一个数比较。数据源不可用或这只票没拿到这个字段时按数据缺失处理"
    example = "数据源「my_push」的字段 ext_hot ≥ 3"
    params = [ParamSpec("source", "", "数据源（push 或 http 数据源）", "ext_source"),
              ParamSpec("field", "", "字段（这个数据源提供的字段）", "ext_field"),
              ParamSpec("op", ">=", "比较", "choice", [(k, k) for k in _OPS]),
              ParamSpec("value", 0, "和这个数比"),
              ParamSpec("missing", "drop", "拿不到这个字段时：drop 按数据缺失淘汰（默认）/ pass 放行", "choice",
                        [("drop", "按数据缺失淘汰（默认）"), ("pass", "放行")])]

    def validate(self):
        ext.check_name(str(self.p.get("source") or ""), "数据源")
        if not str(self.p.get("field") or "").strip():
            raise ValueError("先点选字段")
        if self.p.get("op") not in _OPS:
            raise ValueError("比较只能是 " + " ".join(_OPS))
        if self.num("value") is None:
            raise ValueError("value 要填数字")
        if self.p.get("missing", "drop") not in ("drop", "fail", "pass"):
            raise ValueError("missing 只能是 drop（淘汰）/ pass（放行）")

    def label(self):
        return f"{self.p['source']}.{self.p['field']} {self.p['op']} {self.num('value'):g}"

    def evaluate(self, c, ctx):
        src, fld = str(self.p["source"]), str(self.p["field"])
        _, why = resolve(ctx, src)
        if why:
            return _missing(self, why)
        sf = getattr(c, "src_fields", None) or {}
        v = (sf.get(src) or {}).get(fld) if sf else None
        if v is None and not sf:
            v = c.fields.get(fld)          # 没有逐数据源记录时（旧数据/单测）退回合并后的字段
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return _missing(self, f"数据源 {src} 没给这只票的字段 {fld}")
        tgt = self.num("value")
        ok = _OPS[self.p["op"]](float(v), tgt)
        return Outcome(ok, float(v), f"{fld}={v:g} {self.p['op']} {tgt:g}" + ("" if ok else " 不满足"))
