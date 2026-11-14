"""配置修改的公共逻辑：网页（/api/config、/api/funnel）和对外 REST API（/api/v1）共用。

所有写配置的路径都走这里：合并 → 校验（含漏斗/卖出链/数据源）→ 写回 TOML（留 .bak）→ 热生效。
只读段（execution、live、web、data_dir、api）不能从这里改。
"""
from __future__ import annotations

import copy
import os

from .datasources import SOURCE_TYPES
from .external import check_name
from .funnel import SIDES, config_problems, layers_of, normalize_layers, template_layers


class ConfigError(ValueError):
    pass


def problems(new: dict, root) -> list[str]:
    probs = config_problems(new, root)
    for k, v in (new.get("select") or {}).get("weights", {}).items():
        if not isinstance(v, (int, float)):
            probs.append(f"权重 {k} 必须是数字")
    for name, sc in (new.get("sources") or {}).items():
        if not isinstance(sc, dict):
            probs.append(f"数据源 {name} 必须是对象")
        elif sc.get("type") not in SOURCE_TYPES:
            probs.append(f"数据源 {name}：未知类型 {sc.get('type')}（插件没加载？）")
    return probs


def apply_patch(engine, patch: dict, dry_run: bool = False) -> dict:
    store = engine.store
    try:
        new = store.prepare_update(patch)
    except ValueError as e:
        raise ConfigError(str(e)) from None
    probs = problems(new, engine.root)
    if probs:
        raise ConfigError("；".join(probs))
    if not dry_run:
        store.commit(new)
    return new


def funnel_section(side: str) -> str:
    if side not in SIDES:
        raise ConfigError("side 只能是 buy 或 sell")
    return "buy_funnel" if side == "buy" else "sell_funnel"


def get_funnel(engine, side: str) -> dict:
    sec = funnel_section(side)
    cfg = engine.cfg
    explicit = "layers" in (cfg.get(sec) or {})
    return {"side": side, "layers": layers_of(cfg, side), "explicit": explicit,
            "source": sec if explicit else ("factors（旧的平铺写法）" if side == "buy" else "按 [exits] 生成的默认卖出链")}


def put_funnel(engine, side: str, layers: list, dry_run: bool = False) -> dict:
    sec = funnel_section(side)
    if not isinstance(layers, list):
        raise ConfigError("layers 必须是列表")
    from .funnel import validate_layers
    errs = validate_layers(layers, side, engine.root)
    if errs:
        raise ConfigError("；".join(errs))
    cur = copy.deepcopy(engine.cfg.get(sec) or {})
    cur["layers"] = normalize_layers(layers)
    if dry_run:
        apply_patch(engine, {sec: cur}, dry_run=True)
        return {"side": side, "layers": cur["layers"], "saved": False}
    # 老的漏斗接口只是「改正在生效的那套策略的层级」：内部转调策略接口，配置文件和策略文件一起更新，不会各说各的
    st = getattr(engine, "strategies", None)
    sid = None
    if st is not None:
        try:
            st.ensure()
            sid = ((st.active().get(side) or {}).get("id"))
        except Exception:  # noqa: BLE001
            sid = None
    if sid:
        from .strategies import StrategyError, content_from_cfg
        content = content_from_cfg(engine.cfg, side)
        content["layers"] = cur["layers"]
        try:
            st.save(side, sid, content, by="漏斗接口")
        except StrategyError as e:
            raise ConfigError(str(e)) from None
        except KeyError:
            apply_patch(engine, {sec: cur})
    else:
        apply_patch(engine, {sec: cur})
    return {"side": side, "layers": layers_of(engine.cfg, side), "saved": True, **({"strategy_id": sid} if sid else {})}


def apply_template(engine, side: str, key: str, dry_run: bool = False) -> dict:
    try:
        layers = template_layers(side, key)
    except KeyError as e:
        raise ConfigError(str(e)) from None
    r = put_funnel(engine, side, layers, dry_run=dry_run)
    r["template"] = key
    return r


