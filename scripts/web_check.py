"""牛来量化网页验收（无头浏览器）。只能对测试实例运行：它会切开关、改策略、增删手动持仓、写委托、保存配置，
并把运行模式切到实盘去测手动下单/撤单。

用法：
  python scripts/web_check.py --url http://127.0.0.1:8094 --mock http://127.0.0.1:18765 --shots /tmp/nl3/shots

测试实例要求（必须）：
  * 下单程序地址指向本机模拟服务：python -m qfpy.devtools.mock_xiadan --demo --port 18765 --password htfs
  * 用单独的配置和数据目录，绝不能指向真实的下单程序
  * 建议 QF_SIM_NOW="YYYY-MM-DD 10:00"（交易日盘中），风控的交易时段检查才会放行手动单
脚本开始时检查下单程序地址必须是 --mock 的地址，否则直接退出。/api/order、/api/cancel 只会打到模拟服务；
结束后检查模拟服务从没收到 /api/command、/api/client/restart。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

RESULTS: list[tuple[bool, str, str]] = []


def check(ok, name, detail=""):
    RESULTS.append((bool(ok), name, str(detail)))
    print(("PASS " if ok else "FAIL ") + name + (f"  — {detail}" if detail else ""), flush=True)


def getj(url):
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode())


def sendj(url, method="POST", body=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body or {}).encode(), headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


BROKER_WORDS = ["下单程序与券商数据", "连接服务器", "同步快照", "重查", "对账", "券商资金", "券商持仓", "券商委托", "券商成交", "已连接", "客户端未连接"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8094")
    ap.add_argument("--mock", required=True, help="本机模拟下单程序地址，如 http://127.0.0.1:18765")
    ap.add_argument("--shots", default="/tmp/nl3/shots")
    a = ap.parse_args()
    base = a.url.rstrip("/")
    mock = a.mock.rstrip("/")
    shots = Path(a.shots)
    shots.mkdir(parents=True, exist_ok=True)
    for f in shots.glob("*.png"):
        f.unlink()
    errors: list[str] = []

    cfg0 = getj(base + "/api/config")
    xd = (cfg0["config"].get("live") or {}).get("base_url", "").rstrip("/")
    if xd != mock or not mock.startswith(("http://127.0.0.1", "http://localhost")):
        print(f"拒绝运行：测试实例的下单程序地址是 {xd!r}，不是本机模拟服务 {mock!r}")
        sys.exit(2)
    mode0 = cfg0["config"]["execution"]["mode"]
    calls0 = len(getj(mock + "/_mock/calls")["calls"])

    with sync_playwright() as p:
        br = p.chromium.launch()
        ctx = br.new_context(viewport={"width": 1600, "height": 1000}, device_scale_factor=1)
        pg = ctx.new_page()
        pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        pg.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
        dialogs: list[str] = []

        def on_dialog(d):
            dialogs.append(d.message)
            d.accept(d.default_value or "") if d.type in ("confirm", "prompt") else d.accept()
        pg.on("dialog", on_dialog)
        reqs: list[str] = []
        pg.on("request", lambda r: reqs.append(r.method + " " + r.url.replace(base, "")))

        def goto(tab):
            pg.evaluate(f"go('{tab}')")
            pg.wait_for_timeout(600)

        def main_text():
            return pg.inner_text("#main")

        # ---------- 首页：大盘 + 股池走势曲线 ----------
        t0 = time.time()
        pg.goto(base + "/#home")
        pg.wait_for_selector("#ptitle")
        check(pg.title() == "牛来量化", "网页标题是「牛来量化」", pg.title())
        pg.wait_for_function("document.querySelectorAll('.idx svg.spark').length>=5 && document.querySelector('.breadth') && document.querySelector('.poolc')", timeout=300000)
        check(True, "首页画出指数、情绪和股池走势", f"{time.time() - t0:.1f}s")
        names = pg.eval_on_selector_all(".idx b", "els=>els.map(e=>e.textContent)")
        check(all(n in names for n in ["上证指数", "深证成指", "创业板指", "科创50", "沪深300"]), "五个指数都在", "、".join(names))
        npool = pg.locator(".poolc").count()
        pg.wait_for_function("[...document.querySelectorAll('.poolc')].every(c=>c.querySelectorAll('svg.chart polyline').length>=2)", timeout=120000)
        nline = pg.locator(".poolc svg.chart polyline").count()
        check(npool >= 3 and nline >= 2 * npool, "每个股池都有「当天分时」和「近 N 日」两条曲线", f"{npool} 个股池，{nline} 条曲线")
        pt = pg.inner_text("#hc_pool")
        check("等权" in pt and "幸存者偏差" in pt, "股池走势标明口径（等权、幸存者偏差）")
        npts = pg.evaluate("(()=>{const c=document.querySelector('.poolc .poolcharts > div:first-child polyline');return c?c.getAttribute('points').trim().split(/\\s+/).length:0})()")
        check(npts >= 30, "当天分时曲线是连续的分时点", f"{npts} 个点")
        one = pg.evaluate("(()=>{const d=document.createElement('div');d.innerHTML=lineChart([{t:'11-11',v:1.23}],{dots:true});const pl=d.querySelector('polyline');return {pts:pl?pl.getAttribute('points').trim().split(/\\s+/).length:0,txt:d.textContent}})()")
        check(one["pts"] == 2 and "起点" in one["txt"], "只有一天数据时从 0 起画，仍是一条线", one)
        pg.locator(".poolh").first.click()
        pg.wait_for_function("document.querySelector('.pooldet table tr:nth-child(2)')", timeout=60000)
        det = pg.inner_text(".pooldet")
        check("成分股" in det and "当日平均" in det and "累计" in det, "展开股池：成分股表 + 每日明细表", det.split("\n")[0][:60])
        th = pg.locator(".pooldet th.clk").filter(has_text="现价")
        th.click()
        pg.wait_for_timeout(300)
        check("↓" in pg.locator(".pooldet th.clk").filter(has_text="现价").inner_text(), "成分股表可以点表头排序")
        box = pg.evaluate("(()=>{const m=document.querySelector('main').getBoundingClientRect(),side=document.querySelector('aside').getBoundingClientRect();return {ml:m.left-side.right,mr:innerWidth-m.right,mw:m.width,sw:document.documentElement.scrollWidth,iw:innerWidth}})()")
        check(box["mw"] <= 1281 and abs(box["ml"] - box["mr"]) <= 2 and box["sw"] <= box["iw"], "宽屏：内容居中、无横向溢出", box)
        pg.screenshot(path=str(shots / "1-首页-大盘与股池走势曲线.png"), full_page=True)

        # ---------- 轮询刷新不闪：节点不被整体替换 ----------
        pg.evaluate("""(()=>{window.__mk=[...document.querySelectorAll('#main .card, #main .poolc, .idx, #main svg.chart')];window.__mk.forEach(e=>e.__keep=1);
          window.__flash=0;window.__rm=0;new MutationObserver(ms=>{for(const m of ms){for(const n of m.removedNodes)if(n.nodeType===1&&(n.classList.contains('card')||n.classList.contains('poolc')))window.__rm++;}
          if(document.querySelector('#main').textContent.includes('正在更新')||document.querySelector('#main').textContent.includes('加载中'))window.__flash++}).observe(document.querySelector('#main'),{childList:true,subtree:true,characterData:true})})()""")
        n0 = sum(1 for r in reqs if "/api/overview" in r and "pool?" not in r)
        pg.evaluate("(()=>{const o=cget('/api/overview');homePoll(SEQ,99,1)})()")  # 不等 30 秒：立刻触发一次正常轮询
        pg.wait_for_timeout(4000)
        n1 = sum(1 for r in reqs if "/api/overview" in r and "pool?" not in r)
        kept = pg.evaluate("window.__mk.filter(e=>e.isConnected&&e.__keep).length+'/'+window.__mk.length")
        k1, k2 = map(int, kept.split("/"))
        check(n1 > n0 and k1 == k2 and pg.evaluate("window.__rm") == 0 and pg.evaluate("window.__flash") == 0,
              "首页轮询刷新后卡片、曲线节点都还是原来的，没有「正在更新」闪烁", f"轮询 {n1 - n0} 次，保留节点 {kept}")
        check(pg.locator(".pooldet").count() >= 1, "轮询后展开的股池仍保持展开")
        check(pg.locator("#busy").count() == 1 and pg.locator("#main .empty:has-text('正在更新')").count() == 0, "加载状态只是顶栏一个小圆点")

        # ---------- 顶栏：只有一个合并后的自动交易开关，状态只显示一次 ----------
        check(pg.locator("#autosw").count() == 1 and pg.locator("#haltbtn, #resumebtn").count() == 0 and "一键停止" not in pg.inner_text("body"),
              "顶栏只有一个「自动交易」开关，没有单独的一键停止")
        check(pg.locator("#modepill").count() == 1, "运行模式只在顶栏显示一次")

        # ---------- 设置：切实盘无门槛；下单程序地址/口令/测试/诊断在一处 ----------
        goto("config")
        pg.wait_for_selector("#cfgbody")
        tabs = pg.eval_on_selector_all("#main .seg button", "els=>els.map(e=>e.textContent.trim())")
        check(len(tabs) == len(set(tabs)) and "下单程序" in tabs and "运行模式" in tabs, "设置页分类不重复", "、".join(tabs))
        pg.locator("#main .seg button", has_text="运行模式").click()
        pg.wait_for_selector("input[name=xmode][value=live]")
        nd = len(dialogs)
        pg.check("input[name=xmode][value=live]")
        pg.locator("#main button", has_text="保存并生效").click()
        pg.wait_for_function("document.querySelector('#modepill') && document.querySelector('#modepill').textContent.includes('实盘')", timeout=20000)
        st = getj(base + "/api/status")
        check(st["mode"] == "live" and len(dialogs) == nd and pg.locator(".modal-bg").count() == 0 and "确认切换实盘" not in pg.inner_text("body"),
              "切实盘没有确认框和门槛，保存即生效", st["mode_label"])
        pg.locator("#main .seg button", has_text="下单程序").click()
        pg.wait_for_selector("#xdcard")
        card = pg.inner_text("#xdcard")
        check(all(w in card for w in ["地址", "口令", "测试连接", "连接诊断"]), "下单程序的地址、口令、测试连接、连接诊断在同一张卡片")
        cfg_text = Path(getj(base + "/api/config")["path"]).read_text(encoding="utf-8")
        pg.click("#xdtest")
        pg.wait_for_function("document.querySelector('#tx_live').textContent.trim().length>0", timeout=40000)
        tx = pg.inner_text("#tx_live")
        check("✓" in tx and "✗" not in tx, "测试连接通过（状态 + 只读快照验证口令）", tx.replace("\n", " ")[:160])
        pw = pg.locator("#xdcard input").nth(1)
        pw.fill("wrong-pass")
        pw.dispatch_event("change")
        pg.click("#xdtest")
        pg.wait_for_function("document.querySelector('#tx_live').textContent.includes('✗')", timeout=40000)
        tx2 = pg.inner_text("#tx_live")
        check("403" in tx2 and "一致" in tx2, "口令不对时提示 403、口令要和下单程序一致", tx2.replace("\n", " ")[:160])
        check(Path(getj(base + "/api/config")["path"]).read_text(encoding="utf-8") == cfg_text, "测试连接没有写配置文件")
        pg.wait_for_function("document.querySelector('#diagconn')", timeout=20000)
        check(pg.locator("#diagconn").count() == 1, "连接诊断显示当前连接状态", pg.inner_text("#diagconn"))
        pg.screenshot(path=str(shots / "5-设置-下单程序连接与诊断.png"), full_page=True)
        # 口令输入框全站只有这一处；地址也是
        pg.evaluate("CFGX.dirty=false")
        goto("home")
        goto("config")
        pg.wait_for_selector("#cfgbody")
        seen = {}
        for tname in tabs:
            pg.locator("#main .seg button", has_text=tname).first.click()
            pg.wait_for_timeout(250)
            t = pg.inner_text("#cfgbody")
            for w in ["口令（X-Auth）", "测试连接下单程序", "自动获取持仓", "一键停止", "自动交易开关"]:
                if w in t:
                    seen.setdefault(w, []).append(tname)
        dup = {k: v for k, v in seen.items() if len(v) > 1}
        check(not dup and "一键停止" not in seen and "自动获取持仓" not in [w for w, v in seen.items() if "运行模式" in v],
              "设置去重：口令只在「下单程序」一处，设置页里没有开关的重复入口", json.dumps(seen, ensure_ascii=False))

        # ---------- 账户页（持仓 + 实盘合并），自动获取持仓关闭 ----------
        if getj(base + "/api/positions_mode").get("auto"):
            sendj(base + "/api/positions_mode", body={"auto": False})
        navs = pg.eval_on_selector_all("#nav button", "els=>els.map(e=>e.textContent.trim())")
        check(any("账户" in n for n in navs) and not any(n.strip() in ("实盘", "持仓") for n in navs), "导航里持仓和实盘合并成「账户」", "、".join(navs))
        pg.goto(base + "/#broker")
        pg.wait_for_selector("#pmcard", timeout=20000)
        check(pg.inner_text("#ptitle") == "账户", "旧的 #broker 链接自动转到账户页")
        mock_before = len(getj(mock + "/_mock/calls")["calls"])
        pg.wait_for_selector("#fundcard")
        t = main_text()
        hits = [w for w in BROKER_WORDS if w in t]
        check(not hits and pg.locator("#autopos, #bkconnect, #bkrefresh").count() == 0 and "on" not in (pg.get_attribute("#possw", "class") or ""),
              "自动获取持仓关闭：页面没有任何下单程序/券商数据（连接状态、同步按钮、对账、券商资金和持仓）", "出现：" + "、".join(hits) if hits else "")
        hdr = pg.inner_text("#hdr")
        check("已连接" not in hdr and "未连接" not in hdr and pg.locator("#navdot").count() == 0, "关闭时顶栏也不显示下单程序连接状态", hdr.replace("\n", " "))
        bk = getj(base + "/api/broker")
        ac = getj(base + "/api/account")
        check(bk.get("snapshot") is None and bk.get("report") is None and not ac.get("broker"), "关闭时接口也不返回券商快照和对账报告")
        try:
            sendj(base + "/api/broker/sync", body={"how": "fresh"})
            refused = False
        except urllib.error.HTTPError as e:
            refused = e.code in (400, 409)
        check(refused, "关闭时「同步」类接口直接拒绝")

        # 资金
        pg.fill("#fd_avail", "80000")
        pg.fill("#fd_bal", "81000")
        pg.click("#fd_save")
        pg.wait_for_function("document.querySelector('#k_avail .v').textContent.includes('80,000')", timeout=15000)
        fu = getj(base + "/api/account")
        check(abs((fu.get("funds") or {}).get("available", 0) - 80000) < 0.01 and abs(((fu.get("funds") or {}).get("balance") or 0) - 81000) < 0.01,
              "手动填可用资金和资金余额，保存后显示", pg.inner_text("#k_avail").replace("\n", " "))
        # 持仓：增、改
        pg.fill("#mn_code", "600036")
        pg.fill("#mn_name", "招商银行")
        pg.fill("#mn_qty", "500")
        pg.fill("#mn_cost", "39.8")
        pg.fill("#mn_sell", "500")
        pg.click("#mn_save")
        pg.wait_for_selector("#lp_600036", timeout=15000)
        pg.click("#pe_600036")
        pg.fill("#mn_qty", "600")
        pg.fill("#mn_sell", "600")
        pg.click("#mn_save")
        pg.wait_for_function("document.querySelector('#lp_600036') && document.querySelector('#lp_600036').textContent.includes('600')", timeout=15000)
        q = next((x for x in getj(base + "/api/account")["positions"] if x["symbol"] == "600036"), {})
        check(q.get("quantity") == 600, "手动持仓可以添加和修改", f"600036 数量 {q.get('quantity')}")

        # 手动下单：输入代码后显示名称/现价/涨跌停/可卖
        pg.click("#ordside button:has-text('卖出')")
        pg.fill("#ord_code", "600036")
        pg.wait_for_function("document.querySelector('#q_up')", timeout=30000)
        qt = pg.inner_text("#ordquote")
        check("招商银行" in qt and all(w in qt for w in ["现价", "涨停价", "跌停价", "可卖"]) and "600" in pg.inner_text("#q_sell"),
              "输入代码后显示名称、现价、涨跌停价、可卖数量", qt.replace("\n", " ")[:140])
        pg.screenshot(path=str(shots / "2-账户-手动账本与下单.png"), full_page=True)
        # 风控拒绝：卖出超过可卖
        pg.fill("#ord_qty", "900")
        pg.click("#ordsubmit")
        pg.wait_for_selector("#ordconfirm")
        pg.locator("#ordconfirm button[data-act=ok]").click()
        pg.wait_for_function("document.querySelector('#ordres') && document.querySelector('#ordres').textContent.length>3", timeout=30000)
        rj = pg.inner_text("#ordres")
        check(("拦截" in rj or "拒" in rj) and "可卖" in rj, "卖出超过可卖被风控拒绝并说明原因", rj.replace("\n", " ")[:140])
        # 买入：确认框列出全部要素 → 只发到模拟下单程序
        pg.click("#ordside button:has-text('买入')")
        pg.click("#ordptype button:has-text('限价')")
        price = pg.evaluate("ORD.q.price")
        pg.fill("#ord_price", f"{price:.2f}")
        pg.fill("#ord_qty", "100")
        pg.click("#ordsubmit")
        pg.wait_for_selector("#ordconfirm")
        mt = pg.inner_text("#ordconfirm")
        check(all(w in mt for w in ["方向", "买入", "600036", "招商银行", "委托类型", "限价", "委托价", "数量", "100 股", "金额", "涨停", "可用资金", "去向", "下单程序"]),
              "提交前的确认框列出全部要素（方向、股票、类型、价格、数量、金额、涨跌停、资金、去向）", mt.replace("\n", " ")[:200])
        pg.screenshot(path=str(shots / "3-账户-下单确认框.png"))
        pg.locator("#ordconfirm button[data-act=ok]").click()
        pg.wait_for_function("document.querySelector('#ordlist button[id^=cx_]')", timeout=40000)
        mc = getj(mock + "/_mock/calls")
        tc = mc["trade_calls"]
        check(tc and tc[-1]["path"] == "/api/order" and tc[-1]["body"]["symbol"] == "600036" and str(tc[-1]["body"]["quantity"]) == "100",
              "手动买单只发到了本机模拟下单程序", json.dumps(tc[-1]["body"] if tc else {}, ensure_ascii=False)[:160])
        olist = pg.inner_text("#ordlist")
        check("已报" in olist and "手动" in olist and "合同" in olist, "委托记录自动记下这笔手动单（状态、来源、合同号）")
        # 撤单
        cx = pg.locator("#ordlist button[id^=cx_]").first
        oid = cx.get_attribute("id")[3:]
        cx.click()
        pg.wait_for_selector("#cxconfirm")
        check("合同号" in pg.inner_text("#cxconfirm"), "撤单前确认框列出合同号")
        pg.locator("#cxconfirm button[data-act=ok]").click()
        pg.wait_for_function(f"!document.querySelector('#cx_{oid}')", timeout=40000)
        o = next((x for x in getj(base + "/api/orders")["orders"] if x["id"] == oid), {})
        tc = getj(mock + "/_mock/calls")["trade_calls"]
        check(o.get("status") == "cancelled" and tc[-1]["path"] == "/api/cancel" and tc[-1]["body"].get("entrust_no") == o.get("entrust_no"),
              "撤单按合同号发到模拟下单程序，委托变成已撤", f"{o.get('status_label')} 合同 {o.get('entrust_no')}")
        # 手动补记一笔成交 → 持仓和可用资金自动更新
        cash0 = getj(base + "/api/account")["summary"]["cash"]
        pg.click("#ordadd")
        pg.wait_for_selector("#ordedit")
        f = lambda k, v: pg.fill(f"#ordedit [data-k={k}]", v)
        f("symbol", "000001")
        f("name", "平安银行")
        f("price", "11.00")
        f("quantity", "200")
        pg.select_option("#ordedit [data-k=status]", "filled")
        f("filled_qty", "200")
        f("fill_price", "11.00")
        pg.locator("#ordedit button[data-act=ok]").click()
        pg.wait_for_selector("#lp_000001", timeout=15000)
        acc = getj(base + "/api/account")
        p1 = next((x for x in acc["positions"] if x["symbol"] == "000001"), {})
        check(p1.get("quantity") == 200 and acc["summary"]["cash"] < cash0 - 2199, "补记一笔成交后，持仓和可用资金自动更新",
              f"000001 {p1.get('quantity')} 股，可用 {cash0:.2f} → {acc['summary']['cash']:.2f}")
        # 修改委托
        rec = next(x for x in getj(base + "/api/orders")["orders"] if x["symbol"] == "000001" and x["origin"] == "手动补记")
        pg.locator(f"#jo_{rec['id']} button", has_text="修改").click()
        pg.wait_for_selector("#ordedit")
        f("strategy", "同花顺手工单")
        pg.locator("#ordedit button[data-act=ok]").click()
        pg.wait_for_timeout(1200)
        rec2 = next(x for x in getj(base + "/api/orders")["orders"] if x["id"] == rec["id"])
        check(rec2.get("strategy") == "同花顺手工单" and next(x for x in getj(base + "/api/account")["positions"] if x["symbol"] == "000001")["quantity"] == 200,
              "委托可以修改，改备注不会重复计入成交")
        # 账户页轮询不替换节点
        pg.evaluate("""(()=>{window.__mk=[...document.querySelectorAll('#main .card, #main table, #main tr')];window.__mk.forEach(e=>e.__keep=1);window.__inp=document.querySelector('#ord_code');window.__inp.value='600036'})()""")
        n0 = sum(1 for r in reqs if r.endswith("/api/account"))
        pg.wait_for_timeout(16500)
        n1 = sum(1 for r in reqs if r.endswith("/api/account"))
        kept = pg.evaluate("window.__mk.filter(e=>e.isConnected&&e.__keep).length+'/'+window.__mk.length")
        k1, k2 = map(int, kept.split("/"))
        check(n1 > n0 and k2 > 10 and k1 >= k2 - 2 and pg.evaluate("window.__inp.isConnected"),
              "账户页 15 秒轮询后，卡片、表格、行和输入框都还是原来的节点", f"轮询 {n1 - n0} 次，保留 {kept}")
        calls_mid = getj(mock + "/_mock/calls")["calls"][mock_before:]
        reads = [c["path"] + ("?" + c["query"] if c["query"] else "") for c in calls_mid if c["path"] in ("/api/snapshot", "/api/positions", "/api/balance")]
        only_orders = all("only=orders" in r for r in reads)
        check(only_orders, "关闭时整个过程没读过券商持仓和资金（下单/撤单后只读委托表）", "、".join(reads) or "无")
        pg.screenshot(path=str(shots / "4-账户-委托记录与撤单.png"), full_page=True)

        # ---------- 自动获取持仓打开：只有这一块券商数据 ----------
        pg.click("#possw")
        pg.wait_for_selector("#autopos", timeout=20000)
        pg.wait_for_function("document.querySelector('#autopos') && document.querySelector('#autopos').textContent.includes('数据时间')", timeout=60000)
        btns = pg.eval_on_selector_all("#autopos button", "els=>els.map(e=>e.textContent.trim())")
        check(btns == ["连接服务器", "刷新"] and pg.locator("#fundcard, #posform").count() == 0, "打开后只有「连接服务器」「刷新」，手填资金和持仓表单隐藏", "、".join(btns))
        check(pg.locator("#accPos tr[id^=bp_]").count() >= 1 and "数据时间" in pg.inner_text("#autopos"), "打开后显示最新的券商持仓并标明数据时间", pg.locator("#accPos tr[id^=bp_]").count())
        check(pg.locator("#hdr .pill").filter(has_text="连接").count() + pg.locator("#hdr .pill").filter(has_text="已连接").count() >= 1, "打开后连接状态只在顶栏显示", pg.inner_text("#hdr").replace("\n", " "))
        pg.screenshot(path=str(shots / "6-账户-自动获取持仓.png"), full_page=True)
        pg.click("#possw")
        pg.wait_for_selector("#fundcard", timeout=20000)
        t = main_text()
        check(not [w for w in BROKER_WORDS if w in t] and pg.locator("#accPos tr[id^=bp_]").count() == 0, "再关掉：券商数据立即消失，回到手动账本")

        # ---------- 合并后的自动交易开关 ----------
        st0 = getj(base + "/api/status")["trading_on"]
        if not st0:
            nd = len(dialogs)
            pg.click("#autosw")
            pg.wait_for_function("document.querySelector('#tradesw').classList.contains('on')", timeout=15000)
            check(getj(base + "/api/status")["trading_on"] and len(dialogs) == nd + 1 and "实盘" in dialogs[-1], "实盘下打开自动交易弹一句确认，打开生效", dialogs[-1][:40] if dialogs else "")
        # 放一笔「系统自动」未成交委托 + 一笔手动未成交委托，关闭开关只撤自动的
        # 造一笔真正的「系统自动」委托：临时把买入漏斗换成只排除 ST，只跑自选股、允许交易，跑完立即恢复原漏斗
        auto_id = None
        fb0 = getj(base + "/api/funnel/buy")
        ids0 = {x["id"] for x in getj(base + "/api/orders")["orders"]}
        try:
            sendj(base + "/api/funnel/buy", method="PUT", body={"layers": [{"id": "wc", "name": "验收临时层", "mode": "all", "enabled": True, "factors": [{"kind": "exclude_st"}]}]})
            sendj(base + "/api/run", body={"pools": ["watchlist:all"], "trade": True})   # 买入或卖出（止盈止损）都算自动委托
            ao = next((o for o in getj(base + "/api/orders")["orders"] if o["id"] not in ids0 and o.get("origin") == "系统" and o.get("open")), None)
            auto_id = ao and ao["id"]
        finally:
            sendj(base + "/api/funnel/buy", method="PUT", body={"layers": fb0["layers"]})
        jo = next((x for x in getj(base + "/api/orders")["orders"] if x["id"] == auto_id), {}) if auto_id else {}
        check(auto_id and jo.get("origin") == "系统" and jo.get("open"), "准备：策略自动发出一笔未成交委托（来源「系统」）",
              f"{'买入' if jo.get('side') == 'buy' else '卖出'} {jo.get('symbol')} {jo.get('status_label')}" if jo else "没生成自动委托")
        check(getj(base + "/api/funnel/buy")["layers"] == fb0["layers"], "临时漏斗已恢复原样")
        q2 = getj(base + "/api/trade/quote/601988")  # 换一只票：风控「同票每日一次」会拦第二笔 600036 买单
        man = sendj(base + "/api/trade/order", body={"symbol": "601988", "side": "buy", "price_type": "limit", "price": round(q2["price"] * 0.99, 2), "quantity": 100})
        man_id = man["order"]["id"]
        check(man["order"]["open"], "准备：挂一笔未成交的手动买单", man["order"].get("status_label"))
        nd = len(dialogs)
        pg.click("#autosw")
        pg.wait_for_function("!document.querySelector('#tradesw').classList.contains('on')", timeout=40000)
        st = getj(base + "/api/status")
        mo = next(x for x in getj(base + "/api/orders")["orders"] if x["id"] == man_id)
        check(not st["trading_on"] and len(dialogs) == nd + 1 and "撤" in dialogs[-1], "关闭自动交易：简单确认一句，关闭生效", dialogs[-1][:50] if dialogs else "")
        sendj(base + "/api/orders/refresh")
        ords = {x["id"]: x for x in getj(base + "/api/orders")["orders"]}
        mo = ords[man_id]
        ao = ords.get(auto_id) or {}
        canc = [c for c in getj(mock + "/_mock/calls")["trade_calls"] if c["path"] == "/api/cancel"]
        last_c = canc[-1]["body"].get("entrust_no") if canc else None
        check(ao.get("status") == "cancelled" and last_c == ao.get("entrust_no") and last_c != mo.get("entrust_no"),
              "关闭自动交易时撤掉了当天未成交的自动委托（按合同号撤）", f"自动单 {ao.get('status_label')}，撤单合同号 {last_c}")
        check(mo["open"] and mo["status"] != "cancelled", "手动单保留不撤", f"手动单 {mo['status_label']}")
        sendj(base + f"/api/trade/cancel/{man_id}")

        # ---------- 其它页面仍正常、独立成页 ----------
        goto("funnel")
        pg.wait_for_timeout(1500)
        check("漏斗" in pg.inner_text("#ptitle"), "今日漏斗独立成页")
        goto("buy")
        pg.wait_for_selector("#sidettl")
        check(pg.inner_text("#ptitle") == "买入策略" and pg.locator("#sideseg").count() == 0, "买入策略单独一页")
        goto("sell")
        pg.wait_for_selector("#sellMatrix", timeout=30000)
        check(pg.inner_text("#ptitle") == "卖出策略", "卖出策略单独一页")
        for tab in ["home", "account", "funnel", "buy", "sell", "config"]:
            goto(tab)
            pg.wait_for_timeout(700)
        allbody = pg.inner_text("body")
        check(pg.locator("#autosw").count() == 1 and pg.locator("#modepill").count() == 1, "切遍所有页面后顶栏仍只有一个开关、一个模式标识")

        # ---------- 窄屏 ----------
        m = br.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True).new_page()
        m.on("pageerror", lambda e: errors.append(f"pageerror(窄屏): {e}"))
        m.goto(base + "/#home")
        m.wait_for_function("document.querySelectorAll('.idx').length>=5 && document.querySelector('.poolc svg.chart')", timeout=60000)
        nb = m.evaluate("(()=>{const c=document.querySelector('#main .card').getBoundingClientRect();return {l:c.left,r:innerWidth-c.right,sw:document.documentElement.scrollWidth,iw:innerWidth}})()")
        check(nb["sw"] <= nb["iw"] and abs(nb["l"] - nb["r"]) <= 2, "窄屏 390px：首页无横向溢出", nb)
        m.wait_for_timeout(800)
        m.screenshot(path=str(shots / "7-窄屏-首页.png"), full_page=True)
        m.goto(base + "/#account")
        m.wait_for_selector("#ordcard", timeout=30000)
        nb2 = m.evaluate("({sw:document.documentElement.scrollWidth,iw:innerWidth})")
        check(nb2["sw"] <= nb2["iw"], "窄屏 390px：账户页无横向溢出", nb2)
        m.wait_for_timeout(600)
        m.screenshot(path=str(shots / "8-窄屏-账户.png"), full_page=True)
        br.close()

    # 恢复测试实例原来的运行模式
    if mode0 != "live":
        c = getj(base + "/api/config")["config"]
        c["execution"]["mode"] = mode0
        sendj(base + "/api/config", method="PUT", body=c)

    check(not errors, "页面没有 JS 报错", "; ".join(errors[:5]))
    mc = getj(mock + "/_mock/calls")
    paths = sorted({c["path"] for c in mc["calls"][calls0:]})
    bad = [p for p in paths if p in ("/api/command", "/api/client/restart", "/api/connect")]
    check(not bad and not mc["forbidden_hits"], "模拟下单程序从没收到 /api/command、/api/client/restart", "收到：" + "、".join(paths))
    ok = sum(1 for r in RESULTS if r[0])
    print(f"\n合计 {ok}/{len(RESULTS)} 通过")
    (shots / "result.json").write_text(json.dumps(RESULTS, ensure_ascii=False, indent=1), encoding="utf-8")
    sys.exit(0 if ok == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
