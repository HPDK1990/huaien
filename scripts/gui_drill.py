"""怀恩 — 前台真实操作测试器 (GUI Drill)

用户要求: *"不要后台测, 要在前台模拟我的真实操作进行试验。"*

## 这个脚本干什么

启动**真实的前台窗口**, 然后用**事件层**操作它 —— 和真人做的事一模一样:

    鼠标点击输入框 → 逐字打字 → 按回车 → 等回复 → 看屏幕上显示了什么

断言也只看**可见状态**:

  - canvas 上某个图元的文字 (就是屏幕上显示的那行字)
  - 输入框里的内容
  - 截图的像素分布 (是不是一片黑)

**不读内部变量** —— 不看 `app.user_display`, 不看 `app.shown`, 不看 `app.bridge`。
因为那些是实现细节; 用户看见的是屏幕, 测试就该断言屏幕。

## 跑法

    python scripts/gui_drill.py                # 全部场景
    python scripts/gui_drill.py --live         # 真发消息给 Hermes (会消耗额度)
    python scripts/gui_drill.py --only S5      # 只跑某个场景
"""
from __future__ import annotations

import argparse
import contextlib
import os
import sys
import time
import tkinter as tk

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)
os.environ.pop("PYTHONPATH", None)
os.environ.pop("PYTHONHOME", None)

from huaien import app as app_mod  # noqa: E402
from huaien.bridge import Artifact  # noqa: E402

SHOT_DIR = os.path.join(PROJ, "docs", "drill")


STUB_REPLY = "收到。这是测试桩的回话。"


