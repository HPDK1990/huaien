"""怀恩 — 会话库体检 (只读)

回答用户 2026-10-30 的问题: **会话库到底有没有变慢?**

实测结论:
- 查询本身**很快** (FTS 索引查询 0.3ms, 全表计数 2.2ms)
- 真正会变慢的是**上下文长度** —— 每次续接都要把历史装进模型上下文
- 但**磁盘占用**会一直涨 (现在是 314 MB)

所以盯三件事: 文件多大 / 查询多快 (真测不猜) / 索引有没有冗余。
超过阈值就在回归看门狗里报出来。**纯只读, 不动任何数据。"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

HD = os.environ.get("HERMES_HOME") or str(Path.home() / "AppData" / "Local" / "hermes")
DB = os.path.join(HD, "state.db")

WARN_MB = 500          # 文件大小阈值
WARN_LIKE_MS = 800     # 最坏情况全文扫耗时阈值


def main() -> int:
    if not os.path.exists(DB):
        print("state.db 不存在")
        return 0

    size_mb = os.path.getsize(DB) / 1024 / 1024
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)   # 只读
    try:
        def timed(sql, args=()):
            t0 = time.time()
            con.execute(sql, args).fetchall()
            return (time.time() - t0) * 1000

        n_msg = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        n_ses = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        chars = con.execute("SELECT COALESCE(SUM(LENGTH(content)),0) FROM messages").fetchone()[0]

        t_count = timed("SELECT COUNT(*) FROM messages")
        t_fts = timed("SELECT m.id FROM messages_fts f JOIN messages m ON m.id=f.rowid "
                      "WHERE messages_fts MATCH ? LIMIT 20", ("怀恩",))
        t_like = timed("SELECT * FROM messages WHERE content LIKE ? LIMIT 20", ("%怀恩%",))

        try:
            row = con.execute("SELECT sql FROM sqlite_master WHERE name='messages_fts'").fetchone()
            # content 指向具名表 = external-content (新布局)。
            # 旧判定找 "content=''" 会把新布局误报成旧布局, 白建议 optimize-storage。
            external = bool(row and re.search(r"content\s*=\s*'[^']+'", row[0] or ""))
        except sqlite3.Error:
            external = None
    finally:
        con.close()

    out = {"size_mb": round(size_mb, 1), "messages": n_msg, "sessions": n_ses,
           "content_chars": chars, "ms_count_all": round(t_count, 1),
           "ms_fts_search": round(t_fts, 1), "ms_like_scan": round(t_like, 1),
           "fts_external_content": external}
    print(json.dumps(out, ensure_ascii=False))

    warn = []
    if size_mb > WARN_MB:
        warn.append(f"库 {size_mb:.0f} MB 超过 {WARN_MB} MB")
    if t_like > WARN_LIKE_MS:
        warn.append(f"全文扫 {t_like:.0f} ms 超过 {WARN_LIKE_MS} ms")
    if external is False:
        warn.append("FTS 还是旧布局(自己存了一份消息副本) —— optimize-storage 能省不少空间")
    if warn:
        print("WARN: " + "; ".join(warn))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