def put_source(engine, name: str, sc: dict, role: str | None = None) -> dict:
    """新增/修改一个数据源。role = "pool"（股池数据源）或 "market"（行情数据源）；
    不传时：已有的保持原来那一类，新的按类型自动归类（见 config.classify_source）。"""
    from .config import classify_source, source_role
    check_name(name, "数据源名")
    if not isinstance(sc, dict) or sc.get("type") not in SOURCE_TYPES:
        raise ConfigError(f"type 必须是已注册的数据源类型：{', '.join(SOURCE_TYPES)}")
    sc = {k: v for k, v in sc.items() if k != "role"}
    cfg = engine.cfg
    cur = source_role(cfg, name)
    if role not in ("pool", "market"):
        role = cur or ("pool" if classify_source(name, sc, cfg) == "pool_sources" else "market")
    pool = copy.deepcopy(cfg.get("pool_sources") or {})
    mkt = copy.deepcopy(cfg.get("market_sources") or {})
    old = (pool.get(name) or mkt.get(name) or {})
    if role == "market" and any(str(p).split(":", 1)[0] == name for p in (cfg.get("pool") or {}).get("pools", [])):
        raise ConfigError(f"数据源 {name} 还在提供股池，不能改成行情数据源；先在「股池列表」里去掉它的股池")
    pool.pop(name, None)
    mkt.pop(name, None)
    # 掩码回传的密钥字段用已保存的值
    (pool if role == "pool" else mkt)[name] = {k: (old.get(k) if isinstance(v, str) and (v.startswith("***") or v.startswith("••••")) else v)
                                               for k, v in sc.items()}
    _replace_sources(engine, pool, mkt)
    return {"name": name, "role": role, "saved": True}


def _replace_sources(engine, pool: dict, mkt: dict) -> None:
    new = engine.store.prepare_update({})
    new["pool_sources"], new["market_sources"] = pool, mkt   # 删除要直接替换整段
    from .config import split_sources
    split_sources(new)
    probs = problems(new, engine.root)
    if probs:
        raise ConfigError("；".join(probs))
    try:
        from .config import validate
        validate(new)
    except ValueError as e:
        raise ConfigError(str(e)) from None
    engine.store.commit(new)
    engine._sources = None


def delete_source(engine, name: str) -> dict:
    pool = copy.deepcopy(engine.cfg.get("pool_sources") or {})
    mkt = copy.deepcopy(engine.cfg.get("market_sources") or {})
    if name not in pool and name not in mkt:
        raise KeyError(name)
    used = [p for p in (engine.cfg.get("pool") or {}).get("pools", []) if str(p).startswith(name + ":")]
    if used:
        raise ConfigError(f"数据源 {name} 还在被股池使用：{', '.join(used)}")
    mk = engine.cfg.get("market") or {}
    used_m = [k for k in ("minute_sources", "daily_sources", "quote_sources") if name in (mk.get(k) or [])]
    if used_m:
        raise ConfigError(f"数据源 {name} 还在行情列表里（{'、'.join(used_m)}），先在「行情数据源」里去掉")
    pool.pop(name, None)
    mkt.pop(name, None)
    _replace_sources(engine, pool, mkt)
    return {"name": name, "deleted": True}


# ---------------- 网页「设置」页：所有设置都能改（含运行模式、实盘、对外 API） ----------------


def _flatten(o, prefix=""):
    out = {}
    if isinstance(o, dict):
        for k, v in o.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    else:
        out[prefix] = o
    return out


def config_diff(old: dict, new: dict, readonly: tuple = ()) -> list[dict]:
    """逐个叶子比较，敏感字段只说「已修改」不给值。"""
    from .config import SECRET_KEYS
    skip = set(readonly) | {"sources"}          # sources 是两类数据源的合并总表，变化已在 pool_sources / market_sources 里
    a, b = _flatten({k: v for k, v in old.items() if k not in skip}), _flatten({k: v for k, v in new.items() if k not in skip})
    out = []
    for k in sorted(set(a) | set(b)):
        if a.get(k) == b.get(k):
            continue
        secret = k.rsplit(".", 1)[-1] in SECRET_KEYS
        out.append({"key": k, "old": "***" if secret and a.get(k) else a.get(k), "new": "***" if secret and b.get(k) else b.get(k),
                    "secret": secret})
    return out


