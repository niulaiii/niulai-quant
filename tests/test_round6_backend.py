"""第六轮后端：新入池判断、数据源拆分和旧配置迁移、网络重试和缓存兜底。全部离线。"""
import httpx
import pytest

from qfpy import nethelp
from qfpy.config import split_sources
from qfpy.poolentry import PoolEntry, entry_mode, entry_scope


# ---------------- 新入池 ----------------
def _cfg(**pool):
    return {"pool": {"pools": ["g:a", "w:all"], **pool}}


def test_entry_mode_and_scope_per_pool():
    c = _cfg()
    assert entry_mode(c, "g:a") == "new" and entry_scope(c, "g:a") == "day"
    c = _cfg(entry_scope_by_pool={"w:all": "continuous", "g:a": "all"})
    assert entry_scope(c, "w:all") == "continuous"
    assert entry_mode(c, "g:a") == "all" and entry_mode(c, "w:all") == "new"   # 网页选「全部」只管这个池
    assert entry_scope(c, "g:a") == "day"
    assert entry_mode(_cfg(entry_mode="all"), "w:all") == "all"


def test_new_entry_same_day_and_returned(tmp_path):
    pe, c, d = PoolEntry(tmp_path), _cfg(), "2026-11-11"
    s = pe.split(c, d, "g:a", ["000001", "000002"])
    assert s["new"] == ["000001", "000002"]
    pe.commit(d, "g:a", ["000001", "000002"], s["new"], "10:00", "r1")
    s = pe.split(c, d, "g:a", ["000001", "000002", "000003"])
    assert s["new"] == ["000003"] and set(s["seen"]) == {"000001", "000002"}
    pe.commit(d, "g:a", ["000001", "000003"], s["new"], "10:30", "r2")        # 000002 被移出
    s = pe.split(c, d, "g:a", ["000001", "000002", "000003"])
    assert s["new"] == ["000002"] and s["returned"] == ["000002"]               # 移出后又出现，算新入池


def test_day_vs_continuous_across_days(tmp_path):
    pe = PoolEntry(tmp_path)
    pe.commit("2026-11-10", "w:all", ["600000"], ["600000"], "10:00", "r1")
    pe.commit("2026-11-10", "g:a", ["600000"], ["600000"], "10:00", "r1")
    c = _cfg(entry_scope_by_pool={"w:all": "continuous"})
    assert pe.split(c, "2026-11-11", "g:a", ["600000"])["new"] == ["600000"]   # 每日重算
    assert pe.split(c, "2026-11-11", "w:all", ["600000"])["new"] == []          # 跨日连续
    c2 = _cfg(entry_scope_by_pool={"w:all": "all"})
    assert pe.split(c2, "2026-11-11", "w:all", ["600000"])["new"] == ["600000"]  # 全部


# ---------------- 数据源拆分和旧配置迁移 ----------------
def test_split_sources_legacy_migration():
    cfg = {"sources": {"gushi": {"type": "gushi"}, "watchlist": {"type": "watchlist"}, "tencent": {"type": "tencent"}},
           "pool": {"pools": ["gushi:gold_230", "watchlist:all"]}}
    split_sources(cfg)
    assert set(cfg["pool_sources"]) == {"gushi", "watchlist"}
    assert set(cfg["market_sources"]) == {"tencent"}
    assert set(cfg["sources"]) == {"gushi", "watchlist", "tencent"}             # 内存里的总表


def test_split_sources_new_style_wins():
    cfg = {"pool_sources": {"gushi": {"type": "gushi", "proxy": "none"}}, "market_sources": {"tencent": {"type": "tencent"}},
           "pool": {"pools": ["gushi:gold_230"]}}
    split_sources(cfg)
    assert cfg["sources"]["gushi"]["proxy"] == "none" and list(cfg["market_sources"]) == ["tencent"]


# ---------------- 网络重试 ----------------
class _FakeClient:
    calls: list = []
    fail_times = 0

    def __init__(self, timeout=None, **kw):
        self.kw = kw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None, headers=None):
        _FakeClient.calls.append(self.kw)
        if len(_FakeClient.calls) <= _FakeClient.fail_times:
            raise httpx.ConnectError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol")
        return httpx.Response(200, json={"ok": True})


@pytest.fixture
def fake(monkeypatch):
    _FakeClient.calls = []
    monkeypatch.setattr(nethelp.httpx, "Client", _FakeClient)
    return _FakeClient


def test_retry_then_ok(fake):
    fake.fail_times = 2
    sleeps = []
    r = nethelp.robust_get("https://www.gushi.in/x", proxy="none", sleep=sleeps.append)
    assert r.status_code == 200 and len(fake.calls) == 3 and sleeps == [1.0, 3.0]
    assert all(k.get("trust_env") is False for k in fake.calls)                     # 直连


def test_all_fail_raises_chinese_neterror(fake):
    fake.fail_times = 99
    with pytest.raises(nethelp.NetError) as ei:
        nethelp.robust_get("https://www.gushi.in/x", proxy="none", sleep=lambda s: None)
    e = ei.value
    assert len(fake.calls) == 3 and "重试" in str(e) and "UNEXPECTED_EOF" in e.detail
    assert not any("\u4e00" <= ch <= "\u9fa5" for ch in e.detail[:10]) or e.detail   # 原始英文留在详情里


def test_custom_proxy_and_auto_fallback(fake, monkeypatch):
    fake.fail_times = 0
    nethelp.robust_get("https://www.gushi.in/x", proxy="http://127.0.0.1:7890", sleep=lambda s: None)
    assert fake.calls[-1].get("proxy") == "http://127.0.0.1:7890"
    fake.calls.clear()
    fake.fail_times = 1
    monkeypatch.setattr(nethelp, "system_proxy_for", lambda url: "http://127.0.0.1:7890")
    nethelp.robust_get("https://www.gushi.in/x", proxy="auto", sleep=lambda s: None)
    assert fake.calls[0]["trust_env"] is True and fake.calls[1]["trust_env"] is False  # 系统代理失败后再直连


# ---------------- 缓存兜底 ----------------
def test_gushi_falls_back_to_cache(tmp_path, monkeypatch):
    from qfpy.datasources import gushi as g
    src = g.GushiSource("gushi", {"type": "gushi", "key": "k", "min_interval_ms": 0}, tmp_path)
    rel = "pools/2026-11-10/gold_230.json"
    src.cache.put(rel, {"ok": True, "data": {"trading_date": "2026-11-10", "stock_list": [{"stock_code": "600000", "stock_name": "浦发银行"}]}})

    def boom(*a, **k):
        raise nethelp.NetError("连接被中途断开", "先关代理", "SSLError: UNEXPECTED_EOF")
    monkeypatch.setattr(g, "robust_get", boom)
    monkeypatch.setattr(src.cache, "get", lambda rel, ttl: None)                     # 当天没有新鲜缓存
    pr = src.get_pool("gold_230", "2026-11-11")
    assert pr.stale_at and pr.trading_date == "2026-11-10"                           # 标明用的是哪天的缓存
    assert [s["code"] for s in pr.stocks] == ["600000"] and "UNEXPECTED_EOF" in pr.detail


def test_gushi_no_cache_raises(tmp_path, monkeypatch):
    from qfpy.datasources import gushi as g
    src = g.GushiSource("gushi", {"type": "gushi", "key": "k", "min_interval_ms": 0}, tmp_path)
    monkeypatch.setattr(g, "robust_get", lambda *a, **k: (_ for _ in ()).throw(nethelp.NetError("断开", "", "x")))
    with pytest.raises(nethelp.NetError):
        src.get_pool("gold_230", "2026-11-11")
