"""推送基础：渠道注册表 + 后台发送队列 + 推送日志。

所有消息都会写入 data/notify.jsonl 和程序日志；渠道没填 token 时只写日志不发送。
发送在后台线程做，不会拖慢选股和下单；发送失败只记录，不影响交易流程。
"""
from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

from ..util import append_jsonl, log, read_jsonl, ts

CHANNEL_TYPES: dict[str, type["Channel"]] = {}

APP_NAME = "牛来量化"
PUSH_PREFIX = f"【{APP_NAME}】"   # 所有推送标题的前缀（钉钉关键词安全设置可以填「牛来量化」）


def with_prefix(title: str) -> str:
    return title if title.startswith(PUSH_PREFIX) else PUSH_PREFIX + title

EVENT_LABELS = {
    "run_result": "选股结果", "order": "委托", "risk_reject": "风控拦截", "unknown": "结果未知需人工核对",
    "error": "报错", "signal_only": "信号（自动交易关闭）", "exit_reminder": "退出前提醒", "hold_skip": "保留跳过卖出",
    "manual_needed": "需要人工处理", "switch": "开关切换", "test": "测试消息",
    "reconcile": "实盘对账", "broker_link": "下单程序连接", "backfill": "补录", "config_change": "设置修改",
    "positions_mode": "持仓获取方式", "strategy_switch": "生效策略切换",
    "exit_problem": "止损/卖出检查没法判断", "pool_archive": "股池存档",
}


def register_channel(cls):
    CHANNEL_TYPES[cls.type_name] = cls
    return cls


class Channel:
    type_name = ""
    title = ""
    fields: list[dict] = []      # 网页配置项 [{name, hint, secret}]
    help: str = ""

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}

    def configured(self) -> bool:
        return all(self.cfg.get(f["name"]) for f in self.fields if f.get("required", True))

    def send(self, title: str, text: str) -> None:  # pragma: no cover
        raise NotImplementedError


class Notifier:
    def __init__(self, cfg_getter, data_dir: Path):
        self.cfg_getter = cfg_getter
        self.log_path = Path(data_dir) / "notify.jsonl"
        self.q: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._loop, name="notify", daemon=True)
        self._worker.start()
        self._recent: dict[str, float] = {}

    def channels(self, only: str | None = None) -> list[Channel]:
        cfg = self.cfg_getter()["notify"]
        names = [only] if only else cfg.get("channels", [])
        out = []
        for n in names:
            cls = CHANNEL_TYPES.get(n)
            if cls is None:
                log.warning("未知推送渠道 %s", n)
                continue
            out.append(cls(cfg.get(n, {})))
        return out

    def notify(self, event: str, title: str, text: str, dedupe_key: str | None = None, dedupe_secs: int = 600) -> None:
        """异步推送。事件开关在 [notify.events]。dedupe_key 相同的消息 dedupe_secs 内只发一次。"""
        cfg = self.cfg_getter()["notify"]
        if dedupe_key:
            now = time.time()
            if now - self._recent.get(dedupe_key, 0) < dedupe_secs:
                return
            self._recent[dedupe_key] = now
        enabled = cfg.get("enabled", True) and (event == "test" or cfg.get("events", {}).get(event, True))
        log.info("[推送:%s] %s | %s", EVENT_LABELS.get(event, event), title, text.replace("\n", " / ")[:300])
        if not enabled:
            self._record(event, title, text, [{"channel": "-", "status": "事件未开启推送，仅日志"}])
            return
        self.q.put((event, title, text, None))

    def send_sync(self, event: str, title: str, text: str, only: str | None = None) -> list[dict]:
        """同步发送（网页“发测试消息”用）。和 notify 一样先打印到日志，没配 token 时日志就是唯一去处。"""
        log.info("[推送:%s] %s | %s", EVENT_LABELS.get(event, event), title, text.replace("\n", " / ")[:300])
        return self._deliver(event, title, text, only)

    def test_with(self, channel: str, form_cfg: dict, saved_cfg: dict | None = None, unmask=None) -> list[dict]:
        """用网页表单里当前填的值直接发一条测试消息：不保存、不改动已保存的配置、不看渠道是否已启用。
        form_cfg 里仍是掩码（••••1234）的字段，表示你没改过，就用已保存的值。"""
        cls = CHANNEL_TYPES.get(channel)
        if cls is None:
            raise ValueError(f"未知推送渠道 {channel}")
        saved = dict(saved_cfg or {})
        fdefs = {f["name"]: f for f in cls.fields}
        cfg = {}
        for k, v in (form_cfg or {}).items():
            if k not in fdefs:
                continue
            if unmask is not None:
                v = unmask(v, saved.get(k))
            if fdefs[k].get("type") == "list" and isinstance(v, str):
                v = [x.strip() for x in v.replace("，", ",").split(",") if x.strip()]
            cfg[k] = v.strip() if isinstance(v, str) else v
        ch = cls(cfg)
        title, text = "测试消息（未保存的设置）", f"- 这是牛来量化的测试推送（{cls.title}）\n- 用的是网页表单里当前填的值，尚未保存\n- 收到就说明这组设置可用"
        log.info("[推送:测试/未保存] %s | %s", cls.title, text.replace("\n", " / ")[:300])
        if not ch.configured():
            missing = [f.get("label", f["name"]) for f in cls.fields if f.get("required", True) and not cfg.get(f["name"])]
            res = [{"channel": channel, "status": "缺少必填项：" + "、".join(missing), "ok": False}]
        else:
            try:
                ch.send(with_prefix(title), text)
                res = [{"channel": channel, "status": "已发送", "ok": True}]
            except Exception as e:  # noqa: BLE001
                log.warning("测试推送到 %s 失败：%s", channel, e)
                res = [{"channel": channel, "status": f"发送失败：{str(e)[:300]}", "ok": False}]
        self._record("test", title, text, res)
        return res

    def _loop(self):
        while True:
            event, title, text, only = self.q.get()
            try:
                self._deliver(event, title, text, only)
            except Exception as e:  # noqa: BLE001
                log.warning("推送线程异常：%s", e)

    def _deliver(self, event, title, text, only) -> list[dict]:
        results = []
        chans = self.channels(only)
        if not chans:
            results.append({"channel": "-", "status": "没有启用推送渠道，仅日志"})
        for ch in chans:
            if not ch.configured():
                results.append({"channel": ch.type_name, "status": "未配置 token/webhook，仅打印到日志"})
                continue
            try:
                ch.send(with_prefix(title), text)
                results.append({"channel": ch.type_name, "status": "已发送"})
            except Exception as e:  # noqa: BLE001
                log.warning("推送到 %s 失败：%s", ch.type_name, e)
                results.append({"channel": ch.type_name, "status": f"发送失败：{str(e)[:200]}"})
        self._record(event, title, text, results)
        return results

    def _record(self, event, title, text, results):
        append_jsonl(self.log_path, {"at": ts(), "event": event, "title": title, "text": text, "results": results})

    def recent(self, n: int = 50) -> list[dict]:
        return list(reversed(read_jsonl(self.log_path, n)))

    def flush(self, timeout: float = 10.0) -> None:
        end = time.time() + timeout
        while not self.q.empty() and time.time() < end:
            time.sleep(0.1)
        time.sleep(0.2)
