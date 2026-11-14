"""股池绑定策略：每个股池可以指定自己的买入策略和卖出策略。

配置写在 config/pipeline.toml 的 [pool.bind] 里（没写就全部用当前生效的那套）：

    [pool.bind."gushi:auction_long"]
    buy = "s-20261112-xxxx"     # 买入策略 id，留空 = 用生效的买入策略
    sell = "s-20261112-yyyy"    # 卖出策略 id，留空 = 用生效的卖出策略

规则（和「按股池覆盖卖出参数」是一套机制的两级，不会互相打架）：

1. 先定「按哪个池」：一只票同时在几个池里时，按配置顺序取第一个——先看 [pool.bind] 里的顺序，
   再看卖出策略 [exits.by_pool] 里的顺序；只算真的配了东西的池（绑了策略或配了覆盖参数）。
   买入看选股当时的来源股池；卖出看买入时记下的来源股池（持仓的 sources）。
2. 再定「用哪套策略」：这个池绑了策略就用绑定的那套，没绑就用当前生效的那套。
3. 最后套「这套策略里对这个池的覆盖参数」（exits.by_pool[池]）。覆盖参数是策略内容的一部分，
   跟着策略走：绑定的策略用它自己的 by_pool，不会混进生效策略的 by_pool。

绑定的策略被删掉或找不到时，按生效策略跑，并在结果里写明原因。
"""
from __future__ import annotations

import copy
from typing import Callable

SIDES = ("buy", "sell")


def bindings(cfg: dict) -> dict[str, dict]:
    """{池: {"buy": id 或 "", "sell": id 或 ""}}，保持配置顺序；空绑定会被去掉。"""
    raw = (cfg.get("pool") or {}).get("bind") or {}
    out: dict[str, dict] = {}
    if not isinstance(raw, dict):
        return out
    for key, b in raw.items():
        if not isinstance(b, dict):
            continue
        bb = {s: str(b.get(s) or "").strip() for s in SIDES}
        if bb["buy"] or bb["sell"]:
            out[str(key)] = bb
    return out


def validate(cfg: dict, exists: Callable[[str, str], bool] | None = None) -> list[str]:
    """配置检查：格式不对、策略不存在。exists(side, id) 由调用方提供。"""
    errs = []
    raw = (cfg.get("pool") or {}).get("bind") or {}
    if not isinstance(raw, dict):
        return ["pool.bind 必须是「股池 → {buy, sell}」的表"]
    for key, b in raw.items():
        if ":" not in str(key):
            errs.append(f"pool.bind 的股池「{key}」格式不对，应为 数据源:股池，例如 gushi:auction_long")
        if not isinstance(b, dict):
            errs.append(f"pool.bind.\"{key}\" 必须是表（buy = 策略 id，sell = 策略 id）")
            continue
        for k in b:
            if k not in SIDES and k != "note":
                errs.append(f"pool.bind.\"{key}\" 里只能写 buy / sell / note，不认识「{k}」")
        if exists:
            for s in SIDES:
                sid = str(b.get(s) or "").strip()
                if sid and not exists(s, sid):
                    errs.append(f"股池 {key} 绑定的{'买入' if s == 'buy' else '卖出'}策略 {sid} 不存在")
    return errs


def sell_cfg(cfg: dict, content: dict) -> dict:
    """用某套卖出策略的内容做一份配置副本（by_pool 只用这套策略自己的，不混进别的）。"""
    from .strategies import cfg_patch
    c = {**cfg, **cfg_patch(cfg, "sell", content)}
    c["exits"] = dict(c["exits"])
    c["exits"]["by_pool"] = copy.deepcopy((content.get("exits") or {}).get("by_pool") or {})
    return c


def buy_cfg(cfg: dict, content: dict) -> dict:
    from .strategies import cfg_patch
    return {**cfg, **cfg_patch(cfg, "buy", content)}


def _has_override(by_pool: dict, key: str) -> bool:
    from .exits import EXIT_RULE_KEYS
    ov = (by_pool or {}).get(key)
    return bool(ov) and any(k in EXIT_RULE_KEYS for k in ov)


