"""怀恩 — 安全备份 / 还原 / 前置门禁的单元测试

跑: python -m pytest tests/ -q   或   python tests/test_safe_db_ops.py

覆盖三件**危险**的事:
1. `safe_backup.py` 必须用 SQLite backup API —— 产出一致快照, 不能是撕裂副本
2. `restore_state_db.py` 在有 writer 时必须**拒绝**还原 (覆盖活库 = 撕裂)
3. `preflight.py` 在有活会话时必须**拦住** (这是 19:23 事故的直接防线)
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJ, "scripts"))

import preflight  # noqa: E402
import restore_state_db  # noqa: E402
import safe_backup  # noqa: E402


def _make_db(path, n_messages: int = 50) -> None:
    con = sqlite3.connect(str(path))
    con.executescript("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT
        );
        CREATE TABLE sessions (id TEXT PRIMARY KEY);
        CREATE TABLE session_turn_leases (
            conversation_id TEXT, holder TEXT,
            acquired_at REAL, expires_at REAL
        );
    """)
    con.execute("INSERT INTO sessions VALUES ('s1')")
    con.executemany(
        "INSERT INTO messages VALUES (?, 's1', 'user', ?)",
        [(i, f"message {i}") for i in range(1, n_messages + 1)],
    )
    con.commit()
    con.close()


@pytest.fixture
def src_db(tmp_path):
    db = tmp_path / "state.db"
    _make_db(db, n_messages=50)
    return db


# ---------- safe_backup ----------

def test_backup_produces_readable_copy(src_db, tmp_path):
    """备份必须是能独立打开的库, 行数一致。"""
    dest = tmp_path / "bak.db"
    info = safe_backup.backup(str(src_db), str(dest))

    assert info["integrity_check"] == "ok"
    assert info["messages"] == 50
    assert dest.exists()

    con = sqlite3.connect(str(dest))
    assert con.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 50
    con.close()


def test_backup_overwrites_existing_dest(src_db, tmp_path):
    """目标已存在时不能报错 —— 必须先删再写。"""
    dest = tmp_path / "bak.db"
    dest.write_bytes(b"stale garbage")
    info = safe_backup.backup(str(src_db), str(dest))
    assert info["integrity_check"] == "ok"


def test_backup_is_consistent_under_wal(tmp_path):
    """**关键**: WAL 模式下 backup 必须带上 wal 里的数据。

    2026-10-30 19:23 的事故就是 `copy /Y` 只拷主文件, 丢了 WAL 里的数据。
    这个测试模拟那个场景: 写进 WAL (不 checkpoint) 的行, 备份后必须还在。
    """
    db = tmp_path / "state.db"
    con = sqlite3.connect(str(db))
    con.execute("PRAGMA journal_mode=WAL")
    _make_db(db, n_messages=10)
    # 关掉连接, 重新打开写, 让数据留在 WAL 里
    con.execute("PRAGMA wal_checkpoint(PASSIVE)")
    con.commit()
    con.close()

    # 单独写一批数据到 WAL
    con2 = sqlite3.connect(str(db))
    con2.execute("PRAGMA journal_mode=WAL")
    con2.execute("PRAGMA wal_autocheckpoint=0")   # 阻止自动 checkpoint
    con2.executemany(
        "INSERT INTO messages VALUES (?, 's1', 'user', ?)",
        [(100 + i, f"wal only {i}") for i in range(1, 21)],
    )
    con2.commit()
    n_wal = con2.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    wal_size = os.path.getsize(str(db) + "-wal") if os.path.exists(str(db) + "-wal") else 0
    con2.close()

    assert wal_size > 0, "前提: 数据确实在 WAL 里"

    dest = tmp_path / "bak.db"
    info = safe_backup.backup(str(db), str(dest))
    assert info["messages"] == n_wal, (
        f"备份丢了 WAL 里的数据: 备份 {info['messages']} 行, 源库 {n_wal} 行"
    )


def test_backup_main_rejects_corrupt_result(src_db, tmp_path, monkeypatch):
    """main() 必须校验备份完整性 —— 完整性不过就该 rc=1。

    变异测试发现: 只测 backup() 的话, "main 里不校验" 这个缺陷抓不到。
    """
    dest = tmp_path / "bak.db"

    # 先正常备份, 然后把返回值里的 integrity_check 改成坏的, 看 main 认不认
    real_backup = safe_backup.backup

    def fake_backup(src, dst):
        info = real_backup(src, dst)
        info["integrity_check"] = "*** in database main ***"
        return info

    monkeypatch.setattr(safe_backup, "backup", fake_backup)
    monkeypatch.setattr(sys, "argv",
                        ["safe_backup.py", "--dest", str(dest)])
    assert safe_backup.main() == 1


