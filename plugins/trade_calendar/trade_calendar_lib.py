"""交易日历公共库（独立插件 trade_calendar）。

职责：**只负责生产和维护一份公共数据文件**，不往主程序注册任何东西、不改
`Market.is_trading_day()`、不改调度器。别的插件要用，运行时 `import trade_calendar_lib`
即可（`plugin.py` 已把本目录加进 sys.path）；也可以直接按下面的契约读 JSON 文件。

数据文件：`data/cache/trade_calendar.json`

    {"fetched_at": "2026-11-14 16:20:00", "source": "sina:klc_td_sh",
     "min_year": 2023, "dates": ["2023-01-03", "...", "2026-12-31"]}

`dates` 是升序的 YYYY-MM-DD 交易日列表，**含未来日期**（这是盘前/凌晨能判准
「今天是不是交易日」的关键）。查询日期超出 `[dates[0], dates[-1]]` 时一律返回
None，意思是「判不出来」，调用方自己决定是跳过还是走兜底。

数据来源：新浪财经静态文件 `https://finance.sina.com.cn/realstock/company/klc_td_sh.txt`，
经 akshare 的 `tool_trade_date_hist_sina()` 解码（原文件是压缩混淆过的 JS 字符串，
必须走 V8 解码，所以这里依赖可选的 akshare）。akshare 没装或断网时，已有的日历
文件照常可读，只是不会自动刷新。

注意：新浪这份日历只公布到**当年年底**，跨年后 `stale()` 会自动判定过期并重拉。
"""
from __future__ import annotations

import time
import tomllib
from datetime import date
from pathlib import Path

from qfpy.util import log, read_json, write_json

MIN_YEAR = 2023
REFRESH_DAYS = 30
FILE = "cache/trade_calendar.json"

# 进程内缓存：{文件绝对路径: ((mtime, 大小), 文档)}。判交易日调用很频繁，别每次打盘。
# 带上文件大小：同一秒内重写（mtime 精度不够）时也能识别出内容变了。
_CACHE: dict[str, tuple[tuple[float, int], dict]] = {}


def resolve_data_dir(root: Path) -> Path:
    """按 config/pipeline.toml 的 data_dir 解析数据目录；没配就用 root/data。"""
    root = Path(root)
    try:
        cfg = tomllib.loads((root / "config" / "pipeline.toml").read_text(encoding="utf-8"))
        d = Path(str(cfg.get("data_dir") or "data"))
    except Exception:  # noqa: BLE001  配置读不到不该影响日历
        d = Path("data")
    return d if d.is_absolute() else root / d


def path_of(data_dir) -> Path:
    return Path(data_dir) / FILE


def load(data_dir) -> dict:
    """读日历文档；文件不存在或格式不对时返回 {}。"""
    p = path_of(data_dir)
    try:
        st = p.stat()
    except OSError:
        return {}
    stamp = (st.st_mtime, st.st_size)
    hit = _CACHE.get(str(p))
    if hit and hit[0] == stamp:
        return hit[1]
    doc = read_json(p, {}) or {}
    if not isinstance(doc, dict) or not isinstance(doc.get("dates"), list):
        doc = {}
    _CACHE[str(p)] = (stamp, doc)
    return doc


def dates(data_dir) -> list[str]:
    return [str(d) for d in (load(data_dir).get("dates") or [])]


def range_of(data_dir) -> tuple[str, str] | None:
    ds = dates(data_dir)
    return (ds[0], ds[-1]) if ds else None


def is_trading_day(data_dir, d: str | None = None) -> bool | None:
    """True/False；日期超出日历覆盖范围或没有日历时返回 None（判不出来）。"""
    d = str(d or date.today().isoformat())
    ds = dates(data_dir)
    if not ds or d < ds[0] or d > ds[-1]:
        return None
    return d in set(ds)


def prev_trading_day(data_dir, d: str) -> str | None:
    """d 之前的最近一个交易日（不含 d）。含节假日，不用「只跳周末」的粗算。"""
    prev = [x for x in dates(data_dir) if x < str(d)]
    return prev[-1] if prev else None


def next_trading_day(data_dir, d: str) -> str | None:
    """d 之后的最近一个交易日（不含 d）。"""
    for x in dates(data_dir):
        if x > str(d):
            return x
    return None


def stale(data_dir, max_age_days: int = REFRESH_DAYS) -> bool:
    """该不该重拉：没文件、太久没更新、或者覆盖不到今年年底（跨年了）。"""
    doc = load(data_dir)
    ds = doc.get("dates") or []
    if not ds:
        return True
    try:
        fetched = time.mktime(time.strptime(str(doc.get("fetched_at", "")), "%Y-%m-%d %H:%M:%S"))
    except (ValueError, OverflowError):
        return True
    if time.time() - fetched > max_age_days * 86400:
        return True
    return str(ds[-1]) < f"{date.today().year}-12-31"


def refresh(data_dir, min_year: int = MIN_YEAR) -> dict:
    """联网拉一次并写盘。akshare 没装或网络失败时抛 RuntimeError，绝不写坏文件。"""
    try:
        import akshare as ak
    except ImportError as e:  # akshare 是可选依赖
        raise RuntimeError("未安装 akshare，拉不了交易日历（pip install akshare）") from e
    df = ak.tool_trade_date_hist_sina()
    ds = sorted({str(x) for x in df["trade_date"].astype(str)})
    ds = [x for x in ds if x >= f"{int(min_year)}-01-01"]
    if len(ds) < 200:
        raise RuntimeError(f"交易日历条数异常（只有 {len(ds)} 条），没有写盘")
    doc = {"fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"), "source": "sina:klc_td_sh",
           "min_year": int(min_year), "dates": ds}
    p = path_of(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    write_json(p, doc)
    log.info("交易日历已更新：%d 个交易日（%s → %s）", len(ds), ds[0], ds[-1])
    return doc


def ensure(data_dir, min_year: int = MIN_YEAR, max_age_days: int = REFRESH_DAYS) -> dict:
    """需要时刷新；刷新失败只记日志并返回现有内容（可能是空 {}）。绝不抛错。"""
    if not stale(data_dir, max_age_days):
        return load(data_dir)
    try:
        return refresh(data_dir, min_year)
    except Exception as e:  # noqa: BLE001  日历拿不到不该挡住任何流程
        log.warning("交易日历刷新失败（继续用现有的）：%s", e)
        return load(data_dir)


def main(argv: list[str] | None = None) -> int:
    """命令行：python plugins/trade_calendar/trade_calendar_lib.py [--data-dir data] [--refresh]"""
    import argparse

    ap = argparse.ArgumentParser(description="交易日历（拉取/查看）")
    ap.add_argument("--data-dir", default="")
    ap.add_argument("--refresh", action="store_true", help="强制重拉（默认只在缺失/过期时拉）")
    a = ap.parse_args(argv)
    data_dir = Path(a.data_dir) if a.data_dir else resolve_data_dir(Path(__file__).resolve().parents[2])
    doc = refresh(data_dir) if a.refresh else ensure(data_dir)
    ds = doc.get("dates") or []
    if not ds:
        print("没有交易日历（akshare 未安装或网络不可用）")
        return 1
    print(f"交易日 {len(ds)} 个：{ds[0]} → {ds[-1]}")
    print(f"拉取时间：{doc.get('fetched_at', '未知')}  来源：{doc.get('source', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
