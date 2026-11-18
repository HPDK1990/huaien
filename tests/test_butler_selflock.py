"""巡检 cron 的**自锁**回归测试。

## 现象 (2026-11-17)

重启电脑后, 巡检 cron `怀恩·主动巡检` 卡在 error, failure_streak 一路涨,
而且每轮报的内容都是"1 个定时任务失败: 怀恩·主动巡检" —— **它在自己报自己**。

## 两个根因

1. **自锁**: `check_cron_failures()` 把巡检 cron 自己算进失败名单。
   本轮 exit 1 -> 下轮报"我自己失败" -> 又 exit 1 -> 永远出不来。
2. **exit code 语义错**: 微信 iLink session 过期时 `push()` 返回非 0。
   但那个时刻脚本**已经履约**了 —— 判断做了、简报渲染了、消息进 outbox 了,
   等用户发过话就补发。iLink session 重启后必然过期一次, 所以每重启一次
   就会自锁一次。

## 断言

- 巡检 cron 自己永远不进 cron-fail 名单
- iLink session 失效时 exit 0 (不是失败)
- **别的** cron 失败照样要报 (不能为了不误报而瞎)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ / "scripts"))

import butler  # noqa: E402


# ── ① 巡检 cron 不该把自己算进 cron-fail ────────────────────────────────
def test_self_job_is_excluded_from_cron_fail():
    """巡检 cron 自己的 error 状态不能被报成'定时任务失败'。"""
    assert butler._is_self_job({"script": "butler_watch.py"}) is True
    assert butler._is_self_job({"name": "怀恩·主动巡检"}) is True
    assert butler._is_self_job({"name": "怀恩·主动巡检", "script": "butler_watch.py"}) is True
    # 别的任务不能被误判成自己
    assert butler._is_self_job({"script": "huaien_flush_outbox.py"}) is False
    assert butler._is_self_job({"name": "怀恩·微信补发"}) is False


def test_cron_fail_ignores_self_but_reports_others(monkeypatch, tmp_path):
    """自锁断了, 但**不能瞎** —— 别的 cron 失败照样要报。"""
    jobs = {
        "jobs": [
            {"id": "a", "name": "怀恩·主动巡检", "script": "butler_watch.py",
             "last_status": "error"},
            {"id": "b", "name": "怀恩·微信补发", "script": "huaien_flush_outbox.py",
             "last_status": "error"},
            {"id": "c", "name": "正常任务", "script": "x.py", "last_status": "ok"},
        ]
    }
    jf = tmp_path / "jobs.json"
    jf.write_text(json.dumps(jobs), encoding="utf-8")
    monkeypatch.setattr(butler, "CRON_DIR", str(tmp_path))

    hit, msg = butler.check_cron_failures()
    assert hit is True
    assert "微信补发" in msg
    # 巡检自己**不能**出现在消息里
    assert "主动巡检" not in msg


def test_cron_fail_empty_when_only_self_failed(monkeypatch, tmp_path):
    """只有自己失败 -> 不该报 (否则每轮都刷屏)。"""
    jobs = {"jobs": [
        {"id": "a", "name": "怀恩·主动巡检", "script": "butler_watch.py",
         "last_status": "error"},
    ]}
    (tmp_path / "jobs.json").write_text(json.dumps(jobs), encoding="utf-8")
    monkeypatch.setattr(butler, "CRON_DIR", str(tmp_path))

    hit, msg = butler.check_cron_failures()
    assert hit is False
    assert msg == ""


# ── ② iLink session 失效必须 exit 0 ─────────────────────────────────────
def _fake_send(monkeypatch, rc, blob):
    class R:
        returncode = rc
        stdout = blob
        stderr = ""
    monkeypatch.setattr(butler.subprocess, "run", lambda *a, **k: R())


def test_session_dead_returns_zero(monkeypatch, tmp_path):
    """iLink session 失效 -> 消息进 outbox + exit 0 (已履约, 不是失败)。"""
    monkeypatch.setattr(butler, "_outbox_load", lambda: [])
    saved = {}
    monkeypatch.setattr(butler, "_outbox_save", lambda x: saved.setdefault("items", x))
    monkeypatch.setattr(butler, "_dedup_ok", lambda *a, **k: True)
    monkeypatch.setattr(butler, "_find_wechat", lambda: "fake")
    _fake_send(monkeypatch, 1,
               "iLink sendmessage session not ready: ret=-2 errmsg=prepare failed")

    rc = butler.push("测试内容", max_chars=0, dedup_hours=0)
    assert rc == 0, "session 失效不是巡检失败, 必须 exit 0"
    # 而且消息确实进了 outbox (等补发)
    assert saved.get("items"), "消息必须进 outbox, 否则用户永远收不到"


def test_other_send_failure_is_still_reported(monkeypatch, tmp_path):
    """非 session 失效的失败 -> 仍然 exit 非 0 (真问题要报)。"""
    monkeypatch.setattr(butler, "_outbox_load", lambda: [])
    saved = {}
    monkeypatch.setattr(butler, "_outbox_save", lambda x: saved.setdefault("items", x))
    monkeypatch.setattr(butler, "_dedup_ok", lambda *a, **k: True)
    monkeypatch.setattr(butler, "_find_wechat", lambda: "fake")
    _fake_send(monkeypatch, 1, "Platform 'weixin' is not configured")

    rc = butler.push("测试内容", max_chars=0, dedup_hours=0)
    assert rc != 0, "真失败必须报出来"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
