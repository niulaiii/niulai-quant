"""时间、文件、日志等小工具。"""
from __future__ import annotations

import errno
import json
import logging
import os
import sys
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    import fcntl
except ImportError:          # Windows 没有 flock，退化成只锁进程内
    fcntl = None

CN = ZoneInfo("Asia/Shanghai")
log = logging.getLogger("qf")
_io_lock = threading.RLock()
_LOCK_TIMEOUT = 10.0


def setup_logging(data_dir: Path | None = None, level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if getattr(root, "_qf_setup", False):
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    fmt.converter = lambda *_a: now_cn().timetuple()   # 日志一律用北京时间（服务器可能是 UTC）
    h = logging.StreamHandler()
    h.setFormatter(fmt)
    root.addHandler(h)
    if data_dir is not None:
        data_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(data_dir / "qf.log", encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "urllib3", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    root._qf_setup = True  # type: ignore[attr-defined]


def now_cn() -> datetime:
    """北京时间。设置环境变量 QF_SIM_NOW="YYYY-MM-DD HH:MM" 可以把系统时钟固定到某个时刻（模拟时间，用于端到端演练和测试）。"""
    sim = os.environ.get("QF_SIM_NOW")
    if sim:
        return datetime.fromisoformat(sim.strip()).replace(tzinfo=CN)
    return datetime.now(CN)


def today_cn() -> date:
    return now_cn().date()


def today_str() -> str:
    return today_cn().isoformat()


def ts() -> str:
    return now_cn().strftime("%Y-%m-%d %H:%M:%S")


def parse_hm(s: str) -> tuple[int, int]:
    try:
        h, m = str(s).strip().split(":")[:2]
        h, m = int(h), int(m)
    except ValueError:
        raise ValueError(f"时间 {s} 不合法，要写成 HH:MM，比如 14:50") from None
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"时间 {s} 不合法")
    return h, m


def hm_minus(s: str, minutes: int) -> str:
    h, m = parse_hm(s)
    t = datetime(2000, 1, 1, h, m) - timedelta(minutes=minutes)
    return t.strftime("%H:%M")


def in_sessions(now: datetime, sessions: list) -> bool:
    t = now.strftime("%H:%M:%S")
    for a, b in sessions:
        ha, ma = parse_hm(a)
        hb, mb = parse_hm(b)
        if f"{ha:02d}:{ma:02d}:00" <= t <= f"{hb:02d}:{mb:02d}:59":
            return True
    return False


def normalize_date(s: str) -> str:
    t = str(s).strip()
    if len(t) == 8 and t.isdigit():
        return f"{t[:4]}-{t[4:6]}-{t[6:]}"
    return t[:10]


def round2(v: float) -> float:
    return float(f"{v + (1e-9 if v >= 0 else -1e-9):.2f}")


def lock_for(path: Path) -> Path:
    """状态文件对应的独立锁文件。不能锁数据文件本身：write_json 用 os.replace 换 inode，
    锁在旧 inode 上会失效，所以单独放一个 .xxx.lock。"""
    p = Path(path)
    return p.parent / ("." + p.name + ".lock")


_plock_registry: dict[tuple[str, int], dict] = {}


@contextmanager
def process_lock(path: Path, *, exclusive: bool = True, timeout: float = _LOCK_TIMEOUT):
    """跨进程文件锁（POSIX flock）。只锁 lock_for() 给的独立锁文件。

    同一线程内嵌套获取同一把锁是安全的（只加计数，不重复 flock）——策略的
    create / activate 会嵌套调 ensure，必须能重入。不同线程/不同进程各自开 fd，
    flock 会自然互斥。Windows 没有 flock 时退化成进程内锁。
    """
    if fcntl is None:
        with _io_lock:
            yield
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    key = (str(p), threading.get_ident())
    with _io_lock:
        st = _plock_registry.get(key)
    if st and st["depth"] > 0:
        st["depth"] += 1
        try:
            yield
        finally:
            st["depth"] -= 1
        return
    fh = p.open("a+")
    op = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    deadline = time.monotonic() + max(0.0, timeout)
    try:
        while True:
            try:
                fcntl.flock(fh.fileno(), op | fcntl.LOCK_NB)
                break
            except OSError as ex:
                if ex.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"等待文件锁超时（{timeout:.0f} 秒）：{p}")
                time.sleep(0.02)
        st = {"fh": fh, "depth": 1}
        with _io_lock:
            _plock_registry[key] = st
        yield
    finally:
        with _io_lock:
            _plock_registry.pop(key, None)
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def update_json(path: Path, mutator, default: Any = None) -> Any:
    """跨进程安全的「读 → 改 → 写」：整段在排他锁内完成，用于没有常驻内存态的状态文件
    （自动交易开关、一键停止、持仓获取方式等）。mutator 就地改传入的 dict，或返回新值。"""
    path = Path(path)
    with process_lock(lock_for(path), exclusive=True):
        cur = read_json(path, default)
        new = mutator(cur)
        out = cur if new is None else new
        write_json(path, out)
        return out


