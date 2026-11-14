"""最小外部接口示例（拉）：一个只用标准库的 HTTP 因子服务 + 数据源服务。

牛来量化跑到这一层时会 POST 本层全部候选过来，这里返回每只票的 pass/score。

启动：  python examples/external_factor_server.py        （监听 127.0.0.1:9100）
配置：
  买入漏斗某层里加
    [[buy_funnel.layers.factors]]
    kind = "http"
    params = { url = "http://127.0.0.1:9100/factor", name = "demo", use = "score", min_score = 0.5 }
  数据源（拉股池和字段）
    [sources.demo_http]
    type = "http"
    pool_url = "http://127.0.0.1:9100/pool/{key}?date={date}"
    fields_url = "http://127.0.0.1:9100/fields"
  然后在 [pool].pools 里加 "demo_http:demo"。

请求体（因子）：{"factor": "demo", "side": "buy", "trading_date": "2026-11-11",
                  "candidates": [{"code": "600000", "name": "...", "sources": [...], "fields": {"change_rate": 2.1, ...}}]}
返回：          {"results": [{"code": "600000", "score": 0.7, "pass": true, "note": "..."}]}
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def score(c: dict) -> dict:
    f = c.get("fields") or {}
    s = 0.5 + 0.1 * float(f.get("change_rate") or 0) - 0.05 * float(f.get("drawdown_pct") or 0)
    s = max(0.0, min(1.0, s))
    return {"code": c["code"], "score": round(s, 3), "note": "涨幅加分、回撤扣分（示例）"}


class H(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_POST(self):
        if self.path.startswith("/factor"):
            body = self._body()
            return self._send({"results": [score(c) for c in body.get("candidates", [])]})
        if self.path.startswith("/fields"):
            body = self._body()
            return self._send({"items": [{"code": c, "fields": {"ext_demo": len(c)}} for c in body.get("codes", [])]})
        self._send({"error": "not found"}, 404)

    def do_GET(self):
        if self.path.startswith("/pool/"):
            return self._send({"name": "示例股池", "stocks": [{"code": "600000", "name": "浦发银行"}, {"code": "000001", "name": "平安银行"}]})
        self._send({"error": "not found"}, 404)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print("外部因子示例服务：http://127.0.0.1:9100")
    ThreadingHTTPServer(("127.0.0.1", 9100), H).serve_forever()
