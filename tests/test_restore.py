"""怀恩 — 一键恢复单元测试

跑: python -m pytest tests/ -q   或   python tests/test_restore.py

覆盖 restore.py 的关键判定:
- watchdog 状态解析 (冒号分隔的 key: value)
- 复验逻辑 (gateway/前台必须 alive, 记忆必须 in sync)
- 进程计数 (叶子进程口径: venv launcher 会带一个真解释器)
- 代码回滚在没有那个 tag 时必须不动任何东西

**不真起进程、不真改代码** —— 全用假数据喂。
"""
from __future__ import annotations

import os
import sys

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.join(PROJ, "scripts"))

import restore  # noqa: E402

# ---------- watchdog 状态解析 ----------

def test_watchdog_state_parses_colon_lines(monkeypatch):
    raw = ("gateway: alive\n"
           "frontend: alive\n"
           "memory : in sync\n"
           "web    : DOWN\n")
    monkeypatch.setattr(restore, "run", lambda cmd, timeout=200: (0, raw))
    st = restore.watchdog_state()
    assert st == {"gateway": "alive", "frontend": "alive",
                  "memory": "in sync", "web": "DOWN"}


def test_watchdog_state_empty_when_no_output(monkeypatch):
    monkeypatch.setattr(restore, "run", lambda cmd, timeout=200: (0, ""))
    assert restore.watchdog_state() == {}


# ---------- 复验 ----------

def test_verify_passes_when_all_good(monkeypatch):
    monkeypatch.setattr(restore, "watchdog_state", lambda: {
        "gateway": "alive", "frontend": "alive", "memory": "in sync", "web": "alive"})
    assert restore.verify() is True


def test_verify_fails_when_frontend_down(monkeypatch):
    monkeypatch.setattr(restore, "watchdog_state", lambda: {
        "gateway": "alive", "frontend": "DOWN", "memory": "in sync", "web": "alive"})
    assert restore.verify() is False


def test_verify_fails_when_memory_out_of_sync(monkeypatch):
    monkeypatch.setattr(restore, "watchdog_state", lambda: {
        "gateway": "alive", "frontend": "alive", "memory": "DRIFT", "web": "alive"})
    assert restore.verify() is False


def test_verify_fails_when_key_missing(monkeypatch):
    """状态里没有这个 key -> 不能当成通过了。"""
    monkeypatch.setattr(restore, "watchdog_state", lambda: {"frontend": "alive"})
    assert restore.verify() is False


# ---------- 进程计数 ----------

def test_proc_count_never_negative_and_int(monkeypatch):
    """探针炸了也不能返回负数或非 int。"""
    monkeypatch.setattr(restore, "run", lambda cmd, timeout=200: (0, "garbage\n"))
    n = restore.proc_count("app")
    assert isinstance(n, int) and n >= 0


def test_proc_count_zero_when_no_output(monkeypatch):
    monkeypatch.setattr(restore, "run", lambda cmd, timeout=200: (0, ""))
    assert restore.proc_count("app") == 0


def test_proc_count_parses_leaf_count(monkeypatch):
    monkeypatch.setattr(restore, "run", lambda cmd, timeout=200: (0, "2\n"))
    assert restore.proc_count("app") == 2


# ---------- 代码回滚 ----------

def test_reset_code_no_tag_changes_nothing(monkeypatch):
    """没有那个 tag -> 绝不能动代码 (用户的"不删不改除非明确说"铁律)。"""
    calls = []

    def fake_run(cmd, timeout=200):
        calls.append(cmd)
        if cmd[:2] == ["git", "tag"]:
            return 0, ""          # 没有 tag
        return 0, "should not happen"

    monkeypatch.setattr(restore, "run", fake_run)
    assert restore.reset_code("v-does-not-exist") is False
    assert not any("checkout" in " ".join(c) for c in calls)


def test_reset_code_uses_checkout_not_reset_hard(monkeypatch):
    """回滚必须用 checkout -- . , 绝不能用 reset --hard (会丢未提交改动)。"""
    calls = []

    def fake_run(cmd, timeout=200):
        calls.append(cmd)
        if cmd[:2] == ["git", "tag"]:
            return 0, "v0.3.0\n"
        return 0, ""

    monkeypatch.setattr(restore, "run", fake_run)
    assert restore.reset_code("v0.3.0") is True
    checkout = [c for c in calls if "checkout" in " ".join(c)]
    assert checkout, "应该调过 git checkout"
    assert not any("reset" in " ".join(c) for c in calls), "不许出现 git reset"


# ---------- fix 的分支 ----------

def _state(**kw):
    base = {"gateway": True, "frontend": "alive", "web": "alive",
            "memory": "in sync", "frontend_n": 1, "web_n": 1}
    base.update(kw)
    return base


def test_fix_does_nothing_when_all_alive(monkeypatch):
    started = []
    monkeypatch.setattr(restore, "spawn", lambda *a: started.append(a))
    monkeypatch.setattr(restore, "run", lambda cmd, timeout=200: (0, ""))
    assert restore.fix(_state()) == []
    assert not started


def test_fix_starts_frontend_when_missing(monkeypatch):
    started = []
    monkeypatch.setattr(restore, "spawn", lambda *a: started.append(a))
    monkeypatch.setattr(restore, "run", lambda cmd, timeout=200: (0, ""))
    monkeypatch.setattr(restore.time, "sleep", lambda s: None)
    acts = restore.fix(_state(frontend_n=0, frontend="DOWN"))
    assert "起怀恩前台" in acts
    assert any(a[0] == "huaien.app" for a in started)


def test_fix_starts_web_with_port(monkeypatch):
    started = []
    monkeypatch.setattr(restore, "spawn", lambda *a: started.append(a))
    monkeypatch.setattr(restore, "run", lambda cmd, timeout=200: (0, ""))
    monkeypatch.setattr(restore.time, "sleep", lambda s: None)
    acts = restore.fix(_state(web_n=0, web="DOWN"))
    assert "起手机端 web" in acts
    web = [a for a in started if a[0] == "huaien.web"]
    assert web and "--port" in web[0]


def test_fix_syncs_memory_when_drift(monkeypatch):
    started = []
    monkeypatch.setattr(restore, "spawn", lambda *a: started.append(a))
    monkeypatch.setattr(restore, "run", lambda cmd, timeout=200: (0, ""))
    monkeypatch.setattr(restore.time, "sleep", lambda s: None)
    acts = restore.fix(_state(memory="DRIFT"))
    assert "同步记忆" in acts
    assert not started


if __name__ == "__main__":
    raise SystemExit(pytest_main := __import__("pytest").main([__file__, "-v"]))
