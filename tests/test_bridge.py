"""怀恩 — 桥接层单元测试

不需要网络和 Hermes: 只测纯逻辑 (CLI 定位 / stream-json 解析 / sid 续接)。
跑: python -m pytest tests/ -q   或   python tests/test_bridge.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from huaien import bridge


def test_hermes_cmd_found():
    cmd = bridge._hermes_cmd()
    assert cmd, "应该能定位到 hermes 命令"
    assert any("hermes" in c.lower() for c in cmd), f"命令不像 hermes: {cmd}"


def test_parse_stream_json_text():
    line = json.dumps({"type": "text", "text": "你好"})
    j = json.loads(line)
    assert j["type"] == "text" and j["text"] == "你好"


def test_parse_stream_json_result():
    line = json.dumps({"type": "result", "session_id": "local-abc",
                       "exit_code": 0, "text": "完整回复"})
    j = json.loads(line)
    assert j["type"] == "result"
    assert j["session_id"] == "local-abc"
    assert j["text"] == "完整回复"


def test_parse_ignores_garbage():
    for bad in ("", "   ", "not json", "{broken"):
        try:
            json.loads(bad)
        except json.JSONDecodeError:
            continue
        else:
            if bad == "{broken":
                raise AssertionError("坏 JSON 不该被解析成功")


def test_bridge_config_defaults():
    cfg = bridge.BridgeConfig()
    assert cfg.profile == "huaien"
    assert cfg.timeout > 0


def test_session_reset():
    b = bridge.ChatBridge()
    b.session_id = "local-xyz"
    b.reset()
    assert b.session_id is None


def test_busy_flag_initial():
    b = bridge.ChatBridge()
    assert b.busy is False


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  [PASS] {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"  [FAIL] {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n  {len(fns)-failed}/{len(fns)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())


def test_failed_turn_clears_session_id(monkeypatch):
    """一轮失败后必须清掉 session_id。

    ⚠️ 实测踩过: 手机上点"汇报进度", 上一轮被 kill 的半截会话被 resume,
    怀恩接着旧任务去读运行日志 + spawn 子代理, 80 秒没回。
    留着 session_id 就会把新问题接到旧任务后面 —— 用户说的"盯一大堆会话"。
    """
    b = bridge.ChatBridge()
    b.session_id = "old-broken-session"

    class FakeProc:
        stdout = iter([])
        stderr = None
        returncode = 1

        def wait(self, timeout=None):
            raise __import__("subprocess").TimeoutExpired(cmd="hermes", timeout=timeout)

        def kill(self):
            pass

    monkeypatch.setattr(bridge.subprocess, "Popen",
                        lambda *a, **k: FakeProc())
    b.cfg.timeout = 0.05

    errs = []
    b.ask("新问题", on_error=errs.append)
    import time
    for _ in range(50):
        if errs:
            break
        time.sleep(0.05)
    assert errs, "应该收到错误回调"
    assert b.session_id is None, "失败后 session_id 必须清掉"


def test_kill_tree_uses_taskkill_on_windows(monkeypatch):
    """Windows 上必须用 taskkill /T 杀整棵树。

    ⚠️ 实测坑: `hermes` 是 shim, 真正干活的是 uv python re-exec 出来的**孙进程**
       (命令行 `python.exe -c "import os, runpy, site..."`)。
       `proc.kill()` 只杀 shim -> 孙进程活下来继续跑。
       实测后果: 手机上超时后没停, 24 分钟还在读文件 + spawn 子代理。
    """
    from huaien import bridge

    calls = {}

    class FakeProc:
        pid = 12345

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(bridge.subprocess, "run", fake_run)
    monkeypatch.setattr(bridge.os, "name", "nt")
    bridge._kill_tree(FakeProc())
    assert "taskkill" in calls["cmd"][0].lower()
    assert "/T" in calls["cmd"], "必须带 /T 杀子孙"
    assert "12345" in calls["cmd"]


def test_kill_tree_noop_if_already_dead(monkeypatch):
    """进程已经退了就不用再杀。"""
    from huaien import bridge

    def boom(*a, **k):
        raise AssertionError("不该再调 taskkill")

    monkeypatch.setattr(bridge.subprocess, "run", boom)

    class Dead:
        pid = 1

        def poll(self):
            return 0

    bridge._kill_tree(Dead())


def test_ask_sync_timeout_kills_subprocess(monkeypatch):
    """ask_sync 超时必须把子进程杀掉, 不能让它在后台继续跑。"""
    from huaien import bridge

    killed = []
    monkeypatch.setattr(bridge, "_kill_tree", lambda p: killed.append(p))

    b = bridge.ChatBridge()

    class FakeProc:
        pid = 999

    def fake_ask(*a, **k):
        b._proc = FakeProc()      # 假装有个子进程在跑
    monkeypatch.setattr(b, "ask", fake_ask)

    try:
        b.ask_sync("测试", timeout=0.2)
    except bridge.BridgeError:
        pass
    else:
        raise AssertionError("应该抛超时")
    assert len(killed) == 1, "超时必须杀子进程"
    assert killed[0].pid == 999
