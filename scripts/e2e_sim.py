"""端到端演练（模拟时间 + 真实历史行情 + 模拟盘）：

    模拟买入 → 第二天到期 → 人工保留跳过 → 再到期卖出

用法：  python scripts/e2e_sim.py [买入日 第二天 第三天]   默认 2026-11-06 2026-11-09 2026-11-10

做法：复制一份配置到临时目录（独立的数据目录，不碰你的模拟盘账户），用环境变量 QF_SIM_NOW 把系统时钟
拨到各个时刻，然后调用定时任务的 tick()——和 serve 里的定时线程完全同一条路径。全程 paper 模式，
不会调用下单程序的任何接口。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

E2E_TOML = """
data_dir = "data"

[sources.gushi]
type = "gushi"
enabled = true
base_url = "https://www.gushi.in"
key = "{gushi_key}"
min_interval_ms = 1600

[sources.watchlist]
type = "watchlist"
enabled = true
codes = ["{code}"]

[sources.akshare]
type = "akshare"
enabled = true

[pool]
pools = ["watchlist:all"]

[market]
minute_sources = ["gushi", "akshare"]
daily_sources = ["akshare"]

[[factors]]
kind = "exclude_st"

[[factors]]
kind = "expr"
label = "日K回看连通性"
params = {{ expr = "close[1] > 0 and ma5[1] > 0" }}

[select]
top_n = 1

[sizing]
per_stock_amount = 5000

[execution]
mode = "paper"

[risk]
enforce_session = true

[paper]
ignore_session = true

[exits]
enabled = true
take_profit_pct = 50.0      # 放宽止盈止损，只验证“到期卖出 + 保留”这条链路
stop_loss_pct = 50.0
max_hold_days = 1
exit_time = "14:50"
check_times = []
remind_minutes_before = 5

[schedule]
enabled = true
runs = [{{ time = "14:31", pools = ["watchlist:all"] }}]

[notify]
enabled = true
channels = ["dingtalk"]

[notify.dingtalk]
webhook = ""
"""


def main():
    d1, d2, d3 = (sys.argv[1:4] if len(sys.argv) >= 4 else ["2026-11-06", "2026-11-09", "2026-11-10"])
    code = os.environ.get("E2E_CODE", "300825")
    import tomllib
    real = tomllib.loads((ROOT / "config" / "pipeline.toml").read_text(encoding="utf-8"))
    key = os.environ.get("QF_GUSHI_KEY") or real["sources"]["gushi"].get("key", "")

    work = Path(tempfile.mkdtemp(prefix="qf-e2e-"))
    (work / "config").mkdir()
    (work / "config" / "pipeline.toml").write_text(E2E_TOML.format(gushi_key=key, code=code), encoding="utf-8")
    if (ROOT / "data" / "cache").exists():                 # 复用已有缓存，少打接口
        shutil.copytree(ROOT / "data" / "cache", work / "data" / "cache")

    os.environ["QF_SIM_NOW"] = f"{d1} 09:00"
    from qfpy.config import ConfigStore
    from qfpy.engine import Engine
    from qfpy.scheduler import Scheduler
    from qfpy.util import now_cn, read_jsonl, setup_logging

    store = ConfigStore(work / "config" / "pipeline.toml")
    setup_logging(store.data_dir)
    e = Engine(store)
    sch = Scheduler(e)
    e.set_auto(True, "e2e")
    ok = True

    def at(when: str):
        os.environ["QF_SIM_NOW"] = when
        fired = sch.tick(now_cn())
        e.notifier.flush()
        print(f"\n=== 模拟时间 {when}  触发：{fired or '无'}")
        return fired

    def pos():
        return e.book.get(code)

    def check(cond, msg):
        nonlocal ok
        print(("  ✓ " if cond else "  ✗ ") + msg)
        ok = ok and bool(cond)

    print(f"工作目录：{work}\n买入日 {d1}，到期日 {d2}（保留跳过），再到期 {d3}（卖出），股票 {code}")
    print("定时任务：", [(j["time"], j["kind"]) for j in __import__("qfpy.scheduler", fromlist=["jobs_for"]).jobs_for(e.cfg)])

    # 第 1 天：14:31 定时选股+交易 → 模拟买入
    at(f"{d1} 14:31")
    p = pos()
    check(p is not None, f"{d1} 模拟买入：{p and (p['quantity'], p['avg_cost'])}")
    if not p:
        last = e._run_index()[:1]
        print("最近一次运行：", last)
        return 1
    at(f"{d1} 14:50")
    check(pos() and pos()["quantity"] == p["quantity"], "当天 14:50 退出检查：持有未满 1 天且 T+1，不卖")

    # 第 2 天：14:45 提醒 → 用户点「保留」→ 14:50 跳过
    at(f"{d2} 14:45")
    rem = [r for r in read_jsonl(e.data_dir / "notify.jsonl", 50) if r["event"] == "exit_reminder"]
    check(rem and code in rem[-1]["text"] and "未保留，到点卖出" in rem[-1]["text"], f"{d2} 14:45 收到退出前提醒")
    e.set_hold(code, True, "e2e：想再拿一天", by="e2e")
    at(f"{d2} 14:50")
    p2 = pos()
    check(p2 and p2["quantity"] == p["quantity"] and not p2.get("hold"), f"{d2} 14:50 到期但保留 → 跳过卖出，标记已失效")

    # 第 3 天：再到期 → 卖出
    at(f"{d3} 14:45")
    at(f"{d3} 14:50")
    check(pos() is None, f"{d3} 14:50 再到期 → 卖出，持仓清空")

    print("\n=== 委托记录")
    for o in read_jsonl(e.data_dir / "orders.jsonl", 50):
        print(f"  {o['trading_date']} {o['side']} {o['symbol']} {o['quantity']}股 @{o.get('price')} → {o['status_label']} {o.get('message', '')[:60]}")
    print("=== 已平仓")
    for c in e.book.s.get("closed", []):
        print(f"  {c['symbol']} {c.get('buy_date')}→{c['date']} {c['quantity']}股 卖价 {c['sell_price']} 盈亏 {c['pnl']} （{c['reason']}）")
    print("=== 推送（未配置 token，仅日志）")
    for r in read_jsonl(e.data_dir / "notify.jsonl", 50):
        print(f"  [{r['event']}] {r['title']} → {r['results'][0]['status']}")
    print("\n结果：" + ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
