"""首页「大盘行情 + 股池走势」的数据（后台计算并缓存，网页请求线程从不等待行情接口）。

- 指数：[home].indices 里的指数（默认上证指数、深证成指、创业板指、科创50、沪深300）的点位、涨跌幅和当日分时；
- 市场情绪：涨跌家数、涨停 / 跌停、两市成交额；
- 股池走势：[pool].pools 和牛来策略时段里用到的每个股池 —— 成分数、当日平均涨跌（实时行情），以及两条曲线：
  · 当天：成分股等权平均的分时涨跌曲线（相对昨收；个股分时用腾讯，失败用 akshare，最多取 home.pool_curve_members 只）；
  · 近 N 日：gushi 只能按日期查当天和上一交易日的股池，查不到更早的历史股池，所以用「当前成分股回看近 N 个交易日日K」
    画等权平均的每日涨跌和累计曲线（口径写在 basis 里：不是当时的股池，有幸存者偏差）。
  另外保留每天入池记录（运行记录 + 补录 + gushi 上一交易日股池）的入池当日涨幅和「入池以来」涨跌。

行情按 [market].quote_sources 顺序取，失败自动换下一个源（标准数据接口 qfdp 的 quote / index / index_minute /
market_stats 能力）。股池成分优先用数据源当前的股池，取不到时用最近一次运行记录（含「补录」）里的成分。
"""
from __future__ import annotations

import threading
import time
from typing import Any

from .bg import BgJob
from .dataproto import DEFAULT_INDICES
from .util import log, read_json, today_str, ts

HOME_DEFAULT_INDICES = [c for c, _ in DEFAULT_INDICES[:5]]
INDEX_NAMES = dict(DEFAULT_INDICES)


def _avg(xs: list) -> float | None:
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(sum(xs) / len(xs), 3) if xs else None


def _tmin(t: str) -> int:
    try:
        h, m = str(t)[:5].split(":")
        return int(h) * 60 + int(m)
    except ValueError:
        return -1


def intraday_curve(minutes: list[tuple[dict, float]], keep: int = 121) -> list[dict]:
    """多只股票的分时 → 等权平均涨跌曲线（相对各自昨收，%）。minutes: [(分时, 昨收)]。
    每个时间点对所有已开始交易的股票取平均，某只股票缺某分钟时沿用它上一分钟的值。"""
    series = []
    for mn, pre in minutes:
        if not pre:
            continue
        pts = {}
        for p in mn.get("points") or []:
            t, px = str(p.get("t") or "")[:5], p.get("p")
            if px and 9 * 60 + 25 <= _tmin(t) <= 15 * 60:
                pts[t] = (px / pre - 1) * 100
        if pts:
            series.append(pts)
    if not series:
        return []
    times = sorted({t for sr in series for t in sr}, key=_tmin)
    last: list = [None] * len(series)
    out = []
    for t in times:
        vals = []
        for i, sr in enumerate(series):
            if t in sr:
                last[i] = sr[t]
            if last[i] is not None:
                vals.append(last[i])
        out.append({"t": t, "v": round(sum(vals) / len(vals), 3), "n": len(vals)})
    if len(out) > keep:
        step = len(out) / keep
        out = [out[int(i * step)] for i in range(keep)] + [out[-1]]
    return out


def lookback_curve(bars_by_code: dict[str, list[dict]], days: int, today: str | None = None,
                   today_chg: dict[str, float] | None = None) -> list[dict]:
    """当前成分股回看近 days 个交易日：每天等权平均涨跌（按日K收盘价相对前一天）和累计涨跌。
    today_chg：当天日K还没出时，用实时涨跌补上当天。返回 [{date, avg_pct, cum_pct, up, down, n, live}] 升序。"""
    per_day: dict[str, list[float]] = {}
    for code, bars in bars_by_code.items():
        bars = [b for b in bars if b.get("close")]
        for i in range(1, len(bars)):
            per_day.setdefault(bars[i]["date"], []).append((bars[i]["close"] / bars[i - 1]["close"] - 1) * 100)
    live_day = False
    if today and today_chg and today not in per_day:
        vals = [v for v in today_chg.values() if isinstance(v, (int, float))]
        if vals:
            per_day[today] = vals
            live_day = True
    dates = sorted(per_day)[-days:]
    out, cum = [], 1.0
    for d in dates:
        v = per_day[d]
        avg = sum(v) / len(v)
        cum *= 1 + avg / 100
        out.append({"date": d, "avg_pct": round(avg, 3), "cum_pct": round((cum - 1) * 100, 3), "n": len(v),
                    "up": sum(1 for x in v if x > 0), "down": sum(1 for x in v if x < 0), "live": live_day and d == today})
    return out


