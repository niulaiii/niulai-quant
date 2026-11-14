"""配置加载、校验、热更新与写回（保留注释）。

配置在内存里是普通 dict（方便网页编辑），带默认值合并。
网页保存时：校验 → 用 tomlkit 写回文件（尽量保留注释）→ 替换内存配置，下一次运行即生效。
"""
from __future__ import annotations

import copy
import re
import os
import threading
from pathlib import Path
from typing import Any

import tomlkit

from .util import atomic_write_text, lock_for, log, parse_hm, process_lock

DEFAULTS: dict[str, Any] = {
    "data_dir": "data",
    # 数据源分两类（第六轮拆开）：股池数据源（股池成分股从哪里拉）和行情数据源（实时价、分时、日K、指数、市场情绪）。
    # 文件里分别写 [pool_sources.名字] 和 [market_sources.名字]；旧的 [sources.名字] 读的时候自动归类，下次保存时改写成新写法。
    # 内存里的 "sources" 是两类合在一起的总表（引擎按名字找数据源用），不写回文件。
    "pool_sources": {},
    "market_sources": {},
    "sources": {},
    # entry_mode：new = 每次股池更新只把新入池的票送进策略（默认，规则见 poolentry.py）；all = 每次都送全部成分股（旧做法）
    # entry_scope：day = 按交易日重新算（默认）；continuous = 跨交易日连续算。aliases：股池中文别名 {"gushi:auction_long": "竞价多头"}
    "pool": {"pools": [], "bind": {}, "archive": True, "archive_time": "15:10", "entry_mode": "new", "entry_scope": "day",
             "entry_scope_by_pool": {}, "aliases": {}, "priority": []},
    "market": {"minute_sources": ["gushi", "akshare"], "daily_sources": ["akshare"], "quote_sources": ["akshare"],
               "daily_lookback_days": 150},
    "home": {"indices": ["000001.SH", "399001.SZ", "399006.SZ", "000688.SH", "000300.SH"], "pool_days": 5, "refresh_secs": 15},
    "factors": [],
    "select": {"top_n": 1, "weights": {}},
    "sizing": {"per_stock_amount": 5000, "buy_price_offset_pct": 0.3, "sell_price_offset_pct": 0.3},
    "execution": {"mode": "paper"},
    "automation": {"default_enabled": False},
    "risk": {
        "max_order_amount": 10000, "max_daily_orders": 0, "max_daily_amount": 30000,   # max_daily_orders 已停用（不限笔数）
        "whitelist": [], "blacklist": [], "enforce_session": True,
        "sessions": [["09:30", "11:30"], ["13:00", "14:57"]],
        "max_price_deviation_pct": 3.0, "one_buy_per_symbol_per_day": True,
        "max_per_industry": 0,           # 同一行业最多买几只（含持仓），0 = 不限
    },
    "paper": {"initial_cash": 100000, "commission_rate": 0.00025, "min_commission": 5, "stamp_tax_rate": 0.0005,
              "slippage_rate": 0.001, "ignore_session": True},
    "live": {"enabled": False, "base_url": "http://127.0.0.1:8765", "timeout_secs": 12,
             "arm_env": "QF_LIVE_ARMED", "arm_value": "I_ACCEPT_REAL_MONEY_RISK",
             "auth_env": "QF_XIADAN_PASSWORD", "auth_password": "",
             "monitor": True, "poll_secs": 20, "idle_poll_secs": 300, "reconcile_secs": 60, "full_refresh_secs": 120,
             "timeouts": {}, "min_interval_secs": {}, "reconcile": {}},
    "exits": {"enabled": True, "take_profit_pct": 5.0, "stop_loss_pct": 3.0, "max_hold_days": 3, "exit_time": "14:50",
              "check_times": ["09:35", "10:30", "13:30"], "remind_minutes_before": 5,
              "hold_can_skip_stop_loss": False, "by_pool": {}},
    "web": {"host": "127.0.0.1", "port": 8081},
    # 对外 REST API（/api/v1/*）：token 鉴权；token 优先读环境变量 QF_API_TOKEN。两处都空时 /api/v1 全部返回 401
    "api": {"enabled": True, "token": "", "token_env": "QF_API_TOKEN", "allow_config_write": True},
    "schedule": {"enabled": True, "runs": [], "retry_secs": 60, "backfill": True, "backfill_trade": False},
    "notify": {"enabled": True, "channels": ["dingtalk"], "events": {}, "dingtalk": {}, "pushplus": {}, "serverchan": {},
               "wecom": {}, "feishu": {}, "telegram": {}},
}