def switching_to_live(old: dict, new: dict) -> bool:
    """这次保存会不会让系统切到实盘。"""
    return new["execution"].get("mode") == "live" and old["execution"].get("mode") != "live"


def web_apply(engine, body: dict, by: str = "网页", dry_run: bool = False) -> dict:
    """网页保存设置：合并 → 校验 → 写回 TOML（留 .bak）→ 热生效 → 日志 + 推送。切实盘不需要额外确认。"""
    from .config import WEB_PAGE_READONLY, masked_value
    from .util import log
    store = engine.store
    body = dict(body or {})
    body.pop("_confirm", None)     # 旧版网页会带确认文字，已不需要
    old = engine.cfg
    try:
        new = store.prepare_update(body, readonly=WEB_PAGE_READONLY)
    except ValueError as e:
        raise ConfigError(str(e)) from None
    probs = problems(new, engine.root)
    tok = str(new.get("api", {}).get("token", "") or "")
    if tok != str(old.get("api", {}).get("token", "") or "") and tok and len(tok) < 12:
        probs.append("对外 API token 至少 12 个字符（留空 = 关闭对外 API）")
    if probs:
        raise ConfigError("；".join(probs))
    to_live = switching_to_live(old, new)
    diff = config_diff(old, new, WEB_PAGE_READONLY)
    if dry_run or not diff:
        return {"ok": True, "saved": False, "changes": diff, "to_live": to_live}
    store.commit(new, readonly=WEB_PAGE_READONLY)
    if any(d["key"].split(".")[0] in ("sources", "pool_sources", "market_sources", "market") for d in diff):
        engine._sources = None
    lines = [f"- {d['key']}：{masked_value(d['old'])} → {masked_value(d['new'])}" for d in diff[:30]]
    if len(diff) > 30:
        lines.append(f"- 另有 {len(diff) - 30} 项")
    log.info("网页修改设置（%s，%d 项）：%s", by, len(diff), "；".join(x[2:] for x in lines))
    if old["execution"]["mode"] != new["execution"]["mode"]:
        log.warning("运行模式切换（%s）：%s → %s", by, old["execution"]["mode"], new["execution"]["mode"])
    title = "已切换到实盘" if to_live else ("运行模式已改为 " + new["execution"]["mode"]
                                         if old["execution"]["mode"] != new["execution"]["mode"] else f"设置已修改（{len(diff)} 项）")
    engine.notifier.notify("config_change", title, f"- 操作来源：{by}\n" + "\n".join(lines))
    warn = None
    if engine.mode != new["execution"]["mode"]:
        warn = f"设置已保存，但运行模式没切换成功（仍是 {engine.mode}），请看运行日志"
    return {"ok": True, "saved": True, "changes": diff, "to_live": to_live, "mode": engine.mode, "warning": warn,
            "version": store.version, "config": store.masked()}


class LiveConfirmRequired(ConfigError):
    pass


# ---------------- 测试连接：用表单当前的值，不需要先保存 ----------------
def _unmask_like(store, new, old):
    return store._unmask(new, old)