def test_backup_main_returns_zero_on_good_backup(src_db, tmp_path, monkeypatch):
    dest = tmp_path / "bak.db"
    monkeypatch.setattr(sys, "argv",
                        ["safe_backup.py", "--dest", str(dest)])
    assert safe_backup.main() == 0


def test_backup_missing_source(tmp_path):
    """源不存在 -> 抛 FileNotFoundError, 不是静默成功。"""
    with pytest.raises(FileNotFoundError):
        safe_backup.backup(str(tmp_path / "nope.db"), str(tmp_path / "out.db"))


# ---------- restore_state_db ----------

def test_restore_refuses_when_writers_present(src_db, tmp_path, monkeypatch):
    """**最关键的一条**: 有 writer 时必须拒绝还原。

    覆盖活库就是撕裂 —— 19:23 事故的直接原因。
    """
    bak = tmp_path / "bak.db"
    safe_backup.backup(str(src_db), str(bak))
    dest = tmp_path / "restored.db"

    monkeypatch.setattr(
        restore_state_db, "find_writers",
        lambda: [{"pid": 999, "how": "gateway"}])
    monkeypatch.setattr(sys, "argv",
                        ["restore_state_db.py", "--from", str(bak), "--dest", str(dest)])

    assert restore_state_db.main() == 1
    assert not dest.exists(), "拒绝时不该写出任何文件"


def test_restore_proceeds_when_no_writers(src_db, tmp_path, monkeypatch):
    """没有 writer -> 正常还原, 且写完的库完整。"""
    bak = tmp_path / "bak.db"
    safe_backup.backup(str(src_db), str(bak))
    dest = tmp_path / "restored.db"

    monkeypatch.setattr(restore_state_db, "find_writers", lambda: [])
    monkeypatch.setattr(sys, "argv",
                        ["restore_state_db.py", "--from", str(bak), "--dest", str(dest)])

    assert restore_state_db.main() == 0
    con = sqlite3.connect(str(dest))
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert con.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 50
    con.close()


def test_restore_refuses_corrupt_backup(src_db, tmp_path, monkeypatch):
    """备份自身是坏的 -> 必须拒绝还原 (别把坏东西写进好库)。"""
    bak = tmp_path / "bad.db"
    _make_db(bak, n_messages=5)
    # 制造一个非 db 文件
    bak.write_bytes(b"this is not a sqlite database at all")

    dest = tmp_path / "restored.db"
    monkeypatch.setattr(restore_state_db, "find_writers", lambda: [])
    monkeypatch.setattr(sys, "argv",
                        ["restore_state_db.py", "--from", str(bak), "--dest", str(dest)])

    assert restore_state_db.main() == 1
    assert not dest.exists()


def test_restore_missing_backup(tmp_path, monkeypatch):
    dest = tmp_path / "restored.db"
    monkeypatch.setattr(restore_state_db, "find_writers", lambda: [])
    monkeypatch.setattr(sys, "argv",
                        ["restore_state_db.py", "--from", str(tmp_path / "nope.db"),
                         "--dest", str(dest)])
    assert restore_state_db.main() == 1


# ---------- preflight ----------

def _write_lease(db, conversation_id, holder, expires_in_s=300):
    """往测试库的 session_turn_leases 写一条租约。"""
    import time
    con = sqlite3.connect(str(db))
    now = time.time()
    con.execute(
        "INSERT INTO session_turn_leases VALUES (?,?,?,?)",
        (conversation_id, holder, now, now + expires_in_s),
    )
    con.commit()
    con.close()


def test_preflight_blocks_on_live_session(tmp_path, monkeypatch):
    """**19:23 事故的直接防线**: 有活会话时必须拦住。

    那次脚本自己打印了 "2 live session(s) would be interrupted" 然后照杀。

    注意数据源是 state.db 的 `session_turn_leases` —— 不是
    `runtime/active_sessions.json` (后者看不见 gateway 内部的活 turn)。
    """
    db = tmp_path / "state.db"
    _make_db(db, n_messages=10)
    _write_lease(db, "20261009_151342_87a316",
                 "pid=5484:turn=20261009_151342_87a316:abc")
    monkeypatch.setattr(preflight, "DB", str(db))

    has_live, live = preflight.check_live_sessions()
    assert has_live, "有租约时必须报活会话"
    assert len(live) == 1
    assert live[0]["pid"] == 5484
    assert live[0]["conversation_id"] == "20261009_151342_87a316"


