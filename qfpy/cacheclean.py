"""演示/临时行情缓存的自动回收。只回收带 demo 标记的缓存，绝不碰正常行情缓存。

为什么不是「删持仓就删行情缓存」：分时缓存按 (数据源, 日期, 代码) 索引，是持仓卖出预览、
首页股池走势、回测、选股因子共用的行情数据，不属于某只持仓。删持仓时跟着删会误伤，
而且历史分时删掉后要重新联网拉，接口限流时可能直接拿不到价。

所以只认一种能安全回收的数据：写入时带 `demo: true` 的临时缓存（演示用）。
规则：带 demo 标记 + 这个代码已经不在任何持仓里 → 删。宿主没了，缓存自动消失。
挂载点：网页删持仓后（engine.manual_remove）立即跑一次；serve 启动时后台兜底跑一次。
"""
from __future__ import annotations

import json
from pathlib import Path

from .util import log

# 分时缓存的两种目录形状：data/cache/<源>/minute/<日期>/<代码>.json、data/cache/pool_minute/<日期>/<代码>.json
_GLOBS = ("*/minute/*/*.json", "pool_minute/*/*.json")


def prune_demo_cache(cache_root: Path, live_codes: set[str], dry_run: bool = False) -> dict:
    """回收已无持仓引用的 demo 缓存。dry_run=True 只报告不删。"""
    root = Path(cache_root)
    scanned, kept, removed, would_remove, files = 0, 0, 0, 0, []
    if not root.exists():
        return {"scanned": 0, "demo": 0, "removed": 0, "would_remove": 0, "kept": 0, "files": []}
    seen: set[Path] = set()
    for pattern in _GLOBS:
        for f in sorted(root.glob(pattern)):
            if f in seen:
                continue
            seen.add(f)
            scanned += 1
            try:
                doc = json.loads(f.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(doc, dict) or doc.get("demo") is not True:
                continue                      # 正常行情缓存：一律不动
            code = f.stem
            if code in live_codes:
                kept += 1
                files.append({"code": code, "path": str(f), "action": "keep"})
                continue
            if dry_run:
                would_remove += 1
                files.append({"code": code, "path": str(f), "action": "would_remove"})
            else:
                f.unlink(missing_ok=True)
                removed += 1
                files.append({"code": code, "path": str(f), "action": "removed"})
    if removed:
        log.info("演示缓存自动回收：%d 个（对应持仓已删除）", removed)
    return {"scanned": scanned, "demo": len(files), "removed": removed, "would_remove": would_remove,
            "kept": kept, "files": files}


def prune_for_engine(engine, dry_run: bool = False) -> dict:
    """按当前账本里的持仓判断哪些 demo 缓存已经没人用。"""
    return prune_demo_cache(engine.data_dir / "cache", set(engine.book.positions()), dry_run=dry_run)