# 网页不允许修改的部分（只能手改文件 + 重启）
WEB_READONLY_SECTIONS = ("execution", "live", "web", "data_dir", "api")   # 对外 API / 通用路径不能改的段
# 网页「设置」页能改运行模式（直接切换，不需要确认文字）、实盘设置和对外 API；
# 监听地址和数据目录仍只能手改文件（Web 默认只监听 127.0.0.1）
WEB_PAGE_READONLY = ("web", "data_dir")
# 实盘段里只能手改文件的键：环境变量名（不是日常设置，网页上不显示）。arm_env/arm_value 已不再起作用，留着兼容旧配置
LIVE_LOCKED_KEYS = ("arm_env", "arm_value", "auth_env")
API_LOCKED_KEYS = ("token_env",)
# 需要在网页上遮蔽的密钥字段
SECRET_KEYS = {"webhook", "secret", "token", "sendkey", "bot_token", "key", "auth_password"}
MASK_PREFIX = "••••"


def masked_value(v) -> str:
    """日志/推送里显示配置值：过长的截断。敏感值在 config_diff 里已换成 ***。"""
    t = "（空）" if v is None or v == "" else str(v)
    return t if len(t) <= 80 else t[:77] + "…"

EXIT_RULE_KEYS = ("take_profit_pct", "stop_loss_pct", "max_hold_days", "exit_time")


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def to_plain(obj: Any) -> Any:
    """tomlkit 对象 → 普通 Python 对象。"""
    if hasattr(obj, "unwrap"):
        return obj.unwrap()
    return obj


def _check_hm(v, where):
    try:
        parse_hm(v)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"{where}：时间格式应为 HH:MM（{v}）") from e


SOURCE_ROLES = ("pool_sources", "market_sources")


def _market_names(cfg: dict) -> set[str]:
    mk = cfg.get("market") or {}
    return {str(n) for k in ("minute_sources", "daily_sources", "quote_sources") for n in (mk.get(k) or [])}


def classify_source(name: str, sc: dict, cfg: dict) -> str:
    """旧的 [sources.X] 归到哪一类：被股池用到的（pool.pools 里 X:...）是股池数据源；
    其余被行情列表用到的、或者不能提供股池的，是行情数据源；只能提供股池的（自选股等）也归股池数据源。"""
    pools = [str(p) for p in ((cfg.get("pool") or {}).get("pools") or [])]
    if any(p.split(":", 1)[0] == name for p in pools):
        return "pool_sources"
    if name in _market_names(cfg):
        return "market_sources"
    try:
        from .datasources import SOURCE_TYPES
        caps = getattr(SOURCE_TYPES.get(str(sc.get("type"))), "capabilities", ()) or ()
    except Exception:  # noqa: BLE001
        caps = ()
    # 能出股池的（自选股、外部推送等）归股池数据源；行情列表里照样能点选，算共用
    return "pool_sources" if "pool" in caps else "market_sources"


def split_sources(cfg: dict, prefer_legacy: bool = False) -> dict:
    """整理成两类数据源 + 合并总表（就地修改并返回 cfg）。

    - 文件里已经是新写法（pool_sources / market_sources）：以它们为准；
    - 只有旧写法 [sources.X]（或网页按旧接口提交了 sources）：已经归过类的名字保持原类，新名字按 classify_source 归类；
    - 同一个名字只能在一类里出现（gushi 这种既能出股池又能出分时的，归股池数据源；行情列表里照样可以点选它，算「共用」）。
    """
    pool = dict(cfg.get("pool_sources") or {})
    mkt = dict(cfg.get("market_sources") or {})
    legacy = dict(cfg.get("sources") or {})
    if prefer_legacy or (legacy and not pool and not mkt):
        known = {n: "pool_sources" for n in pool} | {n: "market_sources" for n in mkt}
        pool, mkt = {}, {}
        for n, sc in legacy.items():
            role = known.get(n) or classify_source(n, sc, cfg)
            (pool if role == "pool_sources" else mkt)[n] = sc
    else:
        # 新写法下还混着旧段落里独有的名字（手改文件时可能出现）：一并归类
        for n, sc in legacy.items():
            if n not in pool and n not in mkt and cfg.get("_legacy_sources_in_file"):
                (pool if classify_source(n, sc, cfg) == "pool_sources" else mkt)[n] = sc
    cfg["pool_sources"], cfg["market_sources"] = pool, mkt
    cfg["sources"] = {**mkt, **pool}
    return cfg


