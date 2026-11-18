#!/usr/bin/env python3
"""检查并重打"微信语音优先走本地 STT"的补丁。

## 为什么需要它

2026-11-13 修了 Hermes 微信 adapter 的一个真 bug:
微信把一条语音放成**两个** item —— ``text_item`` (腾讯云 ASR 的文字, 中文
被转成英文乱码) 和 ``voice_item`` (原始音频, 可下载)。官方 ``_extract_text``
先扫 ``ITEM_TEXT`` -> 腾讯的坏 ASR 永远赢, gateway 的中央 STT 从没被调用。

修复是改 **Hermes 核心代码** (``plugins/platforms/weixin/adapter.py``) ——
而 `hermes update` **会覆盖它**。所以更新之后必须重打。

## 用法

    python scripts/apply_weixin_voice_patch.py           # 检查 + 缺了就打
    python scripts/apply_weixin_voice_patch.py --check   # 只检查, 不改
    python scripts/apply_weixin_voice_patch.py --status  # 看补丁状态

## 退出码

0 = 补丁在位 (或刚打上)
1 = 补丁不在且打不上 (需要人工介入)
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = Path(os.environ.get("HERMES_HOME") or os.path.join(
    os.path.expanduser("~"), "AppData", "Local", "hermes"))
AGENT = HOME / "hermes-agent"
ADAPTER = AGENT / "plugins" / "platforms" / "weixin" / "adapter.py"
PATCH = Path(PROJ) / "patches" / "0001-weixin-voice-prefer-local-stt-over-tencent-asr.patch"

MARKER = "def _has_raw_voice_audio("
GUARD_CALL = "if _has_raw_voice_audio(item_list):"


def patch_present() -> bool:
    if not ADAPTER.exists():
        return False
    src = ADAPTER.read_text(encoding="utf-8")
    if MARKER not in src or GUARD_CALL not in src:
        return False
    # 守卫必须在 ITEM_TEXT 扫描**之前** (注释不算)
    body = src[src.find("def _extract_text("):]
    code_lines = []
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("#"):
            continue
        code_lines.append(line)
    code = "\n".join(code_lines)
    g = code.find("_has_raw_voice_audio(")
    t = code.find("ITEM_TEXT")
    return g >= 0 and (t < 0 or g < t)


def apply_patch() -> tuple[bool, str]:
    if not PATCH.exists():
        return False, f"补丁文件不存在: {PATCH}"
    if not AGENT.exists():
        return False, f"找不到 Hermes 源码: {AGENT}"
    r = subprocess.run(["git", "apply", "--3way", str(PATCH)],
                       cwd=str(AGENT), capture_output=True, timeout=300)
    if r.returncode == 0:
        return True, "补丁已应用"
    # 3way 失败 -> 试普通 apply
    r2 = subprocess.run(["git", "apply", str(PATCH)],
                        cwd=str(AGENT), capture_output=True, timeout=300)
    if r2.returncode == 0:
        return True, "补丁已应用 (普通模式)"
    err = (r2.stderr or r2.stdout or b"").decode("utf-8", "replace")
    return False, f"打不上: {err.strip()[:400]}"


def backup() -> Path | None:
    if not ADAPTER.exists():
        return None
    bak = ADAPTER.with_suffix(f".py.bak-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(ADAPTER, bak)
    return bak


def main() -> int:
    ap = argparse.ArgumentParser(description="微信语音补丁管理")
    ap.add_argument("--check", action="store_true", help="只检查, 不改")
    ap.add_argument("--status", action="store_true", help="只看状态")
    args = ap.parse_args()

    if not ADAPTER.exists():
        print(f"❌ 找不到微信 adapter: {ADAPTER}")
        return 1

    if patch_present():
        print("✅ 补丁在位 —— 微信语音走本地 STT")
        if args.status or args.check:
            return 0

    print(f"❌ 补丁不在位 (adapter 被 {'' if args.status else 'hermes update '}"
          f"覆盖过, 或还没打)")
    print(f"   adapter: {ADAPTER}")
    print(f"   patch:   {PATCH}")

    if args.check or args.status:
        return 1

    bak = backup()
    if bak:
        print(f"   已备份原文件: {bak.name}")
    ok, msg = apply_patch()
    print(f"   {msg}")
    if ok and patch_present():
        print("✅ 补丁已应用并验证")
        print("   ⚠️ 需要重启 gateway 才生效")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
