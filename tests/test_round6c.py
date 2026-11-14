"""第六轮小修：选股结果默认全部结果、旧层名替换、运行方式映射、分级并进结果表、
立即检查卖出只在卖出结果页、卖出结果一张表、首页后台自动刷新。网页只查源码结构，点选流程由验收脚本测。"""
from qfpy.web.uisource import read_ui_source
import json
import re
from pathlib import Path

from qfpy import viewsvc

ROOT = Path(__file__).resolve().parent.parent
HTML = read_ui_source()


def fn_body(name: str) -> str:
    m = re.search(r"(?:async )?function " + re.escape(name) + r"\(.*?(?=\n(?:async )?function |\nconst [A-Z_]+=|\nlet [A-Z_]+=|\n/\* -{3}|\ndocument\.)", HTML, re.S)
    assert m, f"没找到 {name}()"
    return m.group(0)


# ---------------- 旧层名、运行方式 ----------------
def test_old_stage_label_replaced_everywhere():
    rec = {"mode": "paper", "stages": [{"index": 0, "type": "pool", "label": "前池（股池合并去重）",
                                         "kept": [{"code": "1", "sources": ["a", "b"]}, {"code": "2", "sources": ["a"]}]},
                                        {"index": 1, "type": "layer", "label": "硬性过滤", "kept": []}]}
    r = viewsvc.normalize_run(rec)
    assert r["stages"][0]["label"] == "进入策略（全部股池）"
    assert r["stages"][0]["by_pool"] == {"a": 2, "b": 1}          # 每个池各贡献几只
    assert rec["stages"][0]["label"].startswith("前池")            # 原记录不改
    assert viewsvc.zh_stage_label("硬性过滤") == "硬性过滤"
    # 网页、引擎都不再写旧叫法
    assert "前池" not in HTML and "合并去重" not in HTML
    assert "前池" not in (ROOT / "qfpy" / "engine.py").read_text(encoding="utf-8")


def test_run_mode_mapping():
    lab = lambda rec: viewsvc.normalize_run(rec)["mode_label"]
    assert lab({"mode": "paper"}) == "模拟盘"
    assert lab({"mode": "dry_run"}) == "演练"
    assert lab({"mode": "live"}) == "实盘"
    assert lab({"mode": "PAPER "}) == "模拟盘"
    assert lab({"mode": "模拟盘"}) == "模拟盘"
    # 顶层没有 mode 的旧记录：看委托上记的 mode；都没有就写「未记录」，绝不当成实盘
    assert lab({"orders": [{"mode": "paper"}]}) == "模拟盘"
    assert lab({}) == "未记录"
    assert lab({"mode": "all"}) == "未记录"
    # 网页：用后端给的 mode_label；记录时和现在不一样时两个都写
    mt = fn_body("modeTxt")
    assert "r.mode_label" in mt and "未记录" in mt and "现在是" in mt
    assert "modeTxt(r)" in fn_body("runDetail")


# ---------------- 选股结果：全部结果 ----------------
class _Tiers:
    def __init__(self, d, data):
        self.dir, self.data = d, data

    def load(self, day):
        return self.data.get(day, {})


class _Eng:
    def __init__(self, tmp, runs, tiers):
        self.runs = runs
        self.tiers = _Tiers(tmp, tiers)
        for day, v in tiers.items():
            (tmp / f"{day}.json").write_text(json.dumps(v), encoding="utf-8")

    def _run_index(self):
        return [{"id": r["id"], "trading_date": r["trading_date"]} for r in self.runs]

    def get_run(self, rid):
        return viewsvc.normalize_run(next(r for r in self.runs if r["id"] == rid))


def _run(rid, day, picked):
    kept0 = [{"code": "600001", "name": "甲", "sources": ["p1"]}, {"code": "600002", "name": "乙", "sources": ["p2"]}]
    return {"id": rid, "trading_date": day, "started_at": f"{day} 09:35:00", "origin": "网页手动", "mode": "paper",
            "picks": [{"code": picked}], "pool_runs": [{"pool": "p1", "new": 1}, {"pool": "p2", "new": 1}],
            "stages": [{"index": 0, "type": "pool", "label": "前池（股池合并去重）", "kept": kept0},
                       {"index": 1, "type": "layer", "label": "硬性过滤",
                        "kept": [k for k in kept0 if k["code"] == picked],
                        "dropped": [dict(k, note="不满足") for k in kept0 if k["code"] != picked]}]}


def test_all_results_merges_runs_filters_and_tiers(tmp_path):
    runs = [_run("20261112-093500", "2026-11-12", "600002"), _run("20261111-093500", "2026-11-11", "600001")]
    tiers = {"2026-11-11": {"600001": {"tier": "focus", "name": "甲"}},
             "2026-11-12": {"600001": {"tier": "candidate", "name": "甲"}, "600002": {"tier": "buy", "name": "乙"}}}
    eng = _Eng(tmp_path, runs, tiers)
    r = viewsvc.all_results(eng)
    assert r["total"] == 4 and r["latest_run"] == "20261112-093500"
    assert [x["trading_date"] for x in r["rows"]][:1] == ["2026-11-12"]          # 新的在上面
    row = next(x for x in r["rows"] if x["run_id"] == "20261112-093500" and x["code"] == "600002")
    assert row["tier_name"] == "买入" and row["time"] == "09:35" and row["mode_label"] == "模拟盘"
    assert all("前池" not in str(x["reached"]) for x in r["rows"])
    # 股池筛选、按日期、按某一次运行、只看重点及以上、分页
    assert {x["code"] for x in viewsvc.all_results(eng, pools=["p1"])["rows"]} == {"600001"}
    assert viewsvc.all_results(eng, day="2026-11-11")["total"] == 2
    assert viewsvc.all_results(eng, run="20261111-093500")["total"] == 2
    focus = viewsvc.all_results(eng, min_tier="focus")["rows"]
    assert {(x["trading_date"], x["code"]) for x in focus} == {("2026-11-11", "600001"), ("2026-11-12", "600002")}
    pg = viewsvc.all_results(eng, limit=3)
    assert len(pg["rows"]) == 3 and pg["more"] is True
    sm = {x["pool"]: x for x in r["summary"]}
    assert sm["p1"]["runs"] == 2 and sm["p1"]["entered"] == 2 and sm["p1"]["picked"] == 1 and sm["p1"]["new"] == 2


