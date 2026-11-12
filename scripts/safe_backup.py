"""怀恩 — state.db 安全备份 (SQLite backup API)

## 为什么不能用 `copy /Y`

**在 WAL 模式下 `copy state.db` 拷到的是撕裂快照。**

SQLite 的 WAL 模式下一个一致的库 = 主文件 + `-wal` + `-shm` 三件套。
`copy /Y` 只拷主文件:

- 主文件里是**最后一次 checkpoint 之前**的数据, `-wal` 里还有已提交但
  尚未回写的数据 —— 拷出来的备份**丢数据**。
- 拷贝过程中主文件可能正在被写 —— 拷出来的**页级撕裂**。

2026-10-30 19:23 的事故就是这么来的: `optimize_db_full.bat` 用
`copy /Y` 做备份, 19:22 那份 334 MB 的"完整版备份"自己就带着
`*** in database main ***` 撕裂错误, 后面 "还原" 它的时候把好库也带坏了。

官方文档也明说: "Do not delete `state.db-wal` or `state.db-shm`. The log
holds committed conversations that are not yet in `state.db`."

## 正确做法

用 SQLite 自己的 backup API (`sqlite3.Connection.backup`)。它:

- 通过 SQLite 的页锁协议拿到**一致快照**, 不用停任何进程
- 自动把 `-wal` 里的已提交数据一并写进备份
- 不会写出撕裂的页

## 用法

```bash
python scripts/safe_backup.py --dest <目标.db>
```

退出码: 0 = 备份成功且已校验, 1 = 失败。
"""
from __future__ import annotations

import argparse
import contextlib
import os
import sqlite3
import sys
from datetime import datetime

# cp936 控制台不能编码输出, 强制 UTF-8
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, ValueError):
        _stream.reconfigure(encoding="utf-8", errors="replace")

HD = os.environ.get("HERMES_HOME") or os.path.join(
    os.path.expanduser("~"), "AppData", "Local", "hermes")
SRC = os.path.join(HD, "state.db")


def _safe_count(con: sqlite3.Connection, table: str) -> int:
    """数表行数。表不存在返回 -1 (不是所有库都有 FTS 表)。"""
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.Error:
        return -1


def backup(src_path: str, dest_path: str) -> dict:
    """用 SQLite backup API 做一致快照。返回统计字典。"""
    if not os.path.exists(src_path):
        raise FileNotFoundError(f"源库不存在: {src_path}")

    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    # 目标必须不存在 —— sqlite backup API 要求目标为空库, 已有文件会报错
    if os.path.exists(dest_path):
        os.remove(dest_path)

    src = sqlite3.connect(src_path)
    try:
        dst = sqlite3.connect(dest_path)
        try:
            # pages=0 = 一次性全部拷 (在事务里做, 源不会被阻塞)
            src.backup(dst, pages=0)
            dst.commit()
            # 立刻跑一次完整性检查 —— 备份必须是可用的
            ic = dst.execute("PRAGMA integrity_check").fetchone()[0]
            n_msg = dst.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            n_sess = dst.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            # FTS 表是可选的 —— 不是每个库都有 (测试库/新建库就没有)。
            # 查不到不能算失败, 备份本身已经由 integrity_check 保证可用。
            n_tg = _safe_count(dst, "messages_fts_trigram")
        finally:
            dst.close()
    finally:
        src.close()

    return {
        "dest": dest_path,
        "size_bytes": os.path.getsize(dest_path),
        "integrity_check": ic,
        "messages": n_msg,
        "sessions": n_sess,
        "trigram_rows": n_tg,
        "ts": datetime.now().isoformat(timespec="seconds"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="state.db 安全备份 (SQLite backup API)")
    ap.add_argument("--dest", required=True, help="备份目标路径")
    ap.add_argument("--source", default=SRC, help="源库路径 (默认 HERMES_HOME/state.db)")
    args = ap.parse_args()

    try:
        info = backup(args.source, args.dest)
    except Exception as e:  # noqa: BLE001
        print(f"❌ 备份失败: {type(e).__name__}: {e}")
        return 1

    print(f"✅ 备份完成: {info['dest']}")
    print(f"   大小      : {info['size_bytes']:,} B")
    print(f"   完整性    : {info['integrity_check']}")
    print(f"   messages  : {info['messages']:,}")
    print(f"   sessions  : {info['sessions']:,}")
    print(f"   trigram   : {info['trigram_rows']:,}")
    print(f"   时间      : {info['ts']}")

    if info["integrity_check"] != "ok":
        print("\n❌ 备份自身完整性检查不通过 —— 这份备份不能用")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
