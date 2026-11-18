"""怀恩 — 会话库优化脚本的单元测试

跑: python -m pytest tests/ -q   或   python tests/test_optimize_scripts.py

重点覆盖两件**危险**的事:
1. `clean_state_meta.py` 默认必须是 dry-run —— 不加 --apply 绝不能删任何东西
2. 它只删 `gateway.admission.result.v1.%` 这一类键, 别的键一个都不动

外加 `verify_optimize.py` 的判定逻辑:
- 检测失败 ("error") 必须跟检测到漂移 ("drift") 分开
- 硬判据 (条数/字符/FTS) 不通过 -> 必须判失败
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJ, "scripts"))

import clean_state_meta  # noqa: E402
import verify_optimize  # noqa: E402


@pytest.fixture
def state_db(tmp_path, monkeypatch):
    """造一个最小 state.db, 并让两个脚本都指向它。

    建库时特意放四类键:
      gateway.admission.result.v1.*  该删的
      fts_rebuild_high_water          索引重建进度标记, 不能删
      别的键                          不认识的键, 不能删
    """
    db = tmp_path / "state.db"
    con = sqlite3.connect(str(db))
    con.executescript("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
            content TEXT, active INTEGER DEFAULT 1, compacted INTEGER DEFAULT 0,
            _compressed_summary INTEGER DEFAULT 0
        );
        CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE sessions (id TEXT PRIMARY KEY);
        CREATE VIRTUAL TABLE messages_fts USING fts5(content);
    """)
    con.executemany(
        "INSERT INTO messages (id, session_id, role, content) VALUES (?, 's1', ?, ?)",
        [(1, "user", "怀恩测试"), (2, "assistant", "回复")],
    )
    # 用 ASCII 词测 FTS 命中 —— 连续 CJK 在默认 unicode61 分词器下是**一个**
    # token ("怀恩测试" 整段), 搜 "怀恩" 匹配不上。真库能匹配是因为它装了
    # cjk_unicode61 分词器 (切 bigram)。测试不该依赖那个分词扩展。
    con.execute("INSERT INTO messages_fts(rowid, content) VALUES (1, 'huatest token')")
    con.executemany(
        "INSERT INTO state_meta VALUES (?, ?)",
        [("gateway.admission.result.v1.abc", "x" * 500),
         ("gateway.admission.result.v1.def", "y" * 500),
         ("fts_rebuild_high_water", "12345"),
         ("别的键", "必须留着")],
    )
    con.commit()
    con.close()

    monkeypatch.setattr(clean_state_meta, "DB", str(db))
    monkeypatch.setattr(verify_optimize, "DB", str(db))
    return db


def _keys(db) -> list[str]:
    con = sqlite3.connect(str(db))
    keys = [r[0] for r in con.execute("SELECT key FROM state_meta")]
    con.close()
    return keys


# ---------- clean_state_meta ----------

def test_clean_is_dry_run_by_default(state_db, monkeypatch, capsys):
    """不加 --apply 绝不能删任何东西。"""
    monkeypatch.setattr(sys, "argv", ["clean_state_meta.py"])

    assert clean_state_meta.main() == 0
    left = [k for k in _keys(state_db) if k.startswith("gateway.admission.result")]
    assert len(left) == 2, "dry-run 不该删任何行"
    assert "dry-run" in capsys.readouterr().out


def test_clean_apply_only_touches_admission_keys(state_db, monkeypatch):
    """--apply 只删网关回执, 别的键必须原封不动。"""
    monkeypatch.setattr(sys, "argv", ["clean_state_meta.py", "--apply"])

    assert clean_state_meta.main() == 0
    keys = _keys(state_db)
    assert not [k for k in keys if k.startswith("gateway.admission.result")]
    assert "fts_rebuild_high_water" in keys, "重建进度标记不能删"
    assert "别的键" in keys, "不认识的键不能删"


def test_clean_is_idempotent(state_db, monkeypatch):
    """跑两遍不会出错, 第二遍无事可做。"""
    monkeypatch.setattr(sys, "argv", ["clean_state_meta.py", "--apply"])

    assert clean_state_meta.main() == 0
    assert clean_state_meta.main() == 0


def test_clean_pattern_is_narrow():
    """模式必须只匹配网关回执这一类, 不能是通配。"""
    assert clean_state_meta.PATTERN == "gateway.admission.result.v1.%"


# ---------- verify_optimize ----------

def test_drift_status_distinguishes_error_from_drift(monkeypatch):
    """检测失败必须返回 'error', 不能混成 'drift'。

    注意: 光 monkeypatch SRC 不够 ——
      1. hermes_state_fts 可能已经被别的测试导入进 sys.modules, import 命中缓存;
      2. verify_optimize 模块加载时把真实 hermes-agent 目录塞进了 sys.path,
         就算 SRC 被换掉, import 还是能从那儿找到。
    所以三样都得动: SRC、sys.modules、sys.path。
    """
    monkeypatch.setattr(verify_optimize, "SRC", "/nonexistent")
    monkeypatch.delitem(sys.modules, "hermes_state_fts", raising=False)
    monkeypatch.setattr(sys, "path",
                        [p for p in sys.path if "hermes-agent" not in p])

    status, found = verify_optimize.drift()
    assert status == "error", f"应该是检测失败, 实际 {status}: {found}"
    assert found and found[0]


def test_snapshot_writes_all_required_keys(state_db):
    snap = verify_optimize.snapshot()
    for key in ("size_mb", "messages", "chars", "summaries", "archived",
                "sessions", "trigram_mb"):
        assert key in snap, f"基线缺 {key}"
    assert snap["messages"] == 2
    assert snap["summaries"] == 0


def test_fts_hits_finds_inserted_row(state_db):
    assert verify_optimize.fts_hits("huatest") >= 1


def test_fts_hits_returns_zero_for_unknown_term(state_db):
    assert verify_optimize.fts_hits("zzzznotexist") == 0


def test_snapshot_mode_writes_json(state_db, tmp_path, monkeypatch):
    out = tmp_path / "before.json"
    monkeypatch.setattr(sys, "argv",
                        ["verify_optimize.py", "--snapshot", str(out)])

    assert verify_optimize.main() == 0
    with open(out, encoding="utf-8") as f:
        assert json.load(f)["messages"] == 2


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