def source_role(cfg: dict, name: str) -> str | None:
    if name in (cfg.get("pool_sources") or {}):
        return "pool"
    if name in (cfg.get("market_sources") or {}):
        return "market"
    return None


def validate(cfg: dict) -> list[str]:
    """基础校验，返回警告列表；严重问题直接抛 ValueError。因子和表达式由 factors.build_chain 校验。"""
    warns: list[str] = []
    mode = cfg["execution"].get("mode")
    if mode not in ("paper", "dry_run", "live"):
        raise ValueError(f"execution.mode 只能是 paper / dry_run / live，当前是 {mode}")
    both = set(cfg.get("pool_sources") or {}) & set(cfg.get("market_sources") or {})
    if both:
        raise ValueError(f"数据源 {'、'.join(sorted(both))} 同时写在股池数据源和行情数据源里，同一个名字只能放一类"
                         "（行情列表里可以直接点选股池数据源，算共用）")
    for name, s in cfg["sources"].items():
        if not isinstance(s, dict) or not s.get("type"):
            raise ValueError(f"数据源 {name} 缺少 type")
    psrc = cfg.get("pool_sources") or cfg["sources"]
    for p in cfg["pool"].get("pools", []):
        if ":" not in str(p):
            raise ValueError(f"股池 {p} 格式应为「数据源:股池key」")
        if str(p).split(":", 1)[0] not in psrc:
            if str(p).split(":", 1)[0] in cfg["sources"]:
                raise ValueError(f"股池 {p} 用的 {str(p).split(':', 1)[0]} 是行情数据源，请先把它放到股池数据源里")
            raise ValueError(f"股池 {p} 引用了不存在的股池数据源")
    from .poolbind import validate as _bind_validate
    errs = _bind_validate(cfg)
    if errs:
        raise ValueError("；".join(errs))
    at = str(cfg["pool"].get("archive_time") or "15:10")
    if not re.fullmatch(r"\d{1,2}:\d{2}", at):
        raise ValueError(f"pool.archive_time 格式应为 15:10 这样的 时:分，当前是 {at}")
    hm = cfg.get("home") or {}
    for c in hm.get("indices", []) or []:
        if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", str(c).upper()):
            raise ValueError(f"home.indices 里的 {c} 格式应为 000001.SH / 399001.SZ 这样的「6 位代码.交易所」")
    if not 1 <= int(hm.get("pool_days", 5)) <= 30:
        raise ValueError("home.pool_days 应在 1~30 之间")
    if not 5 <= float(hm.get("refresh_secs", 15)) <= 3600:
        raise ValueError("home.refresh_secs 应在 5~3600 秒之间")
    r = cfg["risk"]
    for k in ("max_order_amount", "max_daily_amount", "max_price_deviation_pct"):   # max_daily_orders 已停用，不再校验
        if float(r.get(k, 0)) <= 0:
            raise ValueError(f"risk.{k} 必须大于 0")
    for a, b in r.get("sessions", []):
        _check_hm(a, "risk.sessions")
        _check_hm(b, "risk.sessions")
    if float(cfg["sizing"]["per_stock_amount"]) > float(r["max_order_amount"]):
        warns.append("sizing.per_stock_amount 大于单笔上限，委托会被风控拦截")
    ex = cfg["exits"]
    _check_hm(ex["exit_time"], "exits.exit_time")
    for t in ex.get("check_times", []):
        _check_hm(t, "exits.check_times")
    for k, ov in (ex.get("by_pool") or {}).items():
        if "exit_time" in ov:
            _check_hm(ov["exit_time"], f"exits.by_pool.{k}.exit_time")
        for kk in ov:
            if kk not in EXIT_RULE_KEYS:
                raise ValueError(f"exits.by_pool.{k} 不支持字段 {kk}（可用：{', '.join(EXIT_RULE_KEYS)}）")
    for run in cfg["schedule"].get("runs", []):
        if not isinstance(run, dict):
            raise ValueError("schedule.runs 每一项必须是表")
        st = run.get("start") or run.get("time", "")
        _check_hm(st, "schedule.runs.start")
        if run.get("end"):
            _check_hm(run["end"], "schedule.runs.end")
            if run["end"] <= st:
                raise ValueError(f"schedule.runs：结束时间 {run['end']} 必须晚于开始时间 {st}")
        if not isinstance(run.get("pools", []), list):
            raise ValueError("schedule.runs.pools 必须是列表")
    lv = cfg["live"]
    if not re.fullmatch(r"https?://[^\s/]+(:\d+)?/?", str(lv.get("base_url", ""))):
        raise ValueError(f"live.base_url 应为 http://地址:端口（当前 {lv.get('base_url')!r}）")
    for k in ("timeout_secs", "poll_secs", "idle_poll_secs", "reconcile_secs", "full_refresh_secs"):
        try:
            ok = float(lv.get(k, 1)) > 0
        except (TypeError, ValueError):
            ok = False
        if not ok:
            raise ValueError(f"live.{k} 必须是大于 0 的数字")
    for sub in ("timeouts", "min_interval_secs"):
        for k, v in (lv.get(sub) or {}).items():
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
                raise ValueError(f"live.{sub}.{k} 必须是非负数字")
    for k, v in (lv.get("reconcile") or {}).items():
        if not isinstance(v, (int, float, bool)):
            raise ValueError(f"live.reconcile.{k} 必须是数字或开关")
    try:
        if int(cfg["schedule"].get("retry_secs", 60)) < 10:
            raise ValueError("schedule.retry_secs 不能小于 10 秒")
    except (TypeError, ValueError) as e:
        raise ValueError(f"schedule.retry_secs：{e}") from None
    for side in ("buy_funnel", "sell_funnel"):
        if side in cfg and not isinstance(cfg[side].get("layers", []), list):
            raise ValueError(f"{side}.layers 必须是列表")
    if cfg["web"].get("host") not in ("127.0.0.1", "localhost", "::1"):
        warns.append("web.host 不是 127.0.0.1：网页没有登录鉴权，serve 会拒绝启动；请改回 127.0.0.1 并用 SSH 转发")
    return warns


