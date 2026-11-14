"""历史行情缓存自动清理：插件自管后台线程，主程序零改动。

为什么自带线程：主程序调度器只认内置任务类型，插件没有定时钩子。按「不动主程序」
的原则，这个插件自己起一个 daemon 线程——serve 启动 20 秒后跑第一轮，之后每 24
小时跑一轮；另外每 5 秒看一眼配置，[cache_pruner] 一变就立即再跑一轮（网页点
「保存并生效」或「确认删除」不用等下一次定时）。清理是低频幂等操作，不需要精确到
某个时刻。

清理规则（只认「路径里带日期目录」的缓存）：
- 命中的形状：data/cache/<源>/minute/20261113/<代码>.json、
  data/cache/<源>/pools/2026-11-13/...、data/cache/pool_minute/2026-11-13/<代码>.json
- 不碰：根级 trade_calendar.json / trade_dates.json、daily/、indices/、
  market_stats/、meta.json 等所有不带日期目录的数据；也不碰未来日期和保留期内的日期。
- 按自然日算：目录日期 < (今天 - keep_days) 才删。缓存目录本身就是自然日，按交易日
  算要额外依赖日历插件，没必要。

安全：
- keep_days = 0 / 不配 → 完全不跑，只记一条日志，不删任何文件。
- dry_run = true（默认）→ 只出报告，不删文件；改 false 才真删。
- 只删文件，不动目录（空日期目录留着无害，下次写入会自动复用）。删错目录名不会
  被误伤，因为只认纯日期写法（20261113 / 2026-11-13），带后缀的一律跳过。

配置来源：plugin.toml 是兜底默认值；网页「设置 → 插件」改的值在
config/pipeline.toml 的 [cache_pruner] 段，会覆盖默认值。settings() 每次 run_once()
都重新读盘，config_signature() 每 5 秒比对一次，所以网页改完几秒内就用新值，不用
重启、不用重新加载插件。

网页「看清单 → 确认删除」两步：插件不能挂 HTTP 路由（插件机制只让注册因子/数据源/
渠道），所以预览走主程序已有的 /api/logs?q=历史缓存清理 —— 每轮把命中清单写成
「历史缓存清理报告」+ 若干「历史缓存清理明细」结构化日志行；确认删除走已有的
PUT /api/config 把 dry_run 改成 false。这样主程序一行都不用改。

代价（同其它自管线程插件）：网页「定时任务」清单看不到它，失败不进统一告警推送。
状态在 data/cache_pruner/state.json，日志里搜「历史缓存清理」。
"""
from __future__ import annotations

import json
import sys
import threading
import time
import tomllib
from datetime import date, datetime, timedelta
from pathlib import Path

from qfpy.util import log, now_cn, write_json

STARTUP_DELAY_SECS = 20       # 避开启动高峰
INTERVAL_SECS = 86400         # 定时：一天一次
POLL_SECS = 5                 # 配置变更检测：5 秒内生效
DETAIL_LOG_LIMIT = 50         # 日志里最多列多少个文件，避免刷屏
STATE_FILE = "cache_pruner/state.json"
_DATE_FORMATS = ("%Y%m%d", "%Y-%m-%d")

_HERE = Path(__file__).resolve()
_PLUGIN_DIR = _HERE.parents[1]     # plugins/cache_pruner
_ROOT = _HERE.parents[3]           # 项目根
_started = False


_ALLOWED = ("keep_days", "dry_run")


