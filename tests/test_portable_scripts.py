"""脚本可移植性 —— 公开仓下载下来必须能直接跑。

## 为什么要这个

2026-11-12 从 GitHub 克隆验收时抓到三个真问题, 每一个都让"下载就能用"
这句话变成假的:

1. **bat 里写死项目根** —— `set "PROJ=<PROJECT_ROOT>"`。
   脱敏后变成 `set "PROJ=<PROJECT_ROOT>"`, **双击直接报错退出**。
2. **bat / py 里写死用户目录** —— `C:\\Users\\<用户名>\\AppData\\Local\\hermes`。
   脱敏后同样变成占位符, 换台机器就废。
3. **PYTHONPATH 污染** —— 从 Hermes 终端启动时, `import PIL` 拿到的是
   别的环境的 Pillow, 报 "cannot import name '_imaging'"。

修法不是"脱敏时换个别的占位符", 而是**源码里根本不该有硬编码路径**:
bat 用 `%~dp0..` 自动定位, Hermes 目录用 `%LOCALAPPDATA%` / `Path.home()`。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import _real_values as real

PROJ = Path(__file__).resolve().parent.parent
SCRIPTS = PROJ / "scripts"

# 规则真值源 —— 它们**故意**含有真实形态 (脱敏器要用它们当规则)
RULE_SOURCES = {"export_public.py", "audit_publish.py"}


def _scripts(ext: str) -> list[Path]:
    return sorted(p for p in SCRIPTS.glob(f"*{ext}") if p.is_file())


def test_bats_locate_project_root_relatively():
    """bat 必须用 `%~dp0` 自动定位, 不能写死绝对路径。"""
    for p in _scripts(".bat"):
        t = p.read_text(encoding="utf-8", errors="replace")
        if "PROJ=" not in t:
            continue
        assert "%~dp0" in t, (
            f"{p.name}: PROJ 写死了绝对路径 —— 换台机器/公开仓就废。"
            f" 应该用 '%~dp0..' 自动定位")
        assert not re.search(r'set\s+"PROJ=[A-Za-z]:\\', t), (
            f"{p.name}: PROJ 是硬编码绝对路径")


def test_no_hardcoded_user_profile_in_scripts():
    """任何脚本里都不能有 `C:\\Users\\<某人>` —— 换机器就废。"""
    # 用户名复用 _real_values (运行时构造, 源码里不出现明文)
    pattern = re.compile(r"[A-Za-z]:[\\/]Users[\\/]" + re.escape(real.WIN_USER), re.I)
    for ext in (".bat", ".ps1", ".sh", ".py"):
        for p in _scripts(ext):
            if p.name in RULE_SOURCES:
                continue
            t = p.read_text(encoding="utf-8", errors="replace")
            assert not pattern.search(t), (
                f"{p.name}: 有硬编码的用户目录 —— 公开仓脱敏后会变成占位符,"
                f" 换台机器直接坏。应该用 %LOCALAPPDATA% / Path.home() / $HOME")


def test_bats_use_localappdata_for_hermes_home():
    """bat 里的 Hermes 目录要用 `%LOCALAPPDATA%`, 不能写死用户名。"""
    for p in _scripts(".bat"):
        t = p.read_text(encoding="utf-8", errors="replace")
        if 'set "HD=' not in t:
            continue
        assert "%LOCALAPPDATA%" in t, (
            f"{p.name}: HD 用的是硬编码路径 —— 应该是 %LOCALAPPDATA%\\AppData\\Local\\hermes")


def test_clean_sys_path_removes_pythonpath_entries(load_module, tmp_path):
    """`clean_sys_path()` 必须真的把 PYTHONPATH 注入的路径剔掉。

    直接加载模块而不是 `import huaien.runtime` —— 后者会先触发
    `huaien/__init__.py` 里的自动清理, 再调就剔不到东西了。
    """
    mod = load_module("huaien/runtime.py")

    os.environ["PYTHONPATH"] = str(tmp_path)
    sys.path.insert(0, str(tmp_path))
    try:
        n = mod.clean_sys_path()
        assert n >= 1, "clean_sys_path 没剔掉任何路径"
        assert str(tmp_path) not in [os.path.abspath(p or ".") for p in sys.path]
    finally:
        os.environ.pop("PYTHONPATH", None)
        if str(tmp_path) in sys.path:
            sys.path.remove(str(tmp_path))


def test_clean_env_drops_pythonpath():
    """`clean_env()` 给子进程用 —— 必须没有 PYTHONPATH。"""
    os.environ["PYTHONPATH"] = "C:\\some\\stale\\path"
    try:
        from huaien.runtime import clean_env

        env = clean_env()
        assert "PYTHONPATH" not in env
        assert "PATH" in env  # 其余环境变量要保留
    finally:
        os.environ.pop("PYTHONPATH", None)


def test_huaien_import_cleans_sys_path_before_third_party():
    """`import huaien` 必须已经清了污染 —— 入口只有一个, 靠 __init__ 兜住。"""
    code = (
        "import os, sys\n"
        "os.environ['PYTHONPATH'] = r'C:\\stale\\site-packages'\n"
        "import huaien\n"
        "bad = [p for p in sys.path if 'stale' in p.lower()]\n"
        "print('BAD' if bad else 'CLEAN')\n"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=str(PROJ), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120)
    assert "CLEAN" in (r.stdout or ""), f"import huaien 没清掉 PYTHONPATH: {r.stdout}{r.stderr}"


def test_gitignore_blocks_tool_caches():
    """工具缓存里含**绝对路径** —— 提交上去等于泄露机器路径。

    2026-11-12 克隆验收时实测: `.ruff_cache/` 的二进制索引里存着
    `E:\\项目路径\\...` 这样的绝对路径。ruff 自己会写 .gitignore 自保,
    但 pytest / mypy / coverage 不一定 —— 不能指望每个工具都自保护。
    """
    gi = (PROJ / ".gitignore").read_text(encoding="utf-8")
    for name in (".ruff_cache", ".pytest_cache", ".mypy_cache"):
        assert name in gi, f".gitignore 没挡 {name}/ —— 缓存里含绝对路径"
