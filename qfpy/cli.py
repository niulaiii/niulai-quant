"""命令行入口。用法：python -m qfpy <命令> [参数]。运行 python -m qfpy -h 查看全部命令。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .util import log, setup_logging

DEFAULT_CONFIG = os.environ.get("QF_CONFIG", "config/pipeline.toml")


def _engine(args):
    from .config import ConfigStore
    from .engine import Engine
    store = ConfigStore(Path(args.config))
    setup_logging(store.data_dir)
    return Engine(store, offline=getattr(args, "offline", False))


def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


def cmd_run(args):
    e = _engine(args)
    r = e.run(date=args.date, pools=args.pools.split(",") if args.pools else None, trade=not args.no_trade, origin="命令行")
    print(f"运行 {r['id']}  数据日期 {r.get('trading_date')}  模式 {r['mode']}  自动交易 {'开' if r['auto_trade'] else '关'}")
    for w in r["warnings"]:
        print("  ⚠", w)
    for p in r["pools"]:
        print(f"  股池 {p['key']:<24} {p['count']:>3} 只 {p['error'] or ''}")
    print("  漏斗：" + " → ".join(f"{s['label']}({s['input']})" for s in r["stages"]) + f" → 入选 {len(r['picks'])}")
    for p in r["picks"]:
        print(f"  入选：{p['code']} {p['name']} 分 {p['value']}")
    for x in r["exits"]:
        print(f"  退出检查：{x['symbol']} {x['name']} {x['reason']} → {x.get('action') or '-'} {x.get('note', '')}")
    for o in r["orders"]:
        print(f"  委托：{o['side']} {o['symbol']} {o.get('quantity')}@{o.get('price')} → {o.get('status_label', o.get('status'))} {o.get('message', '')}")
    for er in r["errors"]:
        print("  错误：", er)
    e.notifier.flush()
    return 1 if r["errors"] else 0


def cmd_serve(args):
    import uvicorn
    from .scheduler import Scheduler
    from .web.server import create_app
    e = _engine(args)
    cfg = e.cfg
    # 启动兜底：上次会话删掉的演示持仓，临时缓存可能还留着；后台跑，不挡启动
    import threading
    from .cacheclean import prune_for_engine
    threading.Thread(target=lambda: prune_for_engine(e), name="cache-prune", daemon=True).start()
    sch = Scheduler(e)
    host = args.host or cfg["web"]["host"]
    port = args.port or int(cfg["web"]["port"])
    if host not in ("127.0.0.1", "localhost", "::1"):
        # 网页没有登录鉴权，还能切自动交易开关：只允许监听本机，远程用 SSH 端口转发访问
        print(f"启动失败：网页只允许监听 127.0.0.1（当前 {host}）。远程访问请用 ssh -N -L {port}:127.0.0.1:{port} 用户@主机", file=sys.stderr)
        return 2
    if not args.no_schedule:
        sch.start()
        e.link.start(e.in_trading_session)      # 后台：下单程序连接状态 + 券商快照 + 实盘对账
    log.info("网页：http://%s:%s  模式：%s  自动交易：%s", host, port, e.broker.label, "开" if e.switches.auto_enabled() else "关")
    uvicorn.run(create_app(e, None if args.no_schedule else sch), host=host, port=port, log_level="warning")


def cmd_auto(args):
    e = _engine(args)
    if args.state == "status":
        _print(e.switches.auto_state())
    else:
        _print(e.set_auto(args.state == "on", by="命令行"))
        e.notifier.flush()


def cmd_halt(args):
    e = _engine(args)
    _print(e.halt(args.reason, by="命令行"))
    e.notifier.flush()


def cmd_resume(args):
    e = _engine(args)
    _print(e.resume(by="命令行"))
    e.notifier.flush()


def cmd_exits(args):
    e = _engine(args)
    if args.remind:
        _print(e.remind_exits(args.remind))
    else:
        r = e.check_exits(origin="命令行", now_hm=args.now)
        for x in r["results"]:
            print(f"{x['symbol']} {x['name']} 持有 {x['held_days']} 天 浮盈 {x.get('pnl_pct')}% | {x['reason']} → {x.get('action') or '-'} {x.get('note', '')}")
        if not r["results"]:
            print("没有持仓")
    e.notifier.flush()


def cmd_hold(args):
    e = _engine(args)
    p = e.set_hold(args.code, not args.cancel, args.note or "", by="命令行", mode=getattr(args, "mode", None))
    print(f"{p['symbol']} {p['name']} 保留标记：{p['hold']}")


def cmd_positions(args):
    e = _engine(args)
    a = e.account()
    _print({"summary": a["summary"], "positions": a["positions"]})


def cmd_broker_status(args):
    e = _engine(args)
    e.link.check_status(force=True)   # 命令行里同步等结果
    v = e.link.view()
    _print({"base_url": v["base_url"], "auth": v["auth"], "conn": v["conn"]})


def cmd_broker_snapshot(args):
    """只读：读一次券商快照（持仓/委托/成交）。实盘模式下顺便对账（只改本地账本，不发委托）。"""
    from .exec.xiadan import XiadanError
    e = _engine(args)
    try:
        rep = e.link.reconcile_now("fresh" if args.fresh else "cached")
    except XiadanError as ex:
        _print({"ok": False, "error": ex.to_dict()})
        return 1
    snap = e.link.snapshot or {}
    _print({"money": snap.get("money"), "positions": snap.get("positions"), "orders": snap.get("orders"),
            "deals": snap.get("deals"), "reconcile": rep})


def cmd_notify_test(args):
    e = _engine(args)
    _print(e.notifier.send_sync("test", "测试消息", "- 这是牛来量化的测试推送", only=args.channel))


def cmd_backtest(args):
    from .backtest import run_backtest
    e = _engine(args)
    r = run_backtest(e, args.start, args.end, args.codes.split(",") if args.codes else None)
    s = r["summary"]
    print(f"回测 {r['start']} ~ {r['end']}，{len(r['codes'])} 只股票")
    print(f"  总收益 {s['total_return_pct']}%  最大回撤 {s['max_drawdown_pct']}%  胜率 {s['win_rate']}%（{s['win_count']}/{s['closed_count']}）")
    print(f"  使用因子：{'；'.join(r['factors_used']) or '无'}")
    print(f"  跳过因子：{'；'.join(x['label'] + '(' + x['reason'] + ')' for x in r['factors_skipped']) or '无'}")
    for c in r["closed"][-10:]:
        print(f"  {c['symbol']} {c['buy_date']}→{c['sell_date']} {c['reason']} {c['pnl']:+.2f}")
    if args.json:
        Path(args.json).write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")


def cmd_check_config(args):
    from .config import ConfigStore, validate
    from .funnel import config_problems
    from .plugins import load_plugins
    store = ConfigStore(Path(args.config))
    load_plugins(store.root / "plugins")
    warns = validate(store.get())
    errs = config_problems(store.get(), store.root)
    for w in warns:
        print("警告：", w)
    for e in errs:
        print("错误：", e)
    print("配置正常" if not errs else "配置有错误")
    return 1 if errs else 0


def cmd_cache_prune(args):
    """回收已无持仓引用的演示/临时分时缓存（只删带 demo 标记的，正常行情缓存不动）。"""
    from .cacheclean import prune_for_engine
    r = prune_for_engine(_engine(args), dry_run=bool(args.dry_run))
    _print(r)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m qfpy", description="牛来量化：选股 → 因子过滤 → 交易")
    p.add_argument("-c", "--config", default=DEFAULT_CONFIG, help="配置文件（默认 config/pipeline.toml）")
    p.add_argument("--offline", action="store_true", help="只用本地缓存，不请求任何行情接口")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("run", help="跑一次完整链路（选股 → 漏斗 → 退出检查 → 买入）")
    s.add_argument("--date", help="数据日期 YYYY-MM-DD（默认最新交易日）")
    s.add_argument("--pools", help="只用这些股池，逗号分隔")
    s.add_argument("--no-trade", action="store_true", help="只选股，不生成委托")
    s.set_defaults(fn=cmd_run)
    s = sub.add_parser("serve", help="启动网页 + 定时任务")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--no-schedule", action="store_true", help="不启动定时任务")
    s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("auto", help="自动交易开关：on / off / status")
    s.add_argument("state", choices=["on", "off", "status"])
    s.set_defaults(fn=cmd_auto)
    s = sub.add_parser("halt", help="一键停止（拒绝所有新委托）")
    s.add_argument("--reason", default="命令行一键停止")
    s.set_defaults(fn=cmd_halt)
    s = sub.add_parser("resume", help="解除一键停止")
    s.set_defaults(fn=cmd_resume)
    s = sub.add_parser("exits", help="立即检查退出（按规则卖出），或 --remind HH:MM 发退出前提醒")
    s.add_argument("--now", help="按这个时刻判断到期退出（HH:MM，调试用）")
    s.add_argument("--remind", help="发送该退出时间的提醒")
    s.set_defaults(fn=cmd_exits)
    s = sub.add_parser("hold", help="标记/取消人工保留")
    s.add_argument("code")
    s.add_argument("--cancel", action="store_true")
    s.add_argument("--note")
    s.add_argument("--mode", choices=["until_next_check", "until_day_end", "until_manual"],
                   default="until_next_check", help="保留挡多久：到下一次检查 / 到该交易日结束 / 到手工解除")
    s.set_defaults(fn=cmd_hold)
    s = sub.add_parser("positions", help="查看持仓和账户")
    s.set_defaults(fn=cmd_positions)
    s = sub.add_parser("broker-status", help="只读查询下单程序 /api/status")
    s.set_defaults(fn=cmd_broker_status)
    s = sub.add_parser("broker-snapshot", help="只读读取券商持仓/委托/成交（实盘模式下顺便对账）")
    s.add_argument("--fresh", action="store_true", help="用 refresh=1 强制重扫（不加则可能命中下单程序缓存）")
    s.set_defaults(fn=cmd_broker_snapshot)
    s = sub.add_parser("notify-test", help="发测试推送")
    s.add_argument("--channel")
    s.set_defaults(fn=cmd_notify_test)
    s = sub.add_parser("backtest", help="简单回测（日K回放因子 + 退出规则）")
    s.add_argument("--start", required=True)
    s.add_argument("--end", required=True)
    s.add_argument("--codes", help="逗号分隔；默认自选股 + 缓存里的股池成分")
    s.add_argument("--json", help="结果写入这个 JSON 文件")
    s.set_defaults(fn=cmd_backtest)
    s = sub.add_parser("check-config", help="校验配置文件")
    s.set_defaults(fn=cmd_check_config)
    s = sub.add_parser("cache-prune", help="回收已无持仓引用的演示/临时分时缓存（只删带 demo 标记的）")
    s.add_argument("--dry-run", action="store_true", help="只列出会删哪些，不真的删")
    s.set_defaults(fn=cmd_cache_prune)
    args = p.parse_args(argv)
    try:
        rc = args.fn(args)
    except Exception as e:  # noqa: BLE001
        from .exec.brokers import LiveNotArmed
        if isinstance(e, LiveNotArmed):
            print(f"启动失败：{e}", file=sys.stderr)
            sys.exit(2)
        raise
    sys.exit(rc or 0)
