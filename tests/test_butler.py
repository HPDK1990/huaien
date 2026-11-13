"""scripts/butler.py 的单测 —— 管家大脑的核心行为。

重点: 规则驱动 + 噪音控制。没命中规则就一个字都不许说。
"""
import importlib.util
import os

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load():
    spec = importlib.util.spec_from_file_location(
        "butler_mod", os.path.join(PROJ, "scripts", "butler.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_load_policy_parses_rule_ids(tmp_path):
    """规则格式: `- [rule-id] 说明`。ID 是脚本派发检查器的依据。"""
    mod = _load()
    f = tmp_path / "POLICY.md"
    f.write_text(
        "# 标题\n\n"
        "- [blocked] 看板有卡卡住 -> 推\n"
        "- [db-size] 库太大 -> 推\n"
        "\n"
        "## 不是规则的段落\n"
        "- 这行没有方括号, 不是规则\n"
        "- [UPPER] 大写 ID 应该被小写化\n",
        encoding="utf-8")
    rules = mod.load_policy(str(f))
    assert set(rules) == {"blocked", "db-size", "upper"}
    assert "看板有卡卡住" in rules["blocked"]


def test_load_policy_missing_file_is_empty(tmp_path):
    """读不到规则 = 不猜 = 保持安静。绝不能硬编码一套行为。"""
    mod = _load()
    assert mod.load_policy(str(tmp_path / "nope.md")) == {}


def test_assess_only_runs_enabled_rules(monkeypatch):
    """规则里没写的检查器 = 不启用。用户说了算, 不是代码说了算。"""
    mod = _load()
    called = []

    def fake_blocked():
        called.append("blocked")
        return True, "有卡卡住"

    def fake_db():
        called.append("db-size")
        return True, "库太大"

    monkeypatch.setattr(mod, "CHECKERS", {"blocked": fake_blocked, "db-size": fake_db})
    hits = mod.assess({"blocked": "只要这个"})
    assert called == ["blocked"]
    assert len(hits) == 1
    assert hits[0]["hit"] is True
    assert hits[0]["detail"] == "有卡卡住"


def test_assess_survives_broken_checker(monkeypatch):
    """一个检查器炸了不能让整个管家崩 —— 体检脚本不该自己挂。"""
    mod = _load()

    def boom():
        raise RuntimeError("炸了")

    monkeypatch.setattr(mod, "CHECKERS", {"blocked": boom})
    hits = mod.assess({"blocked": "x"})
    assert len(hits) == 1
    assert hits[0]["hit"] is False
    assert "炸了" in hits[0]["detail"]


def test_panel_is_quiet_when_nothing_hits():
    """没命中 = 安静 = 面板说安静 = 推送内容是"一切正常"而不是"有事"。"""
    mod = _load()
    panel = mod.build_panel([], {"blocked": "x"})
    assert panel["安静"] is True
    assert panel["命中规则"] == 0
    assert "无需打扰" in mod.render_text(panel)


def test_panel_lists_what_needs_you():
    """命中了就必须说清楚是哪几件事。"""
    mod = _load()
    hits = [{"rule": "blocked", "hit": True, "detail": "1 张卡卡住", "severity": "高"}]
    panel = mod.build_panel(hits, {"blocked": "x"})
    assert panel["安静"] is False
    assert panel["命中规则"] == 1
    text = mod.render_text(panel)
    assert "blocked" in text
    assert "1 张卡卡住" in text
    assert "高" in text


def test_unknown_rule_id_is_ignored(monkeypatch):
    """规则文件里写了没实现的 ID —— 忽略, 不报错, 更不能瞎猜一个行为。"""
    mod = _load()
    monkeypatch.setattr(mod, "CHECKERS", {})
    hits = mod.assess({"nonexistent": "随便写的"})
    assert hits == []


def test_outbox_roundtrip(monkeypatch, tmp_path):
    """推送失败要能存进 outbox, 之后用户发过消息再补发。"""
    mod = _load()
    monkeypatch.setattr(mod, "OUTBOX_FILE", str(tmp_path / "outbox.json"))
    mod._outbox_save([{"text": "hi", "at": "now", "err": "x"}])
    loaded = mod._outbox_load()
    assert len(loaded) == 1
    assert loaded[0]["text"] == "hi"


def test_push_reports_weixin_session_expiry(monkeypatch, capsys):
    """微信 session 失效时要给人话提示 —— 这坑退避无效, 必须说清楚。"""
    mod = _load()

    class R:
        returncode = 1
        stdout = ""
        stderr = ("iLink sendmessage session not ready: ret=-2 prepare failed "
                  "- the user must send the bot a message first")

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: R())
    monkeypatch.setattr(mod, "OUTBOX_FILE", os.path.join(PROJ, "logs", "_t_outbox.json"))
    rc = mod.push("测试", "weixin:abc")
    out = capsys.readouterr().out
    assert rc == 1
    assert "iLink session 失效" in out
    assert "必须用户先在微信发一条消息" in out
    # 失败的必须进 outbox, 不能丢
    assert mod._outbox_load()
    mod._outbox_save([])


def test_find_wechat_prefers_dm(monkeypatch, tmp_path):
    """微信优先找私聊 —— 群聊容易发错地方。"""
    mod = _load()
    import sqlite3
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE gateway_routing (entry_json TEXT)")
    con.execute("INSERT INTO gateway_routing VALUES (?)",
                ('{"origin": {"platform": "weixin", "chat_id": "grp", "chat_type": "group"}}',))
    con.execute("INSERT INTO gateway_routing VALUES (?)",
                ('{"origin": {"platform": "weixin", "chat_id": "dm1", "chat_type": "dm"}}',))
    con.commit()
    con.close()
    monkeypatch.setattr(mod, "STATE_DB", str(db))
    assert mod._find_wechat() == "dm1"
