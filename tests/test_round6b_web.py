"""第六轮网页（二）：股池分配搬进买入 / 卖出页，仓位与风控搬进账户页，设置只留系统配置，两类数据源分开。
只检查网页源码结构；真实浏览器里的点选、拖动、保存由验收脚本测。"""
from qfpy.web.uisource import read_ui_source
import json
import re
from pathlib import Path

HTML = read_ui_source()


def fn_body(name: str) -> str:
    m = re.search(r"(?:async )?function " + re.escape(name) + r"\(.*?(?=\n(?:async )?function |\nconst [A-Z_]+=|\nlet [A-Z_]+|\n/\* )", HTML, re.S)
    assert m, f"没找到 {name}()"
    return m.group(0)


def _legacy():
    m = re.search(r"const LEGACY=(\{.*?\});", HTML, re.S)
    return json.loads(re.sub(r'([{,]\s*)([A-Za-z_]+):', r'\1"\2":', m.group(1)))


def test_subtabs_buy_sell_account():
    subs = re.search(r"const SUBTABS=(\{.*?\});", HTML).group(1)
    assert '["pools","股池分配"]' in subs
    assert subs.count('["pools","股池分配"]') == 2           # 买入、卖出各一个
    assert 'account:[["overview","资金与持仓"],["risk","仓位与风控"]]' in subs
    assert "pools:poolAssign" in HTML and "embCfg(m,[cfgRisk]" in HTML


def test_settings_only_system_config():
    tabs = re.search(r"const CFGTABS=\[(.*?)\];", HTML).group(1)
    names = re.findall(r'\["(\w+)","([^"]+)"\]', tabs)
    keys = [k for k, _ in names]
    assert keys == ["sources", "pools", "schedule", "live", "notifycfg", "api", "plugins", "home", "mode"]
    for gone in ("bind", "select", "risk", "exits"):
        assert gone not in keys
    # 重复的实现已经删掉：选股打分只在买入策略里、卖出默认参数只在卖出策略里、股池绑定只在股池分配里
    for fn in ("cfgBind", "cfgSelect", "cfgExits", "bindSet", "bindMove"):
        assert f"function {fn}(" not in HTML, fn
    assert "选股与打分" in HTML and "卖出默认参数" in HTML
    assert 'id="movedto"' in HTML                                  # 设置页写明搬到哪里了


def test_legacy_hashes_point_to_new_places():
    lg = _legacy()
    want = {"config/bind": "buy/pools", "bind": "buy/pools", "config/select": "buy/strategy",
            "config/exits": "sell/strategy", "config/risk": "account/risk", "risk": "account/risk",
            "broker": "account", "tiers": "buy/results"}
    for k, v in want.items():
        assert lg.get(k) == v, k
    ph = re.search(r"function parseHash\(h\)\{.*?\n", HTML, re.S).group(0)
    assert "LEGACY[h]" in ph                                       # "config/bind" 这种带斜杠的整串先查


def test_pool_assign_page():
    b = fn_body("drawPA")
    for k in ("pastrat", "paentry", "draggable", "paDrop(", "paMove(", "pausedby", "股池优先顺序"):
        assert k in b, k
    # 新入池方式：跟随默认 / 每日重算 / 跨日连续 / 全部
    assert 'const ENTRY_OPTS=[["","跟随默认"],["day","每日重算"],["continuous","跨日连续"],["all","全部"]]' in HTML
    assert "entry_scope_by_pool" in fn_body("paEntry")
    # 点选、顺序都走股池绑定接口；只改买入或卖出一边
    assert '"/api/pool-bind",{method:"PUT"' in fn_body("paSet") and "[PA.side]:v" in fn_body("paSet")
    assert "order:ks" in fn_body("paOrderSave")
    # 策略页上显示哪些股池在用当前这一套
    assert 'id="usedby"' in HTML and "usedByLine(side)" in HTML
    assert "设置 → 股池绑定策略" not in HTML


def test_embedded_risk_saves_only_its_sections():
    b = fn_body("body")
    assert "EMB.secs" in b and 'k!=="sources"' in b
    assert '["sizing","risk","paper"]' in HTML
    assert 'if(TAB!=="config"){drawEmb();return}' in HTML


def test_sources_two_blocks_status_and_proxy():
    b = fn_body("cfgSources")
    assert 'id="poolsrc"' in b and 'id="marketsrc"' in b
    assert "股池数据源" in b and "行情数据源" in b and "/api/sources/status" in b
    st = fn_body("srcStatusHtml")
    for k in ("用在", "最近一次成功", "最近的报错", "zhErr(h.error)", "<details>"):
        assert k in st, k
    p = fn_body("proxySel")
    assert "跟随系统" in p and "直连" in p and "自定义" in p and 'value="none"' in p and 'value="auto"' in p
    assert '`${srcRole(n)}.${n}.${f.name}`' in fn_body("srcField")
    assert "W[role][n]=o" in fn_body("addSrc")


def test_pool_aliases_in_settings():
    assert 'id="aliascard"' in fn_body("cfgPools")
    assert "W.pool.aliases" in fn_body("setAlias")


def test_no_new_order_paths():
    for p in ("/api/order", "/api/cancel", "/api/command"):
        for fn in ("poolAssign", "drawPA", "paSet", "paOrderSave", "cfgSources", "embCfg"):
            assert p not in fn_body(fn)
