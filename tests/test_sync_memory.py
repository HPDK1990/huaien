"""前后台记忆双向同步 —— 单元测试

验证三件事:
  1. 条目级补齐能把对方的条目搬过来
  2. 只补不删 (绝不丢信息)
  3. 幂等 (反复跑不产生变化)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import sync_memory as sm


def _entries(path):
    return sm.read_entries(path)


def test_split_entries_drops_blank():
    assert sm.split_entries("a\n\n§\n\nb") == ["a", "b"]
    assert sm.split_entries("") == []
    assert sm.split_entries("§") == []


def test_join_roundtrip():
    items = ["第一条", "第二条", "第三条"]
    assert sm.split_entries(sm.join_entries(items)) == items


def test_sync_moves_missing_both_ways(tmp_path):
    back = tmp_path / "back" / "MEMORY.md"
    front = tmp_path / "front" / "MEMORY.md"
    back.parent.mkdir(parents=True)
    front.parent.mkdir(parents=True)

    back.write_text(sm.join_entries(["后台独有"]), encoding="utf-8")
    front.write_text(sm.join_entries(["前台独有"]), encoding="utf-8")

    nb, nf = sm.sync_pair(back, front)

    assert nb == 1 and nf == 1
    assert "前台独有" in back.read_text(encoding="utf-8")
    assert "后台独有" in front.read_text(encoding="utf-8")


def test_sync_never_deletes(tmp_path):
    back = tmp_path / "b.md"
    front = tmp_path / "f.md"
    back.write_text(sm.join_entries(["保留一", "保留二"]), encoding="utf-8")
    front.write_text(sm.join_entries(["新增"]), encoding="utf-8")

    sm.sync_pair(back, front)

    for e in ("保留一", "保留二"):
        assert e in back.read_text(encoding="utf-8")


def test_sync_is_idempotent(tmp_path):
    back = tmp_path / "b.md"
    front = tmp_path / "f.md"
    back.write_text(sm.join_entries(["x"]), encoding="utf-8")
    front.write_text(sm.join_entries(["y"]), encoding="utf-8")

    sm.sync_pair(back, front)
    snap = (back.read_text(encoding="utf-8"), front.read_text(encoding="utf-8"))
    for _ in range(3):
        sm.sync_pair(back, front)
    assert (back.read_text(encoding="utf-8"), front.read_text(encoding="utf-8")) == snap


def test_missing_file_is_created(tmp_path):
    back = tmp_path / "b.md"
    front = tmp_path / "f.md"
    front.write_text(sm.join_entries(["来自前台"]), encoding="utf-8")

    nb, nf = sm.sync_pair(back, front)

    assert nb == 1
    assert back.exists()
    assert "来自前台" in back.read_text(encoding="utf-8")
