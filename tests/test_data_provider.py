"""标准数据接口（qfdp v1）：规范校验、代码格式、提供方服务（鉴权、错误码、分页）、standard 数据源客户端、
内置适配器实现规范、OpenAPI 描述和文档一致、插件示例。全部离线。"""
from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from qfpy import dataproto as dp
from qfpy.devtools.demo_provider import DemoSource

_REAL_SEND = httpx.Client.send
ROOT = Path(__file__).resolve().parent.parent


def provider_client(token: str = "", monkeypatch=None) -> TestClient:
    if monkeypatch is not None:
        monkeypatch.setattr(httpx.Client, "send", _REAL_SEND)
    return TestClient(dp.make_provider_app(DemoSource(), token=token, name="测试提供方"))


def route_httpx_to(monkeypatch, base: str, token: str = ""):
    """把对 base 的 httpx.get 转到进程内的演示提供方（不联网）。"""
    tc = provider_client(token, monkeypatch)
    import qfpy.datasources.standard as std

    class _H:
        TimeoutException = httpx.TimeoutException
        HTTPError = httpx.HTTPError

        @staticmethod
        def get(url, params=None, headers=None, timeout=None):
            assert url.startswith(base), url
            return tc.get(url[len(base):] or "/", params=params, headers=headers)
    monkeypatch.setattr(std, "httpx", _H)
    return tc


# ---------------- 代码格式 ----------------
def test_code_formats():
    assert dp.to_std_code("600000") == "600000.SH"
    assert dp.to_std_code("000001") == "000001.SZ"
    assert dp.to_std_code("300825") == "300825.SZ"
    assert dp.to_std_code("688981") == "688981.SH"
    assert dp.to_std_code("sh600000") == "600000.SH"
    assert dp.to_std_code("600000.sh") == "600000.SH"
    assert dp.code6("300825.SZ") == "300825"
    assert dp.index_std_code("sh000001") == "000001.SH"
    with pytest.raises(dp.ProtocolError):
        dp.index_std_code("000001")             # 指数必须带后缀
    with pytest.raises(dp.ProtocolError):
        dp.to_std_code("60000")


# ---------------- 校验 ----------------
def test_validators_catch_unit_and_format_mistakes():
    good = {"code": "600000.SH", "price": 10.5, "preclose": 10.0, "change_pct": 5.0, "volume": 1e6, "amount": 1e7, "ts": 1762840800000}
    assert dp.validate_quote(good) == []
    assert dp.validate_quote({**good, "code": "600000"})            # 没后缀
    assert dp.validate_quote({**good, "ts": 1762840800})            # 秒不是毫秒
    assert dp.validate_quote({**good, "change_pct": 5000})          # 不像百分数
    assert dp.validate_quote({**good, "price": "10.5"})             # 字符串
    assert dp.validate_quote({"code": "600000.SH", "price": 1})     # 没有 preclose 也没有 change_pct
    m = {"date": "2026-11-11", "points": [{"t": "09:31", "p": 1.0}, {"t": "09:30", "p": 1.0}]}
    assert any("升序" in x for x in dp.validate_minute(m))
    assert dp.validate_daily([{"date": "2026-11-10", "open": 1, "high": 1, "low": 1, "close": 1},
                              {"date": "2026-11-10", "open": 1, "high": 1, "low": 1, "close": 1}])
    assert dp.validate_market_stats({"date": "2026-11-11", "up": 1, "down": -1})
    assert dp.validate_pool({"key": "x", "trading_date": "2026-11-11", "stocks": [{"code": "600000.SH", "fields": {"a": "x"}}]})
    assert dp.validate_pool_list([{"key": "bad key", "name": "x"}])
    assert dp.validate_meta({"protocol": "qfdp", "version": "1.0", "capabilities": ["pool", "teleport"]})