class ConfigStore:
    """持有当前配置；网页保存时原子替换。"""

    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self.root = self.path.parent.parent  # 项目根目录（config/ 的上一级）
        self._lock = threading.RLock()
        self.version = 0
        self.listeners: list = []
        self.cfg = self._load()
        self._sig = self._file_sig()
        self._reloading = False
        # 手工加到文件里、内存里还没有的条目（数据源、股池绑定等），保存时不能被网页冲掉
        self._external_add: dict | None = None

    def _load(self) -> dict:
        doc = tomlkit.parse(self.path.read_text(encoding="utf-8"))
        plain = to_plain(doc)
        cfg = deep_merge(DEFAULTS, plain)
        if plain.get("sources") and (plain.get("pool_sources") or plain.get("market_sources")):
            cfg["_legacy_sources_in_file"] = True
        split_sources(cfg)
        cfg.pop("_legacy_sources_in_file", None)
        self._apply_env(cfg)
        validate(cfg)
        return cfg

    @staticmethod
    def _apply_env(cfg: dict) -> None:
        env = os.environ
        if env.get("QF_GUSHI_KEY"):
            for s in list((cfg.get("pool_sources") or {}).values()) + list((cfg.get("market_sources") or {}).values()):
                if s.get("type") == "gushi":
                    s["key"] = env["QF_GUSHI_KEY"]
        dt = cfg["notify"].setdefault("dingtalk", {})
        if env.get("QF_DINGTALK_WEBHOOK"):
            dt["webhook"] = env["QF_DINGTALK_WEBHOOK"]
        if env.get("QF_DINGTALK_SECRET"):
            dt["secret"] = env["QF_DINGTALK_SECRET"]

    def _file_sig(self) -> tuple:
        """文件的「指纹」：改动时间 + 大小。用来发现有人手工改了 pipeline.toml。"""
        try:
            st = self.path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return ()

    def _notify(self) -> None:
        for fn in list(self.listeners):
            try:
                fn(self.cfg)
            except Exception as e:  # noqa: BLE001
                log.warning("配置变更回调失败：%s", e)

    def _maybe_reload(self) -> bool:
        """文件被外部改过就重读，让手工改动在服务运行期间也能生效。"""
        if self._reloading:
            return False
        sig = self._file_sig()
        if not sig or sig == self._sig:
            return False
        self._reloading = True
        try:
            cfg = self._load()
        except Exception as e:  # noqa: BLE001
            log.warning("配置文件改坏了，继续用内存里的版本：%s", e)
            self._sig = sig          # 不再反复重试同一个坏文件
            return False
        finally:
            self._reloading = False
        adds = _dict_additions(cfg, self.cfg)
        if adds:
            if self._external_add:
                _reapply(self._external_add, adds)
            else:
                self._external_add = adds
        self.cfg = cfg
        self._sig = sig
        self.version += 1
        log.info("检测到 pipeline.toml 被改动，已重新加载（版本 %s）", self.version)
        self._notify()
        return True

    def get(self) -> dict:
        with self._lock:
            self._maybe_reload()
            return self.cfg

    def resolve_path(self, p: str) -> Path:
        pp = Path(p)
        return pp if pp.is_absolute() else (self.root / pp)

    @property
    def data_dir(self) -> Path:
        d = self.resolve_path(self.cfg.get("data_dir", "data"))
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ---------- 网页编辑 ----------
    def masked(self) -> dict:
        with self._lock:
            self._maybe_reload()
        def mask(o, key=None):
            if isinstance(o, dict):
                return {k: mask(v, k) for k, v in o.items()}
            if isinstance(o, list):
                return [mask(v) for v in o]
            if key in SECRET_KEYS and isinstance(o, str) and o:
                return MASK_PREFIX + o[-4:]
            return o
        return mask(copy.deepcopy(self.cfg))

    def _unmask(self, new: Any, old: Any) -> Any:
        if isinstance(new, dict):
            return {k: self._unmask(v, old.get(k) if isinstance(old, dict) else None) for k, v in new.items()}
        if isinstance(new, list):
            return new
        if isinstance(new, str) and new.startswith(MASK_PREFIX):
            return old if isinstance(old, str) else ""
        return new

    def prepare_update(self, patch: dict, readonly: tuple = WEB_READONLY_SECTIONS) -> dict:
        """把网页提交的部分配置合并成完整新配置（不写盘）。只读段被忽略；锁定键保持原值。"""
        with self._lock:
            self._maybe_reload()          # 先吃进手工改动，再在它上面打补丁
            cur = copy.deepcopy(self.cfg)
        patch = {k: v for k, v in patch.items() if k not in readonly and not k.startswith("_")}
        patch = self._unmask(patch, cur)
        new = copy.deepcopy(cur)
        for k, v in patch.items():
            # 列表/标量整体替换；对象整体替换（网页总是提交整段）
            new[k] = v
        new = deep_merge(DEFAULTS, new)
        # 只提交了旧的 sources 总表（旧接口 / 旧网页）：按名字放回原来那一类；提交了新的两段就以新的为准
        if "sources" in patch and not any(r in patch for r in SOURCE_ROLES):
            split_sources(new, prefer_legacy=True)
        else:
            split_sources(new)
        for sec, keys in (("live", LIVE_LOCKED_KEYS), ("api", API_LOCKED_KEYS)):
            for k in keys:
                if k in cur.get(sec, {}):
                    new[sec][k] = cur[sec][k]
        if self._external_add:          # 手工加的条目（网页不认识）原样补回
            _reapply(new, self._external_add)
        validate(new)
        return new

    def commit(self, new: dict, readonly: tuple = WEB_READONLY_SECTIONS) -> None:
        """写回 TOML 并热替换。

        **只写真正变了的键**：磁盘上别的段落（包括你手工加的、网页不知道的）原样保留，
        所以服务在跑的时候直接改 pipeline.toml 也不会被下一次网页保存冲掉。
        写完从磁盘重读，内存和文件保证一致。
        """
        with self._lock:
            with process_lock(lock_for(self.path)):
                self._maybe_reload()                    # 先把手工改动吃进来，base 才是最新的
                raw = self.path.read_text(encoding="utf-8")
                doc = tomlkit.parse(raw)
                base = copy.deepcopy(self.cfg)          # 服务器眼里的「原值」
                if "sources" in doc and (base.get("sources") or {}) != (new.get("sources") or {}):
                    del doc["sources"]                  # 旧写法：第一次保存时改写成两段
                for k, v in new.items():
                    if k in readonly or k == "sources" or k.startswith("_"):
                        continue
                    old = base.get(k)
                    if old == v:
                        continue                            # 这一段服务器没动，磁盘上怎么写的就留什么
                    if k not in doc and k in DEFAULTS and v == DEFAULTS[k]:
                        continue  # 文件里没写、值也还是默认的段（如旧的平铺 factors = []）不写回文件
                    _assign_diff(doc, k, old, v)
                merged = deep_merge(DEFAULTS, to_plain(doc))
                split_sources(merged)
                self._apply_env(merged)
                validate(merged)                        # 合并后不合法就不写，保留原文件
                bak = self.path.with_suffix(".toml.bak")
                atomic_write_text(bak, raw)
                atomic_write_text(self.path, tomlkit.dumps(doc))
            self.cfg = merged
            self._sig = self._file_sig()
            self._external_add = None       # 已经写进文件，不再需要额外保护
            self.version += 1
        log.info("配置已保存并生效（版本 %s）", self.version)
        self._notify()


