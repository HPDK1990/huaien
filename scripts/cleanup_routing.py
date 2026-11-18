r"""怀恩 — 清理 gateway_routing 里指向已删会话的悬空记录

## 为什么要清

Hermes 的 `gateway_routing` 表把"平台会话 (飞书/微信/TG 的 chat_id)" 映射到
本地的会话 id。如果那条本地会话被删了 (比如会话过期清理), routing 里的
`session_id` 就成了**悬空引用**。

后果: 平台消息进来时, mirror 落库会撞外键 —— 日志里表现为:

    Mirror failed ... FOREIGN KEY constraint failed

这会**直接影响飞书入站** (用户在飞书发消息, 怀恩收不到/存不下)。

## 做法

1. 先把整张表备份到 `logs/memory_backup/gateway_routing_备份-<时间戳>.json`
2. 删掉 `session_id` 在 `sessions` 表里找不到的行 (以及 JSON 解析不了的行)
3. **只删悬空, 不碰任何有效的** —— 有效的 routing 是用户和怀恩的会话映射

## 安全

- 备份在删之前完成, 删完可以原样恢复
- 只删"指向不存在会话"的行, 判定条件客观, 不含人工判断
- 不碰 `sessions` 表, 不碰消息, 不碰任何其他表

## 跑法

必须由**网关进程之外**的调用方执行 (网关内不能停自己):

    scripts\清理悬空routing.bat
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

HD = os.environ.get("HERMES_HOME") or str(Path.home() / "AppData" / "Local" / "hermes")
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(HD, "state.db")
BK_DIR = os.path.join(PROJ, "logs", "memory_backup")


def backup(rows: list[sqlite3.Row]) -> str:
    """整张表落盘, 返回备份路径。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(BK_DIR, f"gateway_routing_备份-{stamp}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"table": "gateway_routing", "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "total": len(rows), "rows": [dict(r) for r in rows]},
                  f, ensure_ascii=False, indent=1)
    return path


def dangling(rows: list[sqlite3.Row], sessions: set[str]) -> list[int]:
    """找出悬空的 rowid —— session_id 不在 sessions 里, 或者 JSON 根本解析不了。"""
    bad: list[int] = []
    for r in rows:
        try:
            sid = json.loads(r["entry_json"]).get("session_id")
        except (json.JSONDecodeError, TypeError, AttributeError):
            bad.append(r["rowid"])
            continue
        if not sid or sid not in sessions:
            bad.append(r["rowid"])
    return bad


def main() -> int:
    if not os.path.exists(DB):
        print("state.db 不存在, 跳过")
        return 1
    os.makedirs(BK_DIR, exist_ok=True)

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT rowid, entry_json FROM gateway_routing").fetchall()
        sessions = {r[0] for r in con.execute("SELECT id FROM sessions")}

        path = backup(rows)
        print(f"已备份 {len(rows)} 行 -> {path}")

        bad = dangling(rows, sessions)
        if not bad:
            print("没有悬空记录, 不用清")
            return 0
        print(f"sessions 表 {len(sessions)} 条, routing {len(rows)} 条, 悬空 {len(bad)} 条")

        ph = ",".join("?" * len(bad))
        con.execute(f"DELETE FROM gateway_routing WHERE rowid IN ({ph})", bad)
        con.commit()
        left = con.execute("SELECT COUNT(*) FROM gateway_routing").fetchone()[0]
    finally:
        con.close()

    print(f"已删 {len(bad)} 条, 剩 {left} 条 (备份可原样恢复)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
