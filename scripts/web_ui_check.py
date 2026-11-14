"""牛来量化 WebUI 静态体检 + 结构地图（只读，不修改任何源码）。

检查对象是 qfpy.web.uisource 组装后的完整页面，也就是浏览器实际拿到的
那份 HTML；index.html 按分区拆出的 parts/*.js 会被一并检查，不会漏掉。

项目有意保持「单个 HTML、零前端构建」，但单文件已经 268KB / 1500+ 行，
所有函数共享一个全局作用域，2026-11-14 就发生过 `function spark()` 定义两次、
后者覆盖前者导致迷你图不显示的事故。这个脚本用纯标准库做静态体检，
把「不能靠人眼看出来的冲突」变成可重复执行的检查，不引入 npm、不引入构建。

用法：
    python scripts/web_ui_check.py                 # 体检（有 ERROR 退出码 1）
    python scripts/web_ui_check.py --strict        # WARNING 也算失败
    python scripts/web_ui_check.py --map           # 额外打印结构地图
    python scripts/web_ui_check.py --json          # 机器可读输出
    python scripts/web_ui_check.py --map --json    # 地图也进 JSON

接入测试：tests/test_web_ui_structure.py 会调用本模块的 run_checks()。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML_PATH = ROOT / "qfpy" / "web" / "index.html"
# node --check 只接受文件路径，所以临时落一个 .js；用固定路径避免沙箱 mkdir 限制，用完即删。
TMP_JS = Path(__file__).resolve().parent / ".web_ui_check.tmp.js"

# 历史基线：第四轮 / 第六轮改版叠加留下的重复选择器。
# 它们不是错误（多为有意覆盖），但集合被锁定：新增重复要报错，清理掉了要提示同步下调基线。
CSS_DUP_BASELINE = frozenset({
    ".card", ".card h3", ".chips", ".frow", ".fun .lab", ".grid", ".kv",
    ".layer", ".mnav", ".modal", ".ok-box", ".param", ".params", ".ptwrap",
    ".subnav", ".switch.sm", ".tbstrat", ".topbar", ".tpls",
})

# 内联事件里可以直接用的浏览器全局 / JS 关键字，不算「未定义函数」。
BUILTINS = frozenset({
    "event", "this", "window", "document", "location", "console", "alert",
    "confirm", "prompt", "parseInt", "parseFloat", "isNaN", "JSON", "Math",
    "Date", "Array", "Object", "String", "Number", "Boolean", "RegExp",
    "Error", "Promise", "fetch", "encodeURIComponent", "decodeURIComponent",
    "setTimeout", "setInterval", "localStorage", "navigator", "history",
})
KEYWORDS = frozenset({
    "if", "for", "while", "switch", "return", "function", "typeof", "catch",
    "new", "delete", "void", "in", "of", "else", "do", "try", "case", "await", "async",
})

FN_RE = re.compile(r"(?m)^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(")
DECL_RE = re.compile(r"(?m)^\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=")
# 全大写命名的是全局状态，重名一定会互相污染；局部变量不会这么命名。
GLOBAL_STATE_RE = re.compile(r"(?m)^\s*(?:const|let|var)\s+([A-Z][A-Z_0-9]{2,})\s*=")
CALL_RE = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(")
ATTR_RE = re.compile(r"\bon([a-z]+)\s*=\s*([\"'])(.*?)\2", re.S)
CSS_SEL_RE = re.compile(r"(?m)^\s*([.#][\w\-.#> :\[\]=(),]+)\s*\{")
BLOCK_COMMENT_RE = re.compile(r"^\s*/\*\s*(.*?)\s*\*/\s*$")


@dataclass
class Issue:
    """一条检查结果。

    Args:
        level: "ERROR"（必须修）、"WARN"（需确认）或 "INFO"（已知历史项）。
        code: 检查项短名，便于 CI 定位。
        message: 中文说明。
        details: 具体位置或名单。
    """

    level: str
    code: str
    message: str
    details: list[str] = field(default_factory=list)


def load_parts(html: str) -> tuple[str, str]:
    """从单文件里取出内联 CSS 与主 script。

    Args:
        html: index.html 全文。

    Returns:
        (css, js)。主 script 取最长的那个（head 里还有一小段主题初始化脚本）。
    """
    css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S | re.I))
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S | re.I)
    js = max(blocks, key=len) if blocks else ""
    return css, js


def check_syntax(js: str) -> list[Issue]:
    """用 node --check 校验内联 JS 语法；node 不存在时明确提示跳过，不静默通过。"""
    if not js.strip():
        return [Issue("ERROR", "syntax", "没找到内联 script")]
    try:
        TMP_JS.write_text(js, encoding="utf-8")
        proc = subprocess.run(
            ["node", "--check", str(TMP_JS)], capture_output=True, text=True, timeout=30
        )
    except FileNotFoundError:
        return [Issue("WARN", "syntax", "本机没有 node，跳过 JS 语法检查（其余检查照常执行）")]
    except subprocess.TimeoutExpired:
        return [Issue("ERROR", "syntax", "node --check 超时")]
    finally:
        if TMP_JS.exists():
            TMP_JS.unlink()
    if proc.returncode != 0:
        return [Issue("ERROR", "syntax", "内联 JS 语法错误", [proc.stderr.strip()[:800]])]
    return []  # 通过就什么都不报，否则 --strict 会永远失败


def check_name_collisions(js: str) -> list[Issue]:
    """查重复函数名和重复的全大写全局状态名。

    单文件是普通 script（非 module），同名 function 会静默互相覆盖，
    2026-11-14 的 spark / sparkSeries 事故就是这么来的。
    这里**不查普通变量**：函数内部的 `const rows = ...` 同名完全合法，
    纯正则分不清顶层 / 局部，误报比漏报更吵；而同一作用域里重复的
    const / let 本来就由 node --check 直接报语法错误。
    """
    out: list[Issue] = []
    for label, pattern in (("函数", FN_RE), ("全局状态", GLOBAL_STATE_RE)):
        dup = sorted(name for name, n in Counter(pattern.findall(js)).items() if n > 1)
        if dup:
            out.append(Issue("ERROR", "name-collision", f"重复的{label}名（后者会覆盖前者）", dup))
    return out


def check_inline_handlers(html: str, js: str) -> list[Issue]:
    """查 onclick / onchange 等内联事件里调用的函数是否真的存在。

    内联事件依赖全局函数名唯一且必须存在；拼错一个名字在浏览器里
    要到点击时才报错，静态检查能提前拦下来。
    """
    defined = set(FN_RE.findall(js)) | set(DECL_RE.findall(js)) | BUILTINS | KEYWORDS
    missing: dict[str, str] = {}
    for _event, _quote, value in ATTR_RE.findall(html):
        for name in CALL_RE.findall(value):
            if name not in defined and name not in missing:
                missing[name] = value.strip()[:100]
    if missing:
        return [Issue(
            "ERROR", "unbound-handler", "内联事件引用了没有定义的函数",
            [f"{name}  <-  {snippet}" for name, snippet in sorted(missing.items())],
        )]
    return []


def check_css_duplicates(css: str) -> list[Issue]:
    """重复 CSS 选择器用基线锁定：只允许减少，不允许冒出新的。"""
    selectors = [m.strip() for m in CSS_SEL_RE.findall(css)]
    dup = {name: n for name, n in Counter(selectors).items() if n > 1}
    out: list[Issue] = []
    added = sorted(set(dup) - CSS_DUP_BASELINE)
    if added:
        out.append(Issue("ERROR", "css-duplicate", "新增了重复定义的 CSS 选择器（覆盖式写法）", added))
    removed = sorted(CSS_DUP_BASELINE - set(dup))
    if removed:
        out.append(Issue(
            "WARN", "css-baseline", "基线里的重复选择器已清理，请把脚本中的 CSS_DUP_BASELINE 同步删掉", removed
        ))
    if dup:
        out.append(Issue(
            "INFO", "css-baseline-locked",
            f"历史重复选择器 {len(dup)} 个（已锁定，属多轮改版叠加，逐步清理即可）",
            sorted(f"{name} x{n}" for name, n in dup.items()),
        ))
    return out


def check_single_file(html: str, js: str) -> list[Issue]:
    """守住「零构建单文件」这条设计约束，和 test_web_ui.py 的断言保持一致。"""
    out: list[Issue] = []
    if re.search(r"<script[^>]+src=", html, re.I):
        out.append(Issue("ERROR", "single-file", "出现了外链 script src，破坏零构建单文件"))
    if re.search(r"<link[^>]+stylesheet", html, re.I):
        out.append(Issue("ERROR", "single-file", "出现了外链 stylesheet，破坏零构建单文件"))
    if re.search(r"(?m)^\s*(?:import|export)\s+[\w{*'\"]", js):
        out.append(Issue("ERROR", "single-file", "内联 JS 出现了 ES module 的 import/export"))
    return out


def build_map(html: str, css: str, js: str) -> dict:
    """按分区注释生成结构地图，并汇总函数、事件、接口清单。

    Returns:
        含 sections / stats / api_paths 的字典，可直接 json.dumps。
    """
    offset = html[: html.find(js)].count("\n")  # js 首行在整份 HTML 里的行号偏移
    lines = js.splitlines()
    sections: list[dict] = []
    current = {"title": "开头 / 公共部分", "start": offset + 1, "functions": []}
    for idx, line in enumerate(lines, 1):
        comment = BLOCK_COMMENT_RE.match(line)
        if comment and ("----" in comment.group(1) or "====" in comment.group(1)):
            title = comment.group(1).strip("-= ")
            if title:
                current["end"] = offset + idx - 1
                sections.append(current)
                current = {"title": title, "start": offset + idx, "functions": []}
            continue
        fn = FN_RE.match(line)
        if fn:
            current["functions"].append({"name": fn.group(1), "line": offset + idx})
    current["end"] = offset + len(lines)
    sections.append(current)

    api_paths = sorted(set(re.findall(r"[\"'](/api/[^\"'\s?]*)", js)))
    return {
        "sections": [s for s in sections if s["functions"]],
        "api_paths": api_paths,
        "stats": {
            "html_lines": len(html.splitlines()),
            "css_lines": len(css.splitlines()),
            "js_lines": len(lines),
            "functions": len(FN_RE.findall(js)),
            "inline_handlers": len(ATTR_RE.findall(html)),
            "api_paths": len(api_paths),
            "sections": len([s for s in sections if s["functions"]]),
        },
    }


def run_checks(html: str | None = None) -> dict:
    """跑完所有静态检查。

    Args:
        html: index.html 文本；None 时从仓库读取。

    Returns:
        {"errors": [...], "warnings": [...], "info": [...], "stats": {...}, "map": {...}}
    """
    parts = 0
    if html is None:
        # 脚本可能被直接以 `python scripts/xxx.py` 运行，此时项目根不在 sys.path。
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from qfpy.web.uisource import include_paths, read_ui_source
        html = read_ui_source()
        parts = len(include_paths())
    css, js = load_parts(html)
    issues: list[Issue] = []
    for check in (
        check_syntax(js),
        check_name_collisions(js),
        check_inline_handlers(html, js),
        check_css_duplicates(css),
        check_single_file(html, js),
    ):
        issues.extend(check)
    errors = [i for i in issues if i.level == "ERROR"]
    warnings = [i for i in issues if i.level == "WARN"]
    info = [i for i in issues if i.level == "INFO"]
    return {
        "errors": [vars(i) for i in errors],
        "warnings": [vars(i) for i in warnings],
        "info": [vars(i) for i in info],
        "stats": {
            "html_bytes": len(html.encode("utf-8")),
            "html_lines": len(html.splitlines()),
            "functions": len(FN_RE.findall(js)),
            "inline_handlers": len(ATTR_RE.findall(html)),
            "parts": parts,
        },
        "map": build_map(html, css, js),
    }


def _print_report(result: dict, show_map: bool) -> None:
    """把结果打印成人能看的中文报告。"""
    stats = result["stats"]
    extra = f" + {stats['parts']} 个源码片段" if stats.get("parts") else ""
    print(f"WebUI 体检：{HTML_PATH.relative_to(ROOT)}{extra}  "
          f"{stats['html_bytes']:,}B / {stats['html_lines']} 行 / "
          f"{stats['functions']} 个函数 / {stats['inline_handlers']} 个内联事件")
    for issue in result["errors"]:
        print(f"[ERROR] {issue['code']}: {issue['message']}")
        for d in issue["details"]:
            print(f"        - {d}")
    for issue in result["warnings"]:
        print(f"[WARN ] {issue['code']}: {issue['message']}")
        for d in issue["details"]:
            print(f"        - {d}")
    for issue in result["info"]:
        print(f"[INFO ] {issue['message']}")
    if show_map:
        m = result["map"]
        print(f"\n结构地图（按分区注释切分，共 {m['stats']['sections']} 段有函数）：")
        for sec in m["sections"]:
            names = ", ".join(f["name"] for f in sec["functions"])
            print(f"  L{sec['start']}-{sec['end']}  {sec['title']}")
            print(f"      {len(sec['functions'])} 个函数：{names}")
        print(f"\n接口清单（{m['stats']['api_paths']} 个）：")
        for p in m["api_paths"]:
            print(f"  {p}")
    print(f"\n结论：{len(result['errors'])} 个 ERROR，{len(result['warnings'])} 个 WARN，"
          f"{len(result['info'])} 个 INFO（INFO 是已知历史项，不算失败）")


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。

    Returns:
        进程退出码：0 通过；1 有 ERROR（或 --strict 下有 WARN）。
    """
    ap = argparse.ArgumentParser(description="牛来量化 WebUI 静态体检 + 结构地图")
    ap.add_argument("--map", action="store_true", help="打印结构地图（分区 / 函数 / 行号 / 接口）")
    ap.add_argument("--json", action="store_true", help="输出 JSON，便于 CI 或脚本消费")
    ap.add_argument("--strict", action="store_true", help="WARN 也算失败")
    args = ap.parse_args(argv)

    result = run_checks()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_report(result, args.map)
    failed = bool(result["errors"]) or (args.strict and bool(result["warnings"]))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
