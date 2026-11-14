"""买入/卖出策略（多套）、生效策略、模板、导入导出。

存放（都在 data 文件夹，换包时整个复制过去就行）：
  data/strategies/buy/<id>.json      买入策略：{id, name, note, created_at, updated_at, content}
  data/strategies/sell/<id>.json     卖出策略
  data/strategies/active.json        正在生效的两套：{buy: {id, name, since}, sell: {...}}
  data/templates/buy|sell/<id>.json  你存的模板（自带模板写在代码里，不能删改）
  data/templates/defaults.json       「恢复默认」回到哪个模板

策略内容：
  买入 content = {layers: [...], select: {top_n, weights}}
  卖出 content = {layers: [...], exits: {止盈止损等默认参数, by_pool: 按股池覆盖}}

生效的那套同时写进 config/pipeline.toml（buy_funnel/select 或 sell_funnel/exits），引擎照旧只读配置文件，
所以自动交易、回测、对外 API 的行为不变。手改了配置文件时，生效的那套会自动同步成文件里的内容。
第一次启动时把现在配置里的买入漏斗、打分、卖出链、退出规则搬成「当前买入策略」「当前卖出策略」，不用重填。
"""
from __future__ import annotations

import copy
import json
import re
import threading
import time
from pathlib import Path

from .config import SECRET_KEYS
from .funnel import normalize_layers, templates as builtin_templates
from .util import lock_for, process_lock, read_json, ts, write_json

SIDES = ("buy", "sell")
SIDE_NAMES = {"buy": "买入", "sell": "卖出"}
EXPORT_FORMAT = "niulai-strategy"
EXPORT_VERSION = 1
EXIT_CONTENT_KEYS = ("enabled", "take_profit_pct", "stop_loss_pct", "max_hold_days", "exit_time", "check_times",
                     "remind_minutes_before", "hold_can_skip_stop_loss", "by_pool")
_LOCK = threading.RLock()
# 导出时去掉的参数（密钥/口令）
SECRET_PARAM_KEYS = set(SECRET_KEYS) | {"password", "passwd", "api_key", "apikey", "authorization", "headers", "cookie"}


class StrategyError(ValueError):
    pass


def _check_side(side: str):
    if side not in SIDES:
        raise StrategyError("side 只能是 buy 或 sell")


# ---------------- 内容与配置互转 ----------------
def content_from_cfg(cfg: dict, side: str) -> dict:
    from .funnel import layers_of
    if side == "buy":
        sel = cfg.get("select") or {}
        return {"layers": layers_of(cfg, "buy"),
                "select": {"top_n": int(sel.get("top_n", 1) or 1), "weights": dict(sel.get("weights") or {})}}
    ex = cfg.get("exits") or {}
    return {"layers": layers_of(cfg, "sell"),
            "exits": {k: copy.deepcopy(ex[k]) for k in EXIT_CONTENT_KEYS if k in ex}}


def cfg_patch(cfg: dict, side: str, content: dict) -> dict:
    """把策略内容变成要写回配置文件的段。"""
    if side == "buy":
        bf = copy.deepcopy(cfg.get("buy_funnel") or {})
        bf["layers"] = normalize_layers(content.get("layers") or [])
        sel = copy.deepcopy(cfg.get("select") or {})
        sel.update(copy.deepcopy(content.get("select") or {}))
        return {"buy_funnel": bf, "select": sel}
    sf = copy.deepcopy(cfg.get("sell_funnel") or {})
    sf["layers"] = normalize_layers(content.get("layers") or [])
    ex = copy.deepcopy(cfg.get("exits") or {})
    for k, v in (content.get("exits") or {}).items():
        if k in EXIT_CONTENT_KEYS:
            ex[k] = copy.deepcopy(v)
    return {"sell_funnel": sf, "exits": ex}


