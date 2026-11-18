"""怀恩 — FTS 索引健康自检的单元测试

跑: python -m pytest tests/ -q   或   python tests/test_selftest_fts_health.py

重点覆盖三件**容易搞错**的事:
1. **半重建索引必须被判红** —— 这是真库里 42.8% 覆盖率的形态, 只查"能不能搜"
   会漏掉它 (重建到一半时, 已建的那部分照样能搜)。
2. **搜索判据不能用中文** —— trigram 分词器对 2 字 CJK 永远 MATCH 不到,
   unicode61 把 CJK 切整段, 命中与否取决于数据分布。拿中文当判据 =
   把好库误判成坏库。
3. **健康库必须全绿** —— 修完索引后跑这个脚本要 rc=0。
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJ, "scripts"))

import selftest_fts_health as selftest  # noqa: E402


def _make_db(path, n_messages: int, index_rows: int | None = None,
             with_src_view: bool = True) -> sqlite3.Connection:
    """造一个 state.db。

    n_messages   —— messages 表行数
    index_rows   —— trigram 索引里实际有几行 (None = 全建, 模拟"只重建了一半")
    with_src_view —— 建不建 messages_fts_trigram_src 视图 (Hermes 真实库有)
    """
    con = sqlite3.connect(str(path))
    con.executescript("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
            tool_name TEXT
        );
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT, model_config TEXT
        );
        CREATE VIRTUAL TABLE messages_fts USING fts5(content);
        CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(
            content, tokenize='trigram'
        );
    """)
    con.execute("INSERT INTO sessions VALUES ('s1', 'desktop', '{}')")
    rows = [(i, "s1", "user", f"hermes message {i} content") for i in range(1, n_messages + 1)]
    con.executemany("INSERT INTO messages VALUES (?,?,?,?,NULL)", rows)
    con.execute("INSERT INTO messages_fts(rowid, content) SELECT id, content FROM messages")
    n_tg = n_messages if index_rows is None else index_rows
    if n_tg:
        con.executemany(
            "INSERT INTO messages_fts_trigram(rowid, content) VALUES (?,?)",
            [(i, f"hermes message {i} content") for i in range(1, n_tg + 1)],
        )
    if with_src_view:
        # 跟 Hermes 真库同构: 源视图排除 tool/cron/subagent
        con.execute("""
            CREATE VIEW messages_fts_trigram_src AS
            SELECT m.id, m.role, m.content, m.tool_name
            FROM messages AS m JOIN sessions AS s ON s.id = m.session_id
            WHERE m.role <> 'tool' AND s.source NOT IN ('cron','subagent')
              AND json_extract(
                    (CASE WHEN json_valid(s.model_config) THEN s.model_config
                          ELSE json_object() END), '$._delegate_from') IS NULL
        """)
    con.commit()
    return con


@pytest.fixture
def healthy_db(tmp_path, monkeypatch):
    """索引完整的库 —— 应该全绿。"""
    db = tmp_path / "state.db"
    con = _make_db(db, n_messages=100)
    con.close()
    monkeypatch.setattr(selftest, "DB", str(db))
    return db


@pytest.fixture
def half_rebuilt_db(tmp_path, monkeypatch):
    """只重建了一半的库 —— 覆盖 42.8%, 跟真库出事时的形态一样。"""
    db = tmp_path / "state.db"
    con = _make_db(db, n_messages=100, index_rows=42)
    con.close()
    monkeypatch.setattr(selftest, "DB", str(db))
    return db


