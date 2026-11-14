"""形态信号库：自带信号（config/signals/，只读）+ 自定义信号（data/signals/，可保存/改名/删除）。

文件格式与 tick-stock-panel 自定义信号相同：
  {"id", "name", "kind": "both", "timeframe": "daily", "enabled": true,
   "conditions": [{"left", "op", "right", "leftDays", "rightDays"}], "column": "signal_<id>"}
策略里的形态信号因子用 params.file 指向信号文件（相对项目根目录），所以旧配置、回测都不受影响。
改名只改文件里的 name，文件名（id）不变，引用它的策略不会失效。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .util import atomic_write_text, lock_for, process_lock
from .vendor.tsp_signal import OPS

BUILTIN_DIR = "config/signals"


def custom_dir(root, data_dir=None) -> Path:
    return Path(data_dir or Path(root) / "data") / "signals"


def _rel(root, p: Path) -> str:
    try:
        return p.resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return str(p)


def _read(f: Path) -> dict | None:
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except Exception:  # noqa: BLE001
        return None


def list_signals(root, data_dir=None) -> list[dict]:
    out = []
    for builtin, d in ((True, Path(root) / BUILTIN_DIR), (False, custom_dir(root, data_dir))):
        if not d.exists():
            continue
        for f in sorted(d.glob("*.json")):
            s = _read(f)
            if not s:
                continue
            out.append({"id": s.get("id") or f.stem, "name": s.get("name") or f.stem, "builtin": builtin,
                        "file": _rel(root, f), "conditions": s.get("conditions") or [],
                        "text": describe(s.get("conditions") or [])})
    return out


def describe(conds: list[dict]) -> str:
    parts = []
    for c in conds:
        ld, rd = int(c.get("leftDays") or 0), int(c.get("rightDays") or 0)
        left = f"{c.get('left')}" + (f"（{ld}天前）" if ld else "")
        right = str(c.get("right", "")).replace("field:", "") + (f"（{rd}天前）" if rd else "")
        parts.append(f"{left} {c.get('op')} {right}")
    return " 且 ".join(parts)


def validate(conds: list[dict]) -> list[dict]:
    """校验并规范化条件；出错抛 ValueError（中文）。"""
    from .expr import KNOWN_FIELDS, LAG_ALIAS
    from .market import DAILY_FIELDS
    if not conds:
        raise ValueError("至少要有一条条件")
    out = []
    for i, c in enumerate(conds, 1):
        op = c.get("op")
        if op not in OPS:
            raise ValueError(f"第 {i} 条：不支持的比较符 {op}")
        left = str(c.get("left", "")).strip()
        right = str(c.get("right", "")).strip()
        ld, rd = int(c.get("leftDays") or 0), int(c.get("rightDays") or 0)
        if not 0 <= ld <= 60 or not 0 <= rd <= 60:
            raise ValueError(f"第 {i} 条：「几天前」应在 0~60")
        for side, v, days in (("左边", left, ld), ("右边", right, rd)):
            name = v.replace("field:", "")
            try:
                float(name)
                if side == "左边":
                    raise ValueError(f"第 {i} 条：左边要选一个字段")
                continue
            except ValueError as e:
                if "左边要选" in str(e):
                    raise
            if not name or name not in KNOWN_FIELDS:
                raise ValueError(f"第 {i} 条：{side}的字段 {name or '（空）'} 不认识")
            if days and LAG_ALIAS.get(name, name) not in DAILY_FIELDS:
                raise ValueError(f"第 {i} 条：{name} 不是日K字段，不能取几天前的值")
        try:
            float(right)
            right_out = right
        except ValueError:
            right_out = right if right.startswith("field:") else "field:" + right
        out.append({"left": left.replace("field:", ""), "op": op, "right": right_out, "leftDays": ld, "rightDays": rd})
    return out


def _slug(name: str, existing: set[str]) -> str:
    base = re.sub(r"[^a-zA-Z0-9_]+", "_", name).strip("_").lower() or "signal"
    if not re.match(r"^[a-z]", base):
        base = "sig_" + base
    sid, i = base, 2
    while sid in existing:
        sid, i = f"{base}_{i}", i + 1
    return sid


def save_signal(root, data_dir, name: str, conds: list[dict], sid: str | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("给信号起个名字")
    conds = validate(conds)
    d = custom_dir(root, data_dir)
    d.mkdir(parents=True, exist_ok=True)
    builtin_ids = {s["id"] for s in list_signals(root, data_dir) if s["builtin"]}
    if sid:
        if sid in builtin_ids and not (d / f"{sid}.json").exists():
            raise ValueError("自带信号不能改，请另存为新信号")
        if not re.match(r"^[A-Za-z0-9_\-]+$", sid):
            raise ValueError("信号 id 只能用字母数字下划线")
    else:
        existing = {f.stem for f in d.glob("*.json")} | builtin_ids
        sid = _slug(name, existing) if re.search(r"[A-Za-z0-9]", name) else _slug("custom", existing)
    doc = {"id": sid, "name": name, "kind": "both", "timeframe": "daily", "enabled": True,
           "conditions": conds, "column": f"signal_{sid}"}
    f = d / f"{sid}.json"
    with process_lock(lock_for(f)):
        atomic_write_text(f, json.dumps(doc, ensure_ascii=False, indent=2))
    return {**doc, "file": _rel(root, f), "builtin": False}


def rename_signal(root, data_dir, sid: str, name: str) -> dict:
    f = custom_dir(root, data_dir) / f"{sid}.json"
    s = _read(f) if f.exists() else None
    if not s:
        raise ValueError("只能改名自定义信号")
    return save_signal(root, data_dir, name, s.get("conditions") or [], sid)


def delete_signal(root, data_dir, sid: str, used_by: list[str] | None = None) -> None:
    f = custom_dir(root, data_dir) / f"{sid}.json"
    if not f.exists():
        raise ValueError("只能删除自定义信号（自带信号不能删）")
    if used_by:
        raise ValueError("这个信号还在用：" + "、".join(used_by) + "。先从策略里去掉再删")
    with process_lock(lock_for(f)):
        f.unlink(missing_ok=True)
