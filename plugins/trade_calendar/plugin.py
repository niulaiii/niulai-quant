"""交易日历插件入口。

**完全独立**：不注册因子/数据源/渠道，不改主程序任何一行，只做两件事——

1. 把本目录加进 sys.path，让其他插件运行时可以 `import trade_calendar_lib`；
2. 日历文件缺失/过期/跨年时，后台联网重拉一次，落盘到公共数据文件。

之所以要「后台」：Engine 每次构造都会加载插件（serve、CLI 每条命令、测试、
网页「重新加载插件」），不能让联网阻塞启动。文件正常时这里什么都不做，
只是一次内存判断，开销可忽略。

其他插件怎么读（运行时 import，不用管加载顺序）：

    import trade_calendar_lib as cal
    cal.is_trading_day(data_dir, "2026-11-16")        # True/False/None
    cal.prev_trading_day(data_dir, "2026-11-16")      # "2026-11-13"

也可以完全不 import，直接按 trade_calendar_lib.py 顶部写的数据契约读
`data/cache/trade_calendar.json`——两个插件之间只靠这份公共数据解耦。
"""
from __future__ import annotations

import sys
import threading
import tomllib
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import trade_calendar_lib as cal  # noqa: E402  （暴露给其他插件的稳定模块名）

_ROOT = _HERE.parents[1]          # plugins/trade_calendar → plugins → 项目根


def _settings() -> dict:
    try:
        return tomllib.loads((_HERE / "plugin.toml").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _warm() -> None:
    s = _settings()
    try:
        doc = cal.ensure(cal.resolve_data_dir(_ROOT),
                         min_year=int(s.get("min_year") or cal.MIN_YEAR),
                         max_age_days=int(s.get("refresh_days") or cal.REFRESH_DAYS))
        ds = doc.get("dates") or []
        if ds:
            cal.log.info("交易日历就绪：%d 个交易日（%s → %s）", len(ds), ds[0], ds[-1])
        else:
            cal.log.warning("交易日历不可用：盘前判交易日会判不出来（插件任务会安全跳过）")
    except Exception as e:  # noqa: BLE001  日历预热失败绝不能影响进程启动
        cal.log.warning("交易日历预热失败：%s", e)


# 进程级一次性：guard 挂在被 import 缓存的 trade_calendar_lib 模块上，
# 即使 plugin.py 被「重新加载插件」重新 exec，也不会重复起刷新线程。
if not getattr(cal, "_plugin_warmed", False):
    cal._plugin_warmed = True
    _data_dir = cal.resolve_data_dir(_ROOT)
    if cal.stale(_data_dir, int(_settings().get("refresh_days") or cal.REFRESH_DAYS)):
        threading.Thread(target=_warm, name="trade-calendar", daemon=True).start()
    else:
        _ds = cal.dates(_data_dir)
        cal.log.info("交易日历就绪：%d 个交易日（%s → %s）", len(_ds), _ds[0], _ds[-1])
