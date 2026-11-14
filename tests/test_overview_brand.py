"""首页「大盘行情 + 股池走势」接口、腥讯分时解析、新界面结构，以及「牛来量化」改名。全部离线。"""
from __future__ import annotations
from qfpy.web.uisource import read_ui_source

import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
_REAL_SEND = __import__("httpx").Client.send
OLD_NAMES = re.compile(r"quant[-_ ]?funnel", re.I)


class OvMarket:
    """只实现首页用到的行情方法。"""

    def __init__(self, day="2026-11-11"):
        self.day = day

    def latest_trading_date(self):
        return self.day

    def indices(self, codes):
        return [{"code": c, "name": {"000001.SH": "上证指数"}.get(c, c), "price": 3000.0, "preclose": 2970.0, "change": 30.0,
                 "change_pct": 1.0101, "amount": 5e11} for c in codes], "fake", None

    def index_minute(self, code, day):
        if day != self.day:
            return None, None, "no"
        return {"code": code, "date": day, "preclose": 2970.0, "points": [{"t": f"09:{30 + i:02d}", "p": 2970 + i} for i in range(20)]}, "fake", None

    def market_stats(self, day):
        return {"date": day, "up": 3000, "down": 2000, "flat": 100, "limit_up": 60, "limit_down": 5, "amount": 1.2e12}, "fake", None

    def quotes(self, codes):
        return {c: {"code": c, "name": "N" + c, "price": 11.0, "change_pct": 2.0 if c == "600000" else -1.0} for c in codes}, "fake", None

    def stock_minute(self, code, day):
        # 600000：昨收 10，09:30 10.0 → 09:32 10.2；其他：昨收 20，09:30 19.8 → 09:31 20.2（09:32 缺，沿用）
        if code == "600000":
            return {"preclose": 10.0, "points": [{"t": "09:30", "p": 10.0}, {"t": "09:31", "p": 10.1}, {"t": "09:32", "p": 10.2}]}, "fake", None
        return {"preclose": 20.0, "points": [{"t": "09:30", "p": 19.8}, {"t": "09:31", "p": 20.2}]}, "fake", None

    def daily(self, code, end):
        closes = [10.0, 10.5, 10.0, 11.0]
        return [{"date": f"2026-11-0{5 + i}", "close": c} for i, c in enumerate(closes)], None


@pytest.fixture
def ov_engine(make_engine):
    e = make_engine("paper")
    fm = OvMarket()
    e.market = lambda: fm
    return e


def test_overview_market_and_pools(ov_engine, monkeypatch):
    # 假行情的交易日是 2026-11-11；把系统「今天」固定到这天，换日期跑也不会因为日期过期而挂
    monkeypatch.setenv("QF_SIM_NOW", "2026-11-11 15:00")
    from qfpy.overview import Overview
    o = Overview(ov_engine)
    m = o._market()
    assert [i["code"] for i in m["indices"]] == ["000001.SH", "399001.SZ", "399006.SZ", "000688.SH", "000300.SH"]
    assert m["indices"][0]["name"] == "上证指数" and m["indices"][0]["minute"]["points"][0] == {"t": "09:30", "p": 2970}
    assert m["stats"]["limit_up"] == 60 and m["trading_date"] == "2026-11-11"
    p = o._pools()
    row = p["pools"][0]
    assert row["key"] == "watchlist:all" and row["count"] == 1 and row["avg_change"] == 2.0 and row["up"] == 1
    d = o.pool_detail("watchlist:all")
    assert d["members"][0]["code"] == "600000" and d["members"][0]["price"] == 11.0
    # 股池走势曲线：当天等权分时 + 当前成分回看日K（只有一天入池记录也有曲线）
    assert [p["t"] for p in row["intraday"]["points"]] == ["09:30", "09:31", "09:32"]
    assert row["intraday"]["points"][-1]["v"] == 2.0 and row["intraday"]["members"] == 1
    lb = row["lookback"]["days"]
    assert [x["date"] for x in lb] == ["2026-11-06", "2026-11-07", "2026-11-08", "2026-11-11"]
    assert lb[0]["avg_pct"] == 5.0 and lb[-1]["live"] is True and lb[-1]["avg_pct"] == 2.0
    assert "幸存者偏差" in row["lookback"]["basis"] and len(lb) >= 2


