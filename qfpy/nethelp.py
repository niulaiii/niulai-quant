"""联网请求的公共处理：有限次数重试 + 退避、系统代理识别、失败时给中文说明。

为什么要有这个文件：gushi.in 拉股池时，用户那边出现过
``[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol``。
这个错误的意思是：TLS 握手（或读响应）进行到一半，连接被对方或中间设备直接关掉了。
服务器本身用的是正常的 TLS 1.2/1.3 证书，常见原因按出现频率是：

1. 电脑开了代理 / 加速器（Clash、V2Ray 等）。httpx 默认会用系统代理（Windows 上读注册表里的
   「Internet 设置 → 代理」），代理节点不通、或代理把 https 当成普通 http 转发，就会在握手时断开。
2. 公司 / 校园网络、杀毒软件的「HTTPS 扫描」拦截了这个网站。
3. 网络瞬断或对方限流时直接断开连接，过一会儿就好。

所以这里的做法是：
- 网络层错误（连不上、握手断开、读超时）最多重试 ``retries`` 次，间隔 1 秒、3 秒……逐次加长；
- 默认（proxy = "auto"）先按系统代理走；如果检测到系统代理而且失败了，再试一次直连；
- proxy = "none" 永远直连；proxy = "http://127.0.0.1:7890" 这类网址就固定走这个代理；
- 全部失败时抛出 NetError，带中文的「出了什么问题 / 怎么排查」，原始英文报错放在 detail 里。
"""
from __future__ import annotations

import ssl
import time
import urllib.request
from urllib.parse import urlsplit

import httpx


class NetError(RuntimeError):
    """联网失败：str(e) 是中文的一句话说明；.hint 是排查办法；.detail 是原始报错。"""

    def __init__(self, problem: str, hint: str = "", detail: str = "", attempts: list | None = None):
        super().__init__(problem)
        self.problem = problem
        self.hint = hint
        self.detail = detail
        self.attempts = attempts or []


def system_proxy_for(url: str) -> str | None:
    """按 httpx 的规则看这个网址会不会走系统代理（环境变量；Windows 上还有注册表设置）。"""
    try:
        host = urlsplit(url).hostname or ""
        if host and urllib.request.proxy_bypass(host):
            return None
        px = urllib.request.getproxies()
        return px.get(urlsplit(url).scheme) or px.get("all")
    except Exception:  # noqa: BLE001
        return None


def _is_net_error(e: Exception) -> bool:
    return isinstance(e, (httpx.TransportError, ssl.SSLError, ConnectionError, TimeoutError, OSError))


def explain(e: Exception, url: str, proxy: str | None) -> tuple[str, str]:
    """把底层报错翻译成（问题，排查办法）两句中文。"""
    raw = f"{type(e).__name__}: {e}"
    host = urlsplit(url).hostname or url
    via = f"（经过代理 {proxy}）" if proxy else ""
    if "UNEXPECTED_EOF" in raw or "EOF occurred in violation of protocol" in raw:
        problem = f"连接 {host} 时，加密握手进行到一半连接被断开了{via}"
        hint = ("多半是本机到这个网站的网络有问题，不是数据错了：①开着代理或加速器（Clash、V2Ray 等）时，"
                "先关掉再试，或在代理规则里把 " + host + " 设为直连，也可以在「设置 → 股池数据源」里把这个数据源的「代理」改成「直连」；"
                "②公司或学校网络、杀毒软件的 HTTPS 扫描会拦截，换个网络（比如手机热点）试一下；"
                "③偶尔是网络瞬断或对方限流，系统会自动重试，下一次运行通常就好了。")
    elif "CERTIFICATE_VERIFY_FAILED" in raw:
        problem = f"{host} 的证书校验没通过{via}"
        hint = "通常是代理或杀毒软件替换了证书。关掉 HTTPS 扫描或代理再试；电脑时间不对也会导致这个问题。"
    elif isinstance(e, httpx.TimeoutException) or "timed out" in raw.lower():
        problem = f"连接 {host} 超时{via}"
        hint = "网络慢或连不上。检查网络和代理；系统会自动重试。"
    elif isinstance(e, httpx.ProxyError) or "proxy" in raw.lower():
        problem = f"代理连不上{via}"
        hint = "检查代理软件是否在运行，或在数据源设置里把「代理」改成「直连」。"
    elif isinstance(e, httpx.ConnectError):
        problem = f"连不上 {host}{via}"
        hint = "检查网络是否正常、网址是否写对；开着代理时试试关掉代理或改成直连。"
    else:
        problem = f"请求 {host} 失败{via}"
        hint = "系统会自动重试；一直失败请检查网络和代理设置。"
    return problem, hint


def robust_get(url: str, params: dict | None = None, headers: dict | None = None, timeout: float = 15,
               retries: int = 2, backoff: float = 1.0, proxy: str | None = "auto", sleep=time.sleep) -> httpx.Response:
    """GET，网络层出错时有限次数重试。HTTP 状态码不重试（交给调用方看接口返回）。"""
    proxy = (proxy or "auto").strip() if isinstance(proxy, str) else "auto"
    sysproxy = system_proxy_for(url) if proxy == "auto" else None
    # 每一次尝试用哪种连接方式：(说明, httpx 参数)
    if proxy in ("none", "direct", "直连"):
        plans = [("直连", {"trust_env": False})]
    elif proxy == "auto":
        plans = [(f"系统代理 {sysproxy}" if sysproxy else "直连", {"trust_env": True})]
        if sysproxy:
            plans.append(("直连（绕过系统代理）", {"trust_env": False}))
    else:
        plans = [(f"代理 {proxy}", {"trust_env": False, "proxy": proxy})]
    tries = [plans[min(i, len(plans) - 1)] for i in range(max(1, retries + 1))]
    attempts: list[dict] = []
    last: Exception | None = None
    for i, (how, kw) in enumerate(tries):
        if i:
            sleep(backoff * (3 ** (i - 1)))      # 1 秒、3 秒、9 秒……
        try:
            with httpx.Client(timeout=timeout, **kw) as c:
                r = c.get(url, params=params or {}, headers=headers or {})
            if attempts:
                r.extensions["qf_attempts"] = attempts + [{"how": how, "ok": True}]
            return r
        except Exception as e:  # noqa: BLE001
            if not _is_net_error(e):
                raise
            last = e
            attempts.append({"how": how, "ok": False, "error": f"{type(e).__name__}: {e}"[:300]})
    problem, hint = explain(last, url, sysproxy if proxy == "auto" else (None if proxy in ("none", "direct", "直连") else proxy))
    detail = "；".join(f"第 {n} 次（{a['how']}）：{a['error']}" for n, a in enumerate(attempts, 1))
    raise NetError(f"{problem}（已重试 {len(attempts) - 1} 次）", hint, detail, attempts) from last
