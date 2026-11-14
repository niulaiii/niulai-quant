"""外部因子和数据源的绑定。

外部因子都挂在一个数据源下面，网页上先点选数据源，再点选这个数据源提供的因子或字段：

  push 数据源（[sources.x] type="push"）
      字段 ← 外部程序 POST /api/v1/sources/<x>/fields、PUT .../pools/<股池> 里带的 fields
      因子 ← POST /api/v1/factor-results/<名字> 时 source=<x> 的结果
  http 数据源（type="http"）
      字段 ← fields_url 返回过的字段（自动记下），加上配置里 fields = [...] 写明的字段
      因子 ← 配置里 factor_url（和「外部因子（HTTP 拉取）」同一种协议）+ factor_names = [...]
  @api（不用配置）
      因子 ← POST /api/v1/factor-results/<名字> 没带 source（或 source 不是已配置的数据源）的结果

「可用」的口径：数据源已配置、没停用、类型认得，并且
  push：什么都没推送过 → 不可用；推送过但今天还没有 → 可用（注明「今天还没推送」，取数时按数据缺失）
  http：最近一次请求失败 → 不可用；还没请求过 → 未知（可点「测试连接」）
数据源不可用、或者这只票的数据没拿到，因子一律按「数据缺失」处理。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from .util import read_json, today_str, update_json, write_json

EXT_TYPES = ("push", "http")
API_SOURCE = "@api"
API_TITLE = "REST API 推送（没指定数据源）"

_LOCK = threading.Lock()
# 各数据源最近一次请求的结果（进程内）：{data_dir: {name: {"ok", "at", "error"}}}
_STATUS: dict[str, dict[str, dict]] = {}


def record(data_dir, name: str, ok: bool, error: str = "", fields: list[str] | None = None) -> None:
    """记下一次请求的结果；成功时把返回过的字段名存起来供点选。"""
    with _LOCK:
        _STATUS.setdefault(str(data_dir), {})[name] = {"ok": bool(ok), "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                                       "error": (error or "")[:200]}
        if ok and fields:
            p = Path(data_dir) / "external" / "seen_fields" / f"{name}.json"
            try:
                update_json(p, lambda cur: sorted(set(cur or []) | {str(f) for f in fields})[:500], default=[])
            except Exception:  # noqa: BLE001
                pass


def last_status(data_dir, name: str) -> dict | None:
    return (_STATUS.get(str(data_dir)) or {}).get(name)


def _seen_fields(data_dir, name: str) -> list[str]:
    return list(read_json(Path(data_dir) / "external" / "seen_fields" / f"{name}.json", []) or [])


def _push_fields(store, name: str) -> tuple[list[str], str | None]:
    """push 数据源推送过的字段名（最近一次字段 + 各股池最近一次里的字段）和最近推送日。"""
    names: set[str] = set()
    latest = None
    fdir = store.dir / "sources" / name / "fields"
    if fdir.exists():
        files = sorted(fdir.glob("*.json"))
        if files:
            latest = files[-1].stem
            for v in ((read_json(files[-1], {}) or {}).get("items") or {}).values():
                names.update((v or {}).keys())
    for p in store.list_pools(name):
        if p.get("latest_date"):
            latest = max(latest or "", p["latest_date"])
            rec = store.get_pool(name, p["key"], p["latest_date"]) or {}
            for s in rec.get("stocks") or []:
                names.update((s.get("fields") or {}).keys())
    return sorted(names), latest


def _factor_results_by_source(store, configured: set[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for fr in store.list_factor_results():
        rec = store.get_factor_results(fr["name"], fr["latest_date"]) or {}
        src = str(rec.get("source") or "api")
        sid = src if src in configured else API_SOURCE
        out.setdefault(sid, []).append({"name": fr["name"], "title": fr["name"], "latest_date": fr["latest_date"],
                                        "count": fr["count"], "updated_at": fr.get("updated_at"), "kind": "external"})
    return out


def _as_list(v) -> list[str]:
    if isinstance(v, str):
        v = [x for x in v.replace("，", ",").split(",")]
    return [str(x).strip() for x in (v or []) if str(x).strip()]


def sources(cfg: dict, data_dir, store, day: str | None = None) -> list[dict]:
    """已配置的外部数据源（push/http）+ @api，各自提供的因子和字段，以及现在是否可用。"""
    day = day or today_str()
    conf = {n: s for n, s in (cfg.get("sources") or {}).items() if isinstance(s, dict) and s.get("type") in EXT_TYPES}
    by_src = _factor_results_by_source(store, set(conf))
    out = []
    for name, s in conf.items():
        t = s.get("type")
        item = {"id": name, "title": str(s.get("title") or name), "type": t,
                "type_title": "外部推送" if t == "push" else "外部 HTTP", "enabled": bool(s.get("enabled", True)),
                "factors": list(by_src.get(name, [])), "fields": []}
        if t == "push":
            flds, latest = _push_fields(store, name)
            flds = sorted(set(flds) | set(_as_list(s.get("fields"))))
            item["fields"] = flds
            item["latest_date"] = latest or max((f["latest_date"] for f in item["factors"]), default=None)
            if not item["enabled"]:
                item.update(available=False, status="已停用")
            elif not item["latest_date"]:
                item.update(available=False, status="还没收到过推送")
            elif item["latest_date"] < day:
                item.update(available=True, status=f"可用，最近推送 {item['latest_date']}（今天还没推送，取数按数据缺失）")
            else:
                item.update(available=True, status=f"可用，今天已推送")
        else:
            item["fields"] = sorted(set(_seen_fields(data_dir, name)) | set(_as_list(s.get("fields"))))
            if s.get("factor_url"):
                names = _as_list(s.get("factor_names")) or [""]
                for n in names:
                    item["factors"].append({"name": n, "title": n or "（这个数据源的因子接口）", "kind": "http"})
            st = last_status(data_dir, name)
            if not item["enabled"]:
                item.update(available=False, status="已停用")
            elif not (s.get("fields_url") or s.get("factor_url")):
                item.update(available=False, status="没填 fields_url 或 factor_url，提供不了因子和字段")
            elif st is None:
                item.update(available=None, status="还没请求过，可在设置里「测试连接」")
            elif st["ok"]:
                item.update(available=True, status=f"可用，最近一次请求成功（{st['at']}）")
            else:
                item.update(available=False, status=f"不可用：最近一次请求失败（{st['at']}）{st['error']}")
        out.append(item)
    api = by_src.get(API_SOURCE, [])
    out.append({"id": API_SOURCE, "title": API_TITLE, "type": "api", "type_title": "REST API", "enabled": True,
                "factors": api, "fields": [], "available": bool(api),
                "latest_date": max((f["latest_date"] for f in api), default=None),
                "status": ("可用" if api else "还没收到过推送") +
                          "；外部程序 POST /api/v1/factor-results/<名字> 时带 source=<数据源名> 就会归到那个数据源下"})
    return out


def snapshot(cfg: dict, data_dir, store, day: str | None = None) -> dict[str, dict]:
    """给因子用的简表：{id: {type, available, status, cfg}}。"""
    snap = {}
    for it in sources(cfg, data_dir, store, day):
        snap[it["id"]] = {"type": it["type"], "available": it["available"], "status": it["status"], "title": it["title"],
                          "cfg": dict((cfg.get("sources") or {}).get(it["id"]) or {})}
    return snap


def resolve(ctx, source: str) -> tuple[dict | None, str]:
    """因子运行时查绑定的数据源。返回 (数据源简表, 不可用时的原因)。没绑定（旧配置）返回 (None, "")。"""
    if not source:
        return None, ""
    snap = (ctx.extra or {}).get("ext_sources")
    if snap is None:
        return None, ""          # 回测/单测等没有数据源信息的场景：不挡
    s = snap.get(source)
    if s is None:
        return None, f"数据源 {source} 没有配置"
    if s.get("available") is False:
        return s, f"数据源 {s.get('title') or source} {s.get('status') or '不可用'}"
    return s, ""