def normalize_content(side: str, content: dict, base: dict | None = None) -> dict:
    """补齐内容（只给了 layers 的模板/导入，其余部分沿用 base）。"""
    if not isinstance(content, dict):
        raise StrategyError("策略内容必须是对象")
    base = base or {}
    out = {"layers": normalize_layers(content.get("layers") if "layers" in content else base.get("layers") or [])}
    if side == "buy":
        sel = copy.deepcopy(base.get("select") or {"top_n": 1, "weights": {}})
        if isinstance(content.get("select"), dict):
            sel.update(copy.deepcopy(content["select"]))
        out["select"] = sel
    else:
        ex = copy.deepcopy(base.get("exits") or {})
        if isinstance(content.get("exits"), dict):
            ex.update({k: copy.deepcopy(v) for k, v in content["exits"].items() if k in EXIT_CONTENT_KEYS})
        out["exits"] = ex
    return out


def _strip_url(v: str) -> str:
    """网址里的 ?token=…、user:pass@ 也算密钥：导出时去掉查询参数和登录信息。"""
    from urllib.parse import urlsplit, urlunsplit
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", v):
        return v
    try:
        u = urlsplit(v)
    except ValueError:
        return v
    netloc = u.netloc.rsplit("@", 1)[-1]
    return urlunsplit((u.scheme, netloc, u.path, "", ""))


def strip_secrets(o):
    if isinstance(o, dict):
        return {k: strip_secrets(v) for k, v in o.items() if str(k).lower() not in SECRET_PARAM_KEYS}
    if isinstance(o, list):
        return [strip_secrets(v) for v in o]
    if isinstance(o, str):
        return _strip_url(o)
    return o


# ---------------- 差异 ----------------
def _flat(o, prefix=""):
    out = {}
    if isinstance(o, dict):
        for k, v in o.items():
            out.update(_flat(v, f"{prefix}.{k}" if prefix else str(k)))
    else:
        out[prefix] = o
    return out


def _layer_sig(l: dict) -> str:
    return json.dumps({k: l.get(k) for k in ("name", "mode", "enabled", "factors", "hold_skippable")},
                      ensure_ascii=False, sort_keys=True)


def _fac_text(f: dict) -> str:
    from .factors.base import FACTOR_TYPES
    cls = FACTOR_TYPES.get(f.get("kind"))
    t = f.get("label") or (cls.title if cls else f.get("kind"))
    return t + ("（停用）" if f.get("enabled") is False else "")


def content_diff(side: str, old: dict, new: dict) -> list[dict]:
    """用人话列出从 old 到 new 会改哪些地方。每条 {where, before, after}。"""
    out = []
    ol = {l["id"]: l for l in normalize_layers(old.get("layers") or [])}
    nl = {l["id"]: l for l in normalize_layers(new.get("layers") or [])}
    for lid, l in nl.items():
        if lid not in ol:
            out.append({"where": f"新增一层「{l['name']}」", "before": "",
                        "after": ("全部满足：" if l["mode"] == "all" else "任一满足：") + "、".join(_fac_text(f) for f in l["factors"])})
        elif _layer_sig(l) != _layer_sig(ol[lid]):
            o = ol[lid]
            if o["name"] != l["name"]:
                out.append({"where": f"层「{o['name']}」的名字", "before": o["name"], "after": l["name"]})
            if o["mode"] != l["mode"]:
                out.append({"where": f"层「{l['name']}」的组合方式", "before": "全部满足" if o["mode"] == "all" else "任一满足",
                            "after": "全部满足" if l["mode"] == "all" else "任一满足"})
            if o["enabled"] != l["enabled"]:
                out.append({"where": f"层「{l['name']}」", "before": "启用" if o["enabled"] else "停用",
                            "after": "启用" if l["enabled"] else "停用"})
            of = [json.dumps(f, ensure_ascii=False, sort_keys=True) for f in o["factors"]]
            nf = [json.dumps(f, ensure_ascii=False, sort_keys=True) for f in l["factors"]]
            if of != nf:
                out.append({"where": f"层「{l['name']}」的因子", "before": "、".join(_fac_text(f) for f in o["factors"]) or "（无）",
                            "after": "、".join(_fac_text(f) for f in l["factors"]) or "（无）"})
            if o.get("hold_skippable", True) != l.get("hold_skippable", True):
                out.append({"where": f"层「{l['name']}」点「保留」能否挡卖出",
                            "before": "能挡" if o.get("hold_skippable", True) else "挡不住", "after": "能挡" if l.get("hold_skippable", True) else "挡不住"})
    for lid, o in ol.items():
        if lid not in nl:
            out.append({"where": f"删掉层「{o['name']}」", "before": "、".join(_fac_text(f) for f in o["factors"]), "after": ""})
    common_o = [i for i in ol if i in nl]
    common_n = [i for i in nl if i in ol]
    if common_o != common_n:
        out.append({"where": "层的顺序", "before": " → ".join(ol[i]["name"] for i in common_o),
                    "after": " → ".join(nl[i]["name"] for i in common_n)})
    sec = "select" if side == "buy" else "exits"
    fo, fn = _flat(old.get(sec) or {}), _flat(new.get(sec) or {})
    from .settings_docs import key_title
    for k in sorted(set(fo) | set(fn)):
        if fo.get(k) != fn.get(k):
            out.append({"where": key_title(f"{sec}.{k}"), "key": f"{sec}.{k}",
                        "before": "" if k not in fo else _show(fo[k]), "after": "（去掉）" if k not in fn else _show(fn[k])})
    return out