def test_preflight_main_returns_1_when_lease_exists(tmp_path, monkeypatch):
    """**直接断言 main() 在有租约时返回 1**。

    这是唯一能抓住"完全不拦"这个变异的断言。
    之前只测 check_live_sessions() 返回值, 变异掉 main() 里的拦截逻辑
    照样全绿 —— 测试没有覆盖到真正的决策点。
    """
    db = tmp_path / "state.db"
    _make_db(db, n_messages=10)
    _write_lease(db, "s1", "pid=5484:turn=s1:abc")
    monkeypatch.setattr(preflight, "DB", str(db))
    monkeypatch.setattr(preflight, "check_writers", lambda: (False, []))
    monkeypatch.setattr(sys, "argv", ["preflight.py"])

    assert preflight.main() == 1, "有活会话时 main() 必须返回 1 (拦截)"


def test_preflight_main_returns_0_when_no_lease(tmp_path, monkeypatch):
    """没有租约 -> main() 返回 0 (放行)。跟上面那条成对。"""
    db = tmp_path / "state.db"
    _make_db(db, n_messages=10)
    monkeypatch.setattr(preflight, "DB", str(db))
    monkeypatch.setattr(preflight, "check_writers", lambda: (False, []))
    monkeypatch.setattr(sys, "argv", ["preflight.py"])

    assert preflight.main() == 0


def test_preflight_ignores_expired_lease(tmp_path, monkeypatch):
    """过期租约不算活会话 (否则永远拦着不让动)。"""
    db = tmp_path / "state.db"
    _make_db(db, n_messages=10)
    _write_lease(db, "old_turn", "pid=1234:turn=old", expires_in_s=-60)
    monkeypatch.setattr(preflight, "DB", str(db))

    has_live, live = preflight.check_live_sessions()
    assert not has_live
    assert live == []


def test_preflight_passes_without_lease(tmp_path, monkeypatch):
    """没有租约 -> 门禁放行 (但库仍须完好)。"""
    db = tmp_path / "state.db"
    _make_db(db, n_messages=10)
    monkeypatch.setattr(preflight, "DB", str(db))
    monkeypatch.setattr(preflight, "check_writers", lambda: (False, []))
    monkeypatch.setattr(sys, "argv", ["preflight.py"])

    assert preflight.main() == 0


def test_preflight_blocks_on_broken_db(tmp_path, monkeypatch):
    """库坏了 -> 门禁必须拦 (在坏库上操作只会更坏)。"""
    db = tmp_path / "state.db"
    db.write_bytes(b"not a database")
    monkeypatch.setattr(preflight, "DB", str(db))
    monkeypatch.setattr(preflight, "check_writers", lambda: (False, []))
    monkeypatch.setattr(sys, "argv", ["preflight.py"])

    assert preflight.main() == 1


def test_preflight_ignores_runtime_json(tmp_path, monkeypatch):
    """active_sessions.json 里的条目**不算**活会话信号。

    它只登记桌面端起的会话, 看不见 gateway 内部的活 turn ——
    19:23 被砍断的正是后者。靠它判断 = 门禁形同虚设。
    """
    db = tmp_path / "state.db"
    _make_db(db, n_messages=10)
    monkeypatch.setattr(preflight, "DB", str(db))
    runtime = tmp_path / "active_sessions.json"
    runtime.write_text(json.dumps({
        "entries": [{
            "pid": 5484, "surface": "gateway:local", "session_id": "s1",
            "metadata": {"live_session_id": "agent:main:local:dm:abc"},
        }]
    }), encoding="utf-8")
    monkeypatch.setattr(preflight, "RUNTIME", str(runtime))

    has_live, live = preflight.check_live_sessions()
    assert not has_live, "不能拿 active_sessions.json 当活会话依据"
    assert live == []


def test_preflight_missing_db_is_safe(tmp_path, monkeypatch):
    """库不存在时不能崩 —— 读不到租约就当没有活会话, 但库检查会拦。"""
    monkeypatch.setattr(preflight, "DB", str(tmp_path / "nope.db"))
    has_live, live = preflight.check_live_sessions()
    assert not has_live
    assert live == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
