"""WebUI 源码组装：磁盘上按文件分区维护，交付给浏览器的仍是单个完整 HTML。

为什么不用 `<script src>` 或 ES modules：
    1. 项目约定「零构建、无外链资源」，tests/test_web_ui.py 有断言守着；
    2. 页面有 200 多个内联 onclick，依赖全局函数；改成 module 作用域会全部失效；
    3. 外链会多一个请求，还要处理片段改了但首页 ETag 没变的缓存一致性问题。

所以做法是把大段 JS 拆到 parts/*.js，服务端返回首页时原样内联回去。
浏览器拿到的 HTML 与拆分前**字节级一致**：作用域、加载顺序、
缓存、内联事件全都不变，只是源码 diff 按文件隔离开了。

占位符写在 <script> 块内、单独一行、用 JS 注释语法（不是 HTML 注释），
所以磁盘上的 index.html 本身也仍是合法 JS，不会误导编辑器。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
INCLUDE_RE = re.compile(r"(?m)^/\* @include ([\w./-]+\.js) \*/[ \t]*\r?\n")


def _resolve(rel: str) -> Path:
    """解析并校验片段路径，防止占位符写错或目录穿越。

    Args:
        rel: 相对 qfpy/web/ 的 .js 路径。

    Returns:
        片段的绝对路径。

    Raises:
        FileNotFoundError: 路径不合法或文件不存在。
    """
    p = (HERE / rel).resolve()
    if not p.is_relative_to(HERE) or p.suffix != ".js" or not p.is_file():
        raise FileNotFoundError(f"WebUI 片段不存在或路径不合法：{rel}")
    return p


def include_paths() -> list[Path]:
    """列出 index.html 里引用的所有片段（按出现顺序）。"""
    html = (HERE / "index.html").read_text(encoding="utf-8")
    return [_resolve(rel) for rel in INCLUDE_RE.findall(html)]


def read_ui_source() -> str:
    """组装出交付给浏览器的完整单页 HTML。

    Returns:
        展开所有 @include 后的 HTML 文本。

    Raises:
        FileNotFoundError: 引用的片段缺失（宁可 500，也不能静默给出半残页面）。
    """
    html = (HERE / "index.html").read_text(encoding="utf-8")
    return INCLUDE_RE.sub(lambda m: _resolve(m.group(1)).read_text(encoding="utf-8"), html)


def fingerprint() -> str:
    """算 ETag 用的指纹，覆盖 index.html 和全部片段。

    只按 index.html 的 mtime/size 算的话，改了 parts/*.js 首页 ETag 不变，
    浏览器会一直拿旧缓存，这是拆分后最容易踩的坑。
    """
    h = hashlib.md5()
    for p in [HERE / "index.html", *include_paths()]:
        st = p.stat()
        h.update(f"{p.relative_to(HERE)}:{st.st_mtime_ns}:{st.st_size};".encode())
    return h.hexdigest()
