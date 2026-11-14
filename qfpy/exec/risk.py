"""风控：对所有模式生效（与 Rust 版 exec/risk.rs 同口径，增加卖出检查）。

买入和卖出都检查：一键停止、代码格式、数字限价/整数股数、涨跌停价范围、偏离参考价、交易时段。
只管买入（卖出不受限）：白名单、黑名单、单笔上限、每日累计金额、整手（科创板 ≥200）、同一只票每天只买一次。
  止损/止盈/到点卖出这些卖单不能因为「今天已经买满了」被拦下，否则该卖的卖不掉。
卖出另查：不超过本地记录的 T+1 可卖数量；零股只能一次卖完。
不再限制每日委托笔数：risk.max_daily_orders 这个配置项还认（旧配置不报错），但不再生效。
"""
from __future__ import annotations

import math
from pathlib import Path

from ..util import board_of, in_sessions, lock_for, now_cn, process_lock, read_json, write_json


class Risk:
    def __init__(self, cfg_getter, data_dir: Path, switches):
        self.cfg_getter = cfg_getter
        self.dir = Path(data_dir) / "risk"
        self.switches = switches

    def _day(self, d: str) -> dict:
        u = read_json(self.dir / f"{d}.json", None) or {}
        return {"orders": 0, "amount": 0.0, "bought": [], "buy_orders": 0, "sell_orders": 0, "sell_amount": 0.0, **u}

    def usage(self, d: str) -> dict:
        r = self.cfg_getter()["risk"]
        u = self._day(d)
        # max_daily_orders 已停用（不限笔数），这里照旧返回配置值只为兼容老页面/老脚本
        return {**u, "max_daily_orders": r.get("max_daily_orders"), "daily_orders_limit": None,
                "max_daily_amount": r["max_daily_amount"], "max_order_amount": r["max_order_amount"]}

    def check(self, it: dict, mode: str, day: str) -> list[tuple[str, bool, str]]:
        """返回 [(检查项, 是否通过, 说明)]。不写入任何状态。"""
        cfg = self.cfg_getter()
        r = cfg["risk"]
        side, code = it["side"], str(it["symbol"])
        price, qty = it["price"], it["quantity"]
        out: list[tuple[str, bool, str]] = []

        def add(name, ok, note):
            out.append((name, bool(ok), note))

        h = {} if it.get("manual") else self.switches.halt_state()     # 手动下单是你亲自操作，不受停止状态限制
        add("一键停止", not h.get("halted"), f"已停止：{h.get('reason', '')}（{h.get('at', '')}）" if h.get("halted") else "未停止")
        add("代码格式", len(code) == 6 and code.isdigit(), code)
        wl, bl = [str(x) for x in r.get("whitelist") or []], [str(x) for x in r.get("blacklist") or []]
        if side == "buy":
            add("白名单", not wl or code in wl, "不在白名单" if wl and code not in wl else "通过")
            add("黑名单", code not in bl, "在黑名单" if code in bl else "通过")
        okp = isinstance(price, (int, float)) and math.isfinite(price) and price > 0 and round(price, 2) == round(price, 4)
        add("数字限价", okp, f"{price}")
        okq = isinstance(qty, int) and qty > 0
        add("整数股数", okq, f"{qty}")
        if not (okp and okq):
            return out
        amount = price * qty
        if side == "buy":
            lot_ok = qty % 100 == 0 and (board_of(code) != "star" or qty >= 200)
            add("整手", lot_ok, f"{qty} 股" + ("（科创板至少 200 股）" if board_of(code) == "star" else ""))
        else:
            sellable = int(it.get("sellable", 0))
            add("可卖数量", qty <= sellable, f"卖 {qty} / 本地记录可卖 {sellable}（T+1）")
            add("零股", qty % 100 == 0 or qty == sellable, "零股须一次卖完" if qty % 100 and qty != sellable else "通过")
        u = self._day(day)
        if side == "buy":     # 买入上限只管买入；卖单（止损、止盈、到点卖出、手动卖出）不受这两项限制
            add("单笔上限", amount <= r["max_order_amount"], f"{amount:.0f} / {r['max_order_amount']} 元")
            add("每日金额", u["amount"] + amount <= r["max_daily_amount"], f"今日已买 {u['amount']:.0f} + {amount:.0f} / 上限 {r['max_daily_amount']}")
        if side == "buy" and r.get("one_buy_per_symbol_per_day", True):
            add("同票每日一次", code not in u["bought"], "今天已买过" if code in u["bought"] else "通过")
        up, dn = it.get("limit_up"), it.get("limit_down")
        if up and dn:
            add("涨跌停范围", dn - 1e-9 <= price <= up + 1e-9, f"{price:.2f} 在 [{dn:.2f}, {up:.2f}]")
        elif it.get("no_limit"):
            add("涨跌停范围", True, "上市初期无涨跌幅限制")
        else:
            add("涨跌停范围", False, "缺少昨收，算不出涨跌停价")
        ref = it.get("ref_price") or 0
        if ref > 0:
            dev = abs(price / ref - 1) * 100
            add("偏离参考价", dev <= r["max_price_deviation_pct"], f"{dev:.2f}% / 上限 {r['max_price_deviation_pct']}%")
        else:
            add("偏离参考价", False, "没有参考价")
        need_session = mode == "live" or (r.get("enforce_session", True) and not (mode == "paper" and cfg["paper"].get("ignore_session")))
        if need_session:
            now = now_cn()
            trading = it.get("is_trading_day")
            ok = trading is True and in_sessions(now, r.get("sessions") or [])
            note = f"{now:%H:%M:%S}" + ("" if trading is True else ("，非交易日" if trading is False else "，无法确认今天是交易日"))
            add("交易时段", ok, note + ("" if ok else "，不在交易时段"))
            if mode == "live" and it.get("trading_date") and it.get("trading_date") != now.date().isoformat():
                add("实盘用当天数据", False, f"数据日期 {it.get('trading_date')} 不是今天")
        return out

    @staticmethod
    def first_fail(checks) -> str | None:
        for name, ok, note in checks:
            if not ok:
                return f"{name}：{note}"
        return None

    def record(self, it: dict, day: str) -> None:
        p = self.dir / f"{day}.json"
        with process_lock(lock_for(p)):
            u = self._day(day)
            u["orders"] += 1                       # 只做统计，不再限制笔数
            amt = it["price"] * it["quantity"]
            if it["side"] == "buy":
                u["buy_orders"] += 1
                u["amount"] = round(u["amount"] + amt, 2)     # 「每日金额」只累计买入
                if it["symbol"] not in u["bought"]:
                    u["bought"].append(it["symbol"])
            else:
                u["sell_orders"] += 1
                u["sell_amount"] = round(u["sell_amount"] + amt, 2)
            write_json(p, u)