def test_source(engine, name: str, sc: dict) -> dict:
    """用表单里的数据源配置建一个临时实例，按能力做最小请求。不写配置。"""
    import tempfile

    from .datasources import SOURCE_TYPES
    if not isinstance(sc, dict) or sc.get("type") not in SOURCE_TYPES:
        raise ConfigError(f"type 必须是已注册的数据源类型：{', '.join(SOURCE_TYPES)}")
    saved = (engine.cfg.get("sources") or {}).get(name) or {}
    cfg = _unmask_like(engine.store, sc, saved)
    with tempfile.TemporaryDirectory() as td:
        from pathlib import Path
        src = SOURCE_TYPES[sc["type"]](name or "test", cfg, Path(td), False, engine.root)
        checks = src.test_connection()
    ok = all(c["ok"] for c in checks)
    from .market import note_health
    note_health(name or "test", "test", ok, "；".join(str(c.get("detail") or c.get("error") or "") for c in checks if not c["ok"])[:300])
    if sc["type"] in ("push", "http") and name in (engine.cfg.get("sources") or {}) and checks:
        from . import extsources   # 测试连接的结果也算一次「最近请求」，策略页上的可用状态跟着更新
        extsources.record(engine.data_dir, name, ok, "；".join(str(c.get("detail") or c.get("error") or "") for c in checks if not c["ok"])[:200])
    return {"ok": ok, "checks": checks, "type": sc["type"]}


def test_xiadan(engine, live_form: dict, transport=None) -> dict:
    """用表单里的下单程序地址/口令/超时测试连接：只调只读接口——GET /api/status（连通）和
    GET /api/snapshot?only=money（验证口令；只读缓存，不切页、不下单）。"""
    import copy as _copy

    from .exec.xiadan import ERROR_LABELS, XiadanClient, XiadanError, auth_source
    cfg = _copy.deepcopy(engine.cfg["live"])
    form = _unmask_like(engine.store, dict(live_form or {}), engine.cfg["live"])
    for k in ("base_url", "auth_password", "timeout_secs", "timeouts"):
        if k in form:
            cfg[k] = form[k]
    if not str(cfg.get("base_url", "")).startswith(("http://", "https://")):
        raise ConfigError("下单程序地址应以 http:// 开头")
    cfg["min_interval_secs"] = {}
    env_name = cfg.get("auth_env") or "QF_XIADAN_PASSWORD"
    env_note = f"环境变量 {env_name} 已设置，口令以环境变量为准（这里填的不起作用）" if os.environ.get(env_name) else None
    auth_hint = ("口令错误或未提供（403）：这里填的口令要和下单程序那边设置的口令完全一致（区分大小写）。"
                 "改好后点「测试连接」确认，再点保存。" + (env_note or ""))
    cli = XiadanClient(cfg, transport=transport)
    checks = []

    def fail(name, e):
        hint = {"auth": auth_hint, "network": "连不上：下单程序没运行、地址/端口不对，或防火墙没放行端口",
                "timeout": "超时没响应"}.get(e.kind, ERROR_LABELS.get(e.kind, e.kind))
        checks.append({"check": name, "ok": False, "detail": f"{hint}（{str(e)[:160]}）", "kind": e.kind})

    v = {}
    try:
        v = cli.status()
        checks.append({"check": "连接 GET /api/status", "ok": True,
                       "detail": f"已连上；交易客户端{'已连接' if v.get('ok') or v.get('connected') else '未连接'}"
                                 f"{'，' + str(v.get('message')) if v.get('message') else ''}"})
    except XiadanError as e:
        if e.kind == "not_connected":
            checks.append({"check": "连接 GET /api/status", "ok": True, "detail": "已连上下单程序，但它还没连上同花顺交易客户端（503）"})
        else:
            fail("连接 GET /api/status", e)
    if checks[-1]["ok"]:
        try:
            cli.snapshot(only="money", force=True)
            checks.append({"check": "口令 GET /api/snapshot", "ok": True, "detail": f"口令正确（口令来源：{auth_source(cfg)}）"})
        except XiadanError as e:
            if e.kind == "not_connected":
                checks.append({"check": "口令 GET /api/snapshot", "ok": True, "detail": "口令正确；交易客户端未连接，暂时读不到资金"})
            else:
                fail("口令 GET /api/snapshot", e)
    ok = all(c["ok"] for c in checks)
    auth_bad = any(c.get("kind") == "auth" for c in checks)
    return {"ok": ok, "checks": checks, "paths": [c.get("path") for c in cli.calls], "status": v,
            "auth_error": auth_bad, "hint": auth_hint if auth_bad else None, "note": env_note}