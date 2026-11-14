"""「实盘/下单程序」页不阻塞：下单程序不可达、行情慢的时候，网页用到的接口都立刻返回，慢操作在后台做。

卡顿原因（上一版）：点「下单程序」页时前端 await /api/broker_status，而后端在请求线程里同步调
下单程序 /api/status（连接超时 5 秒，还会被同一把锁上的其他检查排队），页面渲染要等它返回；
持仓页的 /api/account 又在请求里同步算卖出预览（冷缓存时拉行情 3~4 秒）。
"""
from __future__ import annotations
from qfpy.web.uisource import read_ui_source

import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from qfpy.web.server import create_app
from tests.conftest import FakeMarket, add_position

_real_send = httpx.Client.send
SLOW = 2.0


@pytest.fixture
def slow_engine(make_engine, monkeypatch):
    monkeypatch.setattr(httpx.Client, "send", _real_send)
    e = make_engine("dry_run")
    gate = threading.Event()

    def slow_status(force=False):           # 模拟下单程序不可达：连接要等很久才超时
        gate.wait(SLOW)
        with e.link.lock:
            e.link.conn = {"state": "unreachable", "label": "连不上", "error": {"message": "连接超时"}}
        return e.link.conn
    e.link.check_status = slow_status

    class SlowMarket(FakeMarket):
        def minute(self, code, day):
            gate.wait(SLOW)
            return super().minute(code, day)
    fm = SlowMarket("2026-11-10", ["2026-11-09", "2026-11-10"], {"600000": (10.1, 10.0)})
    e.market = lambda: fm
    add_position(e)
    e.switches.set_positions_auto(True, "pytest")     # 读券商数据只在「自动获取持仓」打开时（这里在演练模式下模拟打开）
    e.auto_pos = e.link.auto_positions_fn = lambda: True
    yield e, TestClient(create_app(e)), gate
    gate.set()


def timed(fn):
    t = time.monotonic()
    r = fn()
    return r, time.monotonic() - t


def test_broker_page_endpoints_do_not_wait(slow_engine):
    e, tc, gate = slow_engine
    for path in ("/api/broker", "/api/broker_status", "/api/status", "/api/account", "/api/meta", "/api/sell/preview"):
        r, dt = timed(lambda: tc.get(path))
        assert r.status_code == 200, path
        assert dt < 0.5, f"{path} 用了 {dt:.2f}s"
    r, dt = timed(lambda: tc.post("/api/broker/sync", json={"how": "fresh"}))
    assert r.status_code == 200 and dt < 0.5 and r.json()["sync"]["running"] is True
    # 后台还在连的时候再点一次同步也不会卡住、不会叠加
    r2, dt2 = timed(lambda: tc.post("/api/broker/sync", json={"how": "status"}))
    assert dt2 < 0.5 and r2.json()["sync_started"] is False
    gate.set()
    e.link.sync_job.wait(5)
    v = tc.get("/api/broker").json()
    assert v["sync"]["running"] is False and v["conn"]["state"] == "unreachable"


def test_account_shows_cached_preview_then_fills_in(slow_engine):
    e, tc, gate = slow_engine
    a = tc.get("/api/account").json()
    assert a["positions"][0]["exit_preview"] is None and a["preview"]["running"] is True
    gate.set()
    e.preview_job.wait(5)
    a = tc.get("/api/account").json()
    assert a["preview"]["running"] is False and a["positions"][0]["exit_preview"]["reason"]
    pv = tc.get("/api/sell/preview").json()
    assert pv["results"][0]["layers"] and [l["id"] for l in pv["layers"]] == ["stop_loss", "take_profit", "time"]


def test_link_lock_not_held_by_web_requests(slow_engine):
    """后台同步正在等下单程序时，link.view() 要用的锁是空闲的（只在写结果时短暂持有）。"""
    e, tc, gate = slow_engine
    tc.post("/api/broker/sync", json={"how": "status"})
    time.sleep(0.1)
    assert e.link.lock.acquire(timeout=0.2)
    e.link.lock.release()


def test_html_has_no_blocking_awaits_on_broker():
    """前端：切页先渲染，下单程序和卖出预览都是异步加载；所有 fetch 带超时。"""
    from pathlib import Path
    html = read_ui_source()
    assert "await api('/api/broker_status')" not in html and 'await api("/api/broker_status")' not in html
    assert "AbortController" in html


def test_logs_endpoint_reads_tail(slow_engine):
    eng, client, _gate = slow_engine
    log = eng.data_dir / "qf.log"
    log.write_text("\n".join(f"line {i}" for i in range(1000)) + "\nalpha ERR\n", encoding="utf-8")
    r = client.get("/api/logs?n=5").json()
    assert r["lines"][-1] == "alpha ERR" and len(r["lines"]) == 5
    r = client.get("/api/logs?n=50&q=err").json()
    assert r["lines"] == ["alpha ERR"]
