"""微信语音: 原始音频优先于腾讯 ASR 的回归测试。

## 背景 (2026-11-13 实测)

用户在微信发中文语音, 怀恩收到的是:

    "before the government before the government"

—— 完全不是他说的中文。逐层查下来:

1. 微信把一条语音放成**两个** item:
   - ``text_item``  = 腾讯云 ASR 的文字 (中文被转成英文乱码)
   - ``voice_item`` = 原始音频 (带 media 块, 可下载)
2. 官方 adapter 的 ``_extract_text`` **先扫 ITEM_TEXT** -> 直接返回腾讯的
   ASR 文本, ``voice_item`` 分支根本走不到。
3. 于是 gateway 的中央 STT **从来没被调用** (日志里 "Transcribing user voice"
   出现 0 次), 怀恩拿到的是那份坏 ASR。

官方注释写着 "never trust Tencent's ``voice_item.text``" —— 但漏了
"腾讯也会把 ASR 塞进 ``text_item``" 这条路径。

## 修法

有可下载的原始音频时, ``_extract_text`` 返回空 -> 消息走 VOICE 事件 ->
中央 STT 转写下载到的音频。

## 为什么用静态检查而不是 import

微信 adapter 依赖 gateway 的运行时 (ruamel / aiohttp / 租约环境里的包) ——
公开副本和别的机器上 import 不了。所以这里**读源码**验证逻辑形状。

⚠️ 静态检查只能证明"代码写对了", 证明不了"运行真的对" —— 真实验收靠
用户在微信里发一条语音, 看怀恩收到的是不是本地 STT 的结果。
"""
from __future__ import annotations

from pathlib import Path

import pytest

HOME = Path(r"%LOCALAPPDATA%\AppData\Local\hermes")
ADAPTER = HOME / "hermes-agent" / "plugins" / "platforms" / "weixin" / "adapter.py"


def _adapter_source() -> str:
    if not ADAPTER.exists():
        pytest.skip("本机没有微信 adapter")
    return ADAPTER.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    """去掉 # 注释行和 docstring —— 注释里提到 ITEM_TEXT 会让位置判断失真。

    ⚠️ 踩过: 修复说明里写了"原实现先扫 ITEM_TEXT", 结果注释里的 ITEM_TEXT
    出现在守卫之前, 测试误判成"守卫位置错了"。
    """
    out = []
    in_doc = False
    for line in src.splitlines():
        stripped = line.strip()
        if in_doc:
            if stripped.endswith('"""'):
                in_doc = False
            out.append("")
            continue
        if stripped.startswith('"""') or stripped.startswith('\"\"\"'):
            if not (stripped.endswith('"""') and len(stripped) > 3):
                in_doc = True
            out.append("")
            continue
        # 去掉行尾注释 (简单处理: # 前后都要有内容才当注释)
        code = line.split("  # ", 1)[0] if "  # " in line else line
        out.append(code)
    return "\n".join(out)


def _body_of(src: str, marker: str) -> str:
    """取某个 def 的**函数体** (不含 def 那一行) 到下一个顶层 def 之间的源码。

    ⚠️ 必须跳过 def 行本身 —— 否则 marker 字符串会出现在 body 里,
    位置判断会失真 (守卫函数的定义里也含 ITEM_VOICE / marker)。
    """
    i = src.find(marker)
    assert i >= 0, f"源码里找不到 {marker}"
    rest = src[i + len(marker):]
    j = rest.find("\ndef ")
    body = rest[:j] if j >= 0 else rest
    return _strip_comments(body)


# ---------------------------------------------------------------- 辅助函数存在


def test_helper_exists():
    src = _adapter_source()
    assert "def _has_raw_voice_audio(" in src, \
        "缺 _has_raw_voice_audio —— 没有它就没法判断有没有原始音频"


def test_helper_checks_media_and_url():
    src = _adapter_source()
    body = _body_of(src, "def _has_raw_voice_audio(")
    assert "encrypt_query_param" in body, \
        "只判断 media 存在不够 —— 还要确认里面有可下载的地址"
    assert "full_url" in body, "也要认 full_url 形式的媒体地址"


def test_helper_only_looks_at_voice_items():
    src = _adapter_source()
    body = _body_of(src, "def _has_raw_voice_audio(")
    assert "ITEM_VOICE" in body, "必须只看语音 item —— 别的类型不算"


# ---------------------------------------------------------------- 核心行为


def test_extract_text_returns_empty_when_raw_audio_exists():
    """有原始音频 -> 返回空, 让中央 STT 去转下载到的音频。

    这是整个修复的核心: 腾讯那份 ASR 文本**不能**进消息。
    """
    src = _adapter_source()
    body = _body_of(src, "def _extract_text(")
    # 判定必须在扫描 ITEM_TEXT **之前**
    guard = body.find("_has_raw_voice_audio(")
    text_scan = body.find("ITEM_TEXT")
    assert guard >= 0, "_extract_text 里没有用 _has_raw_voice_audio 守卫"
    assert text_scan >= 0, "_extract_text 里找不到 ITEM_TEXT 扫描"
    assert guard < text_scan, (
        f"原始音频的判定必须在扫描 ITEM_TEXT **之前** "
        f"(guard@{guard} vs ITEM_TEXT@{text_scan}) —— 否则腾讯的坏 ASR 永远赢"
    )


def test_guard_returns_empty_string():
    src = _adapter_source()
    body = _body_of(src, "def _extract_text(")
    guard_pos = body.find("_has_raw_voice_audio(")
    tail = body[guard_pos:guard_pos + 400]
    assert "return \"\"" in tail or "return ''" in tail, \
        "有原始音频时必须返回空字符串 (空 -> 走 VOICE 事件 -> 中央 STT)"


def test_plain_text_path_preserved():
    """普通文字消息不受影响 —— ITEM_TEXT 的正常分支还在。"""
    src = _adapter_source()
    body = _body_of(src, "def _extract_text(")
    assert "text_item" in body, "普通文字的提取逻辑被删了"
    assert "ref_msg" in body, "引用媒体的处理被删了"


def test_voice_without_media_still_uses_weixin_asr():
    """没有原始音频时只能用腾讯的 ASR —— 总比什么都不给强。"""
    src = _adapter_source()
    body = _body_of(src, "def _extract_text(")
    assert "Voice transcription provided by Weixin" in body, \
        "没有可下载音频时应退回腾讯 ASR (并保留 voice 标记)"
