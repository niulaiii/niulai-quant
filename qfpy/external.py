"""开放接口的底层：外部因子结果、外部推送的股池/字段的存储，以及拉取外部 HTTP 接口的工具。

两种接入方式：
  推（push）：外部程序调用本系统的 REST API（/api/v1/factor-results/...、/api/v1/sources/...）写进来，
              落在 data/external/ 下，因子 kind="external" / 数据源 type="push" 读取。
  拉（pull）：因子 kind="http" / 数据源 type="http" 在运行时请求外部地址。
接口格式见 docs/API.md。
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

from .util import lock_for, process_lock, read_json, today_str, ts, write_json

NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
CODE_RE = re.compile(r"^\d{6}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_ITEMS = 5000


def check_name(name: str, what: str = "名字") -> str:
    if not NAME_RE.match(name or ""):
        raise ValueError(f"{what} 只能用字母、数字、_ 和 -，1~64 个字符：{name!r}")
    return name


def check_date(d: str | None) -> str:
    d = d or today_str()
    if not DATE_RE.match(str(d)):
        raise ValueError(f"日期格式应为 YYYY-MM-DD：{d!r}")
    return str(d)


def http_json(method: str, url: str, body: Any = None, headers: dict | None = None, timeout: float = 10.0) -> Any:
    """请求外部接口并解析 JSON。测试里会把这个函数换掉（测试不联网）。"""
    import httpx
    if not str(url).startswith(("http://", "https://")):
        raise ValueError(f"地址必须以 http:// 或 https:// 开头：{url}")
    with httpx.Client(timeout=timeout, trust_env=False) as cl:
        r = cl.request(method, url, json=body if method.upper() != "GET" else None,
                       params=body if method.upper() == "GET" and isinstance(body, dict) else None, headers=headers or {})
    if r.status_code >= 400:
        raise RuntimeError(f"{method} {url} 返回 HTTP {r.status_code}：{r.text[:200]}")
    try:
        return r.json()
    except ValueError as e:
        raise RuntimeError(f"{method} {url} 返回的不是 JSON：{r.text[:200]}") from e


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ValueError(f"score 应为数字：{v!r}") from None


def normalize_results(obj: Any) -> dict[str, dict]:
    """把外部因子结果统一成 {code: {pass, score, note}}。

    接受：{"results": [...]} / {"items": [...]} / {"results": {code: {...}}} / 直接一个列表。
    列表元素：{"code": "600000", "pass": true, "score": 0.8, "note": "..."}，pass 和 score 至少一个。
    """
    if isinstance(obj, dict):
        obj = obj.get("results", obj.get("items", obj))
    items: list = []
    if isinstance(obj, dict):
        items = [{"code": k, **(v if isinstance(v, dict) else {"pass": v})} for k, v in obj.items()]
    elif isinstance(obj, list):
        items = obj
    else:
        raise ValueError("结果应为列表或 {code: 结果} 对象")
    if len(items) > MAX_ITEMS:
        raise ValueError(f"一次最多 {MAX_ITEMS} 条")
    out: dict[str, dict] = {}
    for it in items:
        if not isinstance(it, dict):
            raise ValueError(f"结果项应为对象：{it!r}")
        code = str(it.get("code") or it.get("symbol") or "").strip()
        if not CODE_RE.match(code):
            raise ValueError(f"股票代码应为 6 位数字：{code!r}")
        p = it.get("pass", it.get("passed"))
        s = _num(it.get("score"))
        if p is None and s is None:
            raise ValueError(f"{code}：pass 和 score 至少要有一个")
        out[code] = {"pass": None if p is None else bool(p), "score": s, "note": str(it.get("note") or "")[:200]}
    return out


class ExternalStore:
    """data/external/ 下的外部数据：
    factors/<名字>/<日期>.json            外部因子结果
    sources/<数据源>/pools/<股池>/<日期>.json  推送的股池
    sources/<数据源>/fields/<日期>.json      推送的行情/自定义字段
    """

    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir) / "external"
        self.lock = threading.Lock()

    # ---- 因子结果 ----
    def put_factor_results(self, name: str, results: Any, date: str | None = None, source: str = "api",
                           replace: bool = True) -> dict:
        check_name(name, "因子名")
        date = check_date(date)
        items = normalize_results(results)
        p = self.dir / "factors" / name / f"{date}.json"
        with self.lock, process_lock(lock_for(p)):
            cur = {} if replace else (read_json(p, {}) or {}).get("items", {})
            cur.update(items)
            rec = {"name": name, "date": date, "updated_at": ts(), "source": source, "items": cur}
            write_json(p, rec)
        return {"name": name, "date": date, "count": len(cur), "updated_at": rec["updated_at"]}

    def get_factor_results(self, name: str, date: str | None = None) -> dict | None:
        check_name(name, "因子名")
        return read_json(self.dir / "factors" / name / f"{check_date(date)}.json", None)

    def list_factor_results(self) -> list[dict]:
        out = []
        base = self.dir / "factors"
        if base.exists():
            for d in sorted(p for p in base.iterdir() if p.is_dir()):
                files = sorted(d.glob("*.json"))
                if files:
                    last = read_json(files[-1], {}) or {}
                    out.append({"name": d.name, "latest_date": files[-1].stem, "count": len(last.get("items", {})),
                                "updated_at": last.get("updated_at"), "dates": len(files)})
        return out

    # ---- 推送的股池和字段 ----
    def put_pool(self, source: str, pool: str, stocks: list, date: str | None = None, name: str = "") -> dict:
        check_name(source, "数据源名")
        check_name(pool, "股池 key")
        date = check_date(date)
        if not isinstance(stocks, list) or len(stocks) > MAX_ITEMS:
            raise ValueError(f"stocks 应为列表，最多 {MAX_ITEMS} 只")
        clean = []
        for s in stocks:
            if isinstance(s, str):
                s = {"code": s}
            code = str(s.get("code") or "").strip()
            if not CODE_RE.match(code):
                raise ValueError(f"股票代码应为 6 位数字：{code!r}")
            clean.append({"code": code, "name": str(s.get("name") or ""), "fields": _clean_fields(s.get("fields") or {})})
        rec = {"source": source, "pool": pool, "name": name or pool, "date": date, "updated_at": ts(), "stocks": clean}
        p = self.dir / "sources" / source / "pools" / pool / f"{date}.json"
        with self.lock, process_lock(lock_for(p)):
            write_json(p, rec)
        return {"source": source, "pool": pool, "date": date, "count": len(clean)}

    def get_pool(self, source: str, pool: str, date: str, fallback_latest: bool = False) -> dict | None:
        d = self.dir / "sources" / source / "pools" / pool
        rec = read_json(d / f"{date}.json", None)
        if rec is None and fallback_latest and d.exists():
            files = sorted(d.glob("*.json"))
            rec = read_json(files[-1], None) if files else None
        return rec

    def list_pools(self, source: str) -> list[dict]:
        base = self.dir / "sources" / source / "pools"
        out = []
        if base.exists():
            for d in sorted(p for p in base.iterdir() if p.is_dir()):
                files = sorted(d.glob("*.json"))
                last = read_json(files[-1], {}) if files else {}
                out.append({"key": d.name, "name": (last or {}).get("name") or d.name, "latest_date": files[-1].stem if files else None,
                            "count": len((last or {}).get("stocks", []))})
        return out

    def put_fields(self, source: str, items: Any, date: str | None = None, replace: bool = False) -> dict:
        check_name(source, "数据源名")
        date = check_date(date)
        if isinstance(items, dict):
            items = [{"code": k, "fields": v} for k, v in items.items()]
        if not isinstance(items, list) or len(items) > MAX_ITEMS:
            raise ValueError(f"items 应为列表，最多 {MAX_ITEMS} 条")
        p = self.dir / "sources" / source / "fields" / f"{date}.json"
        with self.lock, process_lock(lock_for(p)):
            cur = {} if replace else (read_json(p, {}) or {}).get("items", {})
            for it in items:
                code = str(it.get("code") or "").strip()
                if not CODE_RE.match(code):
                    raise ValueError(f"股票代码应为 6 位数字：{code!r}")
                cur.setdefault(code, {}).update(_clean_fields(it.get("fields") or {}))
            write_json(p, {"source": source, "date": date, "updated_at": ts(), "items": cur})
        return {"source": source, "date": date, "count": len(cur)}

    def get_fields(self, source: str, date: str) -> dict[str, dict]:
        return (read_json(self.dir / "sources" / source / "fields" / f"{date}.json", {}) or {}).get("items", {})


def _clean_fields(f: dict) -> dict:
    if not isinstance(f, dict):
        raise ValueError("fields 应为对象")
    out = {}
    for k, v in f.items():
        if not str(k).isidentifier():
            raise ValueError(f"字段名 {k!r} 不合法")
        if v is None or isinstance(v, bool):
            out[k] = v
            continue
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"字段 {k} 的值应为数字：{v!r}") from None
    return out
