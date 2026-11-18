#!/usr/bin/env python3
"""微信语音链路验收 —— 重启 gateway 后跑这个, 一眼看出好没好。

## 为什么需要它

2026-11-13 修了两个坑:
  1. 微信 adapter 的 `_extract_text` 先扫 ITEM_TEXT -> 腾讯的坏 ASR 文本
     永远赢, 中央 STT 从没被调用 (改在 Hermes 侧, **要重启 gateway** 才生效)
  2. huaien profile 的 STT 配置是默认的 (`language: en`, prompt 空) ——
     profile 之间**不继承**主 Hermes 的配置

这个脚本做两件事:
  - **静态检查**: adapter 的修复在不在 (不依赖 gateway 进程)
  - **实测**: 拿 gateway 缓存里最近一条真语音, 跑官方转写路径, 看结果像不像中文

## 用法

    python scripts/verify_wechat_voice.py          # 全查
    python scripts/verify_wechat_voice.py --static # 只查代码 (不需要缓存音频)

## 退出码

0 = 一切正常 (代码改对了 + 实测转写出中文)
1 = 有问题 (看输出说哪一项)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = Path(os.environ.get("HERMES_HOME") or os.path.join(
    os.path.expanduser("~"), "AppData", "Local", "hermes"))
ADAPTER = HOME / "hermes-agent" / "plugins" / "platforms" / "weixin" / "adapter.py"
HUAien_CFG = HOME / "profiles" / "huaien" / "config.yaml"


def _strip_comments(src: str) -> str:
    out, in_doc = [], False
    for line in src.splitlines():
        s = line.strip()
        if in_doc:
            if s.endswith('"""'):
                in_doc = False
            out.append("")
            continue
        if s.startswith('"""'):
            if not (s.endswith('"""') and len(s) > 3):
                in_doc = True
            out.append("")
            continue
        out.append(line.split("  # ", 1)[0] if "  # " in line else line)
    return "\n".join(out)


def check_static() -> list[str]:
    """代码层面的检查 —— 不需要 gateway 在跑。"""
    problems: list[str] = []

    if not ADAPTER.exists():
        return [f"找不到微信 adapter: {ADAPTER}"]
    src = ADAPTER.read_text(encoding="utf-8")
    body = src[src.find("def _extract_text("):]
    body = _strip_comments(body)
    guard = body.find("_has_raw_voice_audio(")
    text_scan = body.find("ITEM_TEXT")
    if guard < 0:
        problems.append("adapter 的 _extract_text 没有原始音频守卫 —— "
                        "腾讯的坏 ASR 会直接进对话")
    elif text_scan >= 0 and guard > text_scan:
        problems.append("守卫在 ITEM_TEXT 扫描之后 —— 腾讯的坏 ASR 仍然赢")

    if not HUAien_CFG.exists():
        problems.append(f"找不到 huaien 配置: {HUAien_CFG}")
    else:
        cfg = HUAien_CFG.read_text(encoding="utf-8")
        stt = cfg[cfg.find("\nstt:"):] if "\nstt:" in cfg else ""
        stt = stt[:stt.find("\n\n")] if "\n\n" in stt else stt
        m = re.search(r"^\s*language:\s*(\S+)", stt, re.M)
        if not m:
            problems.append("huaien 的 stt.language 没配 —— 会按英文转中文")
        elif m.group(1).strip("'\"") != "zh":
            problems.append(f"huaien 的 stt.language = {m.group(1)} (应为 zh)")
        if "initial_prompt:" in stt:
            pm = re.search(r"initial_prompt:\s*(.*)", stt)
            prompt = (pm.group(1) if pm else "").strip()
            words = len(prompt.split())
            if words > 8:
                problems.append(
                    f"huaien 的 initial_prompt 有 {words} 个词 —— 太长会把 base "
                    "模型带偏成复读 (转写成 '飞书 看板 飞书 看板')。3-4 个词就够")
    return problems


def _latest_silk() -> Path | None:
    au = HOME / "cache" / "audio"
    if not au.exists():
        return None
    files = sorted(au.glob("*.silk"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def check_live() -> list[str]:
    """实测 —— 拿缓存里最近一条真语音跑官方转写。"""
    problems: list[str] = []
    silk = _latest_silk()
    if not silk:
        return ["缓存里没有 .silk 语音 —— 先在微信发一条语音, 再跑这个脚本"]

    print(f"  测试音频: {silk.name} ({silk.stat().st_size} B)")

    # 复刻 gateway 的运行环境 (skill: weixin-voice-and-gateway-runtime)
    pythons = sorted(HOME.glob("tools/python-3.14.9*/python.exe"))
    sps = sorted(HOME.glob("installs/*/environments/*/venv/Lib/site-packages"))
    if not pythons or not sps:
        return [f"找不到 gateway 的 python / site-packages (工具: {pythons} / {sps})"]

    env = os.environ.copy()
    env.update({
        "HF_ENDPOINT": "https://hf-mirror.com",
        "HF_HUB_DISABLE_XET": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "PYTHONPATH": f"{HOME / 'hermes-agent'};{sps[0]}",
        # 关键: 指向 huaien profile —— 模拟微信的真实作用域
        "HERMES_HOME": str(HOME / "profiles" / "huaien"),
        "PYTHONIOENCODING": "utf-8",
    })
    probe = HOME / "cache" / "scratch" / "_verify_voice.py"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text(
        "import sys, json\n"
        "from tools.transcription_tools import transcribe_audio\n"
        "print(json.dumps(transcribe_audio(sys.argv[1]), ensure_ascii=False))\n",
        encoding="utf-8", newline="\n")

    try:
        r = subprocess.run([str(pythons[0]), str(probe), str(silk)],
                           capture_output=True, timeout=900, env=env)
    except subprocess.TimeoutExpired:
        return ["转写超时 (CPU 上 base 模型可能慢, 再等一次)"]

    out = (r.stdout or b"").decode("utf-8", "replace").strip()
    if not out:
        return [f"转写没有输出: {(r.stderr or b'').decode('utf-8','replace')[-300:]}"]
    try:
        data = json.loads(out.splitlines()[-1])
    except (ValueError, IndexError):
        return [f"转写输出不是 JSON: {out[:200]}"]

    text = str(data.get("transcript") or "")
    print(f"  转写结果: {text!r}  (provider={data.get('provider')})")

    if not data.get("success"):
        problems.append(f"转写失败: {data.get('error')}")
    elif not text.strip():
        problems.append("转写结果是空的")
    else:
        # 中文占比
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        if cjk == 0:
            problems.append(f"转写结果没有中文: {text!r}")
        # 复读检测: 同一个 2-4 字词连续出现 3 次以上
        toks = text.split()
        for t in set(toks):
            if len(t) >= 2 and text.count(t) >= 3:
                problems.append(
                    f"转写结果在复读 ({t!r} 出现 {text.count(t)} 次) —— "
                    "initial_prompt 太长或音频坏了")
                break
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="微信语音链路验收")
    ap.add_argument("--static", action="store_true", help="只查代码, 不跑实测")
    args = ap.parse_args()

    print("=== 静态检查 ===")
    problems = check_static()
    for p in problems:
        print(f"  ❌ {p}")
    if not problems:
        print("  ✅ adapter 守卫在位, huaien 的 STT 配置正确")

    if not args.static:
        print("\n=== 实测转写 ===")
        live = check_live()
        for p in live:
            print(f"  ❌ {p}")
        if not live:
            print("  ✅ 转写正常")
        problems += live

    print()
    if problems:
        print(f"❌ 有 {len(problems)} 项问题")
        return 1
    print("✅ 微信语音链路正常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
