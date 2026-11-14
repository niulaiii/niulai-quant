"""跨进程文件锁与状态事务的回归测试。

核心要防的是 lost update：两个进程各持一份内存快照，后写的把先写的整份覆盖。
旧代码里 Book / OrderJournal 用 threading.RLock（只锁进程内），命令行与网页服务
同时改持仓时会出现「保留标记被冲掉」「持仓状态回退」。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from qfpy.datasources.base import JsonCache
from qfpy.exec.book import Book
from qfpy.exec.state import Switches
from qfpy.util import lock_for, process_lock, read_json, update_json, write_json

ROOT = Path(__file__).resolve().parent.parent
PAPER_CFG = {"initial_cash": 100000.0}


def _book(root: Path) -> Book:
    return Book(root, "paper", PAPER_CFG)


def test_book_transaction_keeps_other_process_write(tmp_path):
    """两个账本实例交替写入，先写的那只票不能被后写的整份覆盖。"""
    b1, b2 = _book(tmp_path), _book(tmp_path)
    b1.manual_upsert("600519", "贵州茅台", 100, 10.0, 100, "2026-11-14")
    b2.manual_upsert("300825", "演示", 100, 20.0, 100, "2026-11-14")
    disk = read_json(tmp_path / "paper" / "account.json")
    assert set(disk["positions"]) == {"600519", "300825"}


def test_book_transaction_keeps_hold_flag(tmp_path):
    """保留标记同理：两个实例各点一次保留，两个都要留在盘上。"""
    b1, b2 = _book(tmp_path), _book(tmp_path)
    b1.manual_upsert("600519", "A", 100, 10.0, 100, "2026-11-14")
    b1.set_hold("600519", True, "进程一")
    b2.manual_upsert("300825", "B", 100, 20.0, 100, "2026-11-14")
    b2.set_hold("300825", True, "进程二")
    disk = read_json(tmp_path / "paper" / "account.json")
    assert disk["positions"]["600519"]["hold"]["note"] == "进程一"
    assert disk["positions"]["300825"]["hold"]["note"] == "进程二"


def test_transaction_releases_lock_after_exception(tmp_path):
    b = _book(tmp_path)
    with pytest.raises(ValueError):
        with b.lock:
            raise ValueError("中途失败")
    with b.lock:          # 能再进去说明锁没被卡住
        pass


def test_update_json_is_atomic_across_real_processes(tmp_path):
    """真开 3 个进程各累加 40 次，结果必须是 120，不能丢更新。"""
    p = tmp_path / "counter.json"
    write_json(p, {"n": 0})
    code = (
        "from pathlib import Path\n"
        "import sys\n"
        "from qfpy.util import update_json\n"
        "p = Path(sys.argv[1])\n"
        "for _ in range(40):\n"
        "    update_json(p, lambda d: {**d, 'n': d['n'] + 1})\n"
    )
    ps = [subprocess.Popen([sys.executable, "-c", code, str(p)], cwd=str(ROOT)) for _ in range(3)]
    for x in ps:
        assert x.wait(timeout=60) == 0
    assert read_json(p)["n"] == 120


def test_process_lock_is_reentrant(tmp_path):
    """同一线程嵌套拿同一把锁必须能重入：策略的 create/activate 会嵌套调 ensure。"""
    lp = lock_for(tmp_path / "y.json")
    with process_lock(lp):
        with process_lock(lp):
            with process_lock(lp):
                pass


def test_process_lock_timeout(tmp_path):
    """另一个进程持锁不放时，本进程等待超时要抛错，不能静默卡死。"""
    lp = lock_for(tmp_path / "x.json")
    holder = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from qfpy.util import process_lock\n"
        "with process_lock(Path(sys.argv[1])):\n"
        "    print('held', flush=True)\n"
        "    time.sleep(2)\n"
    )
    p = subprocess.Popen([sys.executable, "-c", holder, str(lp)], cwd=str(ROOT),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    with pytest.raises(TimeoutError):
        with process_lock(lp, timeout=0.3):
            pass
    assert p.wait(timeout=15) == 0


def test_switches_write_behaviour_unchanged(tmp_path):
    s = Switches(tmp_path, False)
    s.set_auto(True, "测试一")
    s.set_auto(False, "测试二")
    d = read_json(tmp_path / "state" / "automation.json")
    assert d["enabled"] is False and len(d["history"]) == 2
    assert s.halt("测试", "t")["halted"] is True
    assert s.resume("t")["halted"] is False


def test_strategy_guard_is_reentrant(tmp_path):
    """策略模块的 _guard 必须能嵌套：create/activate 内部会调 ensure。"""
    from types import SimpleNamespace
    from qfpy.strategies import Strategies

    s = Strategies(SimpleNamespace(data_dir=tmp_path))
    with s._guard():
        with s._guard():
            pass


def test_strategy_guard_prevents_lost_update_across_processes(tmp_path):
    """两个进程并发「读 active.json → 追加 → 写回」，两条都要留下。"""
    (tmp_path / "strategies").mkdir(parents=True, exist_ok=True)
    write_json(tmp_path / "strategies" / "active.json", {"marks": []})
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from types import SimpleNamespace\n"
        "from qfpy.strategies import Strategies\n"
        "from qfpy.util import read_json, write_json\n"
        "root = Path(sys.argv[1])\n"
        "s = Strategies(SimpleNamespace(data_dir=root))\n"
        "f = root / 'strategies' / 'active.json'\n"
        "with s._guard():\n"
        "    d = read_json(f, {}) or {}\n"
        "    d.setdefault('marks', []).append(sys.argv[2])\n"
        "    write_json(f, d)\n"
    )
    ps = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path), tag], cwd=str(ROOT),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
          for tag in ("A", "B")]
    for p in ps:
        _, err = p.communicate(timeout=30)
        assert p.returncode == 0, err
    assert sorted(read_json(tmp_path / "strategies" / "active.json")["marks"]) == ["A", "B"]


def test_cache_put_is_atomic_and_leaves_no_tmp(tmp_path):
    c = JsonCache(tmp_path, 60, False)
    c.put("gushi/minute/20261113/600519.json", {"ok": True, "n": 1})
    f = tmp_path / "gushi" / "minute" / "20261113" / "600519.json"
    assert json.loads(f.read_text(encoding="utf-8"))["ok"] is True
    assert not list(f.parent.glob("*.tmp*"))