def test_stock_history_tier_changes(tmp_path):
    runs = [_run("20261112-093500", "2026-11-12", "600002"), _run("20261111-093500", "2026-11-11", "600001")]
    tiers = {"2026-11-10": {"600001": {"tier": "candidate"}}, "2026-11-11": {"600001": {"tier": "focus"}},
             "2026-11-12": {"600001": {"tier": "candidate"}}}
    h = viewsvc.stock_history(_Eng(tmp_path, runs, tiers), "600001")
    assert [t["change"] for t in h["tiers"]] == ["进入候选", "升到重点（原来是候选）", "降到候选（原来是重点）"]
    assert h["current"] == {"date": "2026-11-12", "tier": "candidate"} and len(h["runs"]) == 2


def test_buy_results_page_structure():
    f = fn_body("funnel")
    # 股池标签在最上面；历史记录是收起的 details，默认不选日期也不选运行
    tpl = f[f.index("m.innerHTML=`"):]
    assert tpl.index('id="fchips"') < tpl.index('id="rundetail"') < tpl.index("${manual}")
    assert '<details class="fhist" id="fhist" ${FRID||FDATE?"open":""}' in f
    assert "不选（看全部结果）" in f and "allResults()" in f
    assert '<details class="card fmanual"' in f                       # 手动运行收起、放在结果下面
    assert "只看重点及以上" in f
    assert re.search(r"^let FDATE=\"\",FTIER=false", HTML, re.M) and "FRID=null" not in HTML.split("async function funnel")[0][-200:]
    a = fn_body("allResults")
    assert "/api/results?" in a and 'q.set("tier","focus")' in a and "加载更多" in a and "<th>级别</th>" in a
    d = fn_body("stockDetail")
    assert "级别变化" in d and "moveTier(" in d                          # 手动升降级放进这只票的详情
    clean = re.sub(r"/\*.*?\*/", "", HTML, flags=re.S)
    assert 'id="ftiers"' not in clean and "分级跟踪" not in clean


def test_check_exits_only_on_sell_results():
    s = fn_body("sellResults")
    i = HTML.index("/* ---------------- 卖出结果（第六轮小修）")
    assert HTML.count("立即检查卖出") == HTML[i:i + len(s) + 3000].count("立即检查卖出")   # 只出现在卖出结果这一块
    assert "立即检查卖出" in s and "checkExits(this)" in s
    assert "checkExits" not in fn_body("funnel")
    c = fn_body("checkExits")
    assert "/api/exits/check" in c and "SX.fresh=true" in c


def test_sell_results_table_and_filters():
    s = fn_body("sellResults")
    for k in ('id="schips"', 'id="sxtrig"', 'id="sxprob"', 'id="shist"', "不选（看全部结果）", '<details class="small mute sxhelp">',
              '<th class="n">持有天数</th>', "<th>触发的层</th>", "<th>结果</th>"):
        assert k in s, k
    rows = fn_body("sxRows")
    assert "卖出出问题" in rows and "继续持有" in rows and "已卖出" in rows and "PROB_KIND" in rows
    # 页面上不显示 exit_problem 之类的英文
    assert "exit_problem" not in HTML


# ---------------- 首页后台自动刷新 ----------------
def test_home_auto_refresh_frontend():
    t = fn_body("homeTick")
    assert "document.hidden" in t and "HOME.auto" in t and "HOME.fail" in t and "ptQuiet()" in t
    assert 'addEventListener("visibilitychange"' in HTML
    assert "session" in fn_body("homeEvery") and "300" in fn_body("homeEvery")
    tick = fn_body("drawHomeTick")
    assert "最后更新" in tick and "刷新失败，显示的是" in tick and 'id="hc_auto"' in tick
    # 股池走势两栏用 morph 只更新变化的部分，不重建
    assert "morph(el,`" in fn_body("drawPtLeft") and "morph(el,`" in fn_body("drawPtRight")
    assert "交易时段自动刷新间隔（秒）" in HTML


def test_home_session_and_shared_cache(monkeypatch):
    from qfpy import overview

    class M:
        def is_trading_day(self, d):
            return False

    class E:
        def market(self):
            return M()

    o = overview.Overview.__new__(overview.Overview)
    o.engine = E()
    s = o.session()
    assert s == {"state": "holiday", "label": "休市", "trading": False}
    # 同一只票的详情 5 秒内只算一次
    calls = []
    monkeypatch.setattr(viewsvc, "pool_stock_detail", lambda e, c, p: calls.append(c) or {"code": c})
    viewsvc._PS_CACHE.clear()
    viewsvc.pool_stock_detail_cached(None, "600001", "p1")
    viewsvc.pool_stock_detail_cached(None, "600001", "p1")
    assert calls == ["600001"]


def test_no_new_order_paths():
    clean = re.sub(r"/\*.*?\*/", "", HTML, flags=re.S)
    for bad in ('/api/order"', '/api/cancel"', "/api/command"):
        assert bad not in clean
