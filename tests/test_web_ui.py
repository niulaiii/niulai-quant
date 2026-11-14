"""新界面的静态检查：单文件、不依赖构建步骤、旧功能入口都在、测试推送带表单值。"""
from __future__ import annotations
from qfpy.web.uisource import read_ui_source

import re
from pathlib import Path

HTML = read_ui_source()


def test_single_file_no_external_assets():
    assert not re.search(r"<script[^>]+src=", HTML)
    assert not re.search(r"<link[^>]+stylesheet", HTML)
    assert "import " not in HTML.split("<script>")[-1][:200]


def test_all_pages_and_controls_present():
    # 第四轮导航：一级是首页、买入、卖出、账户、回测、设置、日志；今日漏斗改名「选股结果」放到买入下，分级股池也在买入下
    assert 'const TABS=[["home","首页"],["buy","买入"],["sell","卖出"],["account","账户"],["backtest","回测"],["config","设置"],["notify","日志"]]' in HTML
    # 第六轮：分级股池并进选股结果；股池走势内嵌首页，首页不再有子标签
    for sub in ['["strategy","买入策略"]', '["results","选股结果"]', '["strategy","卖出策略"]', '["results","卖出结果"]']:
        assert sub in HTML, sub
    for gone in ['["tiers","分级股池"]', '["pools","股池走势"]', '["overview","大盘与股池"]']:
        assert gone not in HTML, gone
    assert "results:funnel})[curSub()]" in HTML and "results:sellResults" in HTML   # 子页真的接到了页面函数
    assert "async function funnel(" in HTML and "function tiers(" in HTML and "async function sellResults(" in HTML
    assert '["broker",' not in HTML  # 持仓页和实盘页合并成了「账户」
    for fn in ["toggleTrading(", "setHold(", "confirmPos(", "saveCfg(", "testCh(", "bsync(", "ordCancel(", "ordReview(",
               "ordEdit(", "saveFunds(", "soldPos(", "funnelBars(", "curve(", "runBt(", "toggleTheme(", "morph("]:
        assert fn in HTML, fn
    for gone in ["toggleAuto(", "halt()", "resume()", "askLiveConfirm", "确认切换实盘", "一键停止"]:
        assert gone not in HTML, gone
    assert 'data-theme="light"' in HTML or "[data-theme=light]" in HTML or "data-theme='light'" in HTML


def test_notify_test_button_sends_form_values():
    m = re.search(r"async function testCh\(.*?\}\}", HTML, re.S)
    assert m and "/api/notify/test" in m.group(0) and "config:" in m.group(0)
    assert "不用先保存" in HTML


def test_ui_never_references_trade_or_forbidden_endpoints_directly():
    # 网页只调本系统的 /api/trade/*，下单程序的接口由后端去调
    for p in ['"/api/order"', '"/api/cancel"', "/api/command", "/api/client/restart", "/api/connect"]:
        assert p not in HTML


def test_home_is_market_overview_and_editors_exist():
    """首页是大盘行情 + 市场情绪 + 股池走势；自动交易开关在顶栏；漏斗、持仓、买入/卖出策略各自成页；
    策略编辑器、模板、卖出结果矩阵都在。"""
    m = re.search(r"async function home\(m\)\{.*?\nfunction homePoll", HTML, re.S)
    assert m, "home() 没找到"
    body = m.group(0)
    assert "/api/overview" in body
    d = re.search(r"function drawHome\(m,o\)\{.*?\n(?=function |async function )", HTML, re.S).group(0)
    assert "idxTiles(" in d and "statsCard(" in d and "ptBar(" in d and "poolTrend(" in d
    for k in ["posTable(", "fillRun(", "controlCard(", "config", "backtest", "eventList", "/api/notify", "/api/broker"]:
        assert k not in body, k
    assert 'MAIN_TABS=["home","buy","sell","account"],MORE_TABS=["backtest","config","notify"]' in HTML
    for page in ("results:funnel", "overview:account", 'STRAT.side="buy"', 'STRAT.side="sell"', 'home:()=>home'):
        assert page in HTML, page
    assert 'id="tbctl"' in HTML and "function drawTopCtl" in HTML
    # 第四轮：套用模板改成先预览差异再套用（tplPreview），加因子改成从因子库弹窗点选（openFacLib → addFacKind）
    for fn in ["tplPreview(", "openFacLib(", "addFacKind(", "addLayer(", "stratSave(", "stratCheck(", "sellMatrix(", "sellChips(", "mvL(", "mvF("]:
        assert fn in HTML, fn


