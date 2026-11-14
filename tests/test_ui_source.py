"""WebUI 源码组装的回归测试：磁盘上分片维护，交付给浏览器的仍是完整单页。

拆分方案是「服务端内联拼接」，所以必须守住三件事：
    1. 组装后不能残留占位符或外链资源（零构建单文件约定不变）；
    2. ETag 要覆盖所有片段，改了 parts/*.js 浏览器必须能拿到新页面；
    3. / 路由返回的确实是组装后的完整页面，且 304 缓存逻辑仍然有效。
"""
from __future__ import annotations

import os

import httpx
import pytest
from fastapi.testclient import TestClient

from qfpy.web import uisource
from qfpy.web.server import create_app

_real_send = httpx.Client.send


@pytest.fixture(autouse=True)
def _allow_testclient(monkeypatch):
    """conftest 的 no_network 会把 httpx 全拦掉，TestClient 需要放行（和其余网页测试一致）。"""
    monkeypatch.setattr(httpx.Client, "send", _real_send)


def test_assembled_page_is_self_contained():
    """交付给浏览器的页面不依赖任何外部文件，和拆分前是同一份 HTML。"""
    html = uisource.read_ui_source()
    assert "@include" not in html                      # 占位符全部展开
    assert "<script src=" not in html
    assert "<link" not in html
    assert html.count("<script>") == 2                # head 主题脚本 + 主脚本
    assert "function drawStrat(" in html               # 来自 parts/strategy.js
    assert "async function bsync(" in html             # 拆出去那段的最后一个函数
    parts = uisource.include_paths()
    assert len(parts) >= 5 and all(p.suffix == ".js" and p.is_file() for p in parts)


def test_part_order_matches_placeholders():
    """多个占位符必须按页面顺序展开，片段之间不能错位。"""
    from qfpy.web.uisource import HERE, INCLUDE_RE
    html = (HERE / "index.html").read_text(encoding="utf-8")
    declared = INCLUDE_RE.findall(html)
    resolved = [str(p.relative_to(HERE)) for p in uisource.include_paths()]
    assert declared == resolved and len(declared) >= 5


def test_each_part_is_syntactically_complete():
    """每段单独也要是完整 JS：不能在函数体或模板字符串中间切断。"""
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("本机没有 node，跳过片段语法检查")
    for p in uisource.include_paths():
        r = subprocess.run(["node", "--check", str(p)], capture_output=True, text=True)
        assert r.returncode == 0, f"{p.name}: {r.stderr[:300]}"


def test_include_path_is_sandboxed():
    """占位符不能引用 qfpy/web/ 之外的文件。"""
    with pytest.raises(FileNotFoundError):
        uisource._resolve("../../config/pipeline.toml.js")
    with pytest.raises(FileNotFoundError):
        uisource._resolve("parts/not_there.js")


def test_fingerprint_covers_part_files(monkeypatch, tmp_path):
    """只改片段、index.html 不动时，ETag 也必须变，否则浏览器一直拿旧缓存。"""
    web = tmp_path / "web"
    (web / "parts").mkdir(parents=True)
    (web / "index.html").write_text(
        "<script>\n/* @include parts/a.js */\n/* @include parts/b.js */\n</script>", encoding="utf-8")
    (web / "parts" / "a.js").write_text("function a(){}\n", encoding="utf-8")
    part = web / "parts" / "b.js"
    part.write_text("function b(){}\n", encoding="utf-8")
    monkeypatch.setattr(uisource, "HERE", web)

    assert uisource.read_ui_source() == "<script>\nfunction a(){}\nfunction b(){}\n</script>"
    assert len(uisource.include_paths()) == 2
    before = uisource.fingerprint()
    ns = part.stat().st_mtime_ns + 1_000_000_000   # 改的是后面的片段，index.html 没动
    os.utime(part, ns=(ns, ns))
    assert uisource.fingerprint() != before


def test_index_route_returns_assembled_page_and_304(make_engine):
    """/ 返回完整页面，ETag 与指纹一致，重复打开走 304。"""
    client = TestClient(create_app(make_engine()))
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.text == uisource.read_ui_source()
    assert resp.headers["cache-control"] == "no-cache"
    etag = resp.headers["etag"]
    assert etag == f'"{uisource.fingerprint()}"'
    assert client.get("/", headers={"If-None-Match": etag}).status_code == 304
    # 换成内存 Response 后，GZipMiddleware 仍要生效（首页 268KB，压缩后约 86KB）
    assert client.get("/", headers={"Accept-Encoding": "gzip"}).headers.get("content-encoding") == "gzip"
