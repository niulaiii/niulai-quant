"""历史缓存清理插件入口：主程序零改动。

目录式插件的 `plugin.py` 是被单独 exec 的模块，不是包，所以子包要自己加进 sys.path。
这个插件不注册任何因子/数据源/渠道，只做一件事：在 `qfpy serve` 进程起一个自管
daemon 线程，按 plugin.toml 的 keep_days 清理过期的历史行情缓存。

线程 guard 放在 worker 模块里（正常 import，会被 sys.modules 缓存），所以网页
「重新加载插件」重新 exec 本文件时也不会起第二个线程。
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from cache_pruner import worker  # noqa: E402

worker.start_once()