# ---------------- 提供方服务 ----------------
def test_provider_meta_and_every_endpoint_conforms(monkeypatch):
    c = provider_client(monkeypatch=monkeypatch)
    m = c.get("/meta").json()
    assert m["ok"] and dp.validate_meta(m["data"]) == [] and set(m["data"]["capabilities"]) == set(dp.CAPABILITIES)
    q = c.get("/quotes", params={"codes": "600000.SH,300825.SZ"}).json()["data"]
    assert [x["code"] for x in q] == ["600000.SH", "300825.SZ"] and all(dp.validate_quote(x) == [] for x in q)
    mi = c.get("/minute", params={"code": "600000.SH", "date": "2026-11-11"}).json()["data"]
    assert dp.validate_minute(mi) == [] and mi["points"][0]["t"] == "09:30" and mi["points"][-1]["t"] == "15:00"
    d = c.get("/daily", params={"code": "600000.SH", "start": "2026-11-02", "end": "2026-11-11"}).json()["data"]
    assert dp.validate_daily(d) == [] and len(d) == 8
    ix = c.get("/indices").json()["data"]
    assert {x["code"] for x in ix} >= {"000001.SH", "399006.SZ", "000688.SH", "000300.SH"}
    assert all(dp.validate_quote(x, index=True) == [] for x in ix)
    im = c.get("/index_minute", params={"code": "000300.SH", "date": "2026-11-11"}).json()["data"]
    assert dp.validate_minute(im) == [] and im["code"] == "000300.SH"
    st = c.get("/market_stats", params={"date": "2026-11-11"}).json()["data"]
    assert dp.validate_market_stats(st) == []
    pl = c.get("/pools").json()["data"]
    assert dp.validate_pool_list(pl) == [] and pl[0]["key"] == "demo_hot"
    p = c.get("/pools/demo_hot").json()["data"]
    assert dp.validate_pool(p) == [] and p["total"] == 4


def test_provider_pagination(monkeypatch):
    c = provider_client(monkeypatch=monkeypatch)
    p1 = c.get("/pools/demo_hot", params={"page": 1, "page_size": 3}).json()["data"]
    p2 = c.get("/pools/demo_hot", params={"page": 2, "page_size": 3}).json()["data"]
    assert p1["total"] == 4 and len(p1["stocks"]) == 3 and len(p2["stocks"]) == 1
    r = c.get("/pools/demo_hot", params={"page_size": 10000})
    assert r.status_code == 400 and r.json()["error"]["code"] == "BAD_REQUEST"


def test_provider_errors_and_auth(monkeypatch):
    c = provider_client(token="tok-123456789", monkeypatch=monkeypatch)
    r = c.get("/meta")
    assert r.status_code == 401 and r.json() == {"ok": False, "error": {"code": "UNAUTHORIZED", "message": r.json()["error"]["message"]},
                                                 "ts": r.json()["ts"]}
    h = {"Authorization": "Bearer tok-123456789"}
    assert c.get("/meta", headers=h).status_code == 200
    assert c.get("/openapi.json").status_code == 200                       # 描述不需要 token
    assert c.get("/pools/nope", headers=h).json()["error"]["code"] == "NOT_FOUND"
    assert c.get("/quotes", headers=h).json()["error"]["code"] == "BAD_REQUEST"
    r = c.get("/index_minute", params={"code": "000001"}, headers=h)
    assert r.status_code == 400 and "后缀" in r.json()["error"]["message"]
    assert c.get("/daily", params={"code": "600000.SH", "start": "2026-11-01", "adjust": "qfq"}, headers=h).json()["error"]["code"] == "NOT_SUPPORTED"
    # 只声明部分能力的提供方：其余接口 501

    class PoolOnly(DemoSource):
        capabilities = ("pool",)
    c2 = TestClient(dp.make_provider_app(PoolOnly()))
    r = c2.get("/indices")
    assert r.status_code == 501 and r.json()["error"]["code"] == "NOT_SUPPORTED"
    assert c2.get("/meta").json()["data"]["capabilities"] == ["pool"]


# ---------------- standard 数据源（客户端） ----------------
def _std(tmp_path, monkeypatch, token="", cfg=None):
    from qfpy.datasources.standard import StandardSource
    route_httpx_to(monkeypatch, "http://prov.local", token=token)
    return StandardSource("prov", {"url": "http://prov.local", "token": token, **(cfg or {})}, tmp_path)


def test_standard_source_reads_everything(tmp_path, monkeypatch):
    s = _std(tmp_path, monkeypatch, token="tok-123456789")
    assert [p["key"] for p in s.list_pools()] == ["demo_hot", "demo_value"]
    r = s.get_pool("demo_hot", None)
    assert r.key == "prov:demo_hot" and [x["code"] for x in r.stocks][:2] == ["600000", "300825"]
    assert "change_rate" in r.stocks[0]["fields"]
    assert s.get_quotes(["600000", "300825"])["300825"]["code"] == "300825.SZ"
    assert len(s.get_minute("600000", "2026-11-11")["points"]) == 241
    assert s.get_daily("600000", "2026-11-02", "2026-11-06")[0]["date"] == "2026-11-02"
    assert len(s.get_indices()) == 6
    assert s.get_index_minute("000001.SH", "2026-11-11")["code"] == "000001.SH"
    assert s.get_market_stats("2026-11-11")["up"] > 0
    assert all(c["ok"] for c in s.test_connection())