def _dict_additions(new: Any, old: Any) -> dict | None:
    """new 里比 old 多出来的键（只看字典层级），用来保护「手工加到文件里、网页不知道」的条目。"""
    if not (isinstance(new, dict) and isinstance(old, dict)):
        return None
    out: dict = {}
    for k, v in new.items():
        if k not in old:
            out[k] = copy.deepcopy(v)
        else:
            sub = _dict_additions(v, old[k])
            if sub:
                out[k] = sub
    return out or None


def _reapply(cfg: Any, additions: Any) -> None:
    """把外部新增的条目补回 cfg（cfg 里已有的不覆盖）。"""
    if not (isinstance(additions, dict) and isinstance(cfg, dict)):
        return
    for k, v in additions.items():
        if k not in cfg:
            cfg[k] = copy.deepcopy(v)
        else:
            _reapply(cfg[k], v)


def _assign_diff(doc, key: str, old: Any, new: Any) -> None:
    """只把「相对 old 真正变了的部分」写进 doc。

    没变的键（包括磁盘上有、服务器内存里没有的手工改动）一律不动，
    这样服务运行期间手工改 pipeline.toml 不会被下一次网页保存冲掉。
    """
    cur = doc.get(key) if key in doc else None
    cur_plain = to_plain(cur) if cur is not None else None
    if isinstance(new, dict) and isinstance(old, dict) and isinstance(cur_plain, dict):
        for k in list(old.keys()):              # 服务器删掉的键
            if k not in new and k in cur:
                del cur[k]
        for k, v in new.items():
            if k in old and old[k] == v:        # 这个键服务器没动
                continue
            _assign_diff(cur, k, old.get(k), v)
        return
    if cur_plain == new:                        # 磁盘上已经是这个值
        return
    _assign(doc, key, new)