def atomic_write_text(path: Path, text: str) -> None:
    """原子写文本：临时文件 + os.replace。给非 write_json 的落盘点用（行情缓存、toml 配置）。"""
    path = Path(path)
    with _io_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)


class StateTransaction:
    """Book / OrderJournal 用的可重入「读-改-写」事务锁。

    最外层进入：拿跨进程排他锁 → 只有磁盘确实被别的进程/写者改过（mtime+size 变了）
    才从磁盘重载，否则保留本进程内存态（事务外直接改内存的代码不会被冲掉）；
    最外层退出：写回磁盘 → 释放锁。内层嵌套（方法里再调 save()）只计数。

    两个进程交替操作时，后进入的会看到 sig 变化并重载，于是不会把先写的整份覆盖。
    """
    def __init__(self, data_path: Path, lock_path: Path, reload_fn, save_fn):
        self._rlock = threading.RLock()
        self._depth = 0
        self._data_path = Path(data_path)
        self._lock_path = Path(lock_path)
        self._reload = reload_fn
        self._save = save_fn
        self._cm = None
        self._last_sig = self._sig()

    def _sig(self):
        try:
            st = self._data_path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def __enter__(self):
        self._rlock.acquire()
        if self._depth == 0:
            self._cm = process_lock(self._lock_path, exclusive=True)
            self._cm.__enter__()
            try:
                if self._sig() != self._last_sig:
                    self._reload()
                    self._last_sig = self._sig()
            except BaseException:
                self._cm.__exit__(*sys.exc_info())
                self._cm = None
                self._rlock.release()
                raise
        self._depth += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._depth == 1 and exc_type is None:
                self._save()
                self._last_sig = self._sig()
        finally:
            self._depth -= 1
            if self._depth == 0 and self._cm is not None:
                self._cm.__exit__(None, None, None)
                self._cm = None
            self._rlock.release()


def read_json(path: Path, default: Any = None) -> Any:
    with _io_lock:
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except json.JSONDecodeError:
            log.warning("JSON 文件损坏，忽略：%s", path)
            return default


def write_json(path: Path, data: Any) -> None:
    """原子写：先写临时文件再替换，避免写一半断电损坏状态文件。"""
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=1, default=str))


def append_jsonl(path: Path, obj: Any) -> None:
    path = Path(path)
    with _io_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")


def read_jsonl(path: Path, limit: int | None = None) -> list:
    path = Path(path)
    if not path.exists():
        return []
    with _io_lock:
        lines = path.read_text(encoding="utf-8").splitlines()
    if limit:
        lines = lines[-limit:]
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


class RateLimiter:
    """同一数据源两次调用之间至少间隔 min_interval 秒（线程安全）。

    注意：睡眠必须在锁外面。早先的写法把 time.sleep 放在 with self._lock 里面，
    结果同一数据源的调用被彻底串行化（4 线程 × 5 次调用要 11.5 秒，等于没有并发）。
    现在每个调用方在锁里只做两件事：算出自己该等到什么时候、把「下一个可用时刻」往后推，
    然后到锁外面去睡，于是多个线程能按固定间隔排队，而不会互相阻塞。
    """

    def __init__(self, min_interval_ms: float):
        self.min_interval = max(0.0, float(min_interval_ms) / 1000.0)
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + self.min_interval
            delay = slot - now
        if delay > 0:
            time.sleep(delay)


# 板块和 ST 判断只在 qfpy/limits.py 写一份，这里转调（老代码 from qfpy.util import board_of, is_st 照样能用）
def board_of(code: str) -> str:
    from .limits import board_of as _b
    return _b(code)


BOARD_LABELS = {"main": "主板", "gem": "创业板", "star": "科创板", "bse": "北交所"}


def is_st(name: str) -> bool:
    """排雷口径：ST、*ST 或名字带「退」。同 limits.is_risk_name。"""
    from .limits import is_risk_name
    return is_risk_name(name)


def round_buy_qty(code: str, raw_qty: float) -> int:
    """买入整手；科创板不足 200 股不买。"""
    if raw_qty <= 0:
        return 0
    lots = int(raw_qty // 100) * 100
    if board_of(code) == "star" and lots < 200:
        return 0
    return lots


def market_prefix(code: str) -> str:
    if code.startswith(("6", "9")) and not code.startswith("92"):
        return "sh"
    if board_of(code) == "bse":
        return "bj"
    return "sz"