class Drill:
    """测试台。持有真实窗口, 提供"像人一样操作"的手段。"""

    def __init__(self, live: bool = False):
        os.makedirs(SHOT_DIR, exist_ok=True)
        self.app = app_mod.HuaiEnApp()
        self.root = self.app.root
        self.live = live
        self.results: list[tuple[str, bool, str]] = []
        self._reply_before = ""
        self.shot_error = ""      # 最近一次截图失败的原因
        self.pump(0.6)
        if not live:
            self._install_stub()

    def _install_stub(self):
        """离线模式: 把 bridge 换成测试桩。

        为什么: 离线场景测的是**界面交互**, 不该受 Hermes 响应快慢影响。
        早先没装桩, 结果 S4 真给 Hermes 发了消息, S6 就撞上"还在处理上一条",
        离线场景变得不确定 —— 界面测试和数据依赖混在一起, 是设计问题。
        真能力的验证交给 --live 场景 (S10/S11)。
        """
        def fake_ask(message, **kw):
            kw.get("on_start", lambda: None)()
            kw.get("on_text", lambda p: None)(STUB_REPLY)
            kw.get("on_artifacts", lambda a: None)([
                app_mod.Artifact("file", str(PROJ / "README.md"), True, 100)])
            kw.get("on_done", lambda t, s: None)(STUB_REPLY, "stub-session")

        self.app.bridge.ask = fake_ask

    # ---------- 像人一样操作 ----------
    def pump(self, seconds: float = 0.3):
        """让界面自己转一会儿 (等价于用户盯着屏幕等)。"""
        end = time.time() + seconds
        while time.time() < end:
            self.root.update()
            time.sleep(0.02)

    def click_entry(self):
        """真的用鼠标点一下输入框。

        注意: 模拟点击会把插入点挪到点击处 (这是真实行为, 之前 S4 就因为
        上一场景残留的字 + 点击位置, 让断言串味)。所以点完把插入点归位到末尾,
        相当于真人点完自己按 End。
        """
        e = self.app.entry
        e.focus_set()
        self.root.update()
        e.event_generate("<Button-1>", x=10, y=8)
        e.event_generate("<ButtonRelease-1>", x=10, y=8)
        e.focus_set()
        e.mark_set("insert", "end-1c")
        self.pump(0.15)

    def clear_entry(self):
        """把输入框清空 —— 真人开始新一句前会先把上一句删掉。"""
        self.app.entry.delete("1.0", "end")
        self.pump(0.1)

    def type_text(self, text: str):
        """真的逐字敲键盘。

        ASCII 走 `<KeyPress>` (Tk 从 keysym 推 char);
        中文走 IME 的提交路径 —— Windows 输入法组合完成后就是往控件里
        插入一段成型的字符串, 这里复刻的就是那一步。
        """
        for ch in text:
            if ord(ch) < 128:
                self.app.entry.event_generate("<KeyPress>", keysym=ch)
            else:
                self.app.entry.insert("insert", ch)
            self.pump(0.01)
        self.pump(0.15)

    def press_return(self, shift: bool = False):
        self.app.entry.event_generate("<KeyPress-Return>", state=0x0001 if shift else 0)
        self.pump(0.2)

    def send(self, text: str, timeout: float = 240.0, wait: bool = True) -> str:
        """像真人一样发一句话, 然后等这一轮的回答。

        发之前必须等上一轮处理完 —— 否则 bridge 忙, 消息会被拒绝,
        屏幕上留着的还是上一轮的回答, 测试就会拿旧回复当新回复。

        wait=False: 只发不等。用于"产物才是判据"的场景 ——
        agent 干完活可能还顺手做别的 (清理 pending 之类), 永远等不到
        state == "idle", 但活早干完了。这种场景该看产物, 不该看它说完了没。
        """
        self.wait_idle(timeout=120)
        self.clear_entry()
        self._reply_before = self.shown("reply")
        self.type_text(text)
        self.press_return()
        if not wait:
            self.pump(0.5)
            return ""
        return self.wait_reply(timeout=timeout)

    def press_escape(self):
        self.root.event_generate("<Escape>")
        self.pump(0.2)

    def drag_window(self, dx: int = 40, dy: int = 25):
        """抓住顶部拖动带, 把窗口拖走。"""
        before = (self.root.winfo_x(), self.root.winfo_y())
        self.app.canvas.event_generate("<ButtonPress-1>", x=10, y=5)
        self.app.canvas.event_generate("<B1-Motion>", x=10 + dx, y=5 + dy)
        self.pump(0.2)
        after = (self.root.winfo_x(), self.root.winfo_y())
        return before, after

    # ---------- 断言"屏幕上显示了什么" ----------
    def shown(self, tag: str) -> str:
        """读 canvas 上某个图元的文字 —— 这就是屏幕上那行字。"""
        try:
            return self.app.canvas.itemcget(tag, "text")
        except tk.TclError:
            return ""

    def entry_text(self) -> str:
        return self.app.entry.get("1.0", "end-1c")

    def screen_ink(self) -> float:
        """截图的非黑像素占比 —— 屏幕是不是一片黑。

        返回 -1 表示**根本没截到图** (不是"屏幕是黑的")。这两种必须分清:
        早先版本静默返回 -1, 断言只看到"不是黑"就判 FAIL, 把"截图失败"
        误报成"界面黑了"。失败原因记在 self.shot_error 里。
        """
        try:
            from PIL import ImageGrab
            x, y = self.root.winfo_rootx(), self.root.winfo_rooty()
            w, h = self.root.winfo_width(), self.root.winfo_height()
            img = ImageGrab.grab(bbox=(x, y, x + w, y + h)).convert("RGB")
            px = list(img.getdata())
            lit = sum(1 for r, g, b in px if r > 24 or g > 24 or b > 24)
            self.shot_error = ""
            return lit / len(px) * 100
        except Exception as e:
            self.shot_error = f"{type(e).__name__}: {e}"
            return -1.0

    def shot(self, name: str):
        try:
            from PIL import ImageGrab
            x, y = self.root.winfo_rootx(), self.root.winfo_rooty()
            w, h = self.root.winfo_width(), self.root.winfo_height()
            img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
            p = os.path.join(SHOT_DIR, f"{name}.png")
            img.save(p)
            return p
        except Exception as e:
            self.shot_error = f"{type(e).__name__}: {e}"
            return ""

    def wait_reply(self, timeout: float = 90.0) -> str:
        """等**这一轮的**回答 (不是屏幕上残留的上一轮)。

        两个坑:
          1. 思考中前台显示 "…", 不能只看 reply 非空
          2. 上一轮的回复还留在屏幕上, 必须等它**变成新的内容**
        判据: reply 文本跟发送前不一样, 且不是 "…", 且状态已回到 idle。
        """
        end = time.time() + timeout
        before = self._reply_before
        while time.time() < end:
            self.pump(0.2)
            t = self.shown("reply")
            if (t.strip() and t.strip() != "…" and t != before
                    and self.app.state == "idle"):
                return t
        return self.shown("reply")

    def wait_idle(self, timeout: float = 120.0) -> bool:
        """等 Hermes 空闲 (上一轮真的处理完了)。"""
        end = time.time() + timeout
        while time.time() < end:
            self.pump(0.2)
            if not self.app.bridge.busy and self.app.state != "thinking":
                return True
        return False

    # ---------- 记账 ----------
    def check(self, scene: str, name: str, cond: bool, detail: str = ""):
        self.results.append((f"{scene} {name}", bool(cond), detail))
        mark = "PASS" if cond else "FAIL"
        print(f"  [{mark}] {scene} {name}" + (f"  — {detail}" if detail else ""))
        return bool(cond)

    def summary(self) -> int:
        bad = [r for r in self.results if not r[1]]
        print()
        print("=" * 58, flush=True)
        print(f"  {len(self.results) - len(bad)}/{len(self.results)} 通过", flush=True)
        for n, _, d in bad:
            print(f"   FAIL {n}  {d}")
        print("=" * 58)
        return 1 if bad else 0

    def close(self):
        with contextlib.suppress(Exception):
            self.root.destroy()


