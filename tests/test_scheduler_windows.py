"""定时任务：策略运行时段、时段内重试、启动补录（默认不下单、推送、标记补录）。全部离线。"""
from __future__ import annotations

import copy
from datetime import datetime
from pathlib import Path

import pytest

from qfpy.config import DEFAULTS, deep_merge, validate
from qfpy.scheduler import DEFAULT_RUNS, Scheduler, jobs_for, run_window
from qfpy.util import CN as BJ


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def notify(self, event, title, text, **kw):
        self.sent.append((event, title, text))


class FakeMarket:
    def __init__(self, trading=True):
        self.trading = trading

    def is_trading_day(self, day):
        return self.trading


class FakeEngine:
    def __init__(self, tmp: Path, trading=True, counts=None, **sch):
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg["schedule"] = {"enabled": True, "runs": copy.deepcopy(DEFAULT_RUNS), "retry_secs": 60,
                                "backfill": True, "backfill_trade": False, **sch}
        self.cfg["exits"] = {**self.cfg["exits"], "enabled": False}
        self.data_dir = tmp
        self.notifier = FakeNotifier()
        self._m = FakeMarket(trading)
        self.runs = []
        self.counts = counts if counts is not None else [3]

    def market(self):
        return self._m

    def run(self, pools=None, trade=True, origin="", backfill=False):
        n = self.counts.pop(0) if len(self.counts) > 1 else self.counts[0]
        rec = {"id": f"r{len(self.runs) + 1}", "pools": [{"key": k, "count": n, "error": None if n else "暂无数据"} for k in pools],
               "picks": [{"code": "300825", "name": "阿尔特"}] if n else [], "backfill": backfill}
        self.runs.append({"pools": pools, "trade": trade, "origin": origin, "backfill": backfill})
        return rec


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    """测试里的时刻都在 2026-11-11；把系统「今天」也固定到这天（QF_SIM_NOW），否则换个日期跑，
    view() 按真实今天找不到状态，测试会因为日期过期而挂。"""
    monkeypatch.setenv("QF_SIM_NOW", "2026-11-11 08:00")


def at(hm: str, day="2026-11-11") -> datetime:
    h, m = map(int, hm.split(":"))
    return datetime.fromisoformat(day).replace(hour=h, minute=m, tzinfo=BJ)


def test_default_windows_match_niulai_strategies():
    w = {tuple(r["pools"][:3]) if len(r["pools"]) > 1 else r["pools"][0]: run_window(r) for r in DEFAULT_RUNS}
    assert w[("gushi:auction_long", "gushi:auction_alpha", "gushi:morning_star")] == ("09:27", "09:35")
    assert w["gushi:half_past_ten"] == ("10:30", "10:35")
    assert w["gushi:gold_230"] == ("14:30", "14:35")


def test_shipped_config_uses_new_windows():
    import tomllib
    cfg = tomllib.loads((Path(__file__).resolve().parent.parent / "config" / "pipeline.toml").read_text("utf-8"))
    wins = {r["start"] + "-" + r["end"]: r["pools"] for r in cfg["schedule"]["runs"]}
    assert "gushi:auction_long" in wins["09:27-09:35"] and "gushi:morning_star" in wins["09:27-09:35"]
    assert wins["10:30-10:35"] == ["gushi:half_past_ten"] and wins["14:30-14:35"] == ["gushi:gold_230"]
    assert cfg["schedule"]["backfill_trade"] is False


def test_legacy_time_field_still_works():
    assert run_window({"time": "09:31"}) == ("09:31", "09:36")


def test_validate_rejects_bad_window():
    cfg = deep_merge(DEFAULTS, {"schedule": {"runs": [{"start": "10:35", "end": "10:30", "pools": []}]}})
    with pytest.raises(ValueError, match="晚于"):
        validate(cfg)


def test_runs_inside_window_once(tmp_path):
    e = FakeEngine(tmp_path)
    s = Scheduler(e)
    assert s.tick(at("09:26")) == []
    assert len(s.tick(at("09:27"))) == 1
    assert e.runs[0]["trade"] is True and e.runs[0]["backfill"] is False
    assert s.tick(at("09:30")) == []          # 已经拿到数据，不再跑
    assert s.tick(at("10:29")) == []
    s.tick(at("10:33"))                        # 十点半时段内启动也会跑
    assert e.runs[-1]["pools"] == ["gushi:half_past_ten"]
    assert s.tick(at("10:35")) == []          # 时段结束（不含 end）


