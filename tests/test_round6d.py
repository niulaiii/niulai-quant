"""第六轮小修（二）：选股结果 / 卖出结果表格错位、旧记录「新入池」不当 0、股池中文名。
逐列对齐的像素级检查在验收脚本 align_check.py 里（playwright，1920/1600/1366 宽），这里查源码结构和后端。"""
import re

from qfpy import sourcesvc, viewsvc  # noqa: E402

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_round6c import HTML, _Eng, _run, fn_body  # noqa: E402


def _css_rules(sel: str) -> list[str]:
    return re.findall(r"(?<![\w-])" + re.escape(sel) + r"\{([^}]*)\}", HTML)


def test_result_rows_do_not_collide_with_card_classes():
    """错位的原因：全部结果的行用了 class="frow"，和 .frow{display:flex}（表单行）、.frow{border-radius}（因子卡片）撞名。"""
    body = fn_body("allResults")
    assert 'class="frow"' not in body and 'tr class="fres"' in body
    assert any("display:flex" in r for r in _css_rules(".frow"))     # 表单行仍是 flex，所以表格行不能再用这个名字
    assert not _css_rules(".fres")                                   # 表格行没有任何会把它变成卡片的样式
    fo = fn_body("fOpen")
    assert "#falltbl tr.fres" in fo and ".frow" not in fo
    assert 'colspan="8"' in fo                                        # 详情占满整行（8 列）


def test_result_tables_have_thead_tbody_colgroup_and_same_column_count():
    for tid in ("fsumtbl", "falltbl", "sxtbl"):
        m = re.search(r'<table class="[^"]*\brtab\b[^"]*" id="' + tid + r'">(.*?)</table>', HTML, re.S)
        assert m, tid
        t = m.group(1)
        assert "<thead>" in t and "<tbody>" in t and "<colgroup>" in t, tid
        n_col = t.count("<col ")
        head = re.search(r"<thead><tr>(.*?)</tr></thead>", t).group(1)
        n_th = head.count("<th")
        row = re.search(r"<tbody>\$\{\w+\.map\(.*?<tr [^>]*>(.*?)</tr>`\)", t, re.S).group(1)
        n_td = len(re.findall(r"<td[ >]", row))
        assert n_col == n_th == n_td, (tid, n_col, n_th, n_td)
    # 结果表：固定列宽、铺满宽度、表头不用 sticky（sticky + border-collapse 在有些浏览器里表头会错开）
    base = _css_rules("table.rtab")[0]
    assert "width:100%" in base and "table-layout:fixed" in base
    assert any("position:static" in r for r in _css_rules("table.rtab>thead>tr>th"))
    # 手机宽度换成卡片式，桌面仍是表格
    assert re.search(r"@media \(max-width:760px\)\{\s*table\.rtab", HTML)


def test_summary_new_entries_none_for_old_records(tmp_path):
    old = _run("20261110-093500", "2026-11-10", "600001")
    old.pop("pool_runs")                                   # 第六轮之前的旧记录：没有按股池的 pool_runs
    new = _run("20261112-093500", "2026-11-12", "600002")
    # 全是旧记录 → None（网页显示「—」），不是 0
    sm = {x["pool"]: x for x in viewsvc.all_results(_Eng(tmp_path, [old], {}))["summary"]}
    assert sm["p1"]["new"] is None and sm["p1"]["new_missing"] == 1 and sm["p1"]["entered"] == 1
    # 新旧混合 → 只算新记录，另记几次旧记录没有数据
    sm = {x["pool"]: x for x in viewsvc.all_results(_Eng(tmp_path, [new, old], {}))["summary"]}
    assert sm["p1"]["new"] == 1 and sm["p1"]["new_missing"] == 1 and sm["p1"]["runs"] == 2
    # 网页：None 显示「—」并说明是旧记录
    body = fn_body("allResults")
    assert "x.new==null" in body and "旧记录" in body


class _NEng:
    def __init__(self, cfg, runs=()):
        self.cfg, self._runs = cfg, list(runs)

    def _run_index(self):
        return self._runs

    def get_run(self, rid):
        return None


def test_pool_names_cover_schedule_and_new_style_sources():
    cfg = {"pool_sources": {"gushi": {"type": "gushi"}, "watchlist": {"type": "watchlist"}}, "market_sources": {},
           "pool": {"pools": ["gushi:auction_long", "watchlist:all"], "aliases": {"watchlist:all": "我的自选"}},
           "schedule": {"runs": [{"pools": ["gushi:half_past_ten"]}, {"pools": ["gushi:gold_230"]}]}}
    eng = _NEng(cfg, [{"id": "x", "pools": [{"key": "other:p9", "count": 3}]}])
    n = sourcesvc.pool_names(eng)
    assert n["gushi:gold_230"]["name"] == "黄金两点半"           # 只写在定时时段里的股池也有中文名
    assert n["gushi:half_past_ten"]["name"] == "十点半"
    assert n["gushi:auction_long"]["name"] == "竞价多头策略"
    assert n["watchlist:all"]["name"] == "我的自选"                  # 别名优先
    assert n["other:p9"]["name"] == "other:p9" and n["other:p9"]["auto"] == ""   # 拿不到中文名：显示代码
    # 网页：拿不到中文名时提示去 设置 → 股池列表与别名
    assert "设置 → 股池列表与别名" in HTML and "go('config/pools')" in fn_body("pnameH")
    assert "pnameH(" in fn_body("allResults") and "pnameH(" in fn_body("sellResults")
