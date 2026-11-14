"""公开行情接口的小工具（新浪 hq 实时行情、东方财富 trends2 分时）。供 akshare 数据源和示例提供方使用。

这些都是网页公开接口，没有服务保证；调用方负责限速和兜底。返回值已经是标准数据接口（qfdp）的格式。
"""
from __future__ import annotations

import time
from datetime import datetime

import httpx


UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _sina_sym(std: str) -> str:
    d, ex = std.split(".")
    return ex.lower() + d


def parse_sina(text: str, std_codes: list[str], index: bool = False) -> list[dict]:
    """解析新浪 hq 返回的 var hq_str_sh600000="..." 文本。"""
    by_sym = {}
    for line in text.splitlines():
        if "hq_str_" not in line or '"' not in line:
            continue
        sym = line.split("hq_str_", 1)[1].split("=", 1)[0].strip()
        body = line.split('"', 2)[1]
        if body:
            by_sym[sym] = body.split(",")
    out = []
    for std in std_codes:
        f = by_sym.get(_sina_sym(std))
        if not f or len(f) < 10:
            continue
        try:
            op, pre, px, hi, lo = (float(f[i]) for i in (1, 2, 3, 4, 5))
            vol, amt = float(f[8]), float(f[9])
        except ValueError:
            continue
        if px <= 0:          # 停牌或未开盘：最新价为 0，用昨收
            px = pre
        ts = None
        dt = next((x for x in f[28:34] if len(x) == 10 and x[4] == "-"), None)
        tm = next((x for x in f[29:35] if len(x) == 8 and x[2] == ":"), None)
        if dt and tm:
            try:
                ts = int(datetime.fromisoformat(f"{dt}T{tm}+08:00").timestamp() * 1000)
            except ValueError:
                ts = None
        # 新浪的上证指数成交量单位是手，其余是股
        q = {"code": std, "name": f[0], "price": px, "preclose": pre, "open": op or None, "high": hi or None, "low": lo or None,
             "change": round(px - pre, 4) if pre else None, "change_pct": round((px - pre) / pre * 100, 4) if pre else None,
             "volume": vol * 100 if (index and std.endswith(".SH")) else vol, "amount": amt, "ts": ts or int(time.time() * 1000)}
        out.append(q)
    return out


def sina_quotes(std_codes: list[str], index: bool = False, timeout: float = 8.0) -> list[dict]:
    if not std_codes:
        return []
    out: list[dict] = []
    for i in range(0, len(std_codes), 100):
        part = std_codes[i:i + 100]
        r = httpx.get("https://hq.sinajs.cn/list=" + ",".join(_sina_sym(c) for c in part),
                      headers={**UA, "Referer": "https://finance.sina.com.cn"}, timeout=timeout)
        r.raise_for_status()
        out += parse_sina(r.content.decode("gbk", "replace"), part, index=index)
    if not out:
        raise RuntimeError("新浪行情没有返回数据")
    return out


def _secid(std: str) -> str:
    d, ex = std.split(".")
    return ("1." if ex == "SH" else "0.") + d


def parse_trends(data: dict, std: str, date: str) -> dict:
    trends = data.get("trends") or []
    pts = []
    for row in trends:
        p = row.split(",")
        if len(p) < 8 or not p[0].startswith(date):
            continue
        t = p[0][11:16]
        if t < "09:30":           # 9:15~9:25 集合竞价段不画
            continue
        try:
            pts.append({"t": t, "p": float(p[2]), "avg": float(p[7]) or None, "vol": float(p[5]) * 100, "amt": float(p[6])})
        except ValueError:
            continue
    if not pts:
        raise RuntimeError(f"东方财富没有 {std} {date} 的分时（非交易日或还没开盘）")
    return {"code": std, "date": date, "preclose": data.get("preClose") or data.get("prePrice"), "open": pts[0]["p"],
            "high": max(x["p"] for x in pts), "low": min(x["p"] for x in pts), "points": pts}


def em_trends(std: str, date: str, timeout: float = 10.0) -> dict:
    """东方财富分时（只能拿最近 5 个交易日）。"""
    r = httpx.get("https://push2his.eastmoney.com/api/qt/stock/trends2/get",
                  params={"secid": _secid(std), "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
                          "fields2": "f51,f52,f53,f54,f55,f56,f57,f58", "ndays": 5, "iscr": 0},
                  headers={**UA, "Referer": "https://quote.eastmoney.com/"}, timeout=timeout)
    r.raise_for_status()
    data = (r.json() or {}).get("data") or {}
    if not data:
        raise RuntimeError(f"东方财富没有 {std} 的分时")
    d = parse_trends(data, std, date)
    # ndays=5 时 preClose 是 5 天前的昨收；当天的昨收取前一交易日最后一个点
    prev = [x for x in (data.get("trends") or []) if x[:10] < date]
    if prev:
        try:
            d["preclose"] = float(prev[-1].split(",")[2])
        except (ValueError, IndexError):
            pass
    return d


def parse_qq_minute(j: dict, std: str, date: str) -> dict:
    """腾讯分时（web.ifzq.gtimg.cn，只有最近一个交易日）。成交量/额是累计值，这里换成每分钟。"""
    sym = std.split(".")[1].lower() + std.split(".")[0]
    d = ((j or {}).get("data") or {}).get(sym) or {}
    body = d.get("data") or {}
    day = str(body.get("date") or "")
    day = f"{day[:4]}-{day[4:6]}-{day[6:8]}" if len(day) == 8 else ""
    if day != date:
        raise RuntimeError(f"腾讯分时只有 {day or '未知日期'} 的数据，没有 {date} 的")
    pts, pv, pa = [], 0.0, 0.0
    for row in body.get("data") or []:
        p = str(row).split()
        if len(p) < 4:
            continue
        try:
            v, a = float(p[2]), float(p[3])
            pts.append({"t": f"{p[0][:2]}:{p[0][2:4]}", "p": float(p[1]), "avg": None, "vol": max(0.0, v - pv) * 100, "amt": max(0.0, a - pa)})
            pv, pa = v, a
        except ValueError:
            continue
    if not pts:
        raise RuntimeError(f"腾讯没有 {std} {date} 的分时")
    pre = None
    try:
        pre = float((d.get("qt") or {}).get(sym, [])[4])
    except (ValueError, IndexError, TypeError):
        pass
    return {"code": std, "date": date, "preclose": pre, "open": pts[0]["p"], "high": max(x["p"] for x in pts),
            "low": min(x["p"] for x in pts), "points": pts}


def qq_minute(std: str, date: str, timeout: float = 10.0) -> dict:
    sym = std.split(".")[1].lower() + std.split(".")[0]
    r = httpx.get("https://web.ifzq.gtimg.cn/appstock/app/minute/query", params={"code": sym}, headers=UA, timeout=timeout)
    r.raise_for_status()
    return parse_qq_minute(r.json(), std, date)
