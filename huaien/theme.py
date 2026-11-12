"""怀恩 — 极简科技风主题

一整块黑, 一点点光。
没有按钮, 没有菜单, 没有标题栏。
只有一行字 (你说什么), 一个光标 (我在听), 一段回复 (我说什么)。

设计约束 (用户定):
  - 极简到只剩一个对话框
  - 中文必须能正常输入 (IME) → 输入框用原生 Text, 不手工模拟键盘
  - 字必须看得清 → 字号 / 对比度 都要够
"""
from __future__ import annotations

# ---- 颜色 (纯黑科技风) ----
BG = "#000000"          # 背景: 纯黑
FG = "#F2F2F2"          # 主文字 (对比度 ~18:1)
FG_DIM = "#9A9A9A"      # 次级文字 (对比度 ~7.5:1)
FG_FAINT = "#7A7A7A"    # 极弱 (对比度 4.9:1, WCAG 正文达标)
ACCENT = "#7DF9C7"      # 唯一强调色 (薄荷绿, 只用于光标)
ACCENT_DIM = "#4E8C74"  # 强调色暗态 (对比度 5.3:1 —— 原来 2.79:1 几乎看不见)
ERR = "#FF6B6B"         # 错误 (在纯黑上够亮)

# ---- 字体 ----
# 96 DPI 下 15pt 只有 27px 行高, 中文看着发虚 —— 这里整体上调。
FONT_MAIN = ("Microsoft YaHei UI", 17)   # 用户输入 / 正文
FONT_BIG = ("Microsoft YaHei UI", 21)    # 管家回复 (要压得住场)
FONT_HINT = ("Microsoft YaHei UI", 12)   # 提示语 (原来 10pt 太小)

# ---- 尺寸 ----
WINDOW_W = 780
WINDOW_H = 560
PADDING = 48
DRAG_H = 34             # 顶部无内容拖动带高度 (看不见, 但能拖窗口)
ENTRY_MAX_LINES = 4     # 输入框最多长到几行, 超过就内部滚动
CURSOR_ON_MS = 530      # 光标闪烁周期
CURSOR_DRAW_MS = 15     # 光标每帧时长
TYPING_CHARS_PER_TICK = 2   # 打字机: 每个 tick 吐几个字符
TYPING_TICK_MS = 16