def test_standard_source_token_env_and_wrong_token(tmp_path, monkeypatch):
    from qfpy.datasources.standard import ProviderError
    s = _std(tmp_path, monkeypatch, token="tok-123456789")
    s.token = "wrong"
    with pytest.raises(ProviderError, match="UNAUTHORIZED"):
        s.list_pools()
    monkeypatch.setenv("QF_SRC_PROV_TOKEN", "tok-123456789")
    from qfpy.datasources.standard import StandardSource
    s2 = StandardSource("prov", {"url": "http://prov.local", "token": "wrong"}, tmp_path / "b")
    assert s2.token == "tok-123456789" and s2.list_pools()


def test_standard_source_rejects_nonconforming_responses(tmp_path, monkeypatch):
    """不合规范的响应直接报错，不会把脏数据喂给选股。"""
    from qfpy.datasources.standard import ProviderError, StandardSource
    payloads = {
        "/meta": {"ok": True, "data": {"protocol": "qfdp", "version": "1.0", "capabilities": ["pool", "quote"]}},
        "/pools": {"ok": True, "data": [{"key": "p1", "name": "池"}]},
        "/pools/p1": {"ok": True, "data": {"key": "p1", "trading_date": "2026-11-11", "stocks": [{"code": "600000"}]}},
        "/quotes": {"data": []},
    }

    def handler(req: httpx.Request):
        return httpx.Response(200, json=payloads.get(req.url.path, {"ok": False, "error": {"code": "NOT_FOUND", "message": "x"}}))
    cli = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(httpx.Client, "send", _REAL_SEND)
    s = StandardSource("bad", {"url": "http://bad.local"}, tmp_path, client=cli)
    with pytest.raises(ProviderError, match="不合规范"):
        s.get_pool("p1", "2026-11-11")
    with pytest.raises(ProviderError, match="ok"):
        s.get_quotes(["600000"])
    from qfpy.datasources.base import NotSupported
    with pytest.raises(NotSupported):
        s.get_indices()                     # /meta 没声明 index


def test_standard_source_in_market_fallback(tmp_path, monkeypatch):
    """标准接口数据源进入行情源顺序：第一个失败自动换下一个。"""
    import copy

    from qfpy.config import DEFAULTS
    from qfpy.market import Market
    s = _std(tmp_path, monkeypatch)

    class Broken(DemoSource):
        def get_indices(self, codes=None):
            raise RuntimeError("挂了")
    cfg = copy.deepcopy(DEFAULTS)
    cfg["market"]["quote_sources"] = ["broken", "prov"]
    m = Market({"broken": Broken("broken"), "prov": s}, cfg, tmp_path)
    ix, src, err = m.indices()
    assert src == "prov" and len(ix) == 6 and err is None
    st, src, _ = m.market_stats("2026-11-11")
    assert src == "broken" and st["up"] > 0          # broken 的市场情绪是好的


# ---------------- 内置适配器实现规范 ----------------
def test_builtin_adapters_declare_protocol_capabilities():
    from qfpy.datasources import SOURCE_TYPES
    caps = {k: set(v.capabilities) for k, v in SOURCE_TYPES.items()}
    assert {"pool", "quote", "minute", "daily", "index", "index_minute", "market_stats"} <= caps["akshare"]
    assert {"pool", "minute"} <= caps["gushi"]
    assert "pool" in caps["watchlist"]
    assert set(dp.CAPABILITIES) <= caps["standard"]
    from qfpy.datasources.base import DataSource
    for name in ("get_quotes", "get_indices", "get_index_minute", "get_market_stats", "test_connection"):
        assert hasattr(DataSource, name)


def test_builtin_adapters_can_be_served_as_standard_provider(tmp_path, monkeypatch):
    """内置适配器包成标准接口服务后，输出通过规范校验（自选股 CSV + gushi 缓存）。"""
    from qfpy.datasources.watchlist import WatchlistSource
    monkeypatch.setattr(httpx.Client, "send", _REAL_SEND)
    csv = tmp_path / "w.csv"
    csv.write_text("code,name\n600000,浦发银行\n300825,阿尔特\n", encoding="utf-8")
    w = WatchlistSource("watchlist", {"csv": str(csv)}, tmp_path)
    c = TestClient(dp.make_provider_app(w))
    assert c.get("/meta").json()["data"]["capabilities"] == ["pool"]
    p = c.get("/pools/all").json()["data"]
    assert dp.validate_pool(p) == [] and [s["code"] for s in p["stocks"]] == ["600000.SH", "300825.SZ"]


