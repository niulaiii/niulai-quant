"""最小外部接口示例（推）：别的程序算好结果，推送给牛来量化。只用 Python 标准库。

准备：
  1. 启动牛来量化时设置 token：  set QF_API_TOKEN=你自己定的一串随机字符     （Linux/macOS: export QF_API_TOKEN=...）
  2. 运行本脚本：                  python examples/external_push.py
     （默认连 http://127.0.0.1:8081，可用环境变量 QF_URL 改）

本脚本做三件事：
  a. 推送外部因子结果 my_score（漏斗里用 kind = "external", params = { name = "my_score" } 读取）
  b. 建一个 push 数据源 mypush 并推送一个股池 mypush:hot（在 [pool].pools 里加 "mypush:hot" 就会参与选股）
  c. 给两只票推送自定义字段 ext_hot（表达式里直接写 ext_hot >= 3）
对外 API 没有下单接口：这里推送的只是数据，是否买卖仍由漏斗、风控和各个开关决定。
"""
import datetime as dt
import json
import os
import urllib.request

BASE = os.environ.get("QF_URL", "http://127.0.0.1:8081").rstrip("/")
TOKEN = os.environ.get("QF_API_TOKEN", "")


def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


if __name__ == "__main__":
    today = dt.date.today().isoformat()
    print(call("POST", "/api/v1/factor-results/my_score", {"date": today, "results": [
        {"code": "600000", "score": 0.82, "note": "模型打分"},
        {"code": "000001", "pass": False, "note": "淘汰"},
    ]}))
    print(call("PUT", "/api/v1/sources/mypush", {"type": "push"}))
    print(call("PUT", "/api/v1/sources/mypush/pools/hot", {"date": today, "name": "我的热门", "stocks": [
        {"code": "600000", "name": "浦发银行"}, {"code": "000001", "name": "平安银行", "fields": {"ext_hot": 5}}]}))
    print(call("POST", "/api/v1/sources/mypush/fields", {"date": today, "items": [
        {"code": "600000", "fields": {"ext_hot": 3}}]}))
    print(json.dumps(call("GET", "/api/v1/funnel/latest"), ensure_ascii=False)[:500])
