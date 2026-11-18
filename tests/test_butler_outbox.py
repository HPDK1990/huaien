"""scripts/butler.py 的 outbox 补发机制 —— 微信 session 失效后的重投。

重点: **失败不丢、成功要清、session 失效要认得出、dry 绝不发**。
"""
import importlib.util
import json
import os
import sys

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load():
    spec = importlib.util.spec_from_file_location(
        "butler_outbox_mod", os.path.join(PROJ, "scripts", "butler.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_cli():
    spec = importlib.util.spec_from_file_location(
        "flush_outbox_cli", os.path.join(PROJ, "scripts", "flush_outbox.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _R:
    """假 subprocess.CompletedProcess。"""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _install_outbox(mod, monkeypatch, tmp_path, items):
    """把 outbox 指到临时文件, 塞进 items。"""
    path = tmp_path / "outbox_pending.json"
    monkeypatch.setattr(mod, "OUTBOX_FILE", str(path))
    mod._outbox_save(items)
    return path


def test_empty_outbox_sends_nothing(monkeypatch, tmp_path):
    """积压为空时不能发 —— 没有东西发, 一条都不许碰微信。"""
    mod = _load()
    path = _install_outbox(mod, monkeypatch, tmp_path, [])

    calls = []
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda *a, **k: calls.append(a) or _R(0, "ok"))

    assert mod.flush_outbox("weixin:abc") == 0
    assert calls == [], "空 outbox 竟然发了东西"
    assert mod.outbox_pending_count() == 0
    assert json.loads(path.read_text(encoding="utf-8")) == []


def test_successful_send_removes_that_item(monkeypatch, tmp_path):
    """发成功的从队列里去掉 —— 不然下次会重复发。"""
    mod = _load()
    _install_outbox(mod, monkeypatch, tmp_path,
                    [{"text": "第一条", "at": "2026-11-04T09:00:00", "err": "旧错"}])

    sent = []
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda cmd, **k: sent.append(cmd) or _R(0, "已发送"))

    assert mod.flush_outbox("weixin:abc") == 1
    assert mod._outbox_load() == [], "发成功的还留在 outbox 里"
    assert mod.outbox_pending_count() == 0
    assert sent[0][:2] == ["hermes", "send"]
    assert "第一条" in sent[0]


def test_failed_send_keeps_item_and_updates_err(monkeypatch, tmp_path):
    """发失败的**留在队列里**, 并把失败原因写进 err —— 不能丢消息。"""
    mod = _load()
    _install_outbox(mod, monkeypatch, tmp_path,
                    [{"text": "留着", "at": "2026-11-04T09:00:00", "err": "旧错"}])

    monkeypatch.setattr(mod.subprocess, "run",
                        lambda *a, **k: _R(1, "", "connection refused"))

    assert mod.flush_outbox("weixin:abc") == 0
    left = mod._outbox_load()
    assert len(left) == 1, "失败的被丢了"
    assert left[0]["text"] == "留着"
    assert "connection refused" in left[0]["err"]
    assert left[0]["err"] != "旧错", "err 没更新"


def test_mixed_batch_keeps_only_failures(monkeypatch, tmp_path):
    """多条混着来: 成的走, 败的留, 成功条数要准。"""
    mod = _load()
    _install_outbox(mod, monkeypatch, tmp_path, [
        {"text": "A", "at": "t", "err": ""},
        {"text": "B", "at": "t", "err": ""},
        {"text": "C", "at": "t", "err": ""},
    ])

    outcomes = [_R(0, "ok"), _R(1, "", "boom"), _R(0, "ok")]
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: outcomes.pop(0))

    assert mod.flush_outbox("weixin:abc") == 2
    left = mod._outbox_load()
    assert [i["text"] for i in left] == ["B"]
    assert "boom" in left[0]["err"]
    assert mod.outbox_pending_count() == 1


def test_session_dead_marker_is_recognized():
    """session 失效的判据只用用户已经踩过的那两句特征串, 不另造花样。"""
    mod = _load()
    assert mod.SESSION_DEAD_MARKERS == ("session not ready", "prepare failed")
    assert mod._is_session_dead(
        "iLink sendmessage session not ready: ret=-2 prepare failed "
        "- the user must send the bot a message first (or re-pair)")
    assert mod._is_session_dead("prepare failed")
    assert mod._is_session_dead("session not ready")
    # 别的错误不能被误判成 session 失效
    assert not mod._is_session_dead("connection refused")
    assert not mod._is_session_dead("HTTP 401: Missing Authentication header")
    assert not mod._is_session_dead("")


def test_session_dead_failure_is_kept_with_human_hint(monkeypatch, tmp_path):
    """session 失效时消息要留下, err 要说人话 (等用户先发一条)。"""
    mod = _load()
    _install_outbox(mod, monkeypatch, tmp_path,
                    [{"text": "等 session", "at": "t", "err": ""}])

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _R(
        1, "", "iLink sendmessage session not ready: ret=-2 prepare failed "
               "- the user must send the bot a message first (or re-pair)"))

    assert mod.flush_outbox("weixin:abc") == 0
    left = mod._outbox_load()
    assert len(left) == 1
    assert "session" in left[0]["err"]
    assert "用户" in left[0]["err"]


