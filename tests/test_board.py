"""huaien/board.py 的单测 —— 秘书台数据源。"""
import importlib.util
import os
import sqlite3
import sys

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)


def _load():
    spec = importlib.util.spec_from_file_location(
        "board_mod", os.path.join(PROJ, "huaien", "board.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_db(tmp_path):
    db = tmp_path / "kanban.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE tasks (id TEXT, title TEXT, status TEXT,
                   assignee TEXT, priority TEXT, created_by TEXT, created_at TEXT,
                   started_at TEXT, completed_at TEXT, last_failure_error TEXT)""")
    con.execute("CREATE TABLE task_links (parent_id TEXT, child_id TEXT)")
    con.execute("""CREATE TABLE task_runs (task_id TEXT, profile TEXT, status TEXT,
                   last_heartbeat_at TEXT, started_at TEXT)""")
    return con, db


def test_splits_into_three_sections(monkeypatch, tmp_path):
    """待你处理 / 进行中 / 已完成 三段要分对。"""
    mod = _load()
    con, db = _make_db(tmp_path)
    con.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)", [
        ("t_a", "等人拍板", "blocked", "default", "0", "user", "1", None, None, None),
        ("t_b", "正在干", "running", "coder", "0", "user", "2", "2", None, None),
        ("t_c", "干完了", "done", "auditor", "0", "user", "3", "3", "3", None),
        ("t_d", "归档的", "archived", "default", "0", "user", "4", None, None, None),
    ])
    con.commit()
    con.close()
    monkeypatch.setattr(mod, "KANBAN_DB", str(db))
    b = mod.board()
    assert [c["id"] for c in b["needs_you"]] == ["t_a"]
    assert [c["id"] for c in b["in_flight"]] == ["t_b"]
    assert b["done_total"] == 1
    # 归档的既不算待办, 也不算总数
    assert b["total"] == 3


def test_archived_is_excluded(monkeypatch, tmp_path):
    mod = _load()
    con, db = _make_db(tmp_path)
    con.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("t_x", "老的", "archived", "default", "0", "user", "9", None, None, None))
    con.commit()
    con.close()
    monkeypatch.setattr(mod, "KANBAN_DB", str(db))
    b = mod.board()
    assert b["total"] == 0
    assert b["needs_you"] == [] and b["in_flight"] == [] and b["done_total"] == 0


def test_graph_layers_and_edges(monkeypatch, tmp_path):
    """依赖图要算出拓扑层级: 根=0, 下一层=1。"""
    mod = _load()
    con, db = _make_db(tmp_path)
    con.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)", [
        ("t_1", "调研", "done", "researcher", "0", "u", "1", "1", "1", None),
        ("t_2", "实现", "done", "coder", "0", "u", "2", "2", "2", None),
        ("t_3", "验收", "done", "auditor", "0", "u", "3", "3", "3", None),
    ])
    con.executemany("INSERT INTO task_links VALUES (?,?)",
                    [("t_1", "t_3"), ("t_2", "t_3")])
    con.commit()
    con.close()
    monkeypatch.setattr(mod, "KANBAN_DB", str(db))
    g = mod.board()["graph"]
    assert len(g["nodes"]) == 3
    assert len(g["edges"]) == 2
    lay = {n["id"]: n["layer"] for n in g["nodes"]}
    assert lay["t_1"] == 0 and lay["t_2"] == 0
    assert lay["t_3"] == 1


def test_agents_load_counts(monkeypatch, tmp_path):
    """agent 负载: 每个 assignee 手上有几件。"""
    mod = _load()
    con, db = _make_db(tmp_path)
    con.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)", [
        ("t_a", "x", "running", "coder", "0", "u", "1", "1", None, None),
        ("t_b", "y", "running", "coder", "0", "u", "2", "2", None, None),
        ("t_c", "z", "ready", "researcher", "0", "u", "3", None, None, None),
    ])
    con.commit()
    con.close()
    monkeypatch.setattr(mod, "KANBAN_DB", str(db))
    agents = mod.board()["agents"]
    assert agents[0] == {"name": "coder", "count": 2}
    assert agents[1] == {"name": "researcher", "count": 1}


def test_missing_db_degrades_gracefully(monkeypatch, tmp_path):
    """库不存在不能崩, 要返回 available=False。"""
    mod = _load()
    monkeypatch.setattr(mod, "KANBAN_DB", str(tmp_path / "nope.db"))
    b = mod.board()
    assert b["available"] is False
    assert b["total"] == 0
    assert "看板" in mod.summary_text()


def test_summary_text_mentions_counts(monkeypatch, tmp_path):
    mod = _load()
    con, db = _make_db(tmp_path)
    con.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("t_a", "等人", "blocked", "default", "0", "u", "1", None, None, None))
    con.commit()
    con.close()
    monkeypatch.setattr(mod, "KANBAN_DB", str(db))
    s = mod.summary_text()
    assert "待你处理 1 件" in s
    assert "等人拍板" in s or "等人" in s


def test_timestamp_sort_is_numeric(monkeypatch, tmp_path):
    """时间戳按数值排, 不是字典序。

    '999' 在字典序里大于 '1000', 但实际是更早 —— 必须转 int 才排对。
    """
    mod = _load()
    con, db = _make_db(tmp_path)
    con.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)", [
        ("t_early", "早的", "blocked", "default", "0", "u", "999", None, None, None),
        ("t_late", "晚的", "blocked", "default", "0", "u", "1000", None, None, None),
    ])
    con.commit()
    con.close()
    monkeypatch.setattr(mod, "KANBAN_DB", str(db))
    b = mod.board()
    # 新的在前 -> t_late 应该在 t_early 前面
    assert [c["id"] for c in b["needs_you"]] == ["t_late", "t_early"]


def _seed(con):
    con.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)", [
        ("t_a", "等人拍板", "blocked", "default", "0", "user", "1", None, None, "等用户点头"),
        ("t_b", "正在干", "running", "coder", "0", "user", "2", "2", None, None),
        ("t_c", "干完了", "done", "auditor", "0", "user", "3", "3", "3", None),
    ])
    con.execute("INSERT INTO task_runs VALUES ('t_b','coder','running','2','2')")
    con.commit()


def _patch_db(monkeypatch, tmp_path, mod):
    con, db = _make_db(tmp_path)
    _seed(con)
    con.close()
    # 和现有测试一致: 直接换库路径, _connect 自己会设 row_factory
    monkeypatch.setattr(mod, "KANBAN_DB", str(db))
    return db


def test_quick_text_all_keys(monkeypatch, tmp_path):
    """4 个快捷指令都要有话说, 不能返回空。"""
    mod = _load()
    _patch_db(monkeypatch, tmp_path, mod)
    for key in mod.QUICK_KEYS:
        txt = mod.quick_text(key)
        assert txt and txt.strip(), f"{key} 返回空"
        assert "暂时读不到" not in txt, f"{key} 说读不到看板"


def test_quick_text_todo_lists_blocked_with_reason(monkeypatch, tmp_path):
    """待办清单要包含卡住的卡和卡住的原因 —— 用户要知道为什么卡。"""
    mod = _load()
    _patch_db(monkeypatch, tmp_path, mod)
    txt = mod.quick_text("todo")
    assert "等人拍板" in txt
    assert "等用户点头" in txt


def test_quick_text_busy_shows_agent_and_task(monkeypatch, tmp_path):
    """谁在忙 = 哪个 agent + 手上什么活。"""
    mod = _load()
    _patch_db(monkeypatch, tmp_path, mod)
    txt = mod.quick_text("busy")
    assert "coder" in txt
    assert "正在干" in txt


def test_quick_text_unknown_key(monkeypatch, tmp_path):
    """不认识的 key 要有话说, 不能崩。"""
    mod = _load()
    _patch_db(monkeypatch, tmp_path, mod)
    assert "不认识" in mod.quick_text("nope")