def _assign(doc, key: str, value: Any) -> None:
    """逐层合并字典，尽量保留注释；列表/标量整体替换。"""
    cur = doc.get(key) if key in doc else None
    if isinstance(value, dict) and cur is not None and isinstance(to_plain(cur), dict) and hasattr(cur, "keys"):
        for k in list(cur.keys()):
            if k not in value:
                del cur[k]
        for k, v in value.items():
            if k in cur and to_plain(cur[k]) == v:
                continue
            _assign(cur, k, v)
        return
    if key == "factors" or (isinstance(value, list) and value and all(isinstance(x, dict) for x in value)):
        doc[key] = _aot(value)
        return
    doc[key] = strip_none(value)


def strip_none(v: Any) -> Any:
    """TOML 没有 null：去掉值为 None 的键（因子参数留空 = 用默认）。"""
    if isinstance(v, dict):
        return {k: strip_none(x) for k, x in v.items() if x is not None}
    if isinstance(v, list):
        return [strip_none(x) for x in v if x is not None]
    return v


def _aot(items: list) -> Any:
    """字典列表 → TOML 表数组；里面的字典列表（如层里的 factors）也写成表数组，params 等小字典写成内联表。"""
    aot = tomlkit.aot()
    for item in items:
        t = tomlkit.table()
        for k, v in strip_none(item).items():
            if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                t[k] = _aot(v)
            elif isinstance(v, dict):
                it = tomlkit.inline_table()
                it.update(v)
                t[k] = it
            else:
                t[k] = v
        aot.append(t)
    return aot
