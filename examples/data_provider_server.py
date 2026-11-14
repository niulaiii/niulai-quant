"""可运行的「标准数据接口」（qfdp v1）示例提供方服务。

    python examples/data_provider_server.py                       # 演示数据，监听 127.0.0.1:9100，不要 token
    python examples/data_provider_server.py --token my-secret     # 要求 Authorization: Bearer my-secret
    python examples/data_provider_server.py --source akshare      # 把内置 akshare 适配器按标准接口转发（真实行情）

然后在网页「设置 → 数据源」新增一个类型为「标准接口」的数据源，URL 填 http://127.0.0.1:9100，
点「测试连接」。股池 key 写成 数据源名:股池key，比如 prov:demo_hot。

自己写提供方：要么照 docs/DATA_PROVIDER.md 用任何语言实现这几个 GET 接口；要么像这里一样，
继承 qfpy.datasources.DataSource 实现方法，再用 qfpy.dataproto.make_provider_app 包成 HTTP 服务。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qfpy.dataproto import make_provider_app  # noqa: E402


def build(source: str = "demo", token: str = ""):
    if source == "akshare":
        import tempfile

        from qfpy.datasources.akshare_src import AkshareSource
        src = AkshareSource("akshare", {"pools": ["zt_pool", "strong_pool"]}, Path(tempfile.mkdtemp(prefix="qfdp-ak")))
        return make_provider_app(src, token=token, name="akshare 转发（示例）")
    from qfpy.devtools.demo_provider import DemoSource
    return make_provider_app(DemoSource(), token=token, name="演示提供方")


def main():
    ap = argparse.ArgumentParser(description="qfdp v1 示例提供方")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9100)
    ap.add_argument("--token", default="")
    ap.add_argument("--source", choices=["demo", "akshare"], default="demo")
    a = ap.parse_args()
    import uvicorn
    uvicorn.run(build(a.source, a.token), host=a.host, port=a.port, log_level="info")


if __name__ == "__main__":
    main()