def test_akshare_quote_parsers_offline():
    """akshare 适配器新增的实时行情 / 分时解析（用录好的文本，不联网）。"""
    from qfpy.datasources.public_hq import parse_sina, parse_trends
    txt = ('var hq_str_sh000001="上证指数,3990.1,3982.5,4001.2,4010.0,3980.3,0,0,412345678,567890123456,'
           + ",".join(["0"] * 20) + ',2026-11-11,15:30:39,00,";\n'
           'var hq_str_sz300825="阿尔特,94.00,93.50,95.20,96.00,93.10,95.19,95.20,1234567,117000000.5,'
           + ",".join(["0"] * 20) + ',2026-11-11,15:00:03,00";\n')
    q = parse_sina(txt, ["000001.SH", "300825.SZ"], index=True)
    assert q[0]["name"] == "上证指数" and q[0]["price"] == 4001.2 and q[0]["volume"] == 41234567800
    assert abs(q[0]["change_pct"] - (4001.2 - 3982.5) / 3982.5 * 100) < 1e-3 and q[0]["ts"] > 1e12
    assert dp.validate_quote(q[0], index=True) == [] and dp.validate_quote(q[1]) == []
    data = {"preClose": 3982.5, "trends": ["2026-11-11 09:30,3990,3990.5,3991,3989,1000,3990000,3990.2",
                                         "2026-11-11 09:31,3990.5,3992.0,3993,3990,2000,7990000,3991.0"]}
    m = parse_trends(data, "000001.SH", "2026-11-11")
    assert dp.validate_minute(m) == [] and m["points"][1] == {"t": "09:31", "p": 3992.0, "avg": 3991.0, "vol": 200000.0, "amt": 7990000.0}


# ---------------- OpenAPI 和文档 ----------------
def test_openapi_spec_matches_provider_routes():
    spec = dp.openapi_spec()
    app = dp.make_provider_app(DemoSource())
    routes = {r.path for r in app.routes if getattr(r, "path", "") and r.path != "/openapi.json"}
    assert set(spec["paths"]) == routes
    caps = {p["get"]["x-capability"] for p in spec["paths"].values()} - {"meta"}
    assert caps == set(dp.CAPABILITIES)
    assert set(spec["components"]["schemas"]["Error"]["properties"]["error"]["properties"]["code"]["enum"]) == set(dp.ERROR_CODES)


def test_data_provider_doc_complete():
    doc = (ROOT / "docs" / "GUIDE.md").read_text("utf-8")    # 文档已合并进 GUIDE.md
    for path in dp.openapi_spec()["paths"]:
        assert f"`{path}`" in doc or f"GET {path}" in doc, path
    for code in dp.ERROR_CODES:
        assert code in doc, code
    for word in ("Authorization: Bearer", "page_size", "600000.SH", "毫秒", "百分数", "get_index_minute", "register_source",
                 "openapi", "data_provider_server.py", "example_data_provider.py"):
        assert word in doc, word
    # 文档里的 JSON 示例都能解析
    blocks = re.findall(r"```json\n(.*?)```", doc, re.S)
    assert len(blocks) >= 6
    for b in blocks:
        json.loads(b)
    readme = (ROOT / "README.md").read_text("utf-8")
    assert "GUIDE.md" in readme and "标准数据接口" in readme


def test_example_server_and_plugin(tmp_path, monkeypatch):
    import importlib.util
    monkeypatch.setattr(httpx.Client, "send", _REAL_SEND)
    spec = importlib.util.spec_from_file_location("dps", ROOT / "examples" / "data_provider_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    c = TestClient(mod.build("demo", "secret-token-1"))
    assert c.get("/meta").status_code == 401
    assert c.get("/meta", headers={"Authorization": "Bearer secret-token-1"}).json()["data"]["protocol"] == "qfdp"
    from qfpy.plugins import load_plugins
    from qfpy.datasources import SOURCE_TYPES
    infos = load_plugins(ROOT / "plugins")
    assert any(i["name"] == "example_data_provider" and i["ok"] for i in infos)
    csv = tmp_path / "p.csv"
    csv.write_text("code,name,score\n600000,浦发银行,1.5\n1,平安银行,2\n", encoding="utf-8")
    s = SOURCE_TYPES["csv_pool"]("mycsv", {"path": str(csv)}, tmp_path)
    r = s.get_pool("all", "2026-11-11")
    assert [x["code"] for x in r.stocks] == ["600000", "000001"] and r.stocks[0]["fields"] == {"score": 1.5}
    assert s.test_connection()[0]["ok"]
    p = TestClient(dp.make_provider_app(s)).get("/pools/all").json()["data"]
    assert dp.validate_pool(p) == []