def test_retry_inside_window_until_data(tmp_path):
    e = FakeEngine(tmp_path, counts=[0, 0, 5])
    s = Scheduler(e)
    s.tick(at("09:27"))
    assert s.tick(at("09:27").replace(second=30)) == []      # 不到 retry_secs
    s.tick(at("09:28").replace(second=1))
    s.tick(at("09:29").replace(second=2))
    assert len(e.runs) == 3
    st = s.view()["jobs"][0]["fired_today"]
    assert st.startswith("已运行")
    assert s.tick(at("09:31")) == []


def test_window_end_without_data_notifies(tmp_path):
    e = FakeEngine(tmp_path, counts=[0])
    s = Scheduler(e)
    for hm in ("09:27", "09:28", "09:29", "09:30", "09:31", "09:32", "09:33", "09:34"):
        s.tick(at(hm).replace(second=5))
    assert any("没拿到数据" in t for _, t, _ in e.notifier.sent)
    assert len(e.runs) == 8


def test_backfill_missed_windows_on_start(tmp_path):
    e = FakeEngine(tmp_path)
    s = Scheduler(e)
    res = s.backfill(at("11:00"))                   # 11:00 启动：竞价和十点半都错过了，黄金两点半还没到
    assert [r["pools"][0] for r in e.runs] == ["gushi:auction_long", "gushi:half_past_ten"]
    assert all(r["backfill"] and r["trade"] is False for r in e.runs)      # 默认不下单
    assert all(r["origin"].startswith("补录") for r in e.runs)
    assert len(res) == 2
    ev = [x for x in e.notifier.sent if x[0] == "backfill"]
    assert len(ev) == 1 and "不下单" in ev[0][2] and "阿尔特" in ev[0][2]
    jobs = s.view()["jobs"]
    assert jobs[0]["backfilled"] and jobs[0]["fired_today"].startswith("已补录")
    # 再启动一次：不重复补录
    s2 = Scheduler(e)
    assert s2.backfill(at("11:05")) == []
    assert len(e.runs) == 2
    # 之后黄金两点半正常在时段内跑，不算补录
    s2.tick(at("14:30"))
    assert e.runs[-1]["backfill"] is False and e.runs[-1]["trade"] is True


def test_backfill_trade_option(tmp_path):
    e = FakeEngine(tmp_path, backfill_trade=True)
    Scheduler(e).backfill(at("15:30"))
    assert len(e.runs) == 3 and all(r["trade"] for r in e.runs)
    assert "按自动交易开关下单" in [x for x in e.notifier.sent if x[0] == "backfill"][0][2]


def test_no_backfill_before_first_window_or_when_disabled(tmp_path):
    e = FakeEngine(tmp_path)
    assert Scheduler(e).backfill(at("09:00")) == []
    assert Scheduler(e).backfill(at("09:30")) == []         # 时段内交给 tick，不算错过
    e2 = FakeEngine(tmp_path / "b", backfill=False)
    assert Scheduler(e2).backfill(at("15:30")) == [] and e2.runs == []


def test_no_backfill_on_non_trading_day(tmp_path):
    e = FakeEngine(tmp_path, trading=False)
    s = Scheduler(e)
    assert s.backfill(at("15:30")) == [] and e.runs == []
    assert s.view()["jobs"][0]["fired_today"].startswith("未补录")


def test_interrupted_run_is_backfilled(tmp_path):
    """时段内开始运行后进程退出（状态停在「运行中」），重启后算错过、要补录。"""
    from qfpy.util import write_json
    e = FakeEngine(tmp_path)
    jid = jobs_for(e.cfg)[0]["id"]
    write_json(tmp_path / "state" / "scheduler.json", {"2026-11-11": {jid: {"status": "运行中（第 1 次）", "done": False, "last_ts": 0}}})
    Scheduler(e).backfill(at("09:50"))
    assert len(e.runs) == 1 and e.runs[0]["backfill"]