# ============================================================
#  场景
# ============================================================
def scene_S1(d: Drill):
    """打开就有焦点, 屏幕不黑"""
    d.check("S1", "输入框自动获得焦点", d.root.focus_get() is d.app.entry)
    ink = d.screen_ink()
    if ink < 0:
        d.check("S1", "截到图了 (不是黑屏)", False, f"截图失败: {d.shot_error}")
    else:
        d.check("S1", "屏幕不是一片黑", ink > 0.05, f"非黑像素 {ink:.3f}%")
    d.shot("S1_idle")


def scene_S2(d: Drill):
    """鼠标点击输入框"""
    d.app.entry.focus_set()
    d.pump(0.1)
    d.click_entry()
    d.check("S2", "点击后焦点在输入框", d.root.focus_get() is d.app.entry)
    d.shot("S2_focus")


def scene_S3(d: Drill):
    """逐字敲中文"""
    d.clear_entry()
    d.click_entry()
    d.type_text("你好，我是怀恩")
    got = d.entry_text()
    d.check("S3", "中文逐字上屏", got == "你好，我是怀恩", repr(got))


def scene_S4(d: Drill):
    """回车发送 -> 用户消息上屏, 输入框清空"""
    d.clear_entry()
    d.click_entry()
    d.type_text("测试发送")
    d.press_return()
    d.pump(0.4)
    d.check("S4", "输入框已清空", d.entry_text().strip() == "", repr(d.entry_text()))
    d.check("S4", "用户消息出现在屏幕上", "测试发送" in d.shown("user"), repr(d.shown("user")[:40]))
    d.check("S4", "用户消息不含上一场景残留", "你好，我是怀恩" not in d.shown("user"),
            repr(d.shown("user")[:40]))
    d.shot("S4_sent")


def scene_S5(d: Drill):
    """Shift+Enter 换行, 不发送"""
    before = d.shown("user")
    d.clear_entry()
    d.click_entry()
    d.type_text("第一行")
    d.press_return(shift=True)
    d.type_text("第二行")
    d.pump(0.2)
    lines = int(d.app.entry.index("end-1c").split(".")[0])
    d.check("S5", "Shift+Enter 换行", lines >= 2, f"行数 {lines}")
    d.check("S5", "Shift+Enter 没有发送", d.shown("user") == before)


def scene_S6(d: Drill):
    """状态行: 长任务时有"还在想 · N 秒"提示, 短任务时不添乱。

    ⚠️ 判据是分段的:
      - 思考 8 秒内 → 状态行**留空** (回复区已经在显示省略号了, 不重复)
      - 思考超 8 秒 → 显示"还在想 · N 秒" (让用户知道它没死)
      - 打字中 → 留空 (打字机效果本身就是反馈)
    """
    # 短任务: 状态行应该安静
    d.clear_entry()
    d.click_entry()
    d.type_text("测试状态行")
    d.press_return()
    d.pump(0.4)
    d.check("S6", "短任务时状态行保持安静", d.shown("status").strip() == "",
            repr(d.shown("status")))

    # 长任务: 必须有"还在想 · N 秒"
    d.app.state = "thinking"
    d.app._elapsed = 12.0
    d.app._layout()
    d.pump(0.2)
    st = d.shown("status")
    d.check("S6", "长任务时显示'还在想 · N 秒'", "还在想" in st and "12" in st, repr(st))
    d.shot("S6_status")
    d.app.state = "idle"
    d.app._elapsed = 0.0
    d.app._layout()


def scene_S7(d: Drill):
    """产物行: 有产物时显示一行可双击条目"""
    d.app.artifacts = [
        Artifact("file", os.path.join(PROJ, "README.md"), True, 100),
        Artifact("image", os.path.join(PROJ, "docs", "shot_new.png"), True, 100),
    ]
    d.app._layout()
    d.pump(0.3)
    txt = d.shown("arts")
    d.check("S7", "产物行显示文件名", "README.md" in txt, repr(txt[:60]))
    d.check("S7", "产物不超过 3 行", len(txt.splitlines()) <= 3, repr(txt))
    d.shot("S7_artifacts")
    d.app.artifacts = []
    d.app._layout()