@pytest.fixture
def realistic_db(tmp_path, monkeypatch):
    """真库形态: 一半消息是 tool/cron, 按设计**不进** trigram 索引。

    这是旧判据 (trigram / messages) 会误判成坏库的��景 ——
    trigram 只有 42.8%, 但它相对源视图是 100% 完整。
    """
    db = tmp_path / "state.db"
    con = sqlite3.connect(str(db))
    con.executescript("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
            tool_name TEXT
        );
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT, model_config TEXT
        );
        CREATE VIRTUAL TABLE messages_fts USING fts5(content);
        CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(
            content, tokenize='trigram'
        );
        CREATE VIEW messages_fts_trigram_src AS
        SELECT m.id, m.role, m.content, m.tool_name
        FROM messages AS m JOIN sessions AS s ON s.id = m.session_id
        WHERE m.role <> 'tool' AND s.source NOT IN ('cron','subagent')
          AND json_extract(
                (CASE WHEN json_valid(s.model_config) THEN s.model_config
                      ELSE json_object() END), '$._delegate_from') IS NULL;
    """)
    con.execute("INSERT INTO sessions VALUES ('s_real', 'desktop', '{}')")
    con.execute("INSERT INTO sessions VALUES ('s_cron', 'cron', '{}')")
    # 42 条正常消息 -> 进 trigram
    con.executemany(
        "INSERT INTO messages VALUES (?, 's_real', 'user', ?, NULL)",
        [(i, f"hermes real message {i}") for i in range(1, 43)],
    )
    # 58 条 tool 消息 -> **不进** trigram (按设计)
    con.executemany(
        "INSERT INTO messages VALUES (?, 's_real', 'tool', ?, NULL)",
        [(100 + i, f"tool output {i}") for i in range(1, 59)],
    )
    con.execute("INSERT INTO messages_fts(rowid, content) SELECT id, content FROM messages")
    con.execute(
        "INSERT INTO messages_fts_trigram(rowid, content) "
        "SELECT id, content FROM messages_fts_trigram_src"
    )
    con.commit()
    n_tg = con.execute("SELECT COUNT(*) FROM messages_fts_trigram").fetchone()[0]
    n_msg = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    con.close()
    assert n_msg == 100 and n_tg == 42, "fixture 前提: 100 条消息, 42 条进 trigram"
    monkeypatch.setattr(selftest, "DB", str(db))
    return db


# ---------- 判据本身 ----------

def test_healthy_db_is_all_green(healthy_db):
    """索引完整 -> 全绿, rc=0。"""
    results, failures = selftest.check()
    assert not failures, f"健康的库不该有失败项: {failures}"
    assert all(r["ok"] for r in results)
    assert len(results) >= 7


def test_half_rebuilt_db_is_detected(half_rebuilt_db):
    """只重建一半 -> 必须判红, 且点名"trigram 覆盖率"。

    这是本测试最重要的一条: 重建到一半时, 已建的那部分照样能搜,
    只查"能不能搜"会漏掉它 —— 必须靠覆盖率抓。
    """
    results, failures = selftest.check()
    assert failures, "半重建的库必须被判红"

    by_name = {r["name"]: r for r in results}
    assert "trigram 覆盖率" in by_name
    assert not by_name["trigram 覆盖率"]["ok"], "覆盖率 42% 必须判失败"
    detail = by_name["trigram 覆盖率"]["detail"]
    assert "42.0%" in detail or "42" in detail, f"详情应显示 42%: {detail}"


def test_half_rebuilt_db_still_searchable_but_still_fails(half_rebuilt_db):
    """半重建时搜索是通的 —— 正是它容易骗过检查的原因。"""
    con = sqlite3.connect(str(half_rebuilt_db))
    n = con.execute(
        "SELECT COUNT(*) FROM messages_fts_trigram WHERE messages_fts_trigram MATCH ?",
        ("hermes",),
    ).fetchone()[0]
    con.close()
    assert n > 0, "前提: 半重建的库照样能搜到"
    # ...但整体仍判红
    _, failures = selftest.check()
    assert failures


def test_coverage_floor_is_sane():
    """覆盖率下限必须是 0.98 —— 低了会把半重建当健康。"""
    assert selftest.COVERAGE_FLOOR == 0.98


def test_realistic_db_with_tool_messages_is_green(realistic_db):
    """真库形态 (42% 消息是 tool, 按设计不进 trigram) 必须**判绿**。

    这是最重要的一条: trigram 只索引 42/100 = 42%,
    但它相对**源视图**是 100% 完整 —— 是好库, 不是坏库。

    旧判据 (trigram / messages) 会把它误判成坏库,
    然后去"修"一个根本没坏的索引 —— 那才是真的制造事故。
    """
    results, failures = selftest.check()
    assert not failures, (
        f"按设计排除 tool 消息的好库不该被判红: {failures}\n"
        f"  详情: {[(r['name'], r['detail']) for r in results]}"
    )
    by_name = {r["name"]: r for r in results}
    assert "trigram 覆盖率" in by_name
    assert by_name["trigram 覆盖率"]["ok"], "相对源视图应是 100%"
    assert "100.0%" in by_name["trigram 覆盖率"]["detail"]


def test_realistic_db_coverage_uses_src_view_not_total(realistic_db):
    """覆盖率的分母必须是源视图行数 (42), 不是 messages 总数 (100)。"""
    con = sqlite3.connect(str(realistic_db))
    expected = con.execute(
        "SELECT COUNT(*) FROM messages_fts_trigram_src").fetchone()[0]
    total = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    con.close()
    assert expected == 42 and total == 100

    results, _ = selftest.check()
    detail = {r["name"]: r["detail"] for r in results}["trigram 覆盖率"]
    assert "42" in detail, f"详情里应出现源视图行数 42: {detail}"
    assert "100" not in detail.split("=")[0].split("/")[-1], \
        f"分母不该是 messages 总数 100: {detail}"


def test_fallback_expression_works_without_view(tmp_path, monkeypatch):
    """源视图缺失时, 回退表达式必须算出同样的基准 (不能报错, 也不能算错)。"""
    db = tmp_path / "state.db"
    con = _make_db(db, n_messages=100, index_rows=100, with_src_view=False)
    con.close()
    monkeypatch.setattr(selftest, "DB", str(db))

    c = sqlite3.connect(str(db))
    via_fallback = selftest._trigram_expected_rows(c)
    c.close()
    assert via_fallback == 100, f"回退表达式应算出 100, 实际 {via_fallback}"

    results, failures = selftest.check()
    assert not failures, f"无视图但能算出基准时不该判红: {failures}"


def test_expected_rows_returns_none_when_uncomputable(monkeypatch):
    """既没视图又算不出来 -> 返回 None, 不能抛异常。"""
    con = sqlite3.connect(":memory:")
    assert selftest._trigram_expected_rows(con) is None
    con.close()


def test_search_term_is_ascii():
    """搜索抽查词必须是 ASCII。

    trigram 分词器对 2 字 CJK 永远 MATCH 不到; unicode61 把 CJK 切整段,
    命中与否取决于数据分布。拿中文当判据 = 把好库误判成坏库。
    """
    assert selftest.SEARCH_TERM.isascii()
    assert len(selftest.SEARCH_TERM) >= 3, "trigram 要求 >= 3 字符"


# ---------- 检测失败 vs 检测到坏 ----------

def test_missing_db_reports_failure_not_crash(tmp_path, monkeypatch):
    """库不存在 -> 报失败并返回非 0, 不能抛异常。"""
    monkeypatch.setattr(selftest, "DB", str(tmp_path / "nope.db"))
    monkeypatch.setattr(sys, "argv", ["selftest_fts_health.py"])
    assert selftest.main() == 1


def test_main_returns_zero_on_healthy(healthy_db, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["selftest_fts_health.py"])
    assert selftest.main() == 0


def test_main_returns_one_on_broken(half_rebuilt_db, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["selftest_fts_health.py"])
    assert selftest.main() == 1


def test_json_mode_emits_valid_json(healthy_db, monkeypatch, capsys):
    """--json 给 bat 用, 必须输出可解析的 JSON 且 ok=True。"""
    import json

    monkeypatch.setattr(sys, "argv", ["selftest_fts_health.py", "--json"])
    assert selftest.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["failures"] == []
    assert len(payload["results"]) >= 7


def test_json_mode_on_broken_has_failures(half_rebuilt_db, monkeypatch, capsys):
    import json

    monkeypatch.setattr(sys, "argv", ["selftest_fts_health.py", "--json"])
    assert selftest.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["failures"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
