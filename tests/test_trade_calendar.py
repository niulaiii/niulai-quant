"""交易日历插件的离线测试（公共日历，不联网、不起线程）。

日历文件由 plugins/trade_calendar/ 生产，其它插件按文件顶部写明的数据契约读取。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_P = _ROOT / "plugins" / "trade_calendar"
if str(_P) not in sys.path:
    sys.path.insert(0, str(_P))

import trade_calendar_lib as cal  # noqa: E402


def write_cal(tmp: Path, dates: list[str], fetched_at: str | None = None) -> Path:
    p = tmp / cal.FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"fetched_at": fetched_at or time.strftime("%Y-%m-%d %H:%M:%S"),
                             "source": "test", "min_year": 2023, "dates": sorted(dates)}), encoding="utf-8")
    return p


def test_is_trading_day_from_calendar(tmp_path):
    write_cal(tmp_path, ["2026-11-13", "2026-11-16"])
    assert cal.is_trading_day(tmp_path, "2026-11-16") is True
    assert cal.is_trading_day(tmp_path, "2026-11-14") is False   # 周末，不在日历里
    assert cal.is_trading_day(tmp_path, "2027-01-04") is None    # 超出日历范围 = 判不出来


def test_is_trading_day_without_calendar(tmp_path):
    """没有日历文件时返回 None，调用方自己决定跳过还是兜底。"""
    assert cal.is_trading_day(tmp_path, "2026-11-16") is None


def test_prev_trading_day_skips_holiday(tmp_path):
    """国庆长假：10-09 的上一交易日是 09-30，不是 10-08。"""
    write_cal(tmp_path, ["2026-09-30", "2026-10-09", "2026-10-12"])
    assert cal.prev_trading_day(tmp_path, "2026-10-09") == "2026-09-30"
    assert cal.next_trading_day(tmp_path, "2026-10-09") == "2026-10-12"
    assert cal.prev_trading_day(tmp_path, "2026-09-30") is None


def test_stale(tmp_path):
    assert cal.stale(tmp_path) is True                                   # 没有文件
    write_cal(tmp_path, ["2026-11-13"], fetched_at="2026-01-01 09:00:00")
    assert cal.stale(tmp_path) is True                                   # 太久没更新
    write_cal(tmp_path, ["2026-11-13", "2099-12-31"])
    assert cal.stale(tmp_path) is False                                  # 新鲜且覆盖到今年年底


def test_calendar_cached_by_mtime(tmp_path):
    """同一个文件反复读走进程内缓存；文件变了要能读到新内容。"""
    write_cal(tmp_path, ["2026-11-13"])
    assert cal.is_trading_day(tmp_path, "2026-11-13") is True
    assert cal.is_trading_day(tmp_path, "2026-11-16") is None
    write_cal(tmp_path, ["2026-11-13", "2026-11-16"])
    assert cal.is_trading_day(tmp_path, "2026-11-16") is True


def test_resolve_data_dir():
    """按项目自己的 config/pipeline.toml 解析，插件不需要 Engine 实例。"""
    assert cal.resolve_data_dir(_ROOT) == _ROOT / "data"
