"""导出器的"代码占位符"自检 —— 精确到可执行代码。

## 背景

2026-11-12 克隆验收: 脱敏把 bat 里的项目根写死路径换成了占位符,
5 个启动脚本双击直接退出。

修法是**源码里不该有硬编码路径** (bat 用 `%~dp0..` 自动定位)。
但光修源码不够 —— 导出器必须自己拦: **可执行代码里出现脱敏占位符就拒绝导出**。

## 规则的精度

只查**可执行代码**, 不查文档和注释:

- Python: AST 里注释根本不存在; docstring 单独排除。
  文档里写占位符是合理的 (正是在说明脱敏行为)。
- bat / ps1 / sh: 跳过整行注释 (`#` / `REM` / `::`)。

抓的必须是**代码里的字符串字面量** —— 那才是真会跑起来的。
"""

from __future__ import annotations

# 运行时拼接 —— 源码里不出现完整占位符形态
# (导出器的"代码占位符"自检会抓到代码里的完整字面量)
PLACEHOLDER = "<" + "PROJECT_ROOT" + ">"

EP = "scripts/export_public.py"


def test_python_code_literal_placeholder_is_caught(load_module):
    """代码里的字符串字面量含占位符 -> 必须抓到。"""
    hits = load_module(EP)._python_code_placeholders(f'X = "PROJ={PLACEHOLDER}"\n')
    assert hits, "代码字面量里的占位符没被抓到"


def test_python_docstring_placeholder_is_allowed(load_module):
    """docstring 里写占位符是**正常的** (在说明脱敏行为) -> 不该报。"""
    src = f'"""这里说明 {PLACEHOLDER} 是什么。"""\nX = 1\n'
    assert not load_module(EP)._python_code_placeholders(src), "docstring 里的占位符不该被抓"


def test_python_comment_placeholder_is_allowed(load_module):
    """注释在 AST 里不存在 -> 天然不报。"""
    src = f"# 曾经写成 {PLACEHOLDER}\nX = 1\n"
    assert not load_module(EP)._python_code_placeholders(src), "注释里的占位符不该被抓"


def test_shell_comment_placeholder_is_allowed(load_module):
    """bat/sh 的整行注释 -> 不该报。"""
    mod = load_module(EP)
    for comment in ("REM 曾经用 ", "# 曾经用 ", ":: 曾经用 "):
        assert not mod._shell_code_placeholders(comment + PLACEHOLDER + "\n"), (
            f"注释 ({comment!r}) 里的占位符不该被抓")


def test_shell_code_placeholder_is_caught(load_module):
    """bat 里真正会执行的行含占位符 -> 必须抓到。"""
    hits = load_module(EP)._shell_code_placeholders(f'set "PROJ={PLACEHOLDER}"\n')
    assert hits, "bat 代码里的占位符没被抓到"


def test_export_refuses_code_placeholder(load_module, tmp_path):
    """导出器必须拒绝带代码占位符的输出。"""
    out = tmp_path / "out"
    (out / "scripts").mkdir(parents=True)
    (out / "scripts" / "x.bat").write_text(
        f'set "PROJ={PLACEHOLDER}"\n', encoding="utf-8")
    bad = load_module(EP).verify_no_placeholder_in_code(out)
    assert bad, "导出器没抓到代码里的占位符"
    assert bad[0]["file"] == "scripts/x.bat"


def test_export_allows_placeholder_in_docs(load_module, tmp_path):
    """docs/ 里的占位符是给人看的 -> 不该拦。"""
    out = tmp_path / "out"
    (out / "docs").mkdir(parents=True)
    (out / "docs" / "a.md").write_text(f"路径是 {PLACEHOLDER}\n", encoding="utf-8")
    assert not load_module(EP).verify_no_placeholder_in_code(out), "docs 里的占位符不该被拦"