def test_intraday_curve_equal_weight_with_ffill():
    from qfpy.overview import intraday_curve, lookback_curve
    a = {"points": [{"t": "09:30", "p": 10.0}, {"t": "09:31", "p": 10.1}, {"t": "09:32", "p": 10.2}]}
    b = {"points": [{"t": "09:30", "p": 19.8}, {"t": "09:31", "p": 20.2}]}
    c = intraday_curve([(a, 10.0), (b, 20.0)])
    assert [(p["t"], p["v"]) for p in c] == [("09:30", -0.5), ("09:31", 1.0), ("09:32", 1.5)]
    assert intraday_curve([(a, None)]) == []
    lb = lookback_curve({"X": [{"date": "d1", "close": 10}, {"date": "d2", "close": 11}, {"date": "d3", "close": 9.9}],
                         "Y": [{"date": "d1", "close": 10}, {"date": "d2", "close": 10}, {"date": "d3", "close": 10}]}, 5)
    assert [(x["date"], x["avg_pct"], x["up"], x["down"]) for x in lb] == [("d2", 5.0, 1, 0), ("d3", -5.0, 0, 1)]
    assert lb[-1]["cum_pct"] == round((1.05 * 0.95 - 1) * 100, 3)


def test_overview_falls_back_to_run_records_with_backfill(ov_engine):
    """数据源取不到股池时，用最近一次运行记录（含补录）的成分，并算近 N 日走势。"""
    from qfpy.overview import Overview
    from qfpy.util import write_json
    e = ov_engine
    e.cfg["pool"]["pools"] = ["gone:hot"]
    runs = e.data_dir / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    for i, d in enumerate(["2026-11-10", "2026-11-11"]):
        rid = f"{d.replace('-', '')}-093100"
        write_json(runs / f"{rid}.json", {"id": rid, "trading_date": d, "origin": "补录" if i else "定时", "backfill": bool(i),
                                           "picks": [{"code": "000001"}],
                                           "stages": [{"kept": [{"code": "000001", "name": "平安银行", "sources": ["gone:hot"],
                                                                "fields": {"price": 10.0, "change_rate": 3.0 + i}}]}]})
    write_json(runs / "index.json", [{"id": "20261111-093100", "trading_date": "2026-11-11"}, {"id": "20261110-093100", "trading_date": "2026-11-10"}])
    o = Overview(e)
    p = o._pools()
    row = p["pools"][0]
    assert row["count"] == 1 and row["from"] == "运行记录（补录）" and row["trading_date"] == "2026-11-11"
    assert [s["date"] for s in row["series"]] == ["2026-11-10", "2026-11-11"]
    assert row["series"][1]["backfill"] is True and row["series"][1]["avg_entry_change"] == 4.0
    assert row["series"][0]["avg_since_entry"] == 10.0          # 10 → 11
    assert row["series"][0]["picked"] == ["000001 平安银行"]


def test_overview_endpoint_is_async(ov_engine, monkeypatch):
    """/api/overview 立即返回（后台计算），算完后再取就有数据；成分股接口可用。"""
    import httpx
    from qfpy.web.server import create_app
    # 只给进程内的 TestClient 放行（它不走网络）；其他 httpx 请求仍被 conftest 拦截
    monkeypatch.setattr(TestClient, "send", _REAL_SEND, raising=False)
    app = create_app(ov_engine)
    c = TestClient(app)
    t = time.time()
    r = c.get("/api/overview").json()
    assert time.time() - t < 2 and "market_job" in r
    for _ in range(50):
        r = c.get("/api/overview").json()
        if r["market"] and r["pools"]:
            break
        time.sleep(0.1)
    assert r["market"]["indices"][0]["price"] == 3000.0 and r["pools"]["pools"][0]["count"] == 1
    d = c.get("/api/overview/pool", params={"key": "watchlist:all"}).json()
    assert d["members"][0]["code"] == "600000"
    spec = c.get("/api/dataproto/openapi.json").json()
    assert spec["info"]["title"].startswith("牛来量化") and "/quotes" in spec["paths"]