def test_every_request_has_timeout_and_cache_first():
    m = re.search(r"async function api\(path,opt=\{\}\)\{.*?return j\}", HTML, re.S)
    assert m and "AbortController" in m.group(0) and "setTimeout(()=>ctl.abort()" in m.group(0)
    assert "fetch(" not in HTML.replace(m.group(0), "")   # 只有 api() 里有 fetch
    assert "localStorage.setItem(\"qf:\"" in HTML  # 先画缓存


def test_no_tailscale_left_in_repo():
    root = Path(__file__).resolve().parent.parent
    bad = []
    for p in root.rglob("*"):
        if any(part in {".venv", "data", ".git", "__pycache__"} for part in p.relative_to(root).parts) or not p.is_file():
            continue
        if p.suffix not in {".py", ".md", ".toml", ".html", ".txt", ".sh", ".bat", ".json", ".csv"}:
            continue
        t = p.read_text(encoding="utf-8", errors="ignore").lower()
        if "tailscale" in t or re.search(r"\b100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+", t):
            bad.append(str(p.relative_to(root)))
    assert bad == [p for p in bad if p == "tests/test_web_ui.py"], bad


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\(.*?\n(?=(?:async )?function |const |let |/\*)", HTML, re.S)
    assert m, name
    return m.group(0)


def test_account_page_manual_mode_has_no_broker_ui():
    """账户页：自动获取持仓关闭时只画手动账本（资金/持仓/委托/手动下单），券商那一整块只在打开时出现。"""
    d = _fn("drawAccount")
    assert "auto?brokerCard(a):fundsCard(a)" in d and "auto?autoPosTable(a,pv):posTable(a,pv,true)" in d
    assert "orderCard(a)" in d and "ordersCard(a)" in d
    for w in ["连接服务器", "/api/broker/sync"]:
        assert w not in _fn("fundsCard") and w not in _fn("ordersCard") and w not in _fn("orderCard")
    assert "连接服务器" in _fn("brokerCard")
    # 顶栏只在自动获取持仓打开时显示连接状态
    assert "s.positions_auto?connPill():" in _fn("drawHeader")


def test_order_review_lists_all_fields_and_uses_trade_api():
    r = _fn("ordReview")
    for w in ["方向", "股票", "委托类型", "委托价", "数量", "金额约", "涨停", "去向"]:
        assert w in r, w
    assert "/api/trade/order" in _fn("ordSubmit")
    c = _fn("ordCancel")
    assert "/api/trade/cancel/" in c and "合同号" in c
    assert "/api/trade/quote/" in _fn("ordCode")


def test_single_trading_switch_and_polling_morphs_in_place():
    t = _fn("drawTopCtl")
    assert t.count("switch") >= 1 and "halt" not in t
    assert "/api/trading" in _fn("toggleTrading") and "confirm(" in _fn("toggleTrading")
    assert 'Object.defineProperty(m,"innerHTML"' in HTML  # 主区域赋值走原地更新
    assert "正在更新" not in HTML


def test_pool_trend_draws_two_curves_with_basis():
    pc = _fn("poolCard")
    assert pc.count("lineChart(") == 2 and "basis" in pc
    lc = _fn("lineChart")
    assert "pts.length===1" in lc  # 只有一个点时从 0 起画
