"""怀恩 — 一键安装敏感信息审计 pre-commit 钩子

## 为什么

用户的要求 (2026-11-04): **"防止下次更新到github时, 又把敏感信息公布出去"**。

光靠人记得跑审计器 = 迟早会忘。**装上钩子之后, 每次 `git commit` 自动跑,
rc=1 就拒绝提交** —— 敏感值进不了历史, 比事后清理便宜一百倍。

## 装法

```bash
python scripts/install_audit_hook.py          # 装
python scripts/install_audit_hook.py --check  # 只看状态
python scripts/install_audit_hook.py --remove # 卸
```

## 钩子的性质 (必须知道)

- **只在本机生效** —— 别人 clone 不会自动有。远端那道闸是 CI。
- 钩子失败**不阻止 `--no-verify`** —— 这是有意的逃生门: 万一审计器本身坏了,
  不该把人锁死。**但别用它绕过真问题。**
"""

from __future__ import annotations

import argparse
import contextlib
import os
import stat
import sys

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VPY = os.path.join(PROJ, ".venv", "Scripts", "python.exe")
HOOKS_DIR = os.path.join(PROJ, ".git", "hooks")

HOOK_NAME = "pre-commit"
MARK = "# >>> huaien sensitive-info audit >>>"
END = "# <<< huaien sensitive-info audit <<<"

HOOK_BODY = """#!/bin/sh
__MARK__
# 自动生成, 改这里没用 —— 改 scripts/install_audit_hook.py 里的 HOOK_BODY
# 作用: 提交前扫敏感信息, 命中就拒绝提交 (防泄露进 git 历史)
set -e

PY=""
for cand in "__VPY__" "__PROJ_VENV__" "python" "python3"; do
    if [ -x "$cand" ]; then PY="$cand"; break; fi
    if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done

if [ -z "$PY" ]; then
    echo "[audit] 找不到 python, 跳过 (请手动跑 scripts/audit_publish.py)"
    exit 0
fi

if ! "$PY" "__AUDIT__" --worktree; then
    echo ""
    echo "[audit] X 检出敏感信息, 拒绝提交。"
    echo "[audit]    按上面的 文件:行号 定位并脱敏后重试。"
    echo "[audit]    确认是误报可以加 --no-verify, 但先想清楚。"
    exit 1
fi
exit 0
__END__
"""

def _posix(path: str) -> str:
    """Windows 路径 -> sh 能用的形式。

    ⚠️ 反斜杠在 sh 里是转义符, 写成 C:\\Users\\x 会被吃掉。
    MSYS/Git-Bash 能执行 `C:/Users/x/python.exe`, 所以统一转正斜杠。
    """
    return path.replace("\\", "/")


HOOK_BODY = (HOOK_BODY
             .replace("__MARK__", MARK)
             .replace("__END__", END)
             .replace("__VPY__", _posix(VPY))
             .replace("__PROJ_VENV__",
                      _posix(os.path.join(PROJ, ".venv", "Scripts", "python.exe")))
             .replace("__AUDIT__",
                      _posix(os.path.join(PROJ, "scripts", "audit_publish.py"))))


def hook_path() -> str:
    return os.path.join(HOOKS_DIR, HOOK_NAME)


def install() -> int:
    os.makedirs(HOOKS_DIR, exist_ok=True)
    path = hook_path()
    # 已装过就只更新, 不叠加
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            old = fh.read()
        if MARK in old:
            start = old.index(MARK)
            # 保留用户自己加的部分, **整块替换**旧的自有段
            # (只取 head + body 会把 `#!/bin/sh` 留两遍)
            head = old[:start]
            if head.count("#!/bin/sh") and MARK in old:
                head = ""          # 自有段就是完整钩子 -> 整体换掉
            new = head + HOOK_BODY
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(new)
            _make_exec(path)
            print(f"[audit] 已更新钩子: {path}")
            return 0
        # 有别人的钩子 -> 别覆盖, 追加
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(old.rstrip("\n") + "\n\n" + HOOK_BODY)
        _make_exec(path)
        print(f"[audit] 已追加到已有钩子: {path}")
        return 0
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(HOOK_BODY)
    _make_exec(path)
    print(f"[audit] 已安装钩子: {path}")
    return 0


def _make_exec(path: str) -> None:
    with contextlib.suppress(OSError):
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def remove() -> int:
    path = hook_path()
    if not os.path.exists(path):
        print("[audit] 钩子不存在")
        return 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    if MARK not in text:
        print("[audit] 钩子不是本工具装的, 不动它")
        return 0
    head = text[:text.index(MARK)].rstrip("\n")
    if head.strip():
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(head + "\n")
    else:
        os.remove(path)
    print("[audit] 已卸载钩子")
    return 0


def check() -> int:
    path = hook_path()
    if not os.path.exists(path):
        print("[audit] ❌ 钩子未安装")
        return 1
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    if MARK in text:
        print(f"[audit] ✅ 钩子已安装: {path}")
        return 0
    print(f"[audit] ⚠️ 钩子存在但不是本工具装的: {path}")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="安装/卸载敏感信息审计 pre-commit 钩子")
    ap.add_argument("--check", action="store_true", help="只看状态")
    ap.add_argument("--remove", action="store_true", help="卸载")
    args = ap.parse_args()
    if args.check:
        return check()
    if args.remove:
        return remove()
    rc = install()
    return rc


if __name__ == "__main__":
    sys.exit(main())
