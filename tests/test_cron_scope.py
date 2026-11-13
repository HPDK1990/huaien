"""cron 环境作用域的回归测试。

## 为什么要有这个文件

cron 执行器把 `HERMES_HOME` 设成 `profiles/<name>/` —— 这是**设计如此**, 不是 bug。
但怀恩的管家脚本有三处东西**不在 profile 里**:

| 要的东西 | 在哪 | 不处理会怎样 |
|---|---|---|
| 微信凭据 (WEIXIN_*) | 主 Hermes 的 .env | `Platform 'weixin' is not configured` —— 推送全废 |
| gateway 心跳 | 主 Hermes 的 state/ | 误报 "gateway 可能根本没起来" |
| `hermes` 可执行文件 | 主 Hermes的 bin/ | 找不到命令 |

三个症状都是**静默或误导**的: 推送失败不报错、告警说的是假故障。
所以这里把"在 profile 作用域下仍能正确工作"锁死。

⚠️ 这些坑在**手工跑**时全都不出现 (手工环境 HERMES_HOME 是主 Hermes),
只有 cron 里才炸 —— 所以必须有测试守着。
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

HUAien_HOME = Path(os.environ.get("HERMES_HOME", r"%LOCALAPPDATA%\AppData\Local\hermes"))


def _load_fresh():
    """重新加载一遍 butler —— 常量在 import 时算, 得重新算。"""
    spec = importlib.util.spec_from_file_location(
        "butler_scope_test", PROJ / "scripts" / "butler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- base home


def test_base_home_strips_profile_segment(monkeypatch):
    """HERMES_HOME=profiles/huaien 时, base home 必须是主 Hermes 根。"""
    monkeypatch.setenv("HERMES_HOME", str(HUAien_HOME / "profiles" / "huaien"))
    mod = _load_fresh()
    assert mod._base_home() == str(HUAien_HOME)
    assert "profiles" not in mod._base_home()


def test_base_home_keeps_plain_home(monkeypatch):
    """HERMES_HOME 已经是主 Hermes 根时, 别再往上剥。"""
    monkeypatch.setenv("HERMES_HOME", str(HUAien_HOME))
    mod = _load_fresh()
    assert mod._base_home() == str(HUAien_HOME)


def test_profile_home_is_under_base(monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(HUAien_HOME / "profiles" / "huaien"))
    mod = _load_fresh()
    assert mod._profile_home() == str(HUAien_HOME / "profiles" / "huaien")


# ---------------------------------------------------------------- 推送作用域


def test_env_points_send_at_credential_home(monkeypatch):
    """hermes send 必须在**有凭据**的 home 下跑, 否则推送静默全废。"""
    monkeypatch.setenv("HERMES_HOME", str(HUAien_HOME / "profiles" / "huaien"))
    mod = _load_fresh()
    env = mod._env()
    assert env["HERMES_HOME"] == str(HUAien_HOME), \
        "HERMES_HOME 没指回凭据所在的主 Hermes —— 微信推送会报 Platform 'weixin' is not configured"


def test_env_keeps_bin_on_path(monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(HUAien_HOME / "profiles" / "huaien"))
    mod = _load_fresh()
    env = mod._env()
    assert mod.HERMES_BIN in env["PATH"]


# ---------------------------------------------------------------- 心跳误报


def test_gateway_heartbeat_checked_at_base_home(monkeypatch):
    """心跳在主 Hermes 下 —— 在 profile 里找会误报 'gateway 根本没起来'。"""
    monkeypatch.setenv("HERMES_HOME", str(HUAien_HOME / "profiles" / "huaien"))
    _load_fresh()   # 重新加载以刷新常量
    src = (PROJ / "scripts" / "butler.py").read_text(encoding="utf-8")
    assert 'os.path.join(HERMES_BASE, "state", "gateway.heartbeat")' in src, \
        "心跳路径没用 HERMES_BASE —— cron 里会误报 gateway 挂了"
    assert 'os.path.join(HERMES_HOME, "state", "gateway.heartbeat")' not in src


def test_gateway_check_does_not_report_when_heartbeat_fresh(monkeypatch):
    """心跳新鲜时不该报 'gateway 可能根本没起来'。"""
    monkeypatch.setenv("HERMES_HOME", str(HUAien_HOME / "profiles" / "huaien"))
    mod = _load_fresh()
    hb = HUAien_HOME / "state" / "gateway.heartbeat"
    if not hb.exists():
        pytest.skip("本机没有 gateway 心跳文件")
    hit, detail = mod.check_gateway_alive()
    if hit:
        assert "心跳文件都没了" not in detail, \
            f"误报 gateway 没起来 (心跳文件其实在): {detail}"


# ---------------------------------------------------------------- outbox 兜底


def test_non_session_failure_also_goes_to_outbox(tmp_path, monkeypatch):
    """非 session 失效的失败也必须存 outbox —— 否则消息直接消失。"""
    monkeypatch.setenv("HERMES_HOME", str(HUAien_HOME))
    mod = _load_fresh()
    monkeypatch.setattr(mod, "PROJ", str(tmp_path))
    monkeypatch.setattr(mod, "OUTBOX_FILE", str(tmp_path / "logs" / "outbox_pending.json"))

    class _R:
        returncode = 1
        stdout = ""
        stderr = "Platform 'weixin' is not configured"

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _R())
    mod.push("这条不该消失", target="weixin:x")
    data = json.loads((tmp_path / "logs" / "outbox_pending.json").read_text(encoding="utf-8"))
    assert len(data) == 1, f"失败的消息没进 outbox —— 它消失了 (outbox {len(data)} 条)"
    assert "这条不该消失" in data[0]["text"]


def test_successful_push_leaves_outbox_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(HUAien_HOME))
    mod = _load_fresh()
    monkeypatch.setattr(mod, "PROJ", str(tmp_path))
    monkeypatch.setattr(mod, "OUTBOX_FILE", str(tmp_path / "logs" / "outbox_pending.json"))

    class _R:
        returncode = 0
        stdout = "sent"
        stderr = ""

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _R())
    assert mod.push("正常消息", target="weixin:x") == 0
    assert not (tmp_path / "logs" / "outbox_pending.json").exists() or \
        json.loads((tmp_path / "logs" / "outbox_pending.json").read_text(encoding="utf-8")) == []
