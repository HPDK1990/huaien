"""怀恩 — 前台

一个对话框。
上面是他说的话, 下面是管家的话, 最底下是一条线和一根会呼吸的光标。
没有按钮。没有菜单。没有标题栏。

  ┌─────────────────────────────────┐
  │                                 │
  │            你说的话               │
  │                                 │
  │            管家说的话             │
  │                                 │
  │                                 │
  │  ─────────────────────────  ▌   │
  └─────────────────────────────────┘

Enter 发送 · Shift+Enter 换行 · Esc 清空重开 · Ctrl+Q 退出

⚠️ 输入框必须用原生 tk.Text —— 它走 Windows IME, 中文候选框才能正常工作。
   早先版本用 canvas + <Key> 手工模拟键盘 (e.char), IME 组合时 e.char 为空,
   结果一个中文都打不出来。
"""
from __future__ import annotations

import contextlib
import ctypes
import os
import queue
import subprocess
import sys
import tkinter as tk
import tkinter.font as tkfont

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from huaien import theme
from huaien.bridge import ChatBridge
from huaien.runtime import clean_env

HINT = "说点什么…"
SELFTEST_PROMPT = "用一句话说: 你好"


def _enable_dpi_awareness():
    """让 Tk 知道真实 DPI, 否则高缩放屏上字会发虚。必须在建窗口前调。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return
    except Exception:
        pass
    with contextlib.suppress(Exception):
        ctypes.windll.user32.SetProcessDPIAware()


class HuaiEnApp:
    def __init__(self):
        _enable_dpi_awareness()
        self.root = tk.Tk()
        self.root.title("怀恩")
        self.root.configure(bg=theme.BG)
        self.root.geometry(f"{theme.WINDOW_W}x{theme.WINDOW_H}")
        self.root.minsize(560, 380)
        self.root.bind("<Escape>", lambda e: self.reset())
        self.root.bind("<Control-q>", lambda e: self.root.destroy())
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)

        # 去掉标题栏 —— 只留内容
        self.root.overrideredirect(True)
        self._center()
        self._drag_origin = None

        # 状态
        self.bridge = ChatBridge()
        self.q: queue.Queue[tuple] = queue.Queue()
        self.user_display = ""       # 用户已经说过的话 (发送后保留, 用于显示)
        self.reply = ""              # 管家回复 (已收到)
        self.shown = ""              # 已显示的回复 (打字机进度)
        self.pending = ""            # 待显示的增量
        self.state = "idle"          # idle | thinking | typing
        self._cursor_on = True
        self._last_flip = 0
        self._entry_cache = ""      # 输入框上次同步时的内容 (用于状态比对)
        self._elapsed = 0.0         # 本轮已用秒数
        self._t0 = 0.0              # 本轮起始时间
        self._error = ""            # 本轮错误 (非空时状态行显示红色)
        self.artifacts: list = []   # 本轮 agent 交回来的产物

        self._build()
        self.root.after(40, self._tick)
        self.root.after(60, self._drain)
        # 焦点直接落在输入框 —— 用户说"无法输入", 焦点不在这里等于没做。
        self.entry.focus_set()
        self.root.after(150, self.entry.focus_set)

    # ---------------- 界面 ----------------
    def _build(self):
        # ---- 输出区 (Canvas) ----
        self.canvas = tk.Canvas(self.root, bg=theme.BG, highlightthickness=0,
                                bd=0, insertwidth=0)
        self.canvas.pack(side="top", fill="both", expand=True)

        self.t_hint = self.canvas.create_text(theme.PADDING, theme.PADDING, anchor="nw",
                                              text="", fill=theme.FG_FAINT, font=theme.FONT_HINT,
                                              tags=("hint",))
        self.t_user = self.canvas.create_text(theme.PADDING, theme.PADDING,
                                              anchor="nw", text="", fill=theme.FG_DIM,
                                              font=theme.FONT_MAIN,
                                              width=theme.WINDOW_W - theme.PADDING * 2,
                                              tags=("user",))
        self.t_reply = self.canvas.create_text(theme.PADDING, theme.PADDING,
                                               anchor="nw", text="", fill=theme.FG,
                                               font=theme.FONT_BIG,
                                               width=theme.WINDOW_W - theme.PADDING * 2,
                                               tags=("reply",))
        # 状态行: 极简的一行灰字 (正在想 / 已用时 / 出错)。不展开成面板。
        self.t_status = self.canvas.create_text(theme.PADDING, theme.PADDING,
                                                anchor="nw", text="", fill=theme.FG_DIM,
                                                font=theme.FONT_HINT, tags=("status",))
        # 产物行: agent 交回来的文件/图片, 每件一行, 双击打开。
        self.t_arts = self.canvas.create_text(theme.PADDING, theme.PADDING,
                                              anchor="nw", text="", fill=theme.FG_DIM,
                                              font=theme.FONT_HINT, tags=("arts",))

        # ---- 底部容器: 横线 + 输入框 ----
        # 用容器而不是靠 pack 顺序 —— side="bottom" 时先 pack 的在更下面,
        # 之前横线被挤到了输入框底下, 根本看不见。
        self.bottom = tk.Frame(self.root, bg=theme.BG)
        self.bottom.pack(side="bottom", fill="x")

        self.line = tk.Frame(self.bottom, bg=theme.ACCENT_DIM, height=1)
        self.line.pack(side="top", fill="x", padx=theme.PADDING, pady=(0, 2))

        # ---- 输入框 (原生 Text, IME 友好) ----
        self.entry = tk.Text(
            self.bottom,
            height=1, width=10,
            bg=theme.BG, fg=theme.FG,
            font=theme.FONT_MAIN,
            relief="flat", borderwidth=0, highlightthickness=0,
            insertbackground=theme.ACCENT,      # 光标 = 唯一强调色
            insertwidth=2,
            padx=0, pady=6,
            spacing1=0, spacing2=0, spacing3=0,
            wrap="word", undo=True, maxundo=80,
            cursor="xterm",
        )
        self.entry.pack(side="top", fill="x", padx=theme.PADDING,
                        pady=(0, theme.PADDING - 6))
        self.entry.bind("<Return>", self._on_return)
        self.entry.bind("<FocusIn>", lambda e: self._sync_hint())
        self.entry.bind("<FocusOut>", lambda e: self._sync_hint())

        # 占位提示 (叠在输入框上, 有字就藏起来)
        self.hint_label = tk.Label(self.bottom, text=HINT, bg=theme.BG, fg=theme.FG_DIM,
                                   font=theme.FONT_MAIN, anchor="w", bd=0)
        self.hint_label.bind("<Button-1>", lambda e: self.entry.focus_set())

        # 滚轮 (输出区)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        # 点输出区 → 聚焦输入框
        self.canvas.bind("<Button-1>", self._on_canvas_click)
        # 顶部拖动带
        self.canvas.bind("<ButtonPress-1>", self._on_drag_start)
        self.canvas.bind("<B1-Motion>", self._on_drag_move)

        self._sync_hint()

    def _on_canvas_click(self, e):
        """点输出区: 不是拖动带就聚焦输入框。"""
        if e.y < theme.DRAG_H:
            return
        self.entry.focus_set()

    def _on_wheel(self, e):
        self.canvas.yview_scroll(-1 * (e.delta // 120), "units")

    # ---------------- 拖动窗口 ----------------
    def _screen_size(self):
        """真实屏幕尺寸。

        Tkinter 在窗口还没映射时 winfo_screenwidth() 会返回 1 (Windows 上尤其),
        所以这里直接问 Win32, Tk 只当兜底。
        """
        try:
            u = ctypes.windll.user32
            return u.GetSystemMetrics(0), u.GetSystemMetrics(1)   # 0=宽 1=高
        except Exception:
            sw = self.root.winfo_screenwidth() or 1920
            sh = self.root.winfo_screenheight() or 1080
            return sw, sh

    def _center(self):
        """居中显示, 并提到最前 (无边框窗口容易被别的东西盖住)。"""
        sw, sh = self._screen_size()
        x = max(0, (sw - theme.WINDOW_W) // 2)
        y = max(0, (sh - theme.WINDOW_H) // 3)
        self.root.geometry(f"+{x}+{y}")
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(600, lambda: self.root.attributes("-topmost", False))
        # 窗口映射后再按真实位置微调一次。
        # ⚠️ 这里只改位置, 不能再调 lift/topmost —— 反复 raise 会打断正在进行的绘制,
        #    导致画面被清空 (自检曾抓到截图退化成 2698 B 的空窗口)。
        self.root.after(200, self._reposition)

    def _reposition(self):
        sw, sh = self._screen_size()
        x = max(0, (sw - self.root.winfo_width()) // 2)
        y = max(0, (sh - self.root.winfo_height()) // 3)
        self.root.geometry(f"+{x}+{y}")

    def _on_drag_start(self, e):
        if e.y >= theme.DRAG_H:
            return
        self._drag_origin = (e.x_root - self.root.winfo_x(),
                             e.y_root - self.root.winfo_y())

    def _on_drag_move(self, e):
        if self._drag_origin:
            self.root.geometry(f"+{e.x_root - self._drag_origin[0]}+{e.y_root - self._drag_origin[1]}")

    # ---------------- 输入框 ----------------
    def _sync_entry_state(self, force: bool = False):
        """文本变了 → 同步占位提示 + 自适应高度。

        ⚠️ 不能靠 <<TextChanged>> 虚拟事件 —— 实测本机 Tk 8.6.12 该事件根本不触发
        (连真实按键、insert/delete 都不触发)。改用状态比对, 由 _tick 定期调。
        """
        text = self.entry.get("1.0", "end-1c")
        if text == self._entry_cache and not force:
            return
        self._entry_cache = text
        self._sync_hint()
        lines = int(self.entry.index("end-1c").split(".")[0])
        want = max(1, min(lines, theme.ENTRY_MAX_LINES))
        if int(self.entry.cget("height")) != want:
            self.entry.configure(height=want)

    def _sync_hint(self):
        """空的时候显示占位提示。"""
        empty = not self._entry_cache.strip()
        if empty:
            with contextlib.suppress(tk.TclError):
                self.hint_label.place(x=theme.PADDING + 3, y=self._entry_top() + 6)
        else:
            self.hint_label.place_forget()

    def _entry_top(self):
        """输入框在 bottom 容器里的 y 坐标 (占位提示要贴着它)。"""
        try:
            return self.entry.winfo_rooty() - self.bottom.winfo_rooty()
        except tk.TclError:
            return 3

    def _entry_text(self) -> str:
        return self.entry.get("1.0", "end-1c").strip()

    def _set_entry_text(self, text: str):
        self.entry.delete("1.0", "end")
        if text:
            self.entry.insert("1.0", text)
        # 不要在这里写 _entry_cache —— 那样 _sync_entry_state 会认为"没变化"
        # 而提前返回, 输入框高度就永远不更新了 (曾因此只长 1 行)。
        self._sync_entry_state(force=True)

    # ---------------- 发送 ----------------
    def _on_return(self, e):
        # Shift+Enter → 换行 (返回 None 让 Text 默认行为生效)
        if getattr(e, "state", 0) & 0x0001:
            return None
        msg = self._entry_text()
        if not msg:
            return "break"
        if self.bridge.busy:
            # ⚠️ 不能静默丢弃 —— 用户的话会凭空消失, 而他根本不知道发生了什么。
            # 明确告诉他"还在处理上一条", 并把这句话留在输入框里等下一轮。
            self._error = "还在处理上一条, 稍后再发。"
            self._layout()
            return "break"
        self.user_display = (self.user_display + ("\n" if self.user_display else "") + msg)
        self._set_entry_text("")
        self.reply = ""
        self.shown = ""
        self.pending = ""
        self.state = "thinking"
        self._layout()
        self.bridge.ask(
            msg,
            on_start=lambda: self.q.put(("start",)),
            on_text=lambda p: self.q.put(("text", p)),
            on_done=lambda t, sid: self.q.put(("done", t, sid)),
            on_error=lambda m: self.q.put(("error", m)),
        )
        return "break"

    def reset(self):
        self.bridge.reset()
        self.user_display = ""
        self._set_entry_text("")
        self.reply = ""
        self.shown = ""
        self.pending = ""
        self.state = "idle"
        # 这几项必须一起清 —— 不清的话, Esc 之后上一轮的错误提示/产物/计时
        # 还会留在屏幕上, 用户会莫名其妙看到一句旧报错。
        self._error = ""
        self.artifacts = []
        self._elapsed = 0.0
        self._t0 = 0.0
        self._layout()

    # ---------------- 状态与产物 ----------------
    def _status_text(self) -> str:
        """状态行文案。极简: 最多一行。

        用户明确"不想看后台跑的程序和东西" —— 所以这里只给一个极简信号,
        不展开成面板、不列任务、不刷日志。
        """
        if self._error:
            return self._error
        if self.state == "thinking":
            # 前 8 秒留空 —— 回复区本身已经在显示省略号了, 两个省略号叠加很蠢。
            # 超过 8 秒才给"还在想 · N 秒", 让用户知道它没死。
            if self._elapsed < 8:
                return ""
            return f"还在想 · {int(self._elapsed)} 秒"
        if self.state == "typing":
            return ""
        return ""

    def _artifacts_text(self) -> str:
        """产物行文案。每件一行, 最多显示 3 件, 多余的合并成一行。"""
        if not self.artifacts:
            return ""
        lines = []
        for a in self.artifacts[:3]:
            name = os.path.basename(a.path.rstrip("\\/")) or a.path
            mark = "" if a.exists else " (不在)"
            lines.append(f"▸ {name}{mark}")
        if len(self.artifacts) > 3:
            lines.append(f"▸ 还有 {len(self.artifacts) - 3} 件")
        return "\n".join(lines)

    def _open_artifact(self, _e=None):
        """双击产物行 → 用系统默认程序打开对应文件。"""
        if not self.artifacts:
            return
        try:
            y = self.canvas.canvasy(self.canvas.winfo_pointery())
            idx = self._artifact_index_at(y)
            if idx is None:
                return
            art = self.artifacts[idx]
            if not art.exists:
                return
            if os.name == "nt":
                os.startfile(art.path)          # noqa: S606 - 用户双击才触发
            else:
                import subprocess as sp
                sp.Popen(["xdg-open", art.path])
        except Exception as e:
            self._error = f"打不开: {e}"

    def _artifact_index_at(self, y: float):
        """按 y 坐标算出点到的是第几件产物。"""
        bb = self.canvas.bbox("arts")
        if not bb or not self.artifacts:
            return None
        rel = y - bb[1]
        line_h = max(1, (bb[3] - bb[1]) // max(1, min(3, len(self.artifacts))))
        idx = int(rel // line_h)
        return idx if 0 <= idx < min(3, len(self.artifacts)) else None

    # ---------------- 文本排版 ----------------
    def _tokens(self, para: str) -> list[str]:
        """把一段文字切成排版单元: 中文逐字, 英文按词, 空格单独。"""
        toks: list[str] = []
        i = 0
        n = len(para)
        while i < n:
            ch = para[i]
            if ch.isascii() and (ch.isalnum() or ch in "._-/:"):
                j = i
                while j < n and para[j].isascii() and (para[j].isalnum() or para[j] in "._-/:"):
                    j += 1
                toks.append(para[i:j])
                i = j
            else:
                toks.append(ch)
                i += 1
        return toks

    def _wrap(self, text: str, font, max_px: int) -> str:
        """按真实字体宽度自己换行。

        ⚠️ 不能靠 Canvas 的 width= 自动换行 —— 实测 Tk 的 canvas text 换行在
        中文里很不可靠: 684px 的框, 一句 40 字的中文被拆成 "用的就是 Hermes" /
        "本身。模型、工具、记忆、会话库全是 Hermes" / "原生的，…" 这种
        前两行远没占满就断开的排版。Tk 的算法只认空格断点, 中英混排就废了。
        这里改用 tkfont 精确测量, 中文在字边界断, 英文在词边界断。
        """
        if not text or max_px <= 0:
            return text
        f = tkfont.Font(root=self.root, font=font)
        out: list[str] = []
        for para in text.split("\n"):
            line = ""
            for tok in self._tokens(para):
                if line and f.measure(line + tok) > max_px:
                    out.append(line.rstrip())
                    line = "" if tok.isspace() else tok
                else:
                    line += tok
            out.append(line)
        return "\n".join(out)

    # ---------------- 布局 ----------------
    def _layout(self):
        # ⚠️ 不能用 `winfo_width() or WINDOW_W` —— 窗口还没映射时 winfo_width()
        #    返回 1, 而 1 是 truthy, 于是宽度算成 1-96 = -95, Canvas 按负宽度换行,
        #    一句话被拆成每行一个词。必须显式判 "<= 1" 才算无效。
        w = self.root.winfo_width()
        if w <= 1:
            w = theme.WINDOW_W
        pad = theme.PADDING
        width = w - pad * 2

        # 关掉 Canvas 自动换行 (width=0), 改用自己算好的 \n。
        self.canvas.itemconfig("user", width=0)
        self.canvas.itemconfig("reply", width=0)

        # --- 顶部提示 (只在完全空闲时) ---
        idle = (not self.user_display and not self.reply and self.state == "idle")
        if idle:
            self.canvas.itemconfig("hint", text="怀恩 · 在", fill=theme.FG_FAINT)
            self.canvas.coords("hint", pad, pad)
            self.canvas.itemconfig("hint", state="normal")
        else:
            self.canvas.itemconfig("hint", state="hidden")

        # --- 用户说过的话 ---
        y = pad + theme.DRAG_H
        if self.user_display:
            self.canvas.itemconfig("user",
                                   text=self._wrap(self.user_display, theme.FONT_MAIN, width),
                                   state="normal")
            self.canvas.coords("user", pad, y)
            bb = self.canvas.bbox("user")
            y = (bb[3] + 24) if bb else (y + 26)
        else:
            self.canvas.itemconfig("user", text="", state="hidden")

        # --- 管家回复 (思考中显示省略号) ---
        text = self.shown
        if self.state == "thinking" and not text:
            text = "…"
        self.canvas.itemconfig("reply",
                               text=self._wrap(text, theme.FONT_BIG, width),
                               state="normal")
        self.canvas.coords("reply", pad, y)
        bb = self.canvas.bbox("reply")
        y = (bb[3] + 12) if bb else (y + 30)

        # --- 状态行 (一行灰字, 不展开) ---
        status = self._status_text()
        if status:
            self.canvas.itemconfig("status", text=status, state="normal",
                                   fill=theme.ERR if self._error else theme.FG_DIM)
            self.canvas.coords("status", pad, y)
            bb2 = self.canvas.bbox("status")
            y = (bb2[3] + 8) if bb2 else (y + 20)
        else:
            self.canvas.itemconfig("status", state="hidden")

        # --- 产物行 ---
        arts = self._artifacts_text()
        if arts:
            self.canvas.itemconfig("arts", text=arts, state="normal")
            self.canvas.coords("arts", pad, y)
            bb3 = self.canvas.bbox("arts")
            y = (bb3[3] + 8) if bb3 else (y + 20)
        else:
            self.canvas.itemconfig("arts", state="hidden")

        # --- 滚动到底 ---
        self.canvas.update_idletasks()
        bb = self.canvas.bbox("arts") or self.canvas.bbox("reply")
        if bb:
            self.canvas.configure(scrollregion=(0, 0, w, bb[3] + pad))
            if self.state == "typing":
                self.canvas.yview_moveto(1.0)

    # ---------------- 主循环 ----------------
    def _drain(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "start":
                    self.state = "thinking"
                elif kind == "text":
                    self.pending += item[1]
                    if self.state != "typing":
                        self.state = "typing"
                elif kind == "done":
                    self.reply = item[1] or self.reply
                    self.pending = ""
                    # 回答已经完整到手, 但打字机还没吐完 -> 继续 typing。
                    # 如果 pending 本来就是空的, 直接回 idle, 让外部知道"说完了"。
                    self.state = "typing" if self.shown != self.reply else "idle"
                elif kind == "error":
                    self.reply = f"（{item[1]}）"
                    self.pending = ""
                    self.state = "idle"
        except queue.Empty:
            pass
        self._layout()
        self.root.after(60, self._drain)

    def _tick(self):
        now = self.root.winfo_time() if hasattr(self.root, "winfo_time") else 0
        # 输入框状态同步 (每 3 帧 ≈ 45ms 一次, 够跟手)
        self._tick_n = getattr(self, "_tick_n", 0) + 1
        if self._tick_n % 3 == 0:
            self._sync_entry_state()
        # 打字机
        if self.pending:
            take = self.pending[:theme.TYPING_CHARS_PER_TICK]
            self.pending = self.pending[len(take):]
            self.shown += take
            self.canvas.itemconfig("reply", text=self.shown)
            self._layout()
        elif self.state == "typing" and self.shown == self.reply:
            # 全部吐完了 -> 回到 idle (回答结束)
            self.state = "idle"
        # 光标呼吸 (Text 自带光标, 这里只调颜色)
        if now - self._last_flip >= theme.CURSOR_ON_MS:
            self._last_flip = now
            self._cursor_on = not self._cursor_on
            col = theme.ACCENT if (self._cursor_on or self.state != "idle") else theme.ACCENT_DIM
            self.entry.configure(insertbackground=col)
        self.root.after(theme.CURSOR_DRAW_MS, self._tick)

    def run(self):
        self.entry.focus_set()
        self.root.after(150, self.entry.focus_set)
        self.root.mainloop()

    # ---------------- 自检 (自动化验证用) ----------------
    def selftest(self) -> int:
        """自动发一句话, 等回复, 截图, 打印结论, 退出。"""
        out = {}
        orig_ask = self.bridge.ask

        def on_done(text, sid):
            out["text"], out["sid"] = text, sid
            self.root.after(300, self._selftest_finish)

        def ask_spy(msg, **kw):
            kw["on_done"] = on_done
            return orig_ask(msg, **kw)
        self.bridge.ask = ask_spy

        self.root.after(600, self._selftest_send)
        self.root.mainloop()
        print("[SELFTEST] 回复:", (out.get("text") or "")[:160])
        print("[SELFTEST] session:", (out.get("sid") or "")[:32])
        return 0 if out.get("text") else 1

    def _selftest_send(self):
        self._set_entry_text(SELFTEST_PROMPT)
        self.root.after(700, lambda: self._on_return(_FakeEvent()))

    def _shot(self, rel_path: str):
        """截**自己窗口** (不是整个屏幕), 存到项目 logs/ 下。

        ⚠️ 两个坑, 都是踩出来的:
        1. 必须用 bbox 精确截自己的窗口 —— 整屏截图会抓到**别的窗口**
           (实测抓到过一张太空卡车插画, 494 KB 直接进了 Git 历史)。
        2. 输出必须落在 **logs/** —— 落在 docs/ 会被封存脚本当成
           "给用户看的界面图" 复制进版本包, 也就是又一条污染链。
        """
        try:
            x = self.root.winfo_rootx()
            y = self.root.winfo_rooty()
            w = self.root.winfo_width()
            h = self.root.winfo_height()
            # 窗口尺寸异常 (还没映射) 时不截 —— 截出来是别的窗口
            if w <= 1 or h <= 1:
                print("[SHOT] 窗口尺寸异常, 跳过")
                return
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), rel_path)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # ⚠️ **子进程截图 + 干净环境** ——
            #    主进程的 sys.path 里 PYTHONPATH 排在 venv 前面 (Hermes 运行时注入),
            #    import PIL 会拿到**别的环境**的 Pillow, 版本对不上就报
            #    "cannot import name '_imaging' from 'PIL'"。
            #    子进程用本 venv 的 python 且清掉 PYTHONPATH 才是干净环境。
            code = (f"from PIL import ImageGrab;"
                    f"ImageGrab.grab(bbox={(x, y, x + w, y + h)!r}).save({path!r})")
            r = subprocess.run([sys.executable, "-c", code], env=clean_env(),
                               capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                print("[SHOT] 失败:", (r.stderr or r.stdout).strip()[-200:])
                return
            print("[SHOT]", path, os.path.getsize(path), "B")
        except Exception as e:
            print("[SHOT] 失败:", e)

    def _selftest_finish(self):
        self._shot("logs/shot_reply.png")
        self.root.destroy()


class _FakeEvent:
    """给 _on_return 用的假事件 (自检里直接触发发送)。"""
    state = 0


def main() -> int:
    selftest = "--selftest" in sys.argv
    try:
        app = HuaiEnApp()
    except tk.TclError as e:
        print(f"[FAIL] 无法创建窗口: {e}", file=sys.stderr)
        return 1
    if selftest:
        return app.selftest()
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