def _thin(points: list, keep: int = 241) -> list:
    """分时点只留时间和价格（首页画小图用），超过 keep 个时等距抽样。"""
    pts = [{"t": p.get("t"), "p": p.get("p")} for p in points if p.get("p") is not None]
    if len(pts) <= keep:
        return pts
    step = len(pts) / keep
    return [pts[int(i * step)] for i in range(keep)] + [pts[-1]]


class Overview:
    def __init__(self, engine):
        self.engine = engine
        self.market_job = BgJob("大盘行情", self._market, max_age=self._refresh())
        self.pools_job = BgJob("股池走势", self._pools, max_age=max(30, self._refresh() * 2))
        self._members: dict[str, dict] = {}      # 股池 key -> 最近一次计算的成分明细
        self._focus: str | None = None           # 用户正在看哪个股池（只给它算分时/回看曲线）
        self._lock = threading.Lock()

    def focus(self, key: str) -> None:
        """记下用户正在看的股池。

        分时/回看曲线只有「股池卡片」那块旧版界面在用，现在的首页股池走势只用到成分股表格；
        以前是 7 个池每轮都全算（最多两百多次请求，休市日也每 5 分钟重算一遍），纯浪费，
        还容易把东方财富的接口刷到限流。现在只算当前看的那个池，切池时立刻补一次。
        """
        if key and key != self._focus:
            self._focus = key
            self.pools_job.kick(force=True)

    # ---------- 配置 ----------
    def home_cfg(self) -> dict:
        return self.engine.cfg.get("home") or {}

    def _refresh(self) -> float:
        try:
            return max(5.0, float(self.home_cfg().get("refresh_secs", 15)))
        except (TypeError, ValueError):
            return 30.0

    def index_codes(self) -> list[str]:
        return list(self.home_cfg().get("indices") or HOME_DEFAULT_INDICES)

    def pool_keys(self) -> list[str]:
        cfg = self.engine.cfg
        keys = list(cfg.get("pool", {}).get("pools", []))
        for r in cfg.get("schedule", {}).get("runs", []) or []:
            for p in r.get("pools") or []:
                if p not in keys:
                    keys.append(p)
        return keys

    # ---------- 对外 ----------
    def session(self) -> dict:
        """现在是不是交易时段（交易日 9:15–11:30、13:00–15:00）。交易日判断每天只问一次行情源，问不到按周一到周五算。"""
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone(timedelta(hours=8)))
        day = now.strftime("%Y-%m-%d")
        if getattr(self, "_sess_day", None) != day:
            try:
                v = self.engine.market().is_trading_day(day)
                self._sess_trading = (now.weekday() < 5) if v is None else bool(v)
            except Exception:  # noqa: BLE001
                self._sess_trading = now.weekday() < 5
            self._sess_day = day
        hm = now.strftime("%H:%M")
        if not self._sess_trading:
            return {"state": "holiday", "label": "休市", "trading": False}
        if "09:15" <= hm < "11:30" or "13:00" <= hm < "15:00":
            return {"state": "trading", "label": "交易中", "trading": True}
        if "11:30" <= hm < "13:00":
            return {"state": "noon", "label": "午间休市", "trading": False}
        if hm < "09:15":
            return {"state": "pre", "label": "未开盘", "trading": False}
        return {"state": "closed", "label": "已收盘", "trading": False}

    def view(self, force: bool = False) -> dict:
        r = self._refresh()
        sess = self.session()
        # 交易时段按设置的间隔重算（多个页面同时打开时共用这一份缓存，不会各自去拉行情源）；
        # 非交易时段和休市日 5 分钟才重算一次。股池走势和大盘同一节奏。
        age = r if sess["trading"] else max(300.0, r)
        self.market_job.max_age, self.pools_job.max_age = age, max(age, 15.0)
        self.market_job.kick(force=force)
        self.pools_job.kick(force=force)
        return {"market": self.market_job.result, "market_job": self.market_job.view(),
                "pools": self.pools_job.result, "pools_job": self.pools_job.view(), "refresh_secs": r, "now": ts(),
                "session": sess}

    def pool_detail(self, key: str) -> dict | None:
        with self._lock:
            d = self._members.get(key)
        if d is None:
            self.pools_job.kick()
        return d

    # ---------- 大盘 ----------
    def _market(self) -> dict:
        m = self.engine.market()
        day = today_str()
        codes = self.index_codes()
        qs, src, err = m.indices(codes)
        by = {str(q.get("code", "")).upper(): q for q in qs}
        indices = []
        trade_day = None
        for c in codes:
            q = by.get(c.upper()) or {}
            item: dict[str, Any] = {"code": c, "name": q.get("name") or INDEX_NAMES.get(c, c), "price": q.get("price"),
                                    "preclose": q.get("preclose"), "change": q.get("change"), "change_pct": q.get("change_pct"),
                                    "amount": q.get("amount"), "high": q.get("high"), "low": q.get("low"), "ts": q.get("ts")}
            if item["change_pct"] is None and item["price"] and item["preclose"]:
                item["change_pct"] = round((item["price"] / item["preclose"] - 1) * 100, 4)
            mn, msrc, merr = m.index_minute(c, day)
            if not mn:   # 非交易日 / 盘前：用最近交易日的分时
                ld = m.latest_trading_date()
                if ld and ld != day:
                    mn, msrc, merr = m.index_minute(c, ld)
            if mn:
                item["minute"] = {"date": mn.get("date"), "preclose": mn.get("preclose"), "points": _thin(mn.get("points") or []),
                                  "source": msrc}
                trade_day = trade_day or mn.get("date")
            else:
                item["minute"] = None
                item["minute_error"] = merr
            indices.append(item)
        stats, ssrc, serr = m.market_stats(day)
        if not stats:
            ld = m.latest_trading_date()
            if ld and ld != day:
                stats, ssrc, serr = m.market_stats(ld)
        return {"indices": indices, "index_source": src, "index_error": err if not qs else None,
                "stats": stats, "stats_source": ssrc, "stats_error": None if stats else serr,
                "trading_date": trade_day or (stats or {}).get("date"), "updated_at": ts()}

    # ---------- 股池走势 ----------
    def _recent_runs(self, days: int) -> list[dict]:
        """最近 days 个交易日、每天每个股池最近一次运行记录里的成分（含补录）。返回 [{date, run_id, origin, backfill, pools:{key:[brief]}}]。"""
        eng = self.engine
        out: dict[str, dict] = {}
        for row in eng._run_index():
            d = row.get("trading_date")
            if not d:
                continue
            if d not in out and len(out) >= days:
                continue
            rec = read_json(eng.data_dir / "runs" / f"{row['id']}.json", None)
            if not rec or not rec.get("stages"):
                continue
            day = out.setdefault(d, {"date": d, "runs": [], "pools": {}})
            st0 = rec["stages"][0]
            picks = {p.get("code") for p in rec.get("picks") or [] if isinstance(p, dict)}
            for b in st0.get("kept") or []:
                for k in b.get("sources") or []:
                    lst = day["pools"].setdefault(k, {"run_id": rec["id"], "origin": rec.get("origin"), "backfill": bool(rec.get("backfill")),
                                                      "members": {}})
                    if lst["run_id"] != rec["id"]:
                        continue           # 同一天同一股池只用最新一次运行（索引按时间倒序）
                    lst["members"].setdefault(b["code"], {"code": b["code"], "name": b.get("name") or b["code"],
                                                         "fields": b.get("fields") or {}, "picked": b["code"] in picks})
            day["runs"].append(rec["id"])
        return sorted(out.values(), key=lambda x: x["date"], reverse=True)

    def _pools(self) -> dict:
        eng = self.engine
        m = eng.market()
        days_n = int(self.home_cfg().get("pool_days", 5) or 5)
        keys = self.pool_keys()
        hist = self._recent_runs(days_n)
        srcs = eng.sources()
        errors: list[str] = []
        cur: dict[str, dict] = {}
        for key in keys:
            sname, _, pkey = key.partition(":")
            src = srcs.get(sname)
            item = {"key": key, "name": key, "members": [], "from": None, "trading_date": None, "error": None}
            if src is not None:
                try:
                    pr = src.get_pool(pkey, None)
                    item.update(name=pr.name or key, trading_date=pr.trading_date, **{"from": "数据源"})
                    item["members"] = [{"code": s["code"], "name": s.get("name") or s["code"], "fields": s.get("fields") or {}}
                                       for s in pr.stocks]
                except Exception as e:  # noqa: BLE001
                    item["error"] = f"{type(e).__name__}: {str(e)[:160]}"
            else:
                item["error"] = f"数据源 {sname} 未启用或不存在"
            if src is not None and item["trading_date"] and getattr(src, "type_name", "") == "gushi":
                self._add_prev_pool(m, src, key, pkey, item["trading_date"], hist)
            if not item["members"]:
                for h in hist:          # 退回最近一次运行记录
                    p = h["pools"].get(key)
                    if p and p["members"]:
                        item["members"] = list(p["members"].values())
                        item["trading_date"], item["from"] = h["date"], "运行记录" + ("（补录）" if p["backfill"] else "")
                        break
            cur[key] = item
        # 一次批量拉实时行情：当前成分 + 历史各天成分
        codes: list[str] = []
        for it in cur.values():
            codes += [x["code"] for x in it["members"]]
        for h in hist:
            for p in h["pools"].values():
                if isinstance(p, dict):
                    codes += list(p["members"])
        codes = list(dict.fromkeys(c for c in codes if c and c.isdigit()))
        quotes: dict = {}
        qsrc = None
        for i in range(0, len(codes), 150):
            q, s, err = m.quotes(codes[i:i + 150])
            quotes.update(q or {})
            qsrc = qsrc or s
            if err and not q:
                errors.append(f"实时行情：{err}")
                break
        today = today_str()
        try:
            curve_day = m.latest_trading_date() or today
        except Exception:  # noqa: BLE001
            curve_day = today
        max_mem = int(self.home_cfg().get("pool_curve_members", 30) or 30)
        focus = self._focus or (keys[0] if keys else None)
        out = []
        details = {}
        for key, it in cur.items():
            mem = []
            for x in it["members"]:
                q = quotes.get(x["code"]) or {}
                chg = q.get("change_pct")
                if chg is None:
                    chg = (x.get("fields") or {}).get("change_rate")
                mem.append({"code": x["code"], "name": q.get("name") or x["name"], "price": q.get("price") or (x.get("fields") or {}).get("price"),
                            "change_pct": chg, "entry_change": (x.get("fields") or {}).get("change_rate"),
                            "amount": q.get("amount"), "live": bool(q), "preclose": q.get("preclose")})
            chs = [x["change_pct"] for x in mem]
            series = []
            for h in hist:
                p = h["pools"].get(key)
                if not p or not p["members"]:
                    continue
                ent, since = [], []
                for c, b in p["members"].items():
                    f = b.get("fields") or {}
                    if isinstance(f.get("change_rate"), (int, float)):
                        ent.append(f["change_rate"])
                    px0, q = f.get("price"), quotes.get(c) or {}
                    if px0 and q.get("price"):
                        since.append((q["price"] / px0 - 1) * 100)
                series.append({"date": h["date"], "count": len(p["members"]), "avg_entry_change": _avg(ent),
                               "avg_since_entry": _avg(since), "picked": [b["code"] + " " + b["name"] for b in p["members"].values() if b.get("picked")],
                               "backfill": p["backfill"], "origin": p["origin"], "run_id": p["run_id"]})
            curves = self._curves(m, mem[:max_mem], days_n, curve_day, today) if key == focus else {}
            row = {"key": key, "name": it["name"], "count": len(mem), "trading_date": it["trading_date"], "from": it["from"],
                   "avg_change": _avg(chs), "up": sum(1 for c in chs if isinstance(c, (int, float)) and c > 0),
                   "down": sum(1 for c in chs if isinstance(c, (int, float)) and c < 0),
                   "best": max((x for x in mem if isinstance(x["change_pct"], (int, float))), key=lambda x: x["change_pct"], default=None),
                   "series": sorted(series, key=lambda s: s["date"]), "error": it["error"] if not mem else None, **curves}
            out.append(row)
            details[key] = {**{k: v for k, v in row.items() if k != "best"},
                            "members": sorted(mem, key=lambda x: -(x["change_pct"] if isinstance(x["change_pct"], (int, float)) else -1e9))}
        with self._lock:
            self._members = details
        return {"pools": out, "quote_source": qsrc, "errors": errors, "days": days_n, "updated_at": ts()}

    def _add_prev_pool(self, m, src, key: str, pkey: str, day: str, hist: list[dict]) -> None:
        """gushi 能查上一交易日的股池：运行记录里没有那一天时补进入池记录（标「数据源（上一交易日）」）。"""
        from datetime import date as _d, timedelta
        try:
            d0 = _d.fromisoformat(day)
        except ValueError:
            return
        for i in range(1, 5):
            d = (d0 - timedelta(days=i))
            if d.weekday() >= 5:
                continue
            ds = d.isoformat()
            try:
                if m.is_trading_day(ds) is False:
                    continue
            except Exception:  # noqa: BLE001
                pass
            if any(h["date"] == ds and key in h["pools"] for h in hist):
                return
            try:
                pr = src.get_pool(pkey, ds)
            except Exception:  # noqa: BLE001
                return
            if not pr.stocks or (pr.trading_date and pr.trading_date != ds):
                return
            h = next((h for h in hist if h["date"] == ds), None)
            if h is None:
                h = {"date": ds, "runs": [], "pools": {}}
                hist.append(h)
            h["pools"][key] = {"run_id": None, "origin": "数据源（上一交易日）", "backfill": False,
                               "members": {s_["code"]: {"code": s_["code"], "name": s_.get("name") or s_["code"],
                                                         "fields": s_.get("fields") or {}, "picked": False} for s_ in pr.stocks}}
            return

    def _curves(self, m, mem: list[dict], days: int, curve_day: str, today: str) -> dict:
        """一个股池的两条曲线：当天等权分时 + 当前成分回看近 N 日日K。取不到时给出原因。

        逐只取分时/日K 全是网络等待，串行跑 30 只要一分钟以上；这里用线程池并发跑，
        真正的节流交给数据源自己的限速器（RateLimiter 已改成睡在锁外，能按间隔排队而不互相阻塞）。
        """
        from concurrent.futures import ThreadPoolExecutor

        mins, msrc, merr = [], None, None
        fn = getattr(m, "stock_minute", None)
        dfn = getattr(m, "daily", None)
        bars_by, derr = {}, None
        workers = max(1, int(self.home_cfg().get("pool_curve_workers", 3) or 3))

        def one_min(x):
            try:
                return fn(x["code"], curve_day)
            except Exception as ex:  # noqa: BLE001
                return None, None, str(ex)[:120]

        def one_daily(x):
            # 用 curve_day 而不是 today：休市日 today 没有日K，拿它当终点会让缓存永远"不新鲜"，
            # 每次刷新都重拉一遍 30 只票的日K。换成最近交易日，历史日期的缓存就是永久有效的。
            try:
                return dfn(x["code"], curve_day)
            except Exception as ex:  # noqa: BLE001
                return [], str(ex)[:120]

        with ThreadPoolExecutor(max_workers=workers) as ex:
            if fn is None:
                merr = "行情源不支持个股分时"
            else:
                for x, (d, src, e) in zip(mem, ex.map(one_min, mem)):
                    if d:
                        pre = d.get("preclose") or (x.get("preclose") if curve_day == today else None)
                        if not pre and x.get("price") and isinstance(x.get("change_pct"), (int, float)) and curve_day == today:
                            pre = x["price"] / (1 + x["change_pct"] / 100)
                        mins.append((d, pre))
                        msrc = msrc or src
                    else:
                        merr = merr or e
            if dfn is None:
                derr = "行情源不支持日K"
            else:
                for x, (bars, e) in zip(mem, ex.map(one_daily, mem)):
                    if bars:
                        bars_by[x["code"]] = bars[-(days + 1):]
                    else:
                        derr = derr or e
        intraday = intraday_curve(mins)
        live_chg = {x["code"]: x["change_pct"] for x in mem if x.get("live")}
        lookback = lookback_curve(bars_by, days, today, live_chg)
        n = len(mem)
        return {
            "intraday": {"date": curve_day, "points": intraday, "members": len(mins), "of": n, "source": msrc,
                         "error": None if intraday else (merr or "没有分时数据"),
                         "basis": f"{len(mins)} 只成分股的分时等权平均，相对各自昨收"},
            "lookback": {"days": lookback, "members": len(bars_by), "of": n, "error": None if lookback else (derr or "没有日K数据"),
                         "basis": f"当前 {len(bars_by)} 只成分股回看近 {days} 个交易日日K，等权平均；gushi 只能查当天和上一交易日的股池，"
                                  "所以不是当时的股池，有幸存者偏差"},
        }
