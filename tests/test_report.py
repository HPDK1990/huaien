"""scripts/report.py 的单测 —— 重点是主动推送能找到微信。"""
import importlib.util
import os
import sys

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)



def _load():
    spec = importlib.util.spec_from_file_location(
        "report_mod", os.path.join(PROJ, "scripts", "report.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_find_chat_is_platform_scoped(monkeypatch, tmp_path):
    """find_chat 必须按平台过滤, 不能把飞书的 id 拿去发微信。"""
    mod = _load()
    rows = [
        '{"origin": {"platform": "feishu", "chat_id": "oc_aaa", "chat_type": "dm"}}',
        '{"origin": {"platform": "weixin", "chat_id": "wx_bbb", "chat_type": "dm"}}',
    ]
    import sqlite3
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE gateway_routing (entry_json TEXT)")
    for r in rows:
        con.execute("INSERT INTO gateway_routing VALUES (?)", (r,))
    con.commit()
    con.close()

    monkeypatch.setattr(mod, "STATE_DB", str(db))
    # 让 config.yaml 里的 home channel 走不到, 强制只测 routing 表
    monkeypatch.setattr(mod, "HERMES_HOME", str(tmp_path))

    assert mod.find_chat("weixin") == "wx_bbb"
    assert mod.find_chat("feishu") == "oc_aaa"
    assert mod.find_chat("telegram") is None


def test_send_reports_weixin_session_expiry(capsys, monkeypatch):
    """微信 session 失效时要给出人话提示, 而不是只打印 rc=1。"""
    mod = _load()

    class R:
        returncode = 1
        stdout = ""
        stderr = ("iLink sendmessage session not ready: ret=-2 prepare failed "
                  "- the user must send the bot a message first")

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: R())
    rc = mod.send("hi", "weixin:abc", None, False)
    assert rc == 1
    out = capsys.readouterr().out
    assert "session" in out.lower()
    assert "先在微信里发一条消息" in out


def test_send_dry_does_not_call_hermes(capsys, monkeypatch):
    """dry 模式绝对不能真发。"""
    mod = _load()

    def boom(*a, **k):
        raise AssertionError("dry 模式不该起子进程")

    monkeypatch.setattr(mod.subprocess, "run", boom)
    assert mod.send("hi", "weixin:abc", None, True) == 0
    assert "dry" in capsys.readouterr().out


def test_home_channel_wins_over_routing(monkeypatch, tmp_path):
    """配置里写死的 home channel 优先于 routing 表。"""
    mod = _load()
    (tmp_path / "config.yaml").write_text(
        'WEIXIN_HOME_CHANNEL="o9cq80_XXX@im.wechat"\n', encoding="utf-8")
    import sqlite3
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE gateway_routing (entry_json TEXT)")
    con.execute("INSERT INTO gateway_routing VALUES (?)",
                ('{"origin": {"platform": "weixin", "chat_id": "routed", "chat_type": "dm"}}',))
    con.commit()
    con.close()
    monkeypatch.setattr(mod, "HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(mod, "STATE_DB", str(db))
    assert mod.find_chat("weixin") == "o9cq80_XXX@im.wechat"


def test_home_channel_used_even_without_state_db(monkeypatch, tmp_path):
    """没有 state.db 时也要能用配置里的 home channel (刚装好 Hermes 就是这状态)。"""
    mod = _load()
    (tmp_path / "config.yaml").write_text(
        "FEISHU_HOME_CHANNEL=oc_abc123\n", encoding="utf-8")
    monkeypatch.setattr(mod, "HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(mod, "STATE_DB", str(tmp_path / "nope.db"))
    assert mod.find_chat("feishu") == "oc_abc123"
    assert mod.find_chat("weixin") is None


def test_db_health_external_content_detection(tmp_path):
    """db_health 的 external-content 判定不能反。

    ⚠️ 实测坑 (2026-11-03, P3 coder 发现的): 旧判定找 "content=''"
       (空引号 = 内容表是自己 = 旧布局), 但真库早就是 content='messages_fts_src'
       (指向外部表 = 新布局) -> db_health 一直误报"FTS 还是旧布局",
       还会建议跑 optimize-storage (那是会动库的)。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "db_health_mod", os.path.join(PROJ, "scripts", "db_health.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    import re
    new_sql = ("CREATE VIRTUAL TABLE messages_fts USING fts5("
               "content, content='messages_fts_src', content_rowid='id')")
    old_sql = ("CREATE VIRTUAL TABLE messages_fts USING fts5("
               "content, content='', content_rowid='id')")
    assert re.search(r"content\s*=\s*'[^']+'", new_sql), "具名外部表要认成新布局"
    assert not re.search(r"content\s*=\s*'[^']+'", old_sql), "content='' 是旧布局"
