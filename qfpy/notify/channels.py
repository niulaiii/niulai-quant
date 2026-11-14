"""内置推送渠道。新增渠道：写一个 Channel 子类并 @register_channel，配置放在 [notify.<type_name>]。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
import urllib.parse

import httpx

from .base import Channel, register_channel

TIMEOUT = 10


def _check_json(r: httpx.Response, ok_key: str, ok_values: tuple) -> dict:
    r.raise_for_status()
    v = r.json()
    if v.get(ok_key) not in ok_values:
        raise RuntimeError(f"返回 {v}")
    return v


def dingtalk_sign(secret: str, timestamp_ms: int) -> str:
    """钉钉加签：HmacSHA256(key=secret, msg=f"{timestamp}\\n{secret}") → Base64 → URL 编码。"""
    string_to_sign = f"{timestamp_ms}\n{secret}"
    digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    return urllib.parse.quote_plus(base64.b64encode(digest))


@register_channel
class DingTalk(Channel):
    type_name = "dingtalk"
    title = "钉钉群机器人（默认）"
    fields = [{"name": "webhook", "hint": "https://oapi.dingtalk.com/robot/send?access_token=...", "secret": True},
              {"name": "secret", "hint": "加签密钥（SEC 开头，安全设置选「加签」时填）", "secret": True, "required": False},
              {"name": "keyword", "hint": "自定义关键词（安全设置选「关键词」时填）", "required": False},
              {"name": "at_mobiles", "hint": "要 @ 的手机号，逗号分隔", "required": False, "type": "list"}]
    help = "钉钉群 → 群设置 → 机器人 → 添加机器人 → 自定义；安全设置选「加签」，复制 SEC 开头的密钥和 Webhook。限流：每个机器人每分钟最多 20 条。"

    def build_url(self, now_ms: int | None = None) -> str:
        url = self.cfg["webhook"].strip()
        secret = (self.cfg.get("secret") or "").strip()
        if secret:
            t = now_ms or int(time.time() * 1000)
            url += ("&" if "?" in url else "?") + f"timestamp={t}&sign={dingtalk_sign(secret, t)}"
        return url

    def send(self, title: str, text: str) -> None:
        kw = (self.cfg.get("keyword") or "").strip()
        if kw:
            title = f"{kw} {title}"
        mobiles = [str(m).strip() for m in self.cfg.get("at_mobiles") or [] if str(m).strip()]
        body_text = f"### {title}\n\n{text}" + ("\n\n" + " ".join(f"@{m}" for m in mobiles) if mobiles else "")
        payload = {"msgtype": "markdown", "markdown": {"title": title, "text": body_text},
                   "at": {"atMobiles": mobiles, "isAtAll": False}}
        r = httpx.post(self.build_url(), json=payload, timeout=TIMEOUT)
        _check_json(r, "errcode", (0,))


@register_channel
class PushPlus(Channel):
    type_name = "pushplus"
    title = "PushPlus 微信推送"
    fields = [{"name": "token", "hint": "pushplus.plus 的 token", "secret": True}]
    help = "微信登录 https://www.pushplus.plus → 一对一推送 → 复制 token。"

    def send(self, title, text):
        r = httpx.post("https://www.pushplus.plus/send", json={"token": self.cfg["token"], "title": title,
                                                               "content": text, "template": "markdown"}, timeout=TIMEOUT)
        _check_json(r, "code", (200,))


@register_channel
class ServerChan(Channel):
    type_name = "serverchan"
    title = "Server酱 微信推送"
    fields = [{"name": "sendkey", "hint": "SCT 开头的 SendKey", "secret": True}]
    help = "微信扫码登录 https://sct.ftqq.com → SendKey 页面复制。"

    def send(self, title, text):
        key = self.cfg["sendkey"].strip()
        m = re.match(r"^sctp(\d+)t", key)   # Server酱³ 的 key 形如 sctp{uid}t...
        url = f"https://{m.group(1)}.push.ft07.com/send/{key}.send" if m else f"https://sctapi.ftqq.com/{key}.send"
        r = httpx.post(url, data={"title": title[:32], "desp": text}, timeout=TIMEOUT)
        _check_json(r, "code", (0,))


@register_channel
class WeCom(Channel):
    type_name = "wecom"
    title = "企业微信群机器人"
    fields = [{"name": "webhook", "hint": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...", "secret": True}]
    help = "企业微信群 → 右上角 → 添加群机器人 → 复制 Webhook 地址。"

    def send(self, title, text):
        r = httpx.post(self.cfg["webhook"], json={"msgtype": "markdown", "markdown": {"content": f"**{title}**\n{text}"}}, timeout=TIMEOUT)
        _check_json(r, "errcode", (0,))


def feishu_sign(secret: str, timestamp_s: int) -> str:
    string_to_sign = f"{timestamp_s}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


@register_channel
class Feishu(Channel):
    type_name = "feishu"
    title = "飞书群机器人"
    fields = [{"name": "webhook", "hint": "https://open.feishu.cn/open-apis/bot/v2/hook/...", "secret": True},
              {"name": "secret", "hint": "签名校验密钥（可空）", "secret": True, "required": False}]
    help = "飞书群 → 设置 → 群机器人 → 添加自定义机器人；安全设置可选「签名校验」。"

    def send(self, title, text):
        body = {"msg_type": "text", "content": {"text": f"{title}\n{text}"}}
        if self.cfg.get("secret"):
            t = int(time.time())
            body.update(timestamp=str(t), sign=feishu_sign(self.cfg["secret"], t))
        r = httpx.post(self.cfg["webhook"], json=body, timeout=TIMEOUT)
        r.raise_for_status()
        v = r.json()
        if v.get("code", v.get("StatusCode", 0)) != 0:
            raise RuntimeError(f"返回 {v}")


@register_channel
class Telegram(Channel):
    type_name = "telegram"
    title = "Telegram Bot"
    fields = [{"name": "bot_token", "hint": "@BotFather 给的 token", "secret": True}, {"name": "chat_id", "hint": "你的 chat id"},
              {"name": "proxy", "hint": "代理，如 http://127.0.0.1:7890（可空）", "required": False}]
    help = "Telegram 里找 @BotFather 创建机器人拿 token；给机器人发一条消息后访问 getUpdates 拿 chat id。国内网络需要代理。"

    def send(self, title, text):
        proxy = self.cfg.get("proxy") or None
        with httpx.Client(proxy=proxy, timeout=TIMEOUT) as c:
            r = c.post(f"https://api.telegram.org/bot{self.cfg['bot_token']}/sendMessage",
                       json={"chat_id": self.cfg["chat_id"], "text": f"{title}\n{text}", "disable_web_page_preview": True})
            _check_json(r, "ok", (True,))
