"""历史缓存清理插件离线测试：不起线程、不联网、不碰真实 data/cache。"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_p = str(_ROOT / "plugins" / "cache_pruner")
if _p not in sys.path:
    sys.path.insert(0, _p)

from cache_pruner import worker  # noqa: E402

TODAY = date(2026, 11, 14)


def build_tree(root: Path) -> dict[str, Path]:
    """造一棵混合缓存树：过期 / 近期 / 未来 / 无日期目录 / 根级 / 带后缀都有。"""
    files = {
        "akshare/minute/20261001/600000.json": "过期分时",                 # 44 天前 → 该删
        "gushi/pools/2026-10-01/pool.json": "过期股池",                    # 该删
        "akshare/minute/20261113/600000.json": "近期分时",                 # 1 天前 → 留
        "pool_minute/2026-11-13/600519.json": "近期池内分时",              # 留
        "eltdx/index_minute/2026-11-20/000001.json": "未来日期",           # 未来 → 留
        "akshare/daily/600000.json": "日K（无日期目录）",                  # 留
        "akshare/indices/000001.json": "指数（无日期目录）",                # 留
        "trade_calendar.json": "根级公共数据",                             # 留
        "gushi/meta.json": "源级元数据",                                   # 留
        "akshare/minute/2026-11-13_bak/600000.json": "日期带后缀",         # 留
    }
    out = {}
    for rel, txt in files.items():
        p = root / rel
        if not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(txt, encoding="utf-8")
        out[rel] = p
    return out


def test_parse_day():
    assert worker.parse_day("20261113") == date(2026, 11, 13)
    assert worker.parse_day("2026-11-13") == date(2026, 11, 13)
    for bad in ("daily", "2026", "2026-11-13_bak", "indices", "", "20261132"):
        assert worker.parse_day(bad) is None


def test_plan_marks_only_expired_dated_files(tmp_path):
    build_tree(tmp_path)
    got = {Path(i["path"]).relative_to(tmp_path).as_posix()
           for i in worker.plan(tmp_path, TODAY, 30)}
    assert got == {
        "akshare/minute/20261001/600000.json",
        "gushi/pools/2026-10-01/pool.json",
    }


def test_keep_days_zero_disables(tmp_path):
    build_tree(tmp_path)
    assert worker.plan(tmp_path, TODAY, 0) == []
    assert worker.prune(tmp_path, TODAY, 0, dry_run=False)["removed"] == 0


def test_dry_run_never_deletes(tmp_path):
    fs = build_tree(tmp_path)
    r = worker.prune(tmp_path, TODAY, 30, dry_run=True)
    assert r["matched"] == 2 and r["removed"] == 0 and r["dry_run"] is True
    assert all(p.exists() for p in fs.values())


def test_prune_removes_only_expired(tmp_path):
    fs = build_tree(tmp_path)
    r = worker.prune(tmp_path, TODAY, 30, dry_run=False)
    assert r["matched"] == 2 and r["removed"] == 2 and not r["errors"]
    assert not fs["akshare/minute/20261001/600000.json"].exists()
    assert not fs["gushi/pools/2026-10-01/pool.json"].exists()
    for rel, p in fs.items():
        if rel.startswith(("akshare/minute/20261001/", "gushi/pools/2026-10-01/")):
            continue
        assert p.exists(), rel
    # 空日期目录留着（沙箱安全删除层不支持 rmdir），但里面已经没文件了
    assert list((tmp_path / "akshare/minute/20261001").iterdir()) == []


def test_default_settings_are_safe():
    s = worker.settings()
    assert int(s.get("keep_days") or 0) == 30
    assert s.get("dry_run") is True       # 默认绝不真删


def test_is_serve_process(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["qfpy", "serve"])
    assert worker._is_serve_process()
    monkeypatch.setattr(sys, "argv", ["qfpy", "positions"])
    assert not worker._is_serve_process()


def test_resolve_data_dir(tmp_path):
    cfg = tmp_path / "config"
    if not cfg.exists():
        cfg.mkdir()
    (cfg / "pipeline.toml").write_text('data_dir = "mydata"\n', encoding="utf-8")
    assert worker.resolve_data_dir(tmp_path) == tmp_path / "mydata"


def test_settings_pipeline_overrides_plugin(tmp_path, monkeypatch):
    """网页设置写在 pipeline.toml 的 [cache_pruner]，应覆盖 plugin.toml 默认值。"""
    plugin_dir = tmp_path / "plugins" / "cache_pruner"
    cfg_dir = tmp_path / "config"
    plugin_dir.mkdir(parents=True)
    cfg_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text(
        'name = "cache_pruner"\nkeep_days = 30\ndry_run = true\n', encoding="utf-8")
    (cfg_dir / "pipeline.toml").write_text(
        '[cache_pruner]\nkeep_days = 7\ndry_run = false\nunknown_key = "ignore me"\n', encoding="utf-8")
    monkeypatch.setattr(worker, "_PLUGIN_DIR", plugin_dir)
    monkeypatch.setattr(worker, "_ROOT", tmp_path)
    s = worker.settings()
    assert int(s["keep_days"]) == 7
    assert s["dry_run"] is False
    assert "unknown_key" not in s          # 只认两个已知键


def test_settings_falls_back_to_plugin_toml(tmp_path, monkeypatch):
    """pipeline.toml 没有 [cache_pruner] 段时，用插件自己的默认值。"""
    plugin_dir = tmp_path / "plugins" / "cache_pruner"
    cfg_dir = tmp_path / "config"
    plugin_dir.mkdir(parents=True)
    cfg_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text(
        'keep_days = 30\ndry_run = true\n', encoding="utf-8")
    (cfg_dir / "pipeline.toml").write_text('data_dir = "data"\n', encoding="utf-8")
    monkeypatch.setattr(worker, "_PLUGIN_DIR", plugin_dir)
    monkeypatch.setattr(worker, "_ROOT", tmp_path)
    s = worker.settings()
    assert int(s["keep_days"]) == 30 and s["dry_run"] is True


def test_plan_includes_size_and_total_bytes(tmp_path):
    """清单要给网页算「合计大小」，plan 每项带字节数、报告带 total_bytes。"""
    build_tree(tmp_path)
    items = worker.plan(tmp_path, TODAY, 30)
    assert len(items) == 2
    assert all(i["size"] > 0 for i in items)
    r = worker.prune(tmp_path, TODAY, 30, dry_run=True)
    assert r["total_bytes"] == sum(i["size"] for i in items)


def test_config_signature_tracks_only_cache_pruner(tmp_path, monkeypatch):
    """别的配置段变动不触发清理，只有 [cache_pruner] 变了才算。"""
    plugin_dir = tmp_path / "plugins" / "cache_pruner"
    cfg_dir = tmp_path / "config"
    plugin_dir.mkdir(parents=True)
    cfg_dir.mkdir()
    (plugin_dir / "plugin.toml").write_text(
        "keep_days = 30\ndry_run = true\n", encoding="utf-8")
    cfg = cfg_dir / "pipeline.toml"
    cfg.write_text('data_dir = "data"\n[cache_pruner]\nkeep_days = 30\ndry_run = true\n',
                   encoding="utf-8")
    monkeypatch.setattr(worker, "_PLUGIN_DIR", plugin_dir)
    monkeypatch.setattr(worker, "_ROOT", tmp_path)
    a = worker.config_signature()
    cfg.write_text('data_dir = "other"\n[cache_pruner]\nkeep_days = 30\ndry_run = true\n',
                   encoding="utf-8")
    assert worker.config_signature() == a
    cfg.write_text('data_dir = "data"\n[cache_pruner]\nkeep_days = 7\ndry_run = false\n',
                   encoding="utf-8")
    assert worker.config_signature() != a


class _FakeLog:
    def __init__(self):
        self.msgs = []

    def info(self, fmt, *args):
        self.msgs.append(fmt % args if args else fmt)

    def warning(self, fmt, *args):
        self.msgs.append(fmt % args if args else fmt)


def test_log_report_emits_parseable_lines(tmp_path, monkeypatch):
    """网页按「历史缓存清理报告/明细 | ...」解析日志，格式必须稳定可解析。"""
    build_tree(tmp_path)
    r = worker.prune(tmp_path, TODAY, 30, dry_run=True)
    r["ran_at"] = "2026-11-14 21:00:00"
    fake = _FakeLog()
    monkeypatch.setattr(worker, "log", fake)
    worker._log_report(r, tmp_path)
    txt = "\n".join(fake.msgs)
    assert "历史缓存清理报告 |" in txt
    assert "matched=2" in txt and "dry=1" in txt and "cutoff=2026-10-15" in txt
    assert txt.count("历史缓存清理明细 |") == 2
    assert "600000.json" in txt and "pool.json" in txt


def test_loop_runs_immediately_on_config_change(monkeypatch):
    """配置签名一变就跑一次，没变就空转（验证「改完 5 秒生效」的核心分支）。"""
    calls = []
    seq = iter(["a", "b", "b", "b"])
    monkeypatch.setattr(worker, "config_signature", lambda: next(seq, "b"))
    monkeypatch.setattr(worker, "run_once", lambda: calls.append(1))

    class _FakeTime:
        def __init__(self):
            self.n = 0

        def monotonic(self):
            return 0.0            # 定时 24h 永远不到，只测配置变更分支

        def sleep(self, _):
            self.n += 1
            if self.n >= 4:
                raise KeyboardInterrupt

    monkeypatch.setattr(worker, "time", _FakeTime())
    try:
        worker._loop()
    except KeyboardInterrupt:
        pass
    assert len(calls) == 1

