"""WebUI 单文件的静态体检回归测试（纯静态、完全离线，不需要浏览器）。

单文件是普通 script、所有函数共享全局作用域，重名会静默覆盖；
内联 onclick 拼错函数名要到点击时才炸。这两类问题靠肉眼审查抓不住，
所以用 scripts/web_ui_check.py 做静态检查，并在这里验证「检查本身有效」。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("web_ui_check", ROOT / "scripts" / "web_ui_check.py")
assert _SPEC and _SPEC.loader
mod = importlib.util.module_from_spec(_SPEC)
# dataclass 在定义时会查 sys.modules[cls.__module__]，动态加载必须先注册。
sys.modules[_SPEC.name] = mod
_SPEC.loader.exec_module(mod)


def _mini(css: str = ".a{color:red}", body: str = "", js: str = "function go(){}") -> str:
    """拼一份最小单文件页面，用来验证检查器能不能抓到人为制造的问题。"""
    return f"<html><head><style>{css}</style></head><body>{body}<script>{js}</script></body></html>"


def _codes(result: dict) -> set[str]:
    return {i["code"] for i in result["errors"]}


def test_current_index_html_passes_all_static_checks():
    """真实 index.html 现在必须是干净的：没有重名、没有悬空 handler、语法通过。"""
    result = mod.run_checks()
    assert not result["errors"], "\n".join(
        f"{i['code']}: {i['message']} {i['details']}" for i in result["errors"]
    )
    assert result["stats"]["functions"] > 200
    assert result["stats"]["inline_handlers"] > 100


def test_strict_mode_passes_on_clean_baseline():
    """干净基线下 --strict 也该通过：不能把「检查通过」或已知历史项当成警告。"""
    assert mod.main(["--strict", "--json"]) == 0


def test_duplicate_function_name_is_caught():
    """复刻 11/14 的 spark 事故：同名 function 定义两次必须报错。"""
    result = mod.run_checks(_mini(js="function go(){};\nfunction go(){}"))
    assert "name-collision" in _codes(result)


def test_undefined_inline_handler_is_caught():
    """onclick 里写了不存在的函数必须报错，不能等到用户点击才发现。"""
    result = mod.run_checks(_mini(body='<button onclick="typoName()">x</button>', js="function go(){}"))
    assert "unbound-handler" in _codes(result)
    assert any("typoName" in d for i in result["errors"] for d in i["details"])


def test_new_duplicate_css_selector_is_caught():
    """历史重复选择器被基线锁定，新冒出来的必须报错。"""
    css = ".topbar{color:red}\n.brandnew{color:red}\n.brandnew{color:blue}"
    result = mod.run_checks(_mini(css=css))
    assert "css-duplicate" in _codes(result)
    assert any(".brandnew" in d for i in result["errors"] for d in i["details"])


def test_single_file_constraint_is_enforced():
    """引入外链资源或 ES module 语法会破坏零构建约定，必须拦下。"""
    html = _mini().replace("<head>", '<head><script src="app.js"></script>')
    assert "single-file" in _codes(mod.run_checks(html))
    assert "single-file" in _codes(mod.run_checks(_mini(js="import x from './a.js';\nfunction go(){}")))


def test_structure_map_covers_pages_and_apis():
    """结构地图要能列出分区、函数行号和接口清单（代替会过时的手写文档）。"""
    m = mod.run_checks()["map"]
    assert m["stats"]["sections"] >= 10
    assert m["stats"]["functions"] > 200
    assert "/api/status" in m["api_paths"]
    assert all(s["functions"] and s["start"] <= s["end"] for s in m["sections"])
