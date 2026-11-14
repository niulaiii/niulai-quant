"""后台单飞任务：网页请求线程里不同步等待慢操作（下单程序、行情接口）。

用法：job = BgJob("卖出预览", fn)；请求里调 job.kick() 立刻返回，再把 job.view()（上次的结果 + 是否在跑）返回给页面，
页面轮询拿新结果。同一时刻只有一个在跑，重复 kick 不会叠加。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .util import log, ts


class BgJob:
    def __init__(self, name: str, fn: Callable[..., Any], max_age: float = 30.0):
        self.name, self.fn, self.max_age = name, fn, max_age
        self.lock = threading.Lock()
        self.running = False
        self.result: Any = None
        self.error: str | None = None
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.args: tuple = ()
        self._done_mono: float | None = None
        self._thread: threading.Thread | None = None

    def stale(self) -> bool:
        return self._done_mono is None or time.monotonic() - self._done_mono > self.max_age

    def kick(self, *args, force: bool = False) -> bool:
        """需要时在后台启动一次。返回是否新启动了。立刻返回，绝不阻塞。"""
        with self.lock:
            if self.running or (not force and not self.stale()):
                return False
            self.running, self.started_at, self.args = True, ts(), args
        self._thread = threading.Thread(target=self._run, args=args, name=f"bg-{self.name}", daemon=True)
        self._thread.start()
        return True

    def _run(self, *args):
        try:
            r = self.fn(*args)
            with self.lock:
                self.result, self.error = r, None
        except Exception as e:  # noqa: BLE001
            log.warning("后台任务 %s 失败：%s", self.name, e)
            with self.lock:
                self.error = f"{type(e).__name__}: {e}"
        finally:
            with self.lock:
                self.running, self.finished_at, self._done_mono = False, ts(), time.monotonic()

    def wait(self, timeout: float = 30.0) -> None:
        """测试和命令行用。"""
        t = self._thread
        if t is not None:
            t.join(timeout)

    def view(self) -> dict:
        with self.lock:
            return {"running": self.running, "started_at": self.started_at, "finished_at": self.finished_at,
                    "error": self.error, "has_result": self._done_mono is not None and self.error is None}