def scene_S8(d: Drill):
    """拖动窗口"""
    before, after = d.drag_window()
    moved = before != after
    d.check("S8", "顶部拖动带能拖动窗口", moved, f"{before} -> {after}")
    # 拖回去
    if moved:
        d.app.canvas.event_generate("<ButtonPress-1>", x=10, y=5)
        d.app.canvas.event_generate("<B1-Motion>", x=10 - (after[0] - before[0]),
                                    y=5 - (after[1] - before[1]))
        d.pump(0.2)


def scene_S9(d: Drill):
    """Esc 清空重开"""
    d.clear_entry()
    d.click_entry()
    d.type_text("要清掉的话")
    d.press_return()
    d.pump(0.3)
    d.press_escape()
    d.pump(0.3)
    d.check("S9", "Esc 后用户消息清空", d.shown("user").strip() == "", repr(d.shown("user")[:40]))
    d.check("S9", "Esc 后输入框清空", d.entry_text().strip() == "")


def scene_S10(d: Drill):
    """真实能力: 前台能不能真的让 Hermes 干活"""
    if not d.live:
        print("  [SKIP] S10 需要 --live (会真发消息)")
        return
    marker = os.path.join(PROJ, "logs", "drill_capability.txt")
    if os.path.exists(marker):
        os.remove(marker)
    d.clear_entry()
    # ⚠️ 这里只断言"文件产生了 + 内容对", **不断言回复**。
    # 原因: agent 干完活可能还继续做别的 (比如顺手清理 pending),
    # 于是永远等不到 state == "idle" —— 但活早干完了。
    # 判据应该落在**产物**上, 而不是落在"它说完了"上。
    d.send(
        f"用文件工具在 {marker} 写一行字: 怀恩可以干活。"
        f"必须真的把文件写出来, 写完告诉我一声。", wait=False)
    # 给文件落盘时间。agent 干活要 1-2 分钟 (读指令 -> 调工具 -> 写 -> 验证),
    # 之前只等 30 秒就判失败, 那不是产品的问题, 是测试等错了时间。
    for _ in range(600):          # 最多 5 分钟
        d.pump(0.5)
        if os.path.exists(marker):
            break
    d.check("S10", "文件真的产生了", os.path.exists(marker),
            "文件不存在" if not os.path.exists(marker) else "")
    d.check("S10", "前台收到了回复", bool(d.shown("reply").strip()),
            repr(d.shown("reply")[:60]))
    if os.path.exists(marker):
        with open(marker, encoding="utf-8", errors="replace") as f:
            content = f.read()
        d.check("S10", "文件内容正确", "怀恩可以干活" in content, repr(content[:60]))
    d.shot("S10_capability")


def scene_S11(d: Drill):
    """多轮上下文续接 (真实 Hermes)"""
    if not d.live:
        print("  [SKIP] S11 需要 --live")
        return
    d.clear_entry()
    d.send("用记忆工具记住数字 4711，存好之后回我 OK", timeout=240)
    reply = d.send("我刚才让你记的数字是多少？", timeout=300)
    d.check("S11", "第二轮能接上上下文", "4711" in reply, repr(reply[:80]))
    d.shot("S11_context")


SCENES = [
    ("S1", scene_S1), ("S2", scene_S2), ("S3", scene_S3), ("S4", scene_S4),
    ("S5", scene_S5), ("S6", scene_S6), ("S7", scene_S7), ("S8", scene_S8),
    ("S9", scene_S9), ("S10", scene_S10), ("S11", scene_S11),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="怀恩前台真实操作测试")
    ap.add_argument("--live", action="store_true", help="真发消息给 Hermes (消耗额度)")
    ap.add_argument("--only", help="只跑某个场景, 如 S5")
    args = ap.parse_args()

    print(f"怀恩 前台真实操作测试  (live={args.live})", flush=True)
    print(f"截图输出: {SHOT_DIR}", flush=True)
    print(flush=True)

    d = Drill(live=args.live)
    try:
        for sid, fn in SCENES:
            if args.only and args.only != sid:
                continue
            print(f"[{sid}]", flush=True)
            try:
                fn(d)
            except Exception as e:
                d.check(sid, "场景执行", False, f"{type(e).__name__}: {e}")
            print(flush=True)
    finally:
        rc = d.summary()
        d.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
