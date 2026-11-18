"""前台文本排版 —— 单元测试

覆盖:
  - 中文按字边界断行
  - 英文单词不被拆断
  - 每行不超宽
  - 短文本不换行 / 原文换行保留
"""
from __future__ import annotations

import sys
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from huaien import app as app_mod
from huaien import theme


def _app():
    try:
        a = app_mod.HuaiEnApp()
    except tk.TclError:                      # 无显示环境 (CI)
        return None
    a.root.update()
    return a


def test_tokens_split_cjk_per_char_and_latin_per_word():
    a = _app()
    if a is None:
        return
    try:
        toks = a._tokens("你好 world")
        assert toks[:3] == ["你", "好", " "]      # 中文逐字
        assert "world" in toks                    # 英文按词
    finally:
        a.root.destroy()


def test_wrap_never_exceeds_width():
    a = _app()
    if a is None:
        return
    try:
        w = a.root.winfo_width() - theme.PADDING * 2
        if w <= 1:
            w = theme.WINDOW_W - theme.PADDING * 2
        f = tkfont.Font(root=a.root, font=theme.FONT_BIG)
        text = ("用的就是 Hermes 本身。模型、工具、记忆、会话库全是 Hermes 原生的，"
                "前台只负责显示和输入。Hermes 里用 -p huaien sessions list 能看到全部对话。")
        for line in a._wrap(text, theme.FONT_BIG, w).split("\n"):
            assert f.measure(line) <= w, f"超宽: {line!r}"
    finally:
        a.root.destroy()


def test_wrap_keeps_short_text_and_paragraph_breaks():
    a = _app()
    if a is None:
        return
    try:
        assert a._wrap("短句。", theme.FONT_BIG, 600) == "短句。"
        assert a._wrap("第一段\n第二段", theme.FONT_BIG, 600) == "第一段\n第二段"
    finally:
        a.root.destroy()


def test_wrap_breaks_cjk_by_character():
    """中文必须按完整汉字断行, 且一个字符都不能丢。"""
    a = _app()
    if a is None:
        return
    try:
        src = "一二三四五六七八九十" * 6
        out = a._wrap(src, theme.FONT_BIG, 200)
        lines = out.split("\n")
        # 内容一个字符都不能丢 (除换行)
        assert "".join(lines) == src, "换行丢了字符"
        # 确实断成了多行
        assert len(lines) > 1, "这么长的中文在 200px 下应该换行"
        # 每行都是完整汉字串 (没有半个字)
        for line in lines:
            assert line == "" or all("\u4e00" <= c <= "\u9fff" for c in line), line
    finally:
        a.root.destroy()


def test_theme_contrast_is_readable():
    def lum(h):
        h = h.lstrip("#")
        vals = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        def lin(c):
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

        return 0.2126 * lin(vals[0]) + 0.7152 * lin(vals[1]) + 0.0722 * lin(vals[2])

    def ratio(c):
        return (lum(c) + 0.05) / 0.05

    assert ratio(theme.FG) >= 7.0, "主文字对比度不足"
    assert ratio(theme.FG_DIM) >= 4.5, "次级文字对比度不足"
    assert ratio(theme.FG_FAINT) >= 4.5, "弱文字对比度不足 (原来 2.37:1 看不见)"
    assert ratio(theme.ACCENT) >= 7.0
    assert ratio(theme.ACCENT_DIM) >= 3.0, "横线对比度不足 (原来 2.79:1 看不见)"
    assert ratio(theme.ERR) >= 4.5