def test_home_config_validation(ov_engine):
    from qfpy.config import validate
    cfg = ov_engine.cfg
    cfg["home"] = {"indices": ["000001.SH", "bad"], "pool_days": 5, "refresh_secs": 30}
    with pytest.raises(ValueError):
        validate(cfg)


def test_qq_minute_parser():
    from qfpy.datasources.public_hq import parse_qq_minute
    j = {"data": {"sh000001": {"data": {"date": "20261111", "data": ["0930 3000.00 100 30000000.00", "0931 3001.50 150 45000000.00"]},
                               "qt": {"sh000001": ["1", "上证指数", "000001", "3001.50", "2990.00"]}}}}
    d = parse_qq_minute(j, "000001.SH", "2026-11-11")
    assert d["preclose"] == 2990.0 and [p["t"] for p in d["points"]] == ["09:30", "09:31"]
    assert d["points"][1]["vol"] == 5000 and d["points"][1]["amt"] == 15000000.0
    with pytest.raises(RuntimeError):
        parse_qq_minute(j, "000001.SH", "2026-11-10")


def test_ui_structure_new_pages():
    html = read_ui_source()
    # 第四轮导航：买入策略、卖出策略成了「买入」「卖出」下的子页，今日漏斗改名「选股结果」
    for page in ('["buy","买入"]', '["sell","卖出"]', '["strategy","买入策略"]', '["strategy","卖出策略"]',
                 '["results","选股结果"]', '["account","账户"]', '["config","设置"]'):
        assert page in html, page
    assert 'funnel:"buy/results"' in html                                     # 旧 #funnel 还能打开
    assert 'id="tbctl"' in html and "function drawTopCtl" in html               # 顶栏开关
    assert "main{max-width:1280px;margin:0 auto}" in html                      # 内容区居中
    assert "/api/overview" in html and "function askLiveConfirm" not in html   # 切实盘不再有确认框
    assert "/api/test/source" in html and "/api/test/xiadan" in html
    assert "/api/positions_mode" in html and "连接服务器" in html
    for tab in ("cfgSources", "cfgLive", "cfgApi", "cfgMode", "cfgHome", "cfgSchedule"):
        assert f"function {tab}(" in html


def test_push_title_prefix():
    from qfpy.notify.base import with_prefix
    assert with_prefix("补录") == "【牛来量化】补录" and with_prefix("【牛来量化】x") == "【牛来量化】x"


def test_brand_name_everywhere():
    """对外显示的名称都是「牛来量化」，界面、文档、脚本、示例里不再出现旧名。"""
    files = [ROOT / "qfpy/web/index.html", *sorted((ROOT / "qfpy/web/parts").glob("*.js")), ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md")),
             ROOT / "scripts/start.sh", ROOT / "scripts/start.bat", ROOT / "config/pipeline.toml",
             *sorted((ROOT / "examples").glob("*.py")), *sorted((ROOT / "plugins").glob("*.py")), *sorted((ROOT / "qfpy").rglob("*.py"))]
    bad = [f"{f.relative_to(ROOT)}:{i}" for f in files if f.exists()
           for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1) if OLD_NAMES.search(line)]
    assert not bad, "还有旧名：" + "、".join(bad)
    html = read_ui_source()
    assert "<title>牛来量化</title>" in html and "<b>牛来量化</b>" in html
    for doc in ("README.md", "docs/GUIDE.md", "docs/API.md", "docs/DATA_PROVIDER.md"):
        assert (ROOT / doc).read_text(encoding="utf-8").lstrip().startswith("# 牛来量化"), doc
    from qfpy.web.server import create_app  # noqa: F401
    assert "牛来量化" in (ROOT / "scripts/start.sh").read_text(encoding="utf-8")
    # start.bat 必须是纯 ASCII（中文 Windows 的 cmd 按 GBK 读 .bat），所以用拼音名
    assert "Niulai Quant" in (ROOT / "scripts/start.bat").read_text(encoding="ascii")