def test_dry_mode_does_not_send(monkeypatch, tmp_path):
    """dry 只报"会补发什么", 一个都不能真发, 队列也不能动。"""
    mod = _load()
    path = _install_outbox(mod, monkeypatch, tmp_path,
                           [{"text": "只看不发", "at": "t", "err": ""}])

    calls = []
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or _R(0, "ok"))

    assert mod.flush_outbox_preview("weixin:abc") == 1
    assert calls == [], "dry 模式竟然真发了"
    assert len(mod._outbox_load()) == 1, "dry 模式动了队列"
    assert json.loads(path.read_text(encoding="utf-8"))[0]["text"] == "只看不发"


def test_flush_without_target_falls_back_to_wechat(monkeypatch, tmp_path):
    """没给 --to 时用微信私聊 (用户自己找到的 chat_id)。"""
    mod = _load()
    _install_outbox(mod, monkeypatch, tmp_path, [{"text": "X", "at": "t", "err": ""}])
    monkeypatch.setattr(mod, "_find_wechat", lambda: "dm123")

    sent = []
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda cmd, **k: sent.append(cmd) or _R(0, "ok"))

    assert mod.flush_outbox() == 1
    assert "weixin:dm123" in sent[0]


def test_outbox_pending_count_reflects_file(monkeypatch, tmp_path):
    """积压条数得跟文件里真的一样 —— 看板/提醒要用它。"""
    mod = _load()
    _install_outbox(mod, monkeypatch, tmp_path,
                    [{"text": "1", "at": "t", "err": ""},
                     {"text": "2", "at": "t", "err": ""}])
    assert mod.outbox_pending_count() == 2
    mod._outbox_save([])
    assert mod.outbox_pending_count() == 0


def test_broken_outbox_file_is_treated_as_empty(monkeypatch, tmp_path):
    """outbox 文件坏了不能崩 —— 当空的, 队列照常能用。"""
    mod = _load()
    path = tmp_path / "outbox_pending.json"
    path.write_text("{ 这不是 json", encoding="utf-8")
    monkeypatch.setattr(mod, "OUTBOX_FILE", str(path))
    assert mod.outbox_pending_count() == 0
    assert mod.flush_outbox("weixin:abc") == 0


def test_cli_dry_does_not_send(monkeypatch, tmp_path, capsys):
    """scripts/flush_outbox.py --dry 只看不发 —— cron 试水时不能误推。"""
    cli = _load_cli()
    _install_outbox(cli.butler, monkeypatch, tmp_path,
                    [{"text": "积压的一条", "at": "t", "err": ""}])

    calls = []
    monkeypatch.setattr(cli.butler.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or _R(0, "ok"))

    assert cli.main(["--dry", "--to", "weixin:abc"]) == 0
    assert calls == [], "CLI --dry 竟然真发了"
    out = capsys.readouterr().out
    assert "积压的一条" in out
    assert len(cli.butler._outbox_load()) == 1


def test_cli_real_run_sends_and_clears(monkeypatch, tmp_path):
    """scripts/flush_outbox.py 真跑: 发的发出去, 队列清干净。"""
    cli = _load_cli()
    _install_outbox(cli.butler, monkeypatch, tmp_path,
                    [{"text": "补发我", "at": "t", "err": ""}])

    monkeypatch.setattr(cli.butler.subprocess, "run", lambda *a, **k: _R(0, "ok"))

    assert cli.main(["--to", "weixin:abc"]) == 0
    assert cli.butler._outbox_load() == []


def test_cli_empty_outbox_is_quiet(monkeypatch, tmp_path, capsys):
    """没积压就别啰嗦 —— 管家铁律: 没事完全安静。"""
    cli = _load_cli()
    _install_outbox(cli.butler, monkeypatch, tmp_path, [])

    def boom(*a, **k):
        raise AssertionError("没积压却发了")

    monkeypatch.setattr(cli.butler.subprocess, "run", boom)
    assert cli.main([]) == 0
    assert "空的" in capsys.readouterr().out


def test_butler_main_flush_flag_triggers_flush(monkeypatch, tmp_path, capsys):
    """butler.py --flush 要能触发补发 (跟独立脚本等价)。"""
    mod = _load()
    _install_outbox(mod, monkeypatch, tmp_path,
                    [{"text": "走 --flush", "at": "t", "err": ""}])
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _R(0, "ok"))
    monkeypatch.setattr(sys, "argv", ["butler.py", "--flush", "--to", "weixin:abc"])
    assert mod.main() == 0
    assert mod._outbox_load() == []
    assert "补发" in capsys.readouterr().out
