"""演示缓存自动回收：只删「带 demo 标记 + 已无持仓引用」的分时缓存，正常行情缓存一律不动。"""
from __future__ import annotations

import json

from qfpy.cacheclean import prune_demo_cache


def _write(root, rel, payload):
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return f


def _fixture(root):
    demo_a = _write(root, "gushi/minute/20261113/600519.json", {"ok": True, "data": {}, "demo": True})
    demo_b = _write(root, "gushi/minute/20261113/300825.json", {"ok": True, "data": {}, "demo": True})
    normal_a = _write(root, "gushi/minute/20261113/300300.json", {"ok": True, "data": {}})
    normal_b = _write(root, "akshare/minute/20261113/002425.json", {"code": "002425", "points": []})
    normal_c = _write(root, "pool_minute/2026-11-13/600519.json", {"code": "600519", "points": []})
    return demo_a, demo_b, normal_a, normal_b, normal_c


def test_dry_run_only_reports(tmp_path):
    demo_a, demo_b, *normal = _fixture(tmp_path)
    r = prune_demo_cache(tmp_path, {"600519"}, dry_run=True)
    assert r["demo"] == 2 and r["kept"] == 1 and r["removed"] == 0 and r["would_remove"] == 1
    assert all(f.exists() for f in (demo_a, demo_b, *normal))
    assert {x["action"] for x in r["files"]} == {"keep", "would_remove"}


def test_prune_removes_only_orphan_demo(tmp_path):
    demo_a, demo_b, *normal = _fixture(tmp_path)
    r = prune_demo_cache(tmp_path, {"600519"})
    assert r["removed"] == 1 and r["kept"] == 1
    assert demo_a.exists() and not demo_b.exists()          # 还在持仓里的留着，没人用的删掉
    assert all(f.exists() for f in normal)                  # 正常行情缓存一个都不动


def test_prune_when_all_demo_positions_gone(tmp_path):
    demo_a, demo_b, *normal = _fixture(tmp_path)
    r = prune_demo_cache(tmp_path, set())
    assert r["removed"] == 2
    assert not demo_a.exists() and not demo_b.exists()
    assert all(f.exists() for f in normal)


def test_empty_cache_is_safe(tmp_path):
    assert prune_demo_cache(tmp_path / "not-exist", {"600519"})["removed"] == 0


def test_prune_for_engine_reads_live_positions(tmp_path):
    """engine.manual_remove / serve 启动挂的就是这个入口：按当前账本持仓判断。"""
    from types import SimpleNamespace
    from qfpy.cacheclean import prune_for_engine
    cache = tmp_path / "cache"
    kept = _write(cache, "gushi/minute/20261113/600519.json", {"ok": True, "data": {}, "demo": True})
    gone = _write(cache, "gushi/minute/20261113/300825.json", {"ok": True, "data": {}, "demo": True})
    fake = SimpleNamespace(data_dir=tmp_path,
                           book=SimpleNamespace(positions=lambda: {"600519": {"symbol": "600519"}}))
    assert prune_for_engine(fake)["removed"] == 1
    assert kept.exists() and not gone.exists()
