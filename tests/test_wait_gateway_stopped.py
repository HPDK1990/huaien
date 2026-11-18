"""怀恩 — 等 gateway 停下来的单元测试

跑: python -m pytest tests/ -q   或   python tests/test_wait_gateway_stopped.py

覆盖:
1. gateway 还在跑 -> 报告没停, 返回 1 (绝不能"以为停了")
2. gateway 已经不在 -> 返回 0
3. 找不到进程时不能崩 (psutil 不可用的场景)
"""
from __future__ import annotations

import os
import sys

import pytest

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJ, "scripts"))

import wait_gateway_stopped as wgs  # noqa: E402


class _FakeClock:
    """可控时钟 —— 让 main() 的轮询立即超时, 不真等 12 秒。"""

    def __init__(self, step: float = 100.0):
        self.t = 0.0
        self.step = step

    def time(self) -> float:
        self.t += self.step
        return self.t

    def sleep(self, _seconds):
        return None


@pytest.fixture
def fast_clock(monkeypatch):
    """把 time.time / time.sleep 换成假实现。"""
    clock = _FakeClock()
    monkeypatch.setattr(wgs.time, "time", clock.time)
    monkeypatch.setattr(wgs.time, "sleep", clock.sleep)
    return clock


@pytest.fixture
def fake_pids(monkeypatch):
    """控制 gateway_pids() 返回什么。"""
    state = {"pids": []}

    def _set(pids):
        state["pids"] = pids

    monkeypatch.setattr(wgs, "gateway_pids", lambda: list(state["pids"]))
    return _set


def test_reports_running_gateway(fake_pids, fast_clock, monkeypatch, capsys):
    """gateway 还在跑 -> 必须报"没停"并返回 1。

    这是最重要的一条: 误以为停了 -> 继续改 state.db -> 撕裂。
    """
    fake_pids([1234])
    monkeypatch.setattr(wgs, "gateway_pid_from_state", lambda: 1234)
    monkeypatch.setattr(sys, "argv",
                        ["wait_gateway_stopped.py", "--timeout", "12",
                         "--interval", "2"])

    assert wgs.main() == 1
    out = capsys.readouterr().out
    assert "还在跑" in out
    assert "1234" in out
    # 文案不能断言"是 RestartOnFailure 拉回来的" —— gateway 可能根本没被停过。
    # (问句里列举 "把它拉回来了?" 是可以的, 那是在提示排查方向)
    assert "RestartOnFailure 在 60s 内把它拉回来了" not in out


def test_reports_stopped_gateway(fake_pids, monkeypatch, capsys):
    """gateway 已经不在 -> 返回 0。"""
    fake_pids([])
    monkeypatch.setattr(wgs, "gateway_pid_from_state", lambda: None)
    monkeypatch.setattr(sys, "argv",
                        ["wait_gateway_stopped.py", "--timeout", "12",
                         "--interval", "2"])

    assert wgs.main() == 0
    assert "已停" in capsys.readouterr().out


def test_json_mode(fake_pids, fast_clock, monkeypatch, capsys):
    import json

    fake_pids([777])
    monkeypatch.setattr(wgs, "gateway_pid_from_state", lambda: 777)
    monkeypatch.setattr(sys, "argv",
                        ["wait_gateway_stopped.py", "--timeout", "12",
                         "--interval", "2", "--json"])

    assert wgs.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["still_running"] == [777]


def test_json_mode_when_stopped(fake_pids, monkeypatch, capsys):
    import json

    fake_pids([])
    monkeypatch.setattr(wgs, "gateway_pid_from_state", lambda: None)
    monkeypatch.setattr(sys, "argv",
                        ["wait_gateway_stopped.py", "--timeout", "12",
                         "--interval", "2", "--json"])

    assert wgs.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["still_running"] == []


def test_stale_state_file_but_dead_process(fake_pids, monkeypatch):
    """state 文件还在但进程已死 -> 应该判"停了"。

    gateway_state.json 是上次运行留下的, 进程早就没了。
    不看进程只看文件会永远误报"还在跑"。
    """
    fake_pids([])
    # PID 999999999 几乎肯定不存在 -> OpenProcess 失败 -> 判为已停
    monkeypatch.setattr(wgs, "gateway_pid_from_state", lambda: 999999999)
    monkeypatch.setattr(sys, "argv",
                        ["wait_gateway_stopped.py", "--timeout", "12",
                         "--interval", "2", "--json"])

    assert wgs.main() == 0


def test_gateway_pids_returns_list_without_psutil(monkeypatch):
    """psutil 不可用时不能抛异常 —— 返回空列表即可。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "psutil":
            raise ImportError("no psutil")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert wgs.gateway_pids() == []


def test_gateway_pid_from_state_missing_file(tmp_path, monkeypatch):
    """state 文件不存在 -> 返回 None, 不能崩。"""
    monkeypatch.setattr(wgs, "HD", str(tmp_path))
    assert wgs.gateway_pid_from_state() is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


def test_psutil_missing_but_state_pid_alive_is_not_stopped(fake_pids, monkeypatch, capsys):
    """psutil 缺失 + gateway_state.json 里的 PID 还活着 -> **绝不能**报"已停"。

    ⚠️ 实测坑 (2026-11-03, P3 coder 发现的): 旧写法在"进程活着"时
       既不 continue 也不 sleep, 直接落到循环后的无条件 break
       -> 轮询只跑一轮 -> still=[] -> stopped=True -> rc=0 **谎报已停**。
       2026-10-30 那次撕裂事故正是"脚本以为网关停了, 于是在活库上动手"。

    修法: gateway_pid_from_state() 找到的 PID 必须算进 alive/still,
       并且"还在跑"时 sleep 后继续下一轮, 不能 break。

    用 os.getpid() 当"活着的 PID" —— 它一定活着, OpenProcess 必定成功。
    不用 fast_clock: 那个假时钟会让循环条件第一次判断就不成立, 循环体不执行。
    """
    import json
    fake_pids([])                       # psutil 缺失 -> gateway_pids 恒空
    monkeypatch.setattr(wgs, "gateway_pid_from_state", lambda: os.getpid())
    monkeypatch.setattr(sys, "argv",
                        ["wait_gateway_stopped.py", "--timeout", "1",
                         "--interval", "0", "--json"])

    assert wgs.main() == 1, "进程活着必须返回 1(不能动 state.db)"
    payload = json.loads(capsys.readouterr().out)
    assert payload["still_running"] == [os.getpid()], "state 里的活 PID 必须进 still"


def test_state_pid_is_included_in_still(fake_pids, monkeypatch, capsys):
    """gateway_pid_from_state() 找到的活 PID 必须进 still, 不能被丢掉。"""
    import json
    fake_pids([])
    monkeypatch.setattr(wgs, "gateway_pid_from_state", lambda: os.getpid())
    monkeypatch.setattr(sys, "argv",
                        ["wait_gateway_stopped.py", "--timeout", "1",
                         "--interval", "0", "--json"])

    assert wgs.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["still_running"] == [os.getpid()]
    assert payload["ok"] is False
