"""定时任务：serve 运行期间，交易日到点自动跑。

任务来源（每次配置保存后自动重新计算）：
- [[schedule.runs]]：策略运行时段（start~end）。时段内跑完整链路（选股 → 漏斗 → 退出检查 → 买入），
  是否真的下单由自动交易开关决定。股池还没出数据（全部为空或拉取失败）时，时段内每 retry_secs 秒重试一次。
  默认时段（牛来 gushi 各策略）：竞价多头/竞价阿尔法/晨星量化 09:27–09:35，十点半 10:30–10:35，黄金两点半 14:30–14:35。
- [exits].check_times + 所有 exit_time：退出检查（止盈、止损、到期）
- 每个 exit_time 前 remind_minutes_before 分钟：退出前提醒
每个任务每天只触发一次（记录在 data/state/scheduler.json，重启不会重复触发）。
补录：启动时（以及之后每次检查时），交易日里已经过了时段、当天却没跑过的策略，自动补录当天的股池和漏斗结果，
记录标为「补录」。补录默认不下单（时段已过、价格已变），[schedule].backfill_trade = true 才按自动交易开关下单。
退出检查和提醒错过超过 3 分钟的不补跑。非交易日、无法确认是交易日时都不跑。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

from .exits import exit_times
from .util import hm_minus, log, now_cn, parse_hm, read_json, today_str, write_json

CATCH_UP_MINUTES = 3
DEFAULT_WINDOW_MINUTES = 5

# 牛来（gushi）各策略的默认运行时段
DEFAULT_RUNS = [
    {"name": "竞价策略", "start": "09:27", "end": "09:35",
     "pools": ["gushi:auction_long", "gushi:auction_alpha", "gushi:morning_star", "watchlist:all"]},
    {"name": "十点半", "start": "10:30", "end": "10:35", "pools": ["gushi:half_past_ten"]},
    {"name": "黄金两点半", "start": "14:30", "end": "14:35", "pools": ["gushi:gold_230"]},
]


def run_window(r: dict) -> tuple[str, str]:
    """[[schedule.runs]] 的时段。兼容旧写法 time = "09:31"（时段为 time 起 5 分钟）。"""
    start = r.get("start") or r.get("time")
    end = r.get("end") or hm_plus(start, DEFAULT_WINDOW_MINUTES)
    return start, end


def hm_plus(s: str, minutes: int) -> str:
    return hm_minus(s, -minutes)


def jobs_for(cfg: dict, extra_sell: list[dict] | None = None) -> list[dict]:
    """extra_sell：股池绑定的其他卖出策略内容（engine.bound_sell_contents()），它们的到点时刻也排进来。"""
    jobs = []
    sch = cfg.get("schedule", {})
    if sch.get("enabled", True):
        for i, r in enumerate(sch.get("runs", [])):
            if r.get("enabled", True) is False:
                continue
            start, end = run_window(r)
            name = r.get("name") or "选股+交易"
            jobs.append({"id": f"run@{start}-{end}#{i}", "time": start, "end": end, "kind": "run", "pools": r.get("pools") or None,
                         "name": name, "label": f"{name} {start}–{end}（{', '.join(r.get('pools') or ['全部股池'])}）"})
    ex = cfg.get("exits", {})
    if ex.get("enabled", True):
        ets = exit_times(cfg, extra_sell)
        for t in sorted(set(ex.get("check_times", [])) | set(ets)):
            jobs.append({"id": f"exit@{t}", "time": t, "kind": "exit", "label": "退出检查（止盈/止损/到期）"})
        mins = int(ex.get("remind_minutes_before", 0) or 0)
        if mins > 0:
            for t in ets:
                jobs.append({"id": f"remind@{t}", "time": hm_minus(t, mins), "kind": "remind", "exit_time": t,
                             "label": f"{t} 退出前提醒"})
    pc = cfg.get("pool") or {}
    if pc.get("archive", True) and pc.get("archive_time"):
        jobs.append({"id": f"archive@{pc['archive_time']}", "time": str(pc["archive_time"]), "kind": "archive",
                     "label": "股池成分股存档（按股池回测用）"})
    jobs.sort(key=lambda j: j["time"])
    return jobs


class Scheduler:
    def __init__(self, engine):
        self.engine = engine
        self.path = engine.data_dir / "state" / "scheduler.json"
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self.last_check: dict = {}

    def start(self):
        # 补录放进后台线程第一轮做：补录要请求股池和行情（可能几十秒），不能挡住网页启动
        self.thread.start()
        log.info("定时任务已启动：%s", ", ".join(f"{j['time']} {j['kind']}" for j in self._jobs()))

    def stop(self):
        self._stop.set()

    def _jobs(self) -> list[dict]:
        extra = getattr(self.engine, "bound_sell_contents", None)
        return jobs_for(self.engine.cfg, extra() if extra else None)

    def view(self) -> dict:
        fired = (read_json(self.path, {}) or {}).get(today_str(), {})
        def label(v):
            return v.get("status") if isinstance(v, dict) else v
        sch = self.engine.cfg.get("schedule", {})
        return {"jobs": [{**j, "fired_today": label(fired.get(j["id"])), "backfilled": isinstance(fired.get(j["id"]), dict) and bool(fired[j["id"]].get("backfill"))}
                         for j in self._jobs()], "last_check": self.last_check,
                "backfill": sch.get("backfill", True), "backfill_trade": bool(sch.get("backfill_trade", False))}

    def _loop(self):
        while not self._stop.is_set():
            try:
                now = now_cn()
                self.tick(now)
                self.backfill(now)
            except Exception as e:  # noqa: BLE001
                log.exception("定时任务异常：%s", e)
            self._stop.wait(20)

    def _state(self, day: str) -> tuple[dict, dict]:
        state = read_json(self.path, {}) or {}
        return state, state.setdefault(day, {})

    def _save(self, day: str, fired: dict) -> None:
        write_json(self.path, {day: fired})

    def _trading(self, day: str):
        return self.engine.market().is_trading_day(day)

    @staticmethod
    def _at(now: datetime, hm: str) -> datetime:
        h, m = parse_hm(hm)
        return now.replace(hour=h, minute=m, second=0, microsecond=0)

    @staticmethod
    def _data_ready(rec: dict) -> bool:
        """这次运行有没有拿到股池数据（全部股池为空或失败 = 数据还没出）。"""
        pools = rec.get("pools") or []
        return any((p.get("count") or 0) > 0 for p in pools) or (bool(pools) and all(not p.get("error") for p in pools))

    def tick(self, now: datetime) -> list[str]:
        day = now.date().isoformat()
        _, fired = self._state(day)
        retry_secs = int(self.engine.cfg.get("schedule", {}).get("retry_secs", 60) or 60)
        due = []
        for j in self._jobs():
            t = self._at(now, j["time"])
            st = fired.get(j["id"])
            if j["kind"] == "run":
                end = self._at(now, j["end"])
                if not (t <= now < end):
                    continue
                if isinstance(st, dict):                 # 时段内重试：数据还没出
                    if st.get("done") or now.timestamp() - st.get("last_ts", 0) < retry_secs:
                        continue
                elif st:
                    continue
            elif st or not (t <= now < t + timedelta(minutes=CATCH_UP_MINUTES)):
                continue
            due.append(j)
        if not due:
            return []
        trading = self._trading(day)
        self.last_check = {"at": now.strftime("%H:%M:%S"), "trading_day": trading}
        done = []
        for j in due:
            if trading is not True:
                fired[j["id"]] = f"跳过（{'非交易日' if trading is False else '无法确认今天是交易日'}）"
                if trading is None:
                    self.engine.notifier.notify("error", "定时任务未执行", f"- {j['time']} {j['label']}\n- 无法确认今天是交易日（数据源不可用），为安全起见未执行",
                                                dedupe_key=f"sched-unknown-{day}", dedupe_secs=3600)
                continue
            log.info("定时触发：%s %s", j["time"], j["label"])
            try:
                if j["kind"] == "run":
                    prev = fired.get(j["id"]) if isinstance(fired.get(j["id"]), dict) else {}
                    n = int(prev.get("attempts", 0)) + 1
                    fired[j["id"]] = {"status": f"运行中（第 {n} 次）", "attempts": n, "last_ts": now.timestamp(), "done": False}
                    self._save(day, fired)       # 先落盘再执行，重启不重复
                    rec = self.engine.run(pools=j.get("pools"), trade=True, origin=f"定时 {j['name']} {j['time']}–{j['end']}")
                    ok = self._data_ready(rec)
                    last_try = (now + timedelta(seconds=retry_secs)) >= self._at(now, j["end"])
                    fired[j["id"]] = {"status": (f"已运行 {now:%H:%M:%S}" if ok else
                                                 (f"时段结束仍无数据（{n} 次）" if last_try else f"股池暂无数据，{retry_secs} 秒后重试（第 {n} 次）")),
                                      "attempts": n, "last_ts": now.timestamp(), "done": ok or last_try, "run_id": rec.get("id")}
                    if not ok and last_try:
                        self.engine.notifier.notify("error", "策略时段内没拿到数据", f"- {j['label']}\n- 共尝试 {n} 次，股池都为空或拉取失败")
                else:
                    fired[j["id"]] = f"已触发 {now:%H:%M:%S}"
                    self._save(day, fired)
                    if j["kind"] == "exit":
                        self.engine.check_exits(origin=f"定时 {j['time']}")
                    elif j["kind"] == "remind":
                        self.engine.remind_exits(j["exit_time"])
                    elif j["kind"] == "archive":
                        if hasattr(self.engine, "archive_pools"):
                            self.engine.archive_pools(origin=f"定时 {j['time']}")
            except Exception as e:  # noqa: BLE001
                log.exception("定时任务 %s 失败", j["id"])
                if j["kind"] == "run":
                    fired[j["id"]] = {"status": f"失败：{str(e)[:120]}", "attempts": (fired.get(j["id"]) or {}).get("attempts", 1),
                                      "last_ts": now.timestamp(), "done": False}
                self.engine.notifier.notify("error", "定时任务失败", f"- {j['time']} {j['label']}\n- {e}")
            done.append(j["id"])
        self._save(day, fired)
        return done

    # ---------- 补录 ----------
    def missed_runs(self, now: datetime) -> list[dict]:
        """当天已过了时段、却没跑过的策略。"""
        day = now.date().isoformat()
        _, fired = self._state(day)
        out = []
        for j in self._jobs():
            if j["kind"] != "run" or now < self._at(now, j["end"]):
                continue
            st = fired.get(j["id"])
            interrupted = isinstance(st, dict) and not st.get("done") and str(st.get("status", "")).startswith("运行中")
            if st and not interrupted:
                continue          # 跑过（含重试结束、跳过、补录过）；只有「运行中」就被中断（进程退出）的才补
            out.append(j)
        return out

    def backfill(self, now: datetime) -> list[dict]:
        sch = self.engine.cfg.get("schedule", {})
        if not sch.get("enabled", True) or not sch.get("backfill", True):
            return []
        missed = self.missed_runs(now)
        if not missed:
            return []
        day = now.date().isoformat()
        trading = self._trading(day)
        _, fired = self._state(day)
        if trading is not True:
            for j in missed:
                fired[j["id"]] = f"未补录（{'非交易日' if trading is False else '无法确认今天是交易日'}）"
            self._save(day, fired)
            return []
        trade = bool(sch.get("backfill_trade", False))
        results = []
        for j in missed:
            fired[j["id"]] = {"status": f"补录中 {now:%H:%M:%S}", "done": True, "backfill": True, "last_ts": now.timestamp()}
            self._save(day, fired)
            log.info("补录：%s", j["label"])
            try:
                rec = self.engine.run(pools=j.get("pools"), trade=trade, origin=f"补录 {j['name']} {j['time']}–{j['end']}",
                                      backfill=True)
            except Exception as e:  # noqa: BLE001
                log.exception("补录 %s 失败", j["id"])
                fired[j["id"]] = {"status": f"补录失败：{str(e)[:120]}", "done": True, "backfill": True, "last_ts": now.timestamp()}
                results.append({"job": j["id"], "error": str(e)})
                continue
            fired[j["id"]] = {"status": f"已补录 {now:%H:%M:%S}", "done": True, "backfill": True, "run_id": rec.get("id"),
                              "last_ts": now.timestamp()}
            results.append({"job": j["id"], "run_id": rec.get("id"), "picks": rec.get("picks"), "pools": rec.get("pools")})
        self._save(day, fired)
        lines = []
        for j, r in zip(missed, results):
            if r.get("error"):
                lines.append(f"- {j['label']}：补录失败 {r['error'][:100]}")
            else:
                cnt = sum(p.get("count") or 0 for p in r.get("pools") or [])
                picks = "、".join(p["code"] + " " + p["name"] for p in r.get("picks") or []) or "无"
                lines.append(f"- {j['name']} {j['time']}–{j['end']}：股池 {cnt} 只，入选 {picks}")
        lines.append("- " + ("补录结果按自动交易开关下单（backfill_trade 已打开）" if trade else "补录结果只记录、不下单（时段已过、价格已变）"))
        self.engine.notifier.notify("backfill", f"补录 {len(missed)} 个错过的策略时段", "\n".join(lines))
        return results
