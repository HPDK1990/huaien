"""怀恩 — 微信发送工具的单元测试

跑: python -m pytest tests/ -q   或   python tests/test_wechat_send.py

重点覆盖 iLink 的 session 失效处理:
- 认出 "session not ready" / "prepare failed" / "the user must send the bot"
- 认出频率限制 (ret=-2)
- **session 失效后必须停止后续发送** —— 继续发只会连续失败
- 间隔节流真的生效
"""
from __future__ import annotations

import os
import sys

import pytest

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJ, "scripts"))

import wechat_send as w  # noqa: E402

SESSION_ERR = ("hermes send: Weixin send failed: iLink sendmessage session not ready: "
               "ret=-2 errcode=None errmsg=prepare failed — the user must send the bot "
               "a message first (or re-pair)")
RATE_ERR = "hermes send: Weixin send failed: ret=-2 frequency limit, retry later"


# ---------- diagnose ----------

def test_diagnose_detects_session_dead():
    msg = w.diagnose(SESSION_ERR)
    assert "在微信里发一条消息" in msg, msg


def test_diagnose_detects_session_dead_variants():
    for text in ("session not ready",
                 "prepare failed",
                 "the user must send the bot a message first",
                 "or re-pair"):
        assert "在微信里发一条消息" in w.diagnose(text), text


def test_diagnose_detects_rate_limit():
    assert "频率限制" in w.diagnose(RATE_ERR)


def test_diagnose_falls_back_to_raw_output():
    out = w.diagnose("some other failure detail")
    assert "some other failure" in out


def test_session_dead_pattern_covers_real_error():
    assert w.SESSION_DEAD.search(SESSION_ERR)
    assert w.RATE_LIMIT.search(RATE_ERR)


# ---------- main 的发送流程 ----------

def test_main_stops_after_session_death(monkeypatch):
    """session 死了就停 —— 继续发只是连续失败, 还浪费配额。"""
    calls = []
    monkeypatch.setattr(w, "send",
                        lambda target, message, timeout=180:
                        (calls.append(message), (1, SESSION_ERR))[1])
    monkeypatch.setattr(w.time, "sleep", lambda s: None)
    monkeypatch.setattr(sys, "argv", ["wechat_send.py", "--to", "weixin:x", "hi",
                                      "--media", "a.png", "b.mp4"])

    assert w.main() == 1
    assert len(calls) == 1, f"session 死后不该继续发, 实际发了 {len(calls)} 条"


def test_main_sends_text_then_each_media(monkeypatch):
    calls = []
    monkeypatch.setattr(w, "send",
                        lambda target, message, timeout=180:
                        (calls.append(message), (0, "sent"))[1])
    monkeypatch.setattr(w.time, "sleep", lambda s: None)
    monkeypatch.setattr(sys, "argv", ["wechat_send.py", "--to", "weixin:x", "hi",
                                      "--media", "a.png", "b.mp4"])

    assert w.main() == 0
    assert calls == ["hi", "MEDIA:a.png", "MEDIA:b.mp4"]


def test_main_waits_between_messages(monkeypatch):
    slept = []
    monkeypatch.setattr(w, "send", lambda target, message, timeout=180: (0, "sent"))
    monkeypatch.setattr(w.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(sys, "argv", ["wechat_send.py", "--to", "weixin:x", "hi",
                                      "--media", "a.png", "--interval", "12"])

    w.main()
    assert slept == [12.0], f"两条之间应该等 12 秒, 实际 {slept}"


def test_main_dry_run_sends_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(w, "send",
                        lambda target, message, timeout=180: (calls.append(message), (0, "ok"))[1])
    monkeypatch.setattr(sys, "argv", ["wechat_send.py", "--to", "weixin:x", "hi", "--dry-run"])

    assert w.main() == 0
    assert not calls


def test_main_returns_nonzero_when_a_send_fails(monkeypatch):
    """非 session 类失败 (比如文件不存在) 要继续发后面的, 但整体 rc 非 0。"""
    calls = []
    monkeypatch.setattr(w, "send",
                        lambda target, message, timeout=180:
                        (calls.append(message), (1, "file not found"))[1])
    monkeypatch.setattr(w.time, "sleep", lambda s: None)
    monkeypatch.setattr(sys, "argv", ["wechat_send.py", "--to", "weixin:x", "hi",
                                      "--media", "a.png"])

    assert w.main() == 1
    assert len(calls) == 2, "普通失败不该中断整批"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
