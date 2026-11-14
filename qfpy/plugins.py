"""插件加载。

两种写法，放进 plugins/ 就自动注册（启动时加载；网页「重新加载插件」或 POST /api/v1/plugins/reload 也会加载新文件）：

1. 单文件：plugins/<名字>.py（以 _ 开头的文件会被跳过）。文件里可以写
       PLUGIN = {"name": "my_factor", "description": "...", "enabled": True}
   不写就用文件名。
2. 目录：plugins/<名字>/plugin.toml + plugin.py（沿用 tick-stock-panel 的目录式插件做法）：
       name = "ma_trend"
       description = "..."
       enabled = true
       module = "plugin"          # 同目录下的 plugin.py

插件里用装饰器注册：
    from qfpy.factors import Factor, Outcome, ParamSpec, register_factor      # 因子（sides 声明买/卖）
    from qfpy.datasources import DataSource, register_source                # 数据源
    from qfpy.notify import Channel, register_channel                       # 推送渠道
    from qfpy.expr import register_fields                                   # 登记表达式字段
插件加载失败只记日志和插件列表，不影响主程序。插件在本进程里运行，只放你自己写的或审过的代码。
"""
from __future__ import annotations

import importlib.util
import re
import sys
import tomllib
from pathlib import Path

from .util import log

_loaded: dict[str, dict] = {}   # key = 文件路径；同一文件没改过就不重复执行

# 「人工保留」策略注册表：核心只在保留判断处问一句，模式逻辑全部在插件里。
HOLD_POLICIES: dict[str, object] = {}


def register_hold_policy(name: str, policy: object) -> None:
    """注册保留策略对象（需有 decide(...) 方法）。返回 None 表示不干预、走核心默认。"""
    HOLD_POLICIES[name] = policy



def _exec(name: str, mod_file: Path, info: dict) -> dict:
    from .datasources import SOURCE_TYPES
    from .factors import FACTOR_TYPES
    from .notify import CHANNEL_TYPES
    key = str(mod_file.resolve())
    mtime = mod_file.stat().st_mtime
    old = _loaded.get(key)
    if old and old.get("_mtime") == mtime:
        return old
    # 记下注册表里每个 kind 当前的对象：同名 kind 被别的实例（或旧测试）先注册过，也能按“对象变了”认出本插件注册的
    before = (dict(FACTOR_TYPES), dict(SOURCE_TYPES), dict(CHANNEL_TYPES))
    try:
        modname = "qf_plugin_" + re.sub(r"\W", "_", name)
        spec = importlib.util.spec_from_file_location(modname, mod_file)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        def _new(reg, old_reg):
            return sorted(k for k, v in reg.items() if old_reg.get(k) is not v)
        regs = {"factors": _new(FACTOR_TYPES, before[0]), "sources": _new(SOURCE_TYPES, before[1]),
                "channels": _new(CHANNEL_TYPES, before[2])}
        # 插件不能覆盖内置因子/数据源：同名 kind 拒绝注册，恢复内置的，并在插件列表里写明
        conflicts = []
        for reg, old_reg, key in ((FACTOR_TYPES, before[0], "factors"), (SOURCE_TYPES, before[1], "sources")):
            for k in list(regs[key]):
                prev = old_reg.get(k)
                if prev is not None and getattr(prev, "plugin", None) in (None, "", "builtin"):
                    reg[k] = prev
                    regs[key].remove(k)
                    conflicts.append(k)
        if conflicts:
            info["conflicts"] = conflicts
            info["warning"] = f"kind {'、'.join(conflicts)} 和内置的重名，没有注册（内置的照常用）。请换个 kind"
            log.warning("插件 %s 的 kind %s 和内置重名，已拒绝注册", name, conflicts)
        if old and old.get("ok"):   # 改过的插件重新加载：同名 kind 被覆盖，列表沿用上次
            for k in ("factors", "sources", "channels"):
                regs[k] = sorted(set(regs[k]) | set(old.get("registered", {}).get(k, [])))
        for k in regs["factors"]:
            if k in FACTOR_TYPES:
                FACTOR_TYPES[k].plugin = name
        for k in regs["sources"]:
            if k in SOURCE_TYPES:
                SOURCE_TYPES[k].plugin = name
        info.update(ok=True, registered=regs)
        log.info("已加载插件 %s：%s", name, regs)
    except Exception as e:  # noqa: BLE001
        info.update(ok=False, error=f"{type(e).__name__}: {e}")
        log.warning("插件 %s 加载失败：%s", name, e)
    info["_mtime"] = mtime
    _loaded[key] = info
    return info


def _peek_meta(py: Path) -> dict:
    """不执行代码，只从源码里读 PLUGIN = {...} 字面量（用来判断 enabled）。"""
    import ast
    try:
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PLUGIN" for t in node.targets):
                v = ast.literal_eval(node.value)
                return v if isinstance(v, dict) else {}
    except Exception:  # noqa: BLE001
        return {}
    return {}


def load_plugins(directory: Path) -> list[dict]:
    out = []
    if not directory.exists():
        return out
    for d in sorted(directory.iterdir()):
        if d.name.startswith(("_", ".")):
            continue
        if d.is_file() and d.suffix == ".py":
            meta = _peek_meta(d)
            name = str(meta.get("name") or d.stem)
            info = {"dir": d.name, "file": d.name, "name": name, "description": meta.get("description", ""),
                    "enabled": meta.get("enabled", True), "style": "单文件"}
            if not info["enabled"]:
                out.append({**info, "ok": True, "note": "已禁用"})
                continue
            out.append(_exec(name, d, info))
            continue
        if not d.is_dir():
            continue
        manifest = d / "plugin.toml"
        if not manifest.exists():
            continue
        try:
            meta = tomllib.loads(manifest.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            out.append({"dir": d.name, "ok": False, "error": f"plugin.toml 解析失败：{e}"})
            continue
        name = meta.get("name", d.name)
        info = {"dir": d.name, "name": name, "description": meta.get("description", ""), "enabled": meta.get("enabled", True),
                "style": "目录"}
        if not info["enabled"]:
            out.append({**info, "ok": True, "note": "已禁用"})
            continue
        out.append(_exec(name, d / (meta.get("module", "plugin") + ".py"), info))
    return [{k: v for k, v in i.items() if not k.startswith("_")} for i in out]
