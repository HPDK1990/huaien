"""告警分层与去重的回归测试。

防的是这几个坑:
  1. 同一个故障连推 34 次 —— 看门狗每 6 小时跑一轮, 不去重就天天刷用户的手机
  2. 噪音当故障 —— "桌面前台没开"这种可选组件报红, 把真问题埋掉
  3. 推送路径悄悄坏掉 —— 但ler.push 是怀恩唯一的微信出口, 坏了就全哑了
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ))


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, PROJ / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


butler = _load("butler_impl", "scripts/butler.py")


# ---------------------------------------------------------------- 去重


def test_dedup_blocks_identical_content_within_window(tmp_path, monkeypatch):
    monkeypatch.setattr(butler, "PROJ", str(tmp_path))
    assert butler._dedup_ok("网关不在了", 6) is True
    assert butler._dedup_ok("网关不在了", 6) is False
    assert butler._dedup_ok("别的故障", 6) is True


def test_dedup_window_zero_always_allows(tmp_path, monkeypatch):
    monkeypatch.setattr(butler, "PROJ", str(tmp_path))
    assert butler._dedup_ok("x", 0) is True
    assert butler._dedup_ok("x", 0) is True


def test_dedup_state_survives_new_process(tmp_path, monkeypatch):
    """看门狗每轮是新进程 —— 去重状态必须跨进程有效。"""
    monkeypatch.setattr(butler, "PROJ", str(tmp_path))
    assert butler._dedup_ok("跨进程", 6) is True

    fresh = _load("butler_impl_2", "scripts/butler.py")
    fresh.PROJ = str(tmp_path)
    assert fresh._dedup_ok("跨进程", 6) is False


def test_dedup_state_file_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(butler, "PROJ", str(tmp_path))
    for i in range(260):
        butler._dedup_ok(f"第{i}条", 6)
    state_file = tmp_path / "logs" / "push_dedup.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert len(state) <= 200


# ---------------------------------------------------------------- 推送内容


def test_push_never_sends_internal_noise(tmp_path, monkeypatch):
    """内部通知一个字都不许进用户手机。"""
    monkeypatch.setattr(butler, "PROJ", str(tmp_path))
    calls = []

    class _R:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def _fake_run(cmd, **k):
        calls.append(cmd)
        return _R()

    monkeypatch.setattr(butler.subprocess, "run", _fake_run)
    butler.push("[ASYNC DELEGATION COMPLETE - x]\n" + "细节" * 50,
                target="weixin:test")
    assert calls == [], "内部通知不该发出去"


def test_push_respects_max_chars(tmp_path, monkeypatch):
    monkeypatch.setattr(butler, "PROJ", str(tmp_path))
    sent = {}

    class _R:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def _fake_run(cmd, **k):
        sent["cmd"] = cmd
        return _R()

    monkeypatch.setattr(butler.subprocess, "run", _fake_run)
    butler.push("很长的正文" * 100, target="weixin:test", max_chars=50)
    assert "cmd" in sent
    body = sent["cmd"][2]
    assert len(body) <= 80, f"截断后仍有 {len(body)} 字"


def test_push_dedup_blocks_second_identical_push(tmp_path, monkeypatch):
    monkeypatch.setattr(butler, "PROJ", str(tmp_path))
    calls = []

    class _R:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def _fake_run(cmd, **k):
        calls.append(cmd)
        return _R()

    monkeypatch.setattr(butler.subprocess, "run", _fake_run)
    butler.push("网关不在了", target="weixin:test", dedup_hours=6)
    butler.push("网关不在了", target="weixin:test", dedup_hours=6)
    assert len(calls) == 1, "同一条内容不该推两次"


# ---------------------------------------------------------------- 看门狗分层


def test_watchdog_treats_frontend_down_as_hint_not_failure():
    """桌面前台是可选组件 —— 它没开不算故障 (用户主用微信)。"""
    src = (PROJ / "scripts" / "regression_check.py").read_text(encoding="utf-8")
    assert 'problems.append("前台窗口不在了")' not in src, \
        "桌面前台没开又被算成故障了 —— 真问题会被噪音埋掉"
    assert "桌面前台没开" in src, "应该降级成提示并记日志"


def test_watchdog_treats_memory_drift_as_hint_not_failure():
    """记忆是双向收敛过程, 差异存在是常态 —— 看门狗不该改写记忆。"""
    src = (PROJ / "scripts" / "regression_check.py").read_text(encoding="utf-8")
    assert 'problems.append(f"记忆没对齐' not in src, \
        "记忆差异又被算成故障了"
    assert "记忆未收敛" in src


def test_watchdog_still_fails_on_gateway_death():
    """分层不能把真问题也降级 —— gateway 不在了必须仍然报故障。"""
    src = (PROJ / "scripts" / "regression_check.py").read_text(encoding="utf-8")
    assert 'problems.append("gateway 不在了")' in src


def test_watchdog_pushes_real_problems_to_wechat():
    """真问题必须推微信 —— 走但ler.push (有凭据的那条路)。"""
    src = (PROJ / "scripts" / "regression_check.py").read_text(encoding="utf-8")
    assert "impl.push(" in src, "真问题没接微信推送"
    assert "dedup_hours=6" in src, "推送没去重 —— 用户会被同一条消息刷屏"


def test_watchdog_does_not_deliver_to_weixin_directly():
    """不能改用 cron 的 deliver=weixin —— huaien profile 没有微信凭据。"""
    jobs = Path(os.environ.get("HERMES_HOME", "")) / "profiles" / "huaien" / "cron" / "jobs.json"
    if not jobs.exists():
        pytest.skip("本机没有 huaien profile")
    data = json.loads(jobs.read_text(encoding="utf-8"))
    joblist = data.get("jobs", data)
    if isinstance(joblist, dict):
        joblist = list(joblist.values())
    for job in joblist:
        if not isinstance(job, dict):
            continue
        script = str(job.get("script") or "")
        if "flush_outbox" in script or "regression" in script:
            assert not str(job.get("deliver") or "").startswith("weixin"), \
                f"{job.get('name')} 用了 deliver=weixin —— 那条路没凭据"


# ---------------------------------------------------------------- outbox 补发去重


def test_outbox_dedup_keeps_first_and_defers_rest(tmp_path, monkeypatch):
    """同文重复只补第一条, 其余留队 —— 不丢数据, 也不刷屏。"""
    monkeypatch.setattr(butler, "PROJ", str(tmp_path))
    items = [
        {"text": "同一条"},
        {"text": "另一条"},
        {"text": "同一条"},
    ]
    to_send, deferred = butler._dedup_outbox_items(items)
    assert [i["text"] for i in to_send] == ["同一条", "另一条"]
    assert [i["text"] for i in deferred] == ["同一条"]


def test_outbox_preview_count_matches_actual_send(tmp_path, monkeypatch, capsys):
    """dry 说几条就得真发几条 —— 预览骗人比没预览更糟。"""
    monkeypatch.setattr(butler, "PROJ", str(tmp_path))
    # OUTBOX_FILE 是**模块加载时**绑定的常量 —— 只改 PROJ 不会改它
    ob = tmp_path / "logs" / "outbox_pending.json"
    monkeypatch.setattr(butler, "OUTBOX_FILE", str(ob))
    ob.parent.mkdir(parents=True, exist_ok=True)
    ob.write_text(json.dumps([{"text": "a"}, {"text": "b"}, {"text": "a"}],
                             ensure_ascii=False), encoding="utf-8")

    calls = []

    class _R:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def _fake_run(cmd, **k):
        calls.append(cmd)
        return _R()

    monkeypatch.setattr(butler.subprocess, "run", _fake_run)
    monkeypatch.setattr(butler, "resolve_target", lambda t=None: "weixin:x")

    dry_n = butler.flush_outbox_preview()
    assert dry_n == 2, f"dry 说 {dry_n} 条, 去重后应是 2 条"

    butler.flush_outbox()
    assert len(calls) == 2, f"真发了 {len(calls)} 条, 和 dry 说的 {dry_n} 不一致"
