"""配置文件合并：服务在跑的时候手工改 pipeline.toml，不能被网页保存冲掉。

背景：`commit()` 以前是「拿内存里的整份配置覆盖文件」，所以你在网页上保存任何东西，
都会把手工加进去的段落（比如新数据源、股池绑定）删掉。现在改成只写真正变了的键。
"""
from __future__ import annotations

import time

from qfpy.config import ConfigStore

BASE = """
data_dir = "data"

[pool_sources.watchlist]
type = "watchlist"
enabled = true
codes = ["600000"]

[market_sources.akshare]
type = "akshare"
enabled = true

[pool]
pools = ["watchlist:all"]
entry_mode = "new"

[risk]
max_order_amount = 10000
max_daily_orders = 5
max_daily_amount = 30000

[execution]
mode = "paper"
"""


def _store(tmp_path):
    cdir = tmp_path / "config"
    cdir.mkdir(parents=True, exist_ok=True)
    p = cdir / "pipeline.toml"
    p.write_text(BASE, encoding="utf-8")
    return ConfigStore(p), p


def _bump(p):
    """让 mtime 明确变化（同一秒内写入时 mtime_ns 也可能一样）。"""
    time.sleep(0.01)
    p.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")


def test_manual_source_survives_web_save(tmp_path):
    store, path = _store(tmp_path)
    # 手工加一个数据源（网页不知道它）
    path.write_text(BASE + """
[pool_sources.custom]
type = "custom"
enabled = true
file = "data/custom/mother.json"
""", encoding="utf-8")
    # 网页改风控（另一段）
    store.commit(store.prepare_update({"risk": {"max_order_amount": 20000}}))

    text = path.read_text(encoding="utf-8")
    assert "[pool_sources.custom]" in text, "手工加的数据源被冲掉了"
    assert "max_order_amount = 20000" in text
    assert store.get()["pool_sources"]["custom"]["type"] == "custom"


def test_manual_bind_survives_web_save(tmp_path):
    store, path = _store(tmp_path)
    path.write_text(BASE + """
[pool.bind."watchlist:all"]
buy = "some_strategy"
""", encoding="utf-8")
    store.commit(store.prepare_update({"pool": {"pools": ["watchlist:all"]}}))

    text = path.read_text(encoding="utf-8")
    assert '[pool.bind."watchlist:all"]' in text, "手工加的股池绑定被冲掉了"
    assert store.get()["pool"]["bind"]["watchlist:all"]["buy"] == "some_strategy"


def test_web_save_still_applies_its_own_change(tmp_path):
    store, path = _store(tmp_path)
    store.commit(store.prepare_update({"pool": {"pools": ["watchlist:all"], "entry_mode": "all"}}))
    assert store.get()["pool"]["entry_mode"] == "all"
    assert 'entry_mode = "all"' in path.read_text(encoding="utf-8")


def test_manual_edit_is_reloaded_while_running(tmp_path):
    store, path = _store(tmp_path)
    assert store.get()["risk"]["max_order_amount"] == 10000
    path.write_text(BASE.replace("max_order_amount = 10000", "max_order_amount = 8888"), encoding="utf-8")
    assert store.get()["risk"]["max_order_amount"] == 8888, "手工改动没被重新加载"


def test_broken_file_keeps_previous_config(tmp_path):
    store, path = _store(tmp_path)
    before = store.get()["risk"]["max_order_amount"]
    path.write_text("这不是 toml [[[", encoding="utf-8")
    assert store.get()["risk"]["max_order_amount"] == before
    # 修好后还能正常加载
    path.write_text(BASE.replace("max_order_amount = 10000", "max_order_amount = 6666"), encoding="utf-8")
    assert store.get()["risk"]["max_order_amount"] == 6666


def test_delete_from_web_still_removes_key(tmp_path):
    store, path = _store(tmp_path)
    store.commit(store.prepare_update({"pool": {"pools": ["watchlist:all"], "priority": ["watchlist:all"]}}))
    assert 'priority = ["watchlist:all"]' in path.read_text(encoding="utf-8")
    # 网页把 priority 清空 → 文件里也应该没了
    store.commit(store.prepare_update({"pool": {"pools": ["watchlist:all"], "priority": []}}))
    assert "priority" not in path.read_text(encoding="utf-8").split("[pool]")[1].split("[")[0]


def test_same_section_both_sides_change(tmp_path):
    """同一个段落两边都改了：网页的改动生效，手工加的条目也留着。"""
    store, path = _store(tmp_path)
    path.write_text(BASE.replace('codes = ["600000"]', 'codes = ["600000", "000001"]') + """
[pool_sources.custom]
type = "custom"
enabled = true
""", encoding="utf-8")
    # 网页把 watchlist 的 codes 改回一只（它不知道 custom 的存在）
    store.commit(store.prepare_update({"pool_sources": {
        "watchlist": {"type": "watchlist", "enabled": True, "codes": ["600000"]},
    }}))
    text = path.read_text(encoding="utf-8")
    assert "[pool_sources.custom]" in text, "手工加的数据源被冲掉了"
    assert store.get()["pool_sources"]["watchlist"]["codes"] == ["600000"], "网页的改动没生效"
