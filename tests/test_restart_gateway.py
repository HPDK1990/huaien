"""怀恩 — 安全重启脚本的单元测试

跑: python -m pytest tests/ -q   或   python tests/test_restart_gateway.py

重点覆盖三件**危险**的事:
1. gateway 停不掉时**绝不能启动第二个** —— 两个 gateway 写同一个 state.db
2. 围堵扫描必须解析 JSON, 不能用正则捞人类可读文本
   ("integrity_check : ok" 那行会被正则当成一个 session id)
3. 强杀只按精确 PID, 不用按镜像名批量杀
"""
from __future__ import annotations

import json
import os
import sys

import pytest

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJ, "scripts"))

import restart_gateway_safer as r  # noqa: E402


@pytest.fixture(autouse=True)
def _log_to_tmp(tmp_path, monkeypatch):
    """把日志写到临时文件 —— 否则测试的输出会污染 logs/gateway_restart.log,
    以后看真重启记录时满屏都是测试痕迹。"""
    monkeypatch.setattr(r, "LOG", str(tmp_path / "restart.log"))


# ---------- scan_wedges ----------

def test_scan_wedges_parses_json(monkeypatch):
    payload = json.dumps({
        "wedged_session_count": 2,
        "wedged_sessions": [{"session_id": "sess_a"}, {"session_id": "sess_b"}],
    })
    monkeypatch.setattr(r, "run", lambda cmd, timeout=200: (0, payload))
    assert r.scan_wedges() == (2, ["sess_a", "sess_b"])


def test_scan_wedges_ignores_text_output(monkeypatch):
    """人类可读输出不能被当成 id —— 'integrity_check' 看着就像个标识符。"""
    text = ("state.db        : C:\\x\\state.db\n"
            "integrity_check : ok\n"
            "wedged sessions: 0\n")
    monkeypatch.setattr(r, "run", lambda cmd, timeout=200: (0, text))
    n, ids = r.scan_wedges()
    assert n == 0
    assert ids == [], f"不该从文本里捞出 id: {ids}"


def test_scan_wedges_survives_garbage(monkeypatch):
    monkeypatch.setattr(r, "run", lambda cmd, timeout=200: (1, "Traceback..."))
    assert r.scan_wedges() == (0, [])


def test_scan_wedges_handles_missing_scanner(monkeypatch):
    monkeypatch.setattr(r.os.path, "exists", lambda p: False)
    assert r.scan_wedges() == (0, [])


def test_scan_wedges_requests_json_flag(monkeypatch):
    seen = {}

    def fake_run(cmd, timeout=200):
        seen["cmd"] = cmd
        return 0, json.dumps({"wedged_session_count": 0, "wedged_sessions": []})

    monkeypatch.setattr(r, "run", fake_run)
    r.scan_wedges()
    assert "--json" in seen["cmd"], "必须用 --json 输出"


# ---------- gateway_pids ----------

def test_gateway_pids_parses_powershell_output(monkeypatch):
    monkeypatch.setattr(r, "run", lambda cmd, timeout=200: (0, "1234\n5678\n\n"))
    assert r.gateway_pids() == [1234, 5678]


def test_gateway_pids_ignores_noise(monkeypatch):
    monkeypatch.setattr(r, "run", lambda cmd, timeout=200: (0, "abc\n1234\n"))
    assert r.gateway_pids() == [1234]


# ---------- wait_stopped ----------

def test_wait_stopped_returns_true_when_gone(monkeypatch):
    monkeypatch.setattr(r, "gateway_pids", lambda: [])
    stopped, lingering = r.wait_stopped(timeout=1)
    assert stopped is True
    assert lingering == []


def test_wait_stopped_times_out_with_lingering_pids(monkeypatch):
    monkeypatch.setattr(r, "gateway_pids", lambda: [4321])
    monkeypatch.setattr(r.time, "sleep", lambda s: None)
    stopped, lingering = r.wait_stopped(timeout=0)
    assert stopped is False
    assert lingering == [4321]


# ---------- main 的门禁 ----------

def test_main_refuses_to_start_when_gateway_will_not_stop(monkeypatch):
    """停不掉就**不能**启动第二个 gateway —— 两个写同一个库必出事。"""
    calls = []
    monkeypatch.setattr(r, "run",
                        lambda cmd, timeout=200: (calls.append(cmd), (0, ""))[1])
    monkeypatch.setattr(r, "wait_stopped", lambda timeout=90: (False, [1234]))
    monkeypatch.setattr(r, "gateway_pids", lambda: [1234])
    monkeypatch.setattr(sys, "argv", ["restart_gateway_safer.py"])

    assert r.main() == 1
    assert not any(c[:3] == ["hermes", "gateway", "start"] for c in calls), \
        "停不掉就不该启动第二个 gateway"


def test_main_kills_by_explicit_pid_never_by_image_name(monkeypatch):
    """强杀只能用精确 PID。按镜像名批量杀会连 agent/cron/前台一起干掉。"""
    calls = []
    monkeypatch.setattr(r, "run",
                        lambda cmd, timeout=200: (calls.append(cmd), (0, ""))[1])
    monkeypatch.setattr(r, "wait_stopped", lambda timeout=90: (False, [1234]))
    monkeypatch.setattr(r, "gateway_pids", lambda: [1234])
    monkeypatch.setattr(sys, "argv", ["restart_gateway_safer.py"])

    r.main()
    kills = [c for c in calls if c[:1] == ["taskkill"]]
    assert kills, "应该有按 PID 的强杀"
    for k in kills:
        assert "1234" in k, f"强杀必须带明确 PID: {k}"
        assert "/IM" not in k, f"不许按镜像名批量杀: {k}"


def test_main_happy_path_stops_starts_and_scans(monkeypatch):
    calls = []
    monkeypatch.setattr(r, "run",
                        lambda cmd, timeout=200: (calls.append(cmd), (0, ""))[1])
    monkeypatch.setattr(r, "wait_stopped", lambda timeout=90: (True, []))
    monkeypatch.setattr(r, "gateway_pids", lambda: [9999])
    monkeypatch.setattr(r, "scan_wedges", lambda: (1, ["sess_x"]))
    discarded = []
    monkeypatch.setattr(r, "discard", lambda ids: discarded.extend(ids))
    monkeypatch.setattr(r.time, "sleep", lambda s: None)
    monkeypatch.setattr(sys, "argv", ["restart_gateway_safer.py"])

    assert r.main() == 0
    assert any(c[:3] == ["hermes", "gateway", "stop"] for c in calls)
    assert any(c[:3] == ["hermes", "gateway", "start"] for c in calls)
    assert discarded == ["sess_x"], "有围堵就得清"


def test_main_dry_run_changes_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(r, "run",
                        lambda cmd, timeout=200: (calls.append(cmd), (0, ""))[1])
    monkeypatch.setattr(r, "gateway_pids", lambda: [9999])
    monkeypatch.setattr(sys, "argv", ["restart_gateway_safer.py", "--dry-run"])

    assert r.main() == 0
    assert not calls, "dry-run 不能执行任何命令"


# ---------- discard ----------

def test_discard_calls_each_session(monkeypatch):
    calls = []
    monkeypatch.setattr(r, "run",
                        lambda cmd, timeout=200: (calls.append(cmd), (0, "ok"))[1])
    r.discard(["a", "b"])
    assert len(calls) == 2
    assert calls[0][-2:] == ["a", "--yes"]
    assert calls[1][-2:] == ["b", "--yes"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