def _show(v) -> str:
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, list):
        return "、".join(str(x) for x in v) or "（空）"
    return str(v)


# ---------------- 存储 ----------------
class Strategies:
    def __init__(self, engine):
        self.engine = engine

    @property
    def root(self) -> Path:
        return Path(self.engine.data_dir)

    def _dir(self, side: str) -> Path:
        return self.root / "strategies" / side

    def _tdir(self, side: str) -> Path:
        return self.root / "templates" / side

    def _active_file(self) -> Path:
        return self.root / "strategies" / "active.json"

    def _guard(self):
        """策略模块所有写操作共用一把跨进程锁（用 active.json 的锁文件当总闸）。
        一个写操作可能同时动 sid.json / active.json / 配置文件 / switch_log，必须用同一把锁罩住。"""
        return process_lock(lock_for(self._active_file()))

    # ---- 基本读写 ----
    def _read(self, side: str, sid: str) -> dict | None:
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", sid or ""):
            return None
        d = read_json(self._dir(side) / f"{sid}.json", None)
        if not isinstance(d, dict):
            return None
        # 正在生效的那套以配置文件为准：网页旧入口（/api/funnel、/api/config）或手改 pipeline.toml 之后，
        # 读到的仍是实际在用的内容，不会因为策略文件没同步而把改动覆盖回去。
        if (self.active().get(side) or {}).get("id") == sid:
            try:
                d["content"] = normalize_content(side, content_from_cfg(self.engine.cfg, side), d.get("content"))
            except Exception:  # noqa: BLE001
                pass
        return d

    def _write(self, side: str, doc: dict) -> None:
        write_json(self._dir(side) / f"{doc['id']}.json", doc)

    def _new_id(self, side: str, prefix: str = "s") -> str:
        base = f"{prefix}{time.strftime('%Y%m%d%H%M%S')}"
        sid, i = base, 2
        while (self._dir(side) / f"{sid}.json").exists():
            sid, i = f"{base}_{i}", i + 1
        return sid

    def active(self) -> dict:
        return read_json(self._active_file(), None) or {}

    def active_of(self, side: str) -> dict | None:
        self.ensure()
        return self.active().get(side)

    def active_names(self) -> dict:
        a = self.ensure()
        return {s: {"id": (a.get(s) or {}).get("id"), "name": (a.get(s) or {}).get("name"),
                    "since": (a.get(s) or {}).get("since")} for s in SIDES}

    # ---- 迁移 + 同步 ----
    def ensure(self) -> dict:
        """没有策略时把现在配置搬成「当前…策略」；生效策略和配置文件不一致时以配置文件为准。"""
        with _LOCK, self._guard():
            act = self.active()
            changed = False
            cfg = self.engine.cfg
            for side in SIDES:
                cur = content_from_cfg(cfg, side)
                a = act.get(side) or {}
                doc = self._read(side, a.get("id", "")) if a else None
                if doc is None:
                    now = ts()
                    doc = {"id": "current" if not (self._dir(side) / "current.json").exists() else self._new_id(side),
                           "name": f"当前{SIDE_NAMES[side]}策略", "note": "升级时从原配置自动搬过来的（不用重填）",
                           "created_at": now, "updated_at": now, "content": cur}
                    self._write(side, doc)
                    act[side] = {"id": doc["id"], "name": doc["name"], "since": now, "by": "自动迁移"}
                    changed = True
                elif json.dumps(normalize_content(side, doc["content"]), sort_keys=True, ensure_ascii=False) != \
                        json.dumps(normalize_content(side, cur), sort_keys=True, ensure_ascii=False):
                    doc["content"] = cur          # 配置文件被手改或被 /api/v1 改了：生效的那套跟着变
                    doc["updated_at"] = ts()
                    self._write(side, doc)
                if act.get(side, {}).get("name") != doc["name"]:
                    act[side]["name"] = doc["name"]
                    changed = True
            if changed:
                write_json(self._active_file(), act)
            return act

    # ---- 列表 ----
    def list(self, side: str) -> dict:
        _check_side(side)
        act = self.ensure()
        aid = (act.get(side) or {}).get("id")
        items = []
        for f in sorted(self._dir(side).glob("*.json")):
            d = read_json(f, None)
            if not isinstance(d, dict) or "content" not in d:
                continue
            items.append({**{k: d.get(k) for k in ("id", "name", "note", "created_at", "updated_at", "from_template")},
                          "active": d.get("id") == aid, "layers": len((d.get("content") or {}).get("layers") or [])})
        items.sort(key=lambda x: (not x["active"], x.get("created_at") or ""))
        return {"side": side, "active": act.get(side), "items": items}

    def get(self, side: str, sid: str) -> dict:
        _check_side(side)
        self.ensure()
        d = self._read(side, sid)
        if d is None:
            raise KeyError(sid)
        d["content"] = normalize_content(side, d["content"])
        d["active"] = (self.active().get(side) or {}).get("id") == sid
        return d

    # ---- 校验 ----
    def validate(self, side: str, content: dict) -> list[str]:
        from .funnel import validate_layers
        errs = validate_layers(content.get("layers") or [], side, self.engine.root)
        if side == "buy":
            sel = content.get("select") or {}
            try:
                if int(sel.get("top_n", 1)) < 1:
                    errs.append("每次最多买几只（top_n）至少是 1")
            except (TypeError, ValueError):
                errs.append("top_n 必须是整数")
            for k, v in (sel.get("weights") or {}).items():
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    errs.append(f"打分权重 {k} 必须是数字")
        else:
            from .configsvc import problems
            new = copy.deepcopy(self.engine.cfg)
            new.update(cfg_patch(new, side, content))
            errs += problems(new, self.engine.root)
        return errs

    # ---- 写 ----
    def save(self, side: str, sid: str, content: dict, name: str | None = None, note: str | None = None,
             dry_run: bool = False, by: str = "网页") -> dict:
        """保存一套策略。是生效的那套就同时写进配置文件（立即影响自动交易），否则只存文件。"""
        _check_side(side)
        with _LOCK, self._guard():
            doc = self._read(side, sid)
            if doc is None:
                raise KeyError(sid)
            content = normalize_content(side, content, doc["content"])
            errs = self.validate(side, content)
            if errs:
                raise StrategyError("；".join(errs))
            is_active = (self.active().get(side) or {}).get("id") == sid
            diff = content_diff(side, normalize_content(side, doc["content"]), content)
            if dry_run:
                return {"saved": False, "active": is_active, "changes": diff}
            doc["content"] = content
            if name is not None and name.strip():
                doc["name"] = name.strip()[:40]
            if note is not None:
                doc["note"] = note.strip()[:200]
            doc["updated_at"] = ts()
            if is_active:
                self._apply_to_cfg(side, content)
            self._write(side, doc)
            if is_active and name:
                act = self.active()
                act[side]["name"] = doc["name"]
                write_json(self._active_file(), act)
            return {"saved": True, "active": is_active, "changes": diff, "id": sid,
                    "message": "已保存并生效" if is_active else "已保存。这套不是正在生效的策略，不影响自动交易"}

    def _apply_to_cfg(self, side: str, content: dict) -> None:
        from .configsvc import apply_patch
        apply_patch(self.engine, cfg_patch(self.engine.cfg, side, content))

    def create(self, side: str, name: str, content: dict | None = None, note: str = "", from_id: str | None = None,
               from_template: str | None = None) -> dict:
        """新建（复制为新策略 / 导入 / 从模板新建）。不会变成生效的那套。"""
        _check_side(side)
        name = (name or "").strip()[:40]
        if not name:
            raise StrategyError("给新策略起个名字")
        with _LOCK, self._guard():
            self.ensure()
            base = None
            if from_id:
                src = self._read(side, from_id)
                if src is None:
                    raise KeyError(from_id)
                base = src["content"]
            if content is None and from_template:
                content = self.template(side, from_template)["content"]
            if content is None:
                content = base
            if content is None:
                # 都没给内容 = 「新建空白策略」：用「默认模板」当骨架（没设过就用内置 default/classic）
                from_template = self.default_template_id(side)
                content = self.template(side, from_template)["content"]
            active_doc = self._read(side, (self.active().get(side) or {}).get("id", "")) or {}
            content = normalize_content(side, content, base or active_doc.get("content"))
            errs = self.validate(side, content)
            if errs:
                raise StrategyError("；".join(errs))
            now = ts()
            doc = {"id": self._new_id(side), "name": name, "note": (note or "").strip()[:200], "created_at": now,
                   "updated_at": now, "content": content}
            if from_template:
                doc["from_template"] = from_template
            self._write(side, doc)
            return {**doc, "active": False}

    def rename(self, side: str, sid: str, name: str, note: str | None = None) -> dict:
        with _LOCK, self._guard():
            doc = self._read(side, sid)
            if doc is None:
                raise KeyError(sid)
            if not (name or "").strip():
                raise StrategyError("名字不能为空")
            doc["name"] = name.strip()[:40]
            if note is not None:
                doc["note"] = note.strip()[:200]
            self._write(side, doc)
            act = self.active()
            if (act.get(side) or {}).get("id") == sid:
                act[side]["name"] = doc["name"]
                write_json(self._active_file(), act)
            return doc

    def delete(self, side: str, sid: str) -> None:
        with _LOCK, self._guard():
            if (self.active().get(side) or {}).get("id") == sid:
                raise StrategyError("正在生效的策略不能删，先把另一套设为生效")
            from .poolbind import bindings
            used = [k for k, b in bindings(self.engine.cfg).items() if b.get(side) == sid]
            if used:
                raise StrategyError(f"股池 {'、'.join(used)} 还绑定着这套策略，先在「股池绑定策略」里解除")
            f = self._dir(side) / f"{sid}.json"
            if not f.exists() or not re.fullmatch(r"[A-Za-z0-9_\-]+", sid):
                raise KeyError(sid)
            f.unlink()

    def activate(self, side: str, sid: str, by: str = "网页") -> dict:
        """设为生效：写进配置文件（立即生效），记下启用时间，推一条通知。"""
        _check_side(side)
        with _LOCK, self._guard():
            self.ensure()
            doc = self._read(side, sid)
            if doc is None:
                raise KeyError(sid)
            content = normalize_content(side, doc["content"])
            errs = self.validate(side, content)
            if errs:
                raise StrategyError("这套策略有错误，不能生效：" + "；".join(errs))
            act = self.active()
            prev = act.get(side) or {}
            self._apply_to_cfg(side, content)
            act[side] = {"id": sid, "name": doc["name"], "since": ts(), "by": by, "prev": prev.get("name")}
            write_json(self._active_file(), act)
        msg = f"{SIDE_NAMES[side]}策略已切换：{prev.get('name') or '（无）'} → {doc['name']}（{by}）"
        try:
            self.engine.notifier.notify("strategy_switch", f"{SIDE_NAMES[side]}策略切换",
                                        f"- {msg}\n- 自动交易开关不受影响（开关和生效策略是两回事）")
        except Exception:  # noqa: BLE001
            pass
        try:
            from .util import append_jsonl
            append_jsonl(self.root / "strategies" / "switch_log.jsonl",
                         {"time": ts(), "side": side, "id": sid, "name": doc["name"], "prev": prev.get("name"), "by": by})
        except Exception:  # noqa: BLE001
            pass
        return {"active": act[side], "message": msg}

    # ---- 导入导出 ----
    def export(self, side: str, sid: str | None = None, template: str | None = None) -> dict:
        _check_side(side)
        if template:
            t = self.template(side, template)
            name, note, content = t["title"], t["description"], t["content"]
        else:
            d = self.get(side, sid)
            name, note, content = d["name"], d.get("note") or "", d["content"]
        return {"format": EXPORT_FORMAT, "version": EXPORT_VERSION, "side": side, "name": name, "note": note,
                "exported_at": ts(), "content": strip_secrets(copy.deepcopy(content))}

    @staticmethod
    def parse_import(text_or_obj, side: str) -> dict:
        if isinstance(text_or_obj, str):
            try:
                obj = json.loads(text_or_obj)
            except json.JSONDecodeError as e:
                raise StrategyError(f"不是有效的策略文本（JSON 第 {e.lineno} 行有误）") from None
        else:
            obj = text_or_obj
        if not isinstance(obj, dict):
            raise StrategyError("不是有效的策略文本")
        if obj.get("format") == EXPORT_FORMAT:
            if obj.get("side") != side:
                raise StrategyError(f"这是{SIDE_NAMES.get(obj.get('side'), '未知')}策略，不能导入到{SIDE_NAMES[side]}策略")
            return {"name": obj.get("name") or "导入的策略", "note": obj.get("note") or "", "content": obj.get("content") or {}}
        if "layers" in obj:          # 直接粘贴一段 {layers: [...]}
            return {"name": obj.get("name") or "导入的策略", "note": "", "content": obj}
        raise StrategyError("识别不了：策略文本里没有 format/layers")

    def import_preview(self, side: str, text, compare_to: str | None = None) -> dict:
        p = self.parse_import(text, side)
        self.ensure()
        ref_id = compare_to or (self.active().get(side) or {}).get("id")
        ref = self._read(side, ref_id or "") or {"content": {}, "name": "（无）"}
        content = normalize_content(side, p["content"], ref["content"])
        errs = self.validate(side, content)
        return {"name": p["name"], "note": p["note"], "compare_to": ref.get("name"), "errors": errs,
                "changes": content_diff(side, normalize_content(side, ref["content"]), content), "content": content}

    def import_save(self, side: str, text, name: str | None = None) -> dict:
        p = self.import_preview(side, text)
        if p["errors"]:
            raise StrategyError("；".join(p["errors"]))
        existing = {i["name"] for i in self.list(side)["items"]}
        nm = (name or p["name"]).strip()
        if nm in existing:
            nm = f"{nm}（导入 {time.strftime('%m-%d %H:%M')}）"
        return self.create(side, nm, p["content"], note=p["note"])

    # ---- 模板 ----
    def default_template_id(self, side: str) -> str:
        """「默认模板」的 id：新建空白策略时用它当骨架。
        没设过、或设的那套已经被删掉时，回退到内置的 default（买入）/ classic（卖出）。
        注意：这里不能调 template()（它会走 templates()，而 templates() 又要问本函数，会递归）。"""
        _check_side(side)
        fallback = "default" if side == "buy" else "classic"
        defaults = read_json(self.root / "templates" / "defaults.json", None) or {}
        tid = str(defaults.get(side) or fallback)
        if tid in self._builtin(side) or (self._tdir(side) / f"{tid}.json").exists():
            return tid
        return fallback

    def templates(self, side: str) -> list[dict]:
        _check_side(side)
        dflt = self.default_template_id(side)
        out = []
        for k, t in self._builtin(side).items():
            out.append({"id": k, "title": t["title"], "description": t["description"], "builtin": True,
                        "is_default": k == dflt, "content": t["content"]})
        for f in sorted(self._tdir(side).glob("*.json")):
            d = read_json(f, None)
            if isinstance(d, dict) and "content" in d:
                out.append({"id": d["id"], "title": d.get("title") or d["id"], "description": d.get("description") or "",
                            "builtin": False, "is_default": d["id"] == dflt, "content": d["content"],
                            "created_at": d.get("created_at")})
        return out

    def _builtin(self, side: str) -> dict:
        from .config import DEFAULTS
        out = {}
        if side == "buy":
            from .funnel import _HARD
            out["default"] = {"title": "出厂默认", "description": "只有一层硬性过滤（排除 ST、科创板/北交所、市值 30~800 亿），每次买 1 只。在它基础上加层最省事。",
                              "content": {"layers": [copy.deepcopy(_HARD)], "select": copy.deepcopy(DEFAULTS["select"])}}
        else:
            ex = {k: copy.deepcopy(v) for k, v in DEFAULTS["exits"].items() if k in EXIT_CONTENT_KEYS}
        for k, t in builtin_templates(side).items():
            c = {"layers": copy.deepcopy(t["layers"])}
            if side == "sell":
                c["exits"] = copy.deepcopy(ex)
            out[k] = {"title": t["title"], "description": t["description"], "content": c}
        return {k: v for k, v in out.items() if v}

    def template(self, side: str, tid: str) -> dict:
        for t in self.templates(side):
            if t["id"] == tid:
                return t
        raise KeyError(tid)

    def save_template(self, side: str, title: str, description: str, content: dict) -> dict:
        _check_side(side)
        title = (title or "").strip()[:40]
        if not title:
            raise StrategyError("给模板起个名字")
        content = normalize_content(side, content)
        errs = self.validate(side, content)
        if errs:
            raise StrategyError("；".join(errs))
        with self._guard():
            d = self._tdir(side)
            base = f"t{time.strftime('%Y%m%d%H%M%S')}"
            tid, i = base, 2
            while (d / f"{tid}.json").exists():
                tid, i = f"{base}_{i}", i + 1
            doc = {"id": tid, "title": title, "description": (description or "").strip()[:200], "created_at": ts(),
                   "content": strip_secrets(content)}
            write_json(d / f"{tid}.json", doc)
        return {**doc, "builtin": False}

    def update_template(self, side: str, tid: str, title: str | None = None, description: str | None = None) -> dict:
        if tid in self._builtin(side):
            raise StrategyError("自带模板不能改，可以另存一份再改")
        f = self._tdir(side) / f"{tid}.json"
        d = read_json(f, None) if re.fullmatch(r"[A-Za-z0-9_\-]+", tid) else None
        if not d:
            raise KeyError(tid)
        with self._guard():
            d = read_json(f, None) or d
            if title is not None and title.strip():
                d["title"] = title.strip()[:40]
            if description is not None:
                d["description"] = description.strip()[:200]
            write_json(f, d)
        return d

    def delete_template(self, side: str, tid: str) -> None:
        if tid in self._builtin(side):
            raise StrategyError("自带模板不能删")
        f = self._tdir(side) / f"{tid}.json"
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", tid) or not f.exists():
            raise KeyError(tid)
        with self._guard():
            f.unlink()
            defp = self.root / "templates" / "defaults.json"
            defaults = read_json(defp, None) or {}
            if defaults.get(side) == tid:
                defaults.pop(side)
                write_json(defp, defaults)

    def set_default_template(self, side: str, tid: str | None) -> dict:
        _check_side(side)
        if tid:
            self.template(side, tid)        # 不存在抛 KeyError
        f = self.root / "templates" / "defaults.json"
        with self._guard():
            defaults = read_json(f, None) or {}
            if tid:
                defaults[side] = tid
            else:
                defaults.pop(side, None)
            write_json(f, defaults)
        return {"side": side, "default": defaults.get(side) or ("default" if side == "buy" else "classic")}

    def template_preview(self, side: str, tid: str, sid: str | None = None, draft: dict | None = None) -> dict:
        """套用模板前的差异：相对正在编辑的那套（或草稿），只返回内容，不保存。"""
        t = self.template(side, tid)
        if draft is not None:
            base = normalize_content(side, draft)
        else:
            self.ensure()
            doc = self._read(side, sid or (self.active().get(side) or {}).get("id", ""))
            base = normalize_content(side, (doc or {}).get("content") or {})
        new = normalize_content(side, t["content"], base)
        return {"template": t["title"], "content": new, "changes": content_diff(side, base, new)}
