"""第六轮小修（二）验收：选股结果汇总表、全部结果表、卖出结果表逐列对齐。
用法：python align_check.py [--shots]
逐列比较表头单元格与每一行数据单元格的左右边界（getBoundingClientRect），差距须 ≤ 2px；
表格宽度须接近容器宽度（≥ 容器 97%）；行不能是卡片（display 必须是 table-row）。"""
import sys
from playwright.sync_api import sync_playwright

import os
BASE = os.environ.get("QF_WEB", "http://127.0.0.1:8094/")
OUT = os.environ.get("QF_SHOTS", "shots")
TOL = 2.0
res = []


def ok(name, cond, info=""):
    res.append((name, bool(cond), info))
    print(("PASS " if cond else "FAIL ") + name + ("  " + str(info) if info else ""))


MEASURE = """(sel)=>{
  const t=document.querySelector(sel); if(!t) return {missing:true};
  const head=t.querySelector('thead tr')||t.querySelector('tr');
  const hs=[...head.children].map(c=>c.getBoundingClientRect());
  const body=[...t.querySelectorAll('tbody tr')].filter(r=>r!==head && !r.classList.contains('fdet'));
  let worst=0, where='', badDisp=[];
  body.forEach((r,ri)=>{
    const disp=getComputedStyle(r).display; if(disp!=='table-row') badDisp.push(ri+':'+disp);
    const cs=[...r.children];
    if(cs.length!==hs.length){worst=Math.max(worst,999);where='row '+ri+' cells '+cs.length+' vs '+hs.length;return}
    cs.forEach((c,ci)=>{const b=c.getBoundingClientRect();const d=Math.max(Math.abs(b.left-hs[ci].left),Math.abs(b.right-hs[ci].right));
      if(d>worst){worst=d;where='row '+ri+' col '+ci}});
  });
  const hy=hs.map(h=>h.top), hTopSpread=Math.max(...hy)-Math.min(...hy);
  const box=(t.closest('.tw')||t.parentElement).getBoundingClientRect(), card=t.closest('.card').getBoundingClientRect();
  const firstRow=body[0]?body[0].getBoundingClientRect():null;
  return {cols:hs.length, rows:body.length, worst, where, badDisp, hTopSpread,
    tw:t.getBoundingClientRect().width, cw:box.width, cardw:card.width,
    headBottom:Math.max(...hs.map(h=>h.bottom)), firstTop:firstRow?firstRow.top:null,
    rowH:firstRow?firstRow.height:null};
}"""


def check_table(pg, tag, label, sel):
    m = pg.evaluate(MEASURE, sel)
    if m.get("missing"):
        ok(f"{tag} {label}：表格存在", False, sel)
        return m
    ok(f"{tag} {label}：有数据行（{m['rows']} 行 × {m['cols']} 列）", m["rows"] > 0)
    ok(f"{tag} {label}：每行都是表格行，不是卡片", not m["badDisp"], m["badDisp"][:3])
    ok(f"{tag} {label}：逐列左右边界与表头对齐（最大差 {m['worst']:.1f}px）", m["worst"] <= TOL, m["where"])
    ok(f"{tag} {label}：表头各格在同一行", m["hTopSpread"] <= 1, m["hTopSpread"])
    ok(f"{tag} {label}：第一行紧贴表头（差 {abs((m['firstTop'] or 0)-m['headBottom']):.1f}px）", m["firstTop"] is not None and abs(m["firstTop"] - m["headBottom"]) <= 2)
    ok(f"{tag} {label}：表格铺满容器（{m['tw']:.0f}/{m['cw']:.0f}px，卡片 {m['cardw']:.0f}px）", m["tw"] >= m["cw"] * 0.97 - 2 and m["cw"] >= m["cardw"] * 0.9)
    if m.get("rowH") is not None and "汇总" not in label:
        ok(f"{tag} {label}：行高紧凑（{m['rowH']:.0f}px）", m["rowH"] <= 60)
    return m


def main():
    import os
    shots = "--shots" in sys.argv
    os.makedirs(OUT, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        for w in (1920, 1600, 1366):
            tag = f"{w}宽"
            pg = b.new_page(viewport={"width": w, "height": 1000})
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(BASE + "#buy/results"); pg.wait_for_selector("#falltbl tbody tr", timeout=15000)
            pg.wait_for_timeout(400)
            check_table(pg, tag, "选股结果·汇总表", "#fsumtbl")
            check_table(pg, tag, "选股结果·全部结果表", "#falltbl")
            # 点开一行：详情在这一行下面、占满整行，其余行仍对齐
            pg.click("#falltbl tbody tr.fres >> nth=0")
            pg.wait_for_selector("#falltbl tr.fdet", timeout=8000)
            d = pg.evaluate("""()=>{const d=document.querySelector('#falltbl tr.fdet'),p=d.previousElementSibling,t=document.querySelector('#falltbl');
              const td=d.firstElementChild.getBoundingClientRect(),tb=t.getBoundingClientRect();
              return {after:p.classList.contains('fres'),span:td.width/tb.width,below:td.top>=p.getBoundingClientRect().bottom-1}}""")
            ok(f"{tag} 点一行：详情展开在这一行下方并占满整行（{d['span']*100:.0f}%）", d["after"] and d["below"] and d["span"] > 0.98, d)
            check_table(pg, tag, "选股结果·展开详情后", "#falltbl")
            txt = pg.inner_text("#fsumtbl")
            ok(f"{tag} 汇总表新入池列标出了旧记录无此项（不当成 0）", "旧记录无此项" in txt or "—" in txt, txt[:200])
            if shots and w == 1920:
                pg.click("#falltbl tbody tr.fres >> nth=0")  # 收起
                pg.wait_for_timeout(300)
                pg.screenshot(path=f"{OUT}/选股结果页_1920.png", full_page=True)
            pg.goto(BASE + "#sell/results"); pg.wait_for_selector("#sxtbl tbody tr", timeout=15000)
            pg.wait_for_timeout(300)
            check_table(pg, tag, "卖出结果表", "#sxtbl")
            if shots and w == 1920:
                pg.screenshot(path=f"{OUT}/卖出结果页_1920.png", full_page=True)
            ok(f"{tag} 没有 JS 报错", not errs, errs[:2])
            pg.close()
        # 手机宽度：可以是卡片式，但不能横向溢出
        pg = b.new_page(viewport={"width": 390, "height": 844})
        for h, sel in (("#buy/results", "#falltbl"), ("#sell/results", "#sxtbl")):
            pg.goto(BASE + h); pg.wait_for_selector(sel + " tbody tr", timeout=15000); pg.wait_for_timeout(300)
            ov = pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
            ok(f"390宽 {h} 没有横向溢出（{ov}px）", ov <= 1)
        b.close()
    bad = [r for r in res if not r[1]]
    print(f"\n共 {len(res)} 项，通过 {len(res)-len(bad)} 项，失败 {len(bad)} 项")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
