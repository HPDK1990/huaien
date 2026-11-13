"""scripts/await_kanban.py 的单测 —— "派完活就走"这条命令必须可信。"""
from __future__ import annotations

import importlib.util
import os
import sys

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)


def _load():
    spec = importlib.util.spec_from_file_location(
        "await_mod", os.path.join(PROJ, "scripts", "await_kanban.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_all_done_returns_true(monkeypatch):
    """全 done 时立刻返回 True。"""
    mod = _load()
    monkeypatch.setattr(mod, "board_status",
                        lambda ids: {"t_a": "done", "t_b": "done"})
    ok, states = mod.await_tasks(["t_a", "t_b"], timeout=5, interval=0.01, quiet=True)
    assert ok
    assert states == {"t_a": "done", "t_b": "done"}


def test_failure_state_stops_early(monkeypatch):
    """有卡 failed 就不该继续等 —— 直接返回 False。"""
    mod = _load()
    monkeypatch.setattr(mod, "board_status",
                        lambda ids: {"t_a": "done", "t_b": "failed"})
    ok, states = mod.await_tasks(["t_a", "t_b"], timeout=5, interval=0.01, quiet=True)
    assert not ok
    assert states["t_b"] == "failed"


def test_timeout_returns_false(monkeypatch):
    """一直 running 就等到超时返回 False, 不能死循环。"""
    mod = _load()
    monkeypatch.setattr(mod, "board_status", lambda ids: {"t_a": "running"})
    ok, states = mod.await_tasks(["t_a"], timeout=0.15, interval=0.05, quiet=True)
    assert not ok
    assert states == {"t_a": "running"}


def test_board_status_parses_json(monkeypatch):
    """看板返回的 JSON 要能解出 id->status。"""
    mod = _load()

    class R:
        stdout = ('[{"id": "t_a", "status": "done"}, '
                  '{"id": "t_b", "status": "running"}, '
                  '{"id": "t_c", "status": "blocked"}]')
        stderr = ""
        returncode = 0

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: R())
    got = mod.board_status(["t_a", "t_c", "t_missing"])
    assert got == {"t_a": "done", "t_c": "blocked"}


def test_board_status_survives_bad_json(monkeypatch):
    """看板输出坏了不能崩, 返回空 dict。"""
    mod = _load()

    class R:
        stdout = "not json at all"
        stderr = ""
        returncode = 1

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: R())
    assert mod.board_status(["t_a"]) == {}


def test_main_rejects_bad_id_format():
    """t_ 开头以外的 id 直接退出码 2。"""
    mod = _load()
    import sys as _sys
    old = _sys.argv
    _sys.argv = ["await_kanban.py", "not-a-task-id"]
    try:
        assert mod.main() == 2
    finally:
        _sys.argv = old


def test_progress_shows_real_task_ids(monkeypatch, capsys):
    """进度输出必须显示**真实卡号**, 不能显示 P1/P2 这种硬编码标签。

    ⚠️ 实测踩过: 传一张卡 t_9bb79090, 输出却是 "P1:running" —— 标签写死了,
    看着像别的卡, 差点误判成"卡片状态错乱"。
    """
    mod = _load()
    monkeypatch.setattr(mod, "board_status", lambda ids: {ids[0]: "done"})
    mod.await_tasks(["t_9bb79090"], timeout=5, interval=0.01, quiet=False)
    out = capsys.readouterr().out
    assert "t_9bb79090:done" in out
    assert "P1:" not in out