class Resolver:
    """按股池找策略。get_doc(side, id) -> {"id","name","content"} 或 None；active_doc(side) 同上（生效策略）。"""

    def __init__(self, cfg: dict, get_doc: Callable[[str, str], dict | None], active_doc: Callable[[str], dict]):
        self.cfg = cfg
        self.get_doc = get_doc
        self.active_doc = active_doc
        self.bind = bindings(cfg)
        self._docs: dict[tuple[str, str], dict | None] = {}

    def _doc(self, side: str, sid: str) -> dict | None:
        k = (side, sid)
        if k not in self._docs:
            try:
                self._docs[k] = self.get_doc(side, sid)
            except Exception:  # noqa: BLE001
                self._docs[k] = None
        return self._docs[k]

    def strategy_for_pool(self, side: str, key: str | None) -> dict:
        """某个池用哪套策略：{"id","name","content","by": 说明, "bound": bool}。"""
        act = self.active_doc(side)
        sid = (self.bind.get(key) or {}).get(side) if key else ""
        if sid:
            d = self._doc(side, sid)
            if d:
                return {"id": d["id"], "name": d["name"], "content": d["content"], "bound": True,
                        "by": f"股池 {key} 绑定的策略"}
            return {**act, "bound": False, "by": f"股池 {key} 绑定的策略 {sid} 找不到，按生效策略", "broken": sid}
        return {**act, "bound": False, "by": "生效策略" + (f"（股池 {key} 没有绑定）" if key else "")}

    # ---- 卖出 ----
    def sell_priority(self) -> list[str]:
        act = self.active_doc("sell")
        order = [k for k, b in self.bind.items() if b.get("sell")]
        for k in ((act.get("content") or {}).get("exits") or {}).get("by_pool") or {}:
            if k not in order and _has_override(act["content"]["exits"]["by_pool"], k):
                order.append(k)
        pr = self.priority()
        if pr:
            order.sort(key=lambda k: pr.index(k) if k in pr else len(pr))
        return order

    def resolve_sell(self, sources: list[str] | None, forced_id: str | None = None) -> dict:
        """一只持仓按哪个池、哪套策略卖。返回 {"pool","strategy_id","strategy","by","content","cfg"}。

        forced_id：持仓页手动指定的卖出策略（个股级覆盖，优先级最高）。指定了就用它，
        来源股池只用来套「这套策略自己配的 by_pool 覆盖参数」；策略找不到时回退到股池绑定 / 生效策略。
        """
        srcs = list(sources or [])
        pool = next((k for k in self.sell_priority() if k in srcs), None)
        forced = str(forced_id or "").strip()
        if forced:
            d = self._doc("sell", forced)
            if d:
                content = d["content"]
                override = bool(pool) and _has_override((content.get("exits") or {}).get("by_pool") or {}, pool)
                by = f"持仓手动指定「{d['name']}」" + (f"，套用这套策略对 {pool} 的覆盖参数" if override else "")
                return {"pool": pool, "strategy_id": d["id"], "strategy": d["name"], "by": by, "bound": False,
                        "override": override, "forced": True, "content": content, "cfg": sell_cfg(self.cfg, content)}
            pool_st = self.strategy_for_pool("sell", pool)
            by = f"持仓指定的卖出策略 {forced} 找不到，回退：{pool_st['by']}"
            return {"pool": pool, "strategy_id": pool_st.get("id"), "strategy": pool_st.get("name"), "by": by,
                    "bound": pool_st["bound"], "override": False, "forced": False, "broken": forced,
                    "content": pool_st["content"], "cfg": sell_cfg(self.cfg, pool_st["content"])}
        st = self.strategy_for_pool("sell", pool)
        override = bool(pool) and _has_override(((st["content"] or {}).get("exits") or {}).get("by_pool") or {}, pool)
        by = st["by"] + (f"，套用这套策略对 {pool} 的覆盖参数" if override else "")
        return {"pool": pool, "strategy_id": st.get("id"), "strategy": st.get("name"), "by": by, "bound": st["bound"],
                "override": override, "forced": False, "content": st["content"], "cfg": sell_cfg(self.cfg, st["content"])}

    def sell_contents(self) -> list[dict]:
        """所有会用到的卖出策略内容（生效 + 各池绑定的），定时任务据此排卖出检查时刻。"""
        out, seen = [self.active_doc("sell")["content"]], set()
        for k, b in self.bind.items():
            sid = b.get("sell")
            if sid and sid not in seen:
                seen.add(sid)
                d = self._doc("sell", sid)
                if d:
                    out.append(d["content"])
        return out

    # ---- 买入 ----
    def buy_groups(self, pool_keys: list[str]) -> list[dict]:
        """把要跑的股池按买入策略分组：[{"id","name","content","pools": [...], "by"}]，生效策略那组排第一。"""
        groups: dict[str, dict] = {}
        act = self.active_doc("buy")
        for key in pool_keys:
            st = self.strategy_for_pool("buy", key)
            gid = st.get("id") or "@active"
            g = groups.get(gid)
            if g is None:
                g = groups[gid] = {"id": st.get("id"), "name": st.get("name"), "content": st["content"], "pools": [],
                                   "active": st.get("id") == act.get("id"), "notes": []}
            g["pools"].append(key)
            if st.get("broken"):
                g["notes"].append(st["by"])
        out = sorted(groups.values(), key=lambda g: not g["active"])
        return out

    def priority(self) -> list[str]:
        """网页上拖动排出来的股池优先顺序（[pool].priority，第六轮新增）；没设时为空。"""
        return [str(k) for k in ((self.cfg.get("pool") or {}).get("priority") or [])]

    def buy_priority(self, pool_keys: list[str]) -> list[str]:
        """一只票同时在几个池里时按哪个池的策略选。
        设了 [pool].priority（网页上拖动排序）时就按它；没设时沿用旧规则：先 [pool.bind] 里绑了买入策略的池（按绑定顺序），
        再其余池（按股池配置顺序）。"""
        pr = [k for k in self.priority() if k in pool_keys]
        if pr:
            return pr + [k for k in pool_keys if k not in pr]
        order = [k for k, b in self.bind.items() if b.get("buy") and k in pool_keys]
        return order + [k for k in pool_keys if k not in order]

    def pool_of(self, sources: list[str], pool_keys: list[str]) -> str | None:
        srcs = set(sources or [])
        return next((k for k in self.buy_priority(pool_keys) if k in srcs), None)

    def table(self, pool_keys: list[str]) -> list[dict]:
        """网页用：每个股池用的买入/卖出策略。"""
        keys = list(dict.fromkeys(list(pool_keys) + list(self.bind)))
        rows = []
        for k in keys:
            b, s = self.strategy_for_pool("buy", k), self.strategy_for_pool("sell", k)
            by_pool = ((s["content"] or {}).get("exits") or {}).get("by_pool") or {}
            rows.append({"pool": k, "buy": {"id": b.get("id"), "name": b.get("name"), "bound": b["bound"], "by": b["by"]},
                         "sell": {"id": s.get("id"), "name": s.get("name"), "bound": s["bound"], "by": s["by"]},
                         "override": copy.deepcopy(by_pool.get(k) or {}), "configured": k in pool_keys})
        return rows
