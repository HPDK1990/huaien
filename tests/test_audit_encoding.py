"""审计脚本必须在**非 UTF-8 环境**下也能跑 —— CI 会炸在这。

## 事故经过

2026-11-11: GitHub Actions 的"敏感信息审计"连续红 6 次, 而本地 263 项测试全绿。
根因: Windows 上 Python 输出 stdout 用 **locale 编码** (中文系统 GBK,
CI runner cp1252), 脚本打印 "✅" 和中文时直接:

    UnicodeEncodeError: 'gbk' codec can't encode character '\u2705'

本地之所以看不出来: 开发者 shell 里普遍设了 PYTHONIOENCODING=utf-8,
CI runner 没有 —— **环境差异被本地环境掩盖了**。

## 这个测试干什么

用**清空 PYTHONIOENCODING / PYTHONUTF8 的子进程**跑审计脚本,
模拟 CI 的裸环境, 断言退出码为 0 且没有 UnicodeEncodeError。

⚠️ 必须用**子进程** —— 同一个解释器里改不了自己的 stdout 编码。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
SCRIPT = PROJ / "scripts" / "audit_publish.py"


def _bare_env() -> dict:
    """模拟 CI: 去掉所有 Python 编码相关的环境变量。"""
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONIOENCODING", "PYTHONUTF8", "PYTHONIOENCODING")}
    env.pop("PYTHONIOENCODING", None)
    env.pop("PYTHONUTF8", None)
    return env


def test_audit_runs_in_bare_env():
    """裸环境 (无 PYTHONUTF8 / PYTHONIOENCODING) 必须**不因编码崩溃**。

    ⚠️ 退出码**不能**断言 0 —— 开发仓本身有真值 (那正是审计要报的),
       公开副本才应该是 0。这里断言的是**没有 UnicodeEncodeError**,
       且脚本把报告完整打出来了 (有"统计:"行 = 跑到了最后)。
    """
    r = subprocess.run([sys.executable, str(SCRIPT), "--worktree"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300, cwd=str(PROJ), env=_bare_env())
    combined = (r.stdout or "") + (r.stderr or "")
    assert "UnicodeEncodeError" not in combined, (
        "审计脚本在非 UTF-8 环境下崩了 —— CI 会红:\n" + combined[-600:])
    # 跑到最后一步 = 打印了统计行
    assert "统计:" in combined, (
        "审计脚本没跑到打印统计那一步 —— 说明中途崩了:\n" + combined[-600:])
    # 退出码只能是 0/1 (0=干净, 1=有致命命中), 不能是编码错误导致的异常值
    assert r.returncode in (0, 1), (
        f"退出码异常 {r.returncode} (期望 0 或 1):\n" + combined[-600:])


def test_audit_script_forces_utf8_stdout():
    """源码里必须显式接管 stdout 编码 —— 不能只靠 CI 配环境变量。"""
    src = SCRIPT.read_text(encoding="utf-8")
    assert "reconfigure" in src, "审计脚本没有接管 stdout 编码"
    assert "utf-8" in src, "审计脚本没有指定 utf-8"
