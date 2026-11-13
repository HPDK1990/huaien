"""前台状态机的单测 —— 重点是"清干净"这件事。"""
from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from huaien import app as app_mod


def _app():
    try:
        a = app_mod.HuaiEnApp()
    except tk.TclError:
        return None
    a.root.update()
    return a


def test_reset_clears_error_artifacts_and_timer():
    a = _app()
    if a is None:
        return
    try:
        from huaien.bridge import Artifact
        a._error = "上一轮出错"
        a.artifacts = [Artifact("file", "x.txt", True, 1)]
        a._elapsed = 42.0
        a._t0 = 123.0
        a.state = "thinking"

        a.reset()

        assert a._error == "", "reset 后还留着上一轮的错误提示"
        assert a.artifacts == [], "reset 后还留着上一轮的产物"
        assert a._elapsed == 0.0, "reset 后计时没归零"
        assert a._t0 == 0.0, "reset 后计时起点没归零"
        assert a.state == "idle"
    finally:
        a.root.destroy()


def test_status_line_does_not_duplicate_ellipsis():
    """思考 8 秒内状态行留空 —— 回复区已经在显示省略号了。"""
    a = _app()
    if a is None:
        return
    try:
        a.state = "thinking"
        a._elapsed = 2.0
        assert a._status_text() == "", "前 8 秒不该再显示一个省略号"
        a._elapsed = 12.0
        assert "12" in a._status_text()
    finally:
        a.root.destroy()


def test_busy_does_not_swallow_the_message():
    """bridge 忙的时候, 不能静默丢掉用户的话 —— 必须明确告诉他。"""
    a = _app()
    if a is None:
        return
    try:
        a.bridge._busy = True
        a._set_entry_text("不能丢的话")
        a._on_return(type("E", (), {"state": 0})())
        assert a._error, "busy 时应该给提示"
        assert a._entry_text() == "不能丢的话", "busy 时输入框里的字被吞了"
    finally:
        a.bridge._busy = False
        a.root.destroy()
