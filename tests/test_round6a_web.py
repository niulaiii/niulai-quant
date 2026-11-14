"""第六轮网页（一）：宽屏布局、首页内嵌股池走势、选股结果和分级股池合并并按股池点选、报错只显示一次。
只检查网页源码结构；真实浏览器里的点选流程由验收脚本测。"""
from qfpy.web.uisource import read_ui_source
import re
from pathlib import Path

HTML = read_ui_source()


def fn_body(name: str) -> str:
    m = re.search(r"(?:async )?function " + re.escape(name) + r"\(.*?(?=\n(?:async )?function |\nconst [A-Z_]+=|\n/\* -{3})", HTML, re.S)
    assert m, f"没找到 {name}()"
    return m.group(0)


def _js_strings_and_templates_only():
    """去掉注释后的脚本，用来查页面上会显示的文字。"""
    return re.sub(r"/\*.*?\*/", "", HTML, flags=re.S)


def test_wide_layout_no_1280_cap():
    # 最后生效的规则去掉了 main 的限宽和居中
    assert "main{max-width:none!important;margin:0!important" in HTML
    # 表单：标签列和输入列对齐，说明在输入框下方
    assert ".kv{grid-template-columns:minmax(150px,230px) minmax(0,1fr)" in HTML
    kv = fn_body("kv")
    assert "docBelow(" in kv and 'class="kl"' in kv and 'class="kc"' in kv
    # 手机宽度：表单变成一列
    assert re.search(r"@media \(max-width:860px\)\{main\{padding:12px\}.*?\.kv\{grid-template-columns:1fr", HTML)


def test_settings_doc_not_glued_with_semicolon():
    assert '?"；":""' not in HTML and "什么时候生效：" not in HTML
    sd = fn_body("sdLines")
    assert "生效：" in sd and "留空：" in sd
    clean = fn_body("sdClean")
    assert "。；;，,、" in clean   # 句尾多余的标点去掉，只留一个句号


def test_home_embeds_pool_trend_below_market():
    d = fn_body("drawHome")
    i, s, p = d.index('id="hc_idx"'), d.index('id="hc_stat"'), d.index('id="hc_pool"')
    assert i < s < p                                    # 大盘指数 → 市场情绪 → 股池走势
    assert 'id="homePT"' in d and "poolTrend(" in d
    bar = fn_body("ptBar")
    assert "pname(p.key,p.name)" in bar and "ptPick(" in bar   # 用股池中文名做标签切换
    pt = fn_body("poolTrend")
    assert "ptleft" in pt and "ptright" in pt              # 左边列表、右边详情
    assert "SUBTABS={buy:" in HTML                          # 首页没有子标签了


def test_legacy_hashes_go_to_new_places():
    m = re.search(r"const LEGACY=\{(.*?)\};", HTML, re.S)
    body = m.group(1)
    for k, v in [("tiers", "buy/results"), ('"buy/tiers"', "buy/results"), ('"home/pools"', "home"),
                 ("pools", "home"), ('"home/overview"', "home"), ("funnel", "buy/results")]:
        assert f'{k}:"{v}"' in body, k


def test_buy_results_merged_and_pool_chips():
    assert '["tiers","分级股池"]' not in HTML
    f = fn_body("funnel")
    assert "loadPNames()" in f and "poolChips(" in f
    chips = fn_body("poolChips")
    assert "全部股池" in chips and "fpToggle(" in chips and "pname(k)" in chips
    tog = fn_body("fpToggle")
    assert "FPOOLS.clear()" in tog and "FPOOLS.add(k)" in tog and "FPOOLS.delete(k)" in tog   # 全部 / 单选 / 多选
    rd = fn_body("runDetail")
    for k in ("x.members", "x.new", "x.picks", "poolProblems(r)", "fpOn(x.pool)"):
        assert k in rd, k
    # 第六轮小修：单独的「分级跟踪」块去掉，级别变成全部结果表里的一列
    assert 'id="ftiers"' not in HTML and "fillTiers" not in HTML
    assert "前池" not in HTML and "合并去重" not in HTML
    # 「按每只票看」按选中的股池筛
    assert "pools=" in fn_body("runStocks") or "pools" in fn_body("allResults")


def test_problems_shown_once_in_chinese_with_details():
    pp = fn_body("poolProblems")
    assert "<details" in pp and "详情" in pp
    assert "seen" in pp and "!ps.some(" in pp           # 已经按股池显示过的报错，不在警告里再显示一次
    z = fn_body("zhErr")
    assert "UNEXPECTED_EOF" in z and "代理" in z


def test_visible_text_chinese_for_mode_and_exit_problem():
    assert 'MODE_N={paper:"模拟盘"' in HTML
    rd = fn_body("runDetail")
    assert "modeTxt(r)" in rd and "MODE_N[r.mode]" in fn_body("modeTxt")


def test_no_new_order_paths_in_web():
    # 网页里不出现对券商接口的直接调用
    for bad in ("/api/order\"", "/api/cancel\"", "/api/command"):
        assert bad not in _js_strings_and_templates_only()
