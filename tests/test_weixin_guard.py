#!/usr/bin/env python3
"""Tests for the weixin channel guardian.

The guardian's whole value rests on one invariant: it may only ever act on a
gateway that is ALREADY dead. Replacing a live gateway fences every in-flight
turn, and a fenced turn permanently stalls that session's FIFO. Two unrelated
sessions were lost that way, so the rule is pinned by tests, not by comment.

Run:  python -m pytest tests/test_weixin_guard.py -v
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

GUARD_PATH = Path(__file__).resolve().parents[1] / "scripts" / "weixin_guard.py"
if not GUARD_PATH.exists():
    # 装在 Hermes home 里的那份 (计划任务实际跑的就是它)
    GUARD_PATH = Path.home() / "AppData" / "Local" / "hermes" / "weixin_guard.py"
if not GUARD_PATH.exists():
    pytest.skip("weixin_guard.py not found (repo copy and installed copy both missing)")


def load_guard(monkeypatch, home: Path):
    """Import the guardian with HOME pointed at a temp dir."""
    spec = importlib.util.spec_from_file_location("weixin_guard_under_test", GUARD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "HOME", str(home))
    monkeypatch.setattr(mod, "PROFILE", str(home / "profiles" / "huaien"))
    monkeypatch.setattr(mod, "HEARTBEAT", str(home / "state" / "gateway.heartbeat"))
    monkeypatch.setattr(mod, "GATEWAY_STATE", str(home / "gateway_state.json"))
    monkeypatch.setattr(mod, "GATEWAY_LOCK", str(home / "gateway.lock"))
    monkeypatch.setattr(mod, "GATEWAY_LOG", str(home / "profiles" / "huaien" / "logs" / "gateway.log"))
    monkeypatch.setattr(mod, "GUARD_LOG", str(home / "logs" / "weixin-guard.log"))
    return mod


@pytest.fixture
def guard(tmp_path, monkeypatch):
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "profiles" / "huaien" / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    mod = load_guard(monkeypatch, tmp_path)
    # main() sleeps between recovery and re-probe. Patch the sleep on the
    # module's own time reference; monkeypatching time.sleep globally would
    # leak into every other test in the session.
    mod.time = _TimeProxy(time)
    return mod


class _TimeProxy:
    """time module proxy whose sleep() is a no-op."""

    def __init__(self, real):
        self._real = real
        self.slept = []

    def __getattr__(self, name):
        return getattr(self._real, name)

    def sleep(self, seconds):
        self.slept.append(seconds)


# --- gateway identity -------------------------------------------------------

def test_gateway_cmdline_matches_real_entrypoint(guard):
    real = ("C:\\...\\python.exe -I -c \"...runpy.run_module('hermes_cli.main')\" "
            "gateway run --replace")
    assert guard._is_gateway_cmdline(real)


def test_ticket_helper_is_not_the_gateway(guard):
    """The ticket-helper child process must never be mistaken for the gateway."""
    helper = ("python.exe -P -u -c \"from hermes_cli.gateway_client import _session_ticket\"")
    assert not guard._is_gateway_cmdline(helper)


def test_powershell_mentioning_gateway_is_not_the_gateway(guard):
    cmd = ("powershell -NoProfile -Command \"Get-CimInstance Win32_Process | "
           "Where-Object { $_.CommandLine -like '*gateway run*' }\"")
    assert not guard._is_gateway_cmdline(cmd)


def test_empty_cmdline_is_not_the_gateway(guard):
    assert not guard._is_gateway_cmdline("")
    assert not guard._is_gateway_cmdline("   ")


# --- poll-channel detection -------------------------------------------------

def _write_log(guard, lines: str):
    p = Path(guard.GATEWAY_LOG)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(lines, encoding="utf-8")


def test_old_poll_errors_do_not_count(guard):
    """A DNS outage two days ago must not mark the channel broken today."""
    old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 86400 * 2))
    _write_log(guard, "".join(
        f"{old},705 WARNING adapter: [Weixin] poll error: getaddrinfo failed\n"
        for _ in range(20)
    ))
    broken, hits = guard.poll_channel_broken()
    assert hits == 0, "stale errors must not count"
    assert broken is False


def test_recent_poll_errors_count(guard):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_log(guard, "".join(
        f"{now},705 WARNING adapter: [Weixin] poll error: getaddrinfo failed\n"
        for _ in range(guard.POLL_ERROR_LIMIT + 2)
    ))
    broken, hits = guard.poll_channel_broken()
    assert hits > guard.POLL_ERROR_LIMIT
    assert broken is True


def test_recovery_lines_are_ignored(guard):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_log(guard, "".join(
        f"{now},705 WARNING adapter: [Weixin] poll error recovered after retry\n"
        for _ in range(20)
    ))
    broken, hits = guard.poll_channel_broken()
    assert hits == 0
    assert broken is False


# --- the core invariant: never replace a live gateway -----------------------

def test_live_gateway_is_never_replaced(guard, monkeypatch):
    """A healthy gateway plus a broken weixin channel must NOT trigger recovery."""
    calls = []
    monkeypatch.setattr(guard, "gateway_pid", lambda: 4242)
    monkeypatch.setattr(guard, "pid_alive", lambda pid: True)
    monkeypatch.setattr(guard, "heartbeat_age_s", lambda: 1.0)
    monkeypatch.setattr(guard, "poll_channel_broken", lambda: (True, 99))
    monkeypatch.setattr(guard, "ollama_ok", lambda: True)
    monkeypatch.setattr(guard, "recover_gateway", lambda: calls.append("recover") or True)
    monkeypatch.setattr(guard, "log", lambda m: None)

    rc = guard.main()
    assert calls == [], "recovery must not run while the gateway is alive"
    assert rc == 1, "unhealthy state is still reported"


def test_dead_gateway_triggers_recovery(guard, monkeypatch):
    calls = []
    monkeypatch.setattr(guard, "gateway_pid", lambda: 0)
    monkeypatch.setattr(guard, "pid_alive", lambda pid: False)
    monkeypatch.setattr(guard, "heartbeat_age_s", lambda: None)
    monkeypatch.setattr(guard, "poll_channel_broken", lambda: (False, 0))
    monkeypatch.setattr(guard, "ollama_ok", lambda: True)
    monkeypatch.setattr(guard, "recover_gateway", lambda: calls.append("recover") or True)
    monkeypatch.setattr(guard, "log", lambda m: None)

    rc = guard.main()
    assert calls == ["recover"], "a dead gateway must be recovered"
    assert rc == 1


def test_stale_heartbeat_triggers_recovery(guard, monkeypatch):
    calls = []
    monkeypatch.setattr(guard, "gateway_pid", lambda: 4242)
    monkeypatch.setattr(guard, "pid_alive", lambda pid: True)
    monkeypatch.setattr(guard, "heartbeat_age_s",
                        lambda: guard.HEARTBEAT_MAX_AGE_S + 500)
    monkeypatch.setattr(guard, "poll_channel_broken", lambda: (False, 0))
    monkeypatch.setattr(guard, "ollama_ok", lambda: True)
    monkeypatch.setattr(guard, "recover_gateway", lambda: calls.append("recover") or True)
    monkeypatch.setattr(guard, "log", lambda m: None)

    guard.main()
    assert calls == ["recover"], "a wedged gateway must be recovered"


def test_healthy_gateway_does_nothing(guard, monkeypatch):
    calls = []
    monkeypatch.setattr(guard, "gateway_pid", lambda: 4242)
    monkeypatch.setattr(guard, "pid_alive", lambda pid: True)
    monkeypatch.setattr(guard, "heartbeat_age_s", lambda: 2.0)
    monkeypatch.setattr(guard, "poll_channel_broken", lambda: (False, 0))
    monkeypatch.setattr(guard, "ollama_ok", lambda: True)
    monkeypatch.setattr(guard, "recover_gateway", lambda: calls.append("recover") or True)
    monkeypatch.setattr(guard, "log", lambda m: None)

    rc = guard.main()
    assert calls == []
    assert rc == 0


# --- recovery path ordering --------------------------------------------------

def test_recovery_prefers_graceful_restart_over_hard_replace(guard, monkeypatch):
    """The graceful path must be tried first; the hard replace is a fallback."""
    order = []

    class R:
        returncode = 0
        stdout = b"ok"
        stderr = b""

    def fake_run(cmd, **kw):
        order.append(cmd[:3])
        return R()

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    monkeypatch.setattr(guard, "_hermes_cli", lambda: "hermes")
    monkeypatch.setattr(guard, "log", lambda m: None)

    assert guard.recover_gateway() is True
    assert order, "a recovery command must be issued"
    assert order[0][:3] == ["hermes", "gateway", "restart"], \
        "graceful restart must come first"


def test_falls_back_to_scheduled_task_when_graceful_fails(guard, monkeypatch):
    order = []

    class R:
        def __init__(self, rc):
            self.returncode = rc
            self.stdout = b""
            self.stderr = b""

    def fake_run(cmd, **kw):
        order.append(cmd[:3])
        if cmd[:3] == ["hermes", "gateway", "restart"]:
            return R(1)
        return R(0)

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    monkeypatch.setattr(guard, "_hermes_cli", lambda: "hermes")
    monkeypatch.setattr(guard, "log", lambda m: None)

    assert guard.recover_gateway() is True
    assert len(order) >= 2
    assert order[0][:3] == ["hermes", "gateway", "restart"]
    assert "schtasks" in order[1][0] or "powershell" in order[1][0]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ---------------------------------------------------------------------------
# 防再犯: "文件进了仓, 但守卫让它跑不起来" 这个坑已经栽过两次。
#   1) .gitignore 挡住了测试文件        -> 提交 6d8b89e 解除
#   2) conftest.py 的 collect_ignore    -> 提交 fe374de 补提交
#   3) pyproject.toml 的 ruff exclude   -> 提交 5821ff6 解除
# 每一处单独看都像"合理的例外", 合起来就是 13 项关键测试从来没跑过。
# 这里把三处一次性钉死, 以后谁再解除忽略, 漏了下游守卫就会当场红灯。
# ---------------------------------------------------------------------------


def test_guard_contract_tests_are_not_silently_disabled():
    """守护契约测试必须真的被收集、被 lint 检查、没被 git 忽略。"""
    import subprocess

    repo = Path(__file__).resolve().parents[1]
    guard_test = repo / "tests" / "test_weixin_guard.py"

    # 0) 不是 git 仓库就跳过第 3/4 项 ——
    #    公开副本是导出产物, **没有 .git**, 在那里跑 git check-ignore /
    #    ls-files 必然报 "not a git repository"。
    #    那是环境差异, 不是回归 —— 该 skip 而不是 fail。
    #    ⚠️ 注释里别写完整盘符形态 —— 公开审计会按"本机绝对路径"命中。
    inside_repo = (repo / ".git").exists()
    if not inside_repo:
        import pytest
        pytest.skip("不在 git 仓库里 (公开副本是导出产物, 没有 .git)")

    # 1) conftest.py 不得把它排除在收集之外
    conftest = (repo / "tests" / "conftest.py").read_text(encoding="utf-8")
    ignore_line = next(
        (line for line in conftest.splitlines() if line.strip().startswith("collect_ignore =")),
        "collect_ignore = []",
    )
    assert "test_weixin_guard" not in ignore_line, (
        f"tests/conftest.py 又把守护测试排除在收集之外了: {ignore_line.strip()}"
    )

    # 2) ruff 不得把它排除在检查之外
    pyproject = (repo / "pyproject.toml").read_text(encoding="utf-8")
    ruff_seg = pyproject[pyproject.find("[tool.ruff]"):]
    ruff_seg = ruff_seg[: ruff_seg.find("\n[") if "\n[" in ruff_seg else len(ruff_seg)]
    assert "test_weixin_guard" not in ruff_seg, (
        "pyproject.toml 的 ruff 配置又把守护测试排除在 lint 之外了 —— "
        "豁免会掩盖真实 lint 错"
    )

    # 3) git 不得把它忽略掉
    rc = subprocess.run(
        ["git", "check-ignore", "-q", str(guard_test)],
        cwd=str(repo),
        capture_output=True,
    )
    assert rc.returncode != 0, (
        "tests/test_weixin_guard.py 又被 .gitignore 忽略了 —— "
        "文件不进仓就等于没有测试"
    )

    # 4) 实际证明它进了版本管理 (不是只在磁盘上)
    proc = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "tests/test_weixin_guard.py"],
        cwd=str(repo),
        capture_output=True,
    )
    assert proc.returncode == 0, "守护测试没有进版本管理"