def settings() -> dict:
    """配置来源：plugin.toml 给兜底默认，config/pipeline.toml 的 [cache_pruner] 覆盖。

    网页「设置 → 插件」改的是 pipeline.toml。每次 run_once() 都重新读盘，所以改完
    几秒内就用新值，不用重启、也不用重新加载插件。只认两个已知键，避免网页上误加的
    其它键影响插件；配置文件读不到就退回插件默认值，绝不让清理线程挂掉。
    """
    try:
        s = tomllib.loads((_PLUGIN_DIR / "plugin.toml").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        s = {}
    try:
        cfg = tomllib.loads((_ROOT / "config" / "pipeline.toml").read_text(encoding="utf-8"))
        over = cfg.get("cache_pruner") or {}
        if isinstance(over, dict):
            s.update({k: v for k, v in over.items() if k in _ALLOWED and v is not None})
    except Exception:  # noqa: BLE001
        pass
    return s


def config_signature() -> str:
    """只看 [cache_pruner] 两个键。其它配置段变化不触发清理，避免网页保存别处时误跑。"""
    s = settings()
    return json.dumps({k: s.get(k) for k in _ALLOWED},
                      sort_keys=True, default=str, ensure_ascii=False)


def resolve_data_dir(root: Path) -> Path:
    """按 config/pipeline.toml 的 data_dir 解析；没配就用 root/data。"""
    root = Path(root)
    try:
        cfg = tomllib.loads((root / "config" / "pipeline.toml").read_text(encoding="utf-8"))
        d = Path(str(cfg.get("data_dir") or "data"))
    except Exception:  # noqa: BLE001
        d = Path("data")
    return d if d.is_absolute() else root / d


def parse_day(name: str) -> date | None:
    """目录名 → 日期。只认纯日期写法，带后缀/非日期一律返回 None（不会误删）。"""
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(name, fmt).date()
        except ValueError:
            continue
    return None


def expired_day(parts, today: date, keep_days: int) -> date | None:
    """路径的各段目录里，找第一个「早于保留期」的日期段。"""
    if keep_days <= 0:
        return None
    cutoff = today - timedelta(days=keep_days)
    for seg in parts:
        d = parse_day(seg)
        if d is not None and d < cutoff:
            return d
    return None


def plan(cache_root: Path, today: date, keep_days: int) -> list[dict]:
    """扫描待删清单，纯读、不删。返回 [{path, day, size}]。"""
    root = Path(cache_root)
    if keep_days <= 0 or not root.exists():
        return []
    out = []
    for f in sorted(root.rglob("*.json")):
        d = expired_day(f.relative_to(root).parts[:-1], today, keep_days)
        if d is not None:
            try:
                size = f.stat().st_size
            except OSError:
                size = 0
            out.append({"path": str(f), "day": d.isoformat(), "size": size})
    return out


def prune(cache_root: Path, today: date, keep_days: int, dry_run: bool = True) -> dict:
    """按清单删除；dry_run=True 只报告。返回报告 dict。"""
    items = plan(cache_root, today, keep_days)
    removed, errors = 0, []
    if not dry_run:
        for it in items:
            p = Path(it["path"])
            try:
                p.unlink(missing_ok=True)
                removed += 1
            except Exception as e:  # noqa: BLE001
                errors.append(f"{p}: {e}")
                continue
    return {"today": today.isoformat(), "keep_days": keep_days,
            "cutoff": (today - timedelta(days=keep_days)).isoformat() if keep_days > 0 else "",
            "dry_run": bool(dry_run), "matched": len(items), "removed": removed,
            "total_bytes": sum(i.get("size", 0) for i in items),
            "errors": errors[:20], "files": items[:100]}


def _save_state(data_dir: Path, report: dict) -> None:
    p = Path(data_dir) / STATE_FILE
    if not p.parent.exists():          # 沙箱里 mkdir(exist_ok=True) 会被代执行层误报 EEXIST
        p.parent.mkdir(parents=True, exist_ok=True)
    write_json(p, report)


def _short_path(path: str, data_dir: Path) -> str:
    """日志里显示相对 data/cache/ 的短路径，别把一长串绝对路径刷进日志。"""
    try:
        return str(Path(path).relative_to(Path(data_dir) / "cache"))
    except ValueError:
        return path


def _log_report(report: dict, data_dir: Path) -> None:
    """结构化报告：网页用 /api/logs?q=历史缓存清理 拉这些行来显示命中清单。"""
    log.info("历史缓存清理报告 | ran=%s | keep=%d | cutoff=%s | dry=%d | matched=%d | removed=%d | bytes=%d",
             report.get("ran_at", ""), report["keep_days"], report["cutoff"],
             1 if report["dry_run"] else 0, report["matched"], report["removed"],
             report["total_bytes"])
    files = report.get("files") or []
    for it in files[:DETAIL_LOG_LIMIT]:
        log.info("历史缓存清理明细 | %s | %d | %s",
                 it["day"], it.get("size", 0), _short_path(it["path"], data_dir))
    rest = report["matched"] - min(len(files), DETAIL_LOG_LIMIT)
    if rest > 0:
        log.info("历史缓存清理明细 | … | 0 | 另有 %d 个文件未列出（完整清单见 data/cache_pruner/state.json）", rest)


def run_once() -> dict | None:
    """跑一次（供 serve 线程和手动验证共用）。keep_days<=0 时返回 None。"""
    s = settings()
    keep_days = int(s.get("keep_days") or 0)
    if keep_days <= 0:
        return None
    dry = bool(s.get("dry_run", True))
    data_dir = resolve_data_dir(_ROOT)
    report = prune(data_dir / "cache", now_cn().date(), keep_days, dry_run=dry)
    report["ran_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save_state(data_dir, report)
    log.info("历史缓存清理：保留最近 %d 天，%s，命中 %d 个、删除 %d 个",
             keep_days, "试运行（未删）" if dry else "已清理", report["matched"], report["removed"])
    _log_report(report, data_dir)
    return report


def _is_serve_process() -> bool:
    """`python -m qfpy serve` 才起线程；CLI / pytest / 重新加载插件都不重复起。"""
    return any(str(a).strip().lower() == "serve" for a in sys.argv[1:])


def _loop() -> None:
    """定时 24h + 配置变更即时：每 POLL_SECS 醒一次，[cache_pruner] 变了就立即跑。"""
    sig = config_signature()
    next_due = time.monotonic() + STARTUP_DELAY_SECS
    while True:
        time.sleep(POLL_SECS)
        now = time.monotonic()
        new_sig = config_signature()
        changed = new_sig != sig
        due = now >= next_due
        if not (changed or due):
            continue
        try:
            run_once()
        except Exception as e:  # noqa: BLE001  单次出错不能让守护线程死掉
            log.warning("历史缓存清理检查异常：%s", e)
        sig = new_sig
        if due:
            next_due = now + INTERVAL_SECS


def start_once() -> bool:
    """进程级只起一次。guard 在会被 sys.modules 缓存的子包模块上，
    即使 plugin.py 被重新 exec 也不会起第二个线程。"""
    global _started
    if _started:
        return False
    _started = True
    if not _is_serve_process():
        return False
    s = settings()
    threading.Thread(target=_loop, name="cache-pruner", daemon=True).start()
    log.info("历史缓存清理插件已启动：保留最近 %d 天，%s（配置改动 %d 秒内生效）",
             int(s.get("keep_days") or 0),
             "试运行（不删文件）" if s.get("dry_run", True) else "自动删除",
             POLL_SECS)
    return True
