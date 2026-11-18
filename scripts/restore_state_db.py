r"""怀恩 — 从备份安全还原 state.db

## 为什么不能直接 `copy /Y` 还原

2026-10-30 19:24 的事故: 优化脚本 verify 失败后用
`copy /Y "%BAK%" "%HD%\state.db"` 还原, 而当时 gateway 已经在
STEP7 被重新拉起, 正在往 state.db 写。结果:

- 覆盖一个**正在被写**的 db -> 页级撕裂
  (`*** in database main *** / Tree 45 page 81169 cell 215:
  2nd reference to page 79607`)
- 好库被备份 (本身也是坏的) 一起带坏
- 桌面端所有会话 epoch 作废 -> 连续 20+ 次 `stale_epoch`, 看起来像"启动不了"

## 本脚本做的三件事

1. **确认没有 writer** —— 有进程持有 state.db 就拒绝还原
   (除非 `--force`, 但那需要你自己确认)
2. **用 SQLite backup API 写回** —— 不是文件拷贝, 走 SQLite 自己的页锁协议
3. **写完立刻校验** —— `PRAGMA integrity_check` 不 ok 就报告失败

## 用法

```bash
python scripts/restore_state_db.py --from <备份.db>
python scripts/restore_state_db.py --from <备份.db> --force   # 明知有 writer 也还原
```

退出码: 0 = 还原成功且已校验, 1 = 失败。
"""
from __future__ import annotations

import argparse
import contextlib
import json
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
DB = os.path.join(HD, "state.db")


def _safe_count(con: sqlite3.Connection, table: str) -> int:
    """数表行数。表不存在返回 -1。"""
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.Error:
        return -1


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def find_writers() -> list[dict]:
    """找持有 state.db 的进程。"""
    writers: list[dict] = []
    try:
        import psutil  # noqa: PLC0415
    except ImportError:
        gs = _read_json(os.path.join(HD, "gateway_state.json"))
        if gs and gs.get("pid"):
            writers.append({"pid": gs.get("pid"), "how": "gateway_state.json"})
        return writers

    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmd = " ".join(proc.info.get("cmdline") or [])
            name = (proc.info.get("name") or "").lower()
        except Exception:  # noqa: BLE001, S110
            continue
        if "state.db" in cmd:
            writers.append({"pid": proc.info["pid"], "how": "cmdline"})
        elif "hermes_cli.main" in cmd and "gateway" in cmd:
            writers.append({"pid": proc.info["pid"], "how": "gateway"})
        elif name in ("hermes.exe", "hermes"):
            writers.append({"pid": proc.info["pid"], "how": "desktop"})
    return writers


def restore(backup_path: str, dest_path: str) -> dict:
    """用 SQLite backup API 把备份写回 dest_path。"""
    if not os.path.exists(backup_path):
        raise FileNotFoundError(f"备份不存在: {backup_path}")

    # 先验备份本身 —— 坏备份不能往好库里写
    b = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    try:
        b_ic = b.execute("PRAGMA integrity_check").fetchone()[0]
        b_msg = _safe_count(b, "messages")
    finally:
        b.close()
    if b_ic != "ok":
        raise ValueError(f"备份自身完整性不通过 ({b_ic}), 拒绝还原")

    # 目标必须不存在 —— backup API 要求目标是空库
    for suffix in ("-wal", "-shm"):
        side = dest_path + suffix
        if os.path.exists(side):
            os.remove(side)
    if os.path.exists(dest_path):
        os.remove(dest_path)

    src = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(dest_path)
        try:
            src.backup(dst, pages=0)
            dst.commit()
            d_ic = dst.execute("PRAGMA integrity_check").fetchone()[0]
            d_msg = _safe_count(dst, "messages")
        finally:
            dst.close()
    finally:
        src.close()

    return {
        "dest": dest_path,
        "backup": backup_path,
        "backup_integrity": b_ic,
        "backup_messages": b_msg,
        "dest_integrity": d_ic,
        "dest_messages": d_msg,
        "size_bytes": os.path.getsize(dest_path),
        "ts": datetime.now().isoformat(timespec="seconds"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="从备份安全还原 state.db")
    ap.add_argument("--from", dest="src", required=True, help="备份文件路径")
    ap.add_argument("--dest", default=DB, help="目标 (默认 HERMES_HOME/state.db)")
    ap.add_argument("--force", action="store_true",
                    help="明知有 writer 也还原 (危险)")
    args = ap.parse_args()

    writers = find_writers()
    if writers and not args.force:
        print("⛔ 拒绝还原: 有进程持有 state.db, 覆盖它会造成撕裂")
        for w in writers:
            print(f"   - PID {w['pid']} ({w['how']})")
        print("\n先停掉这些进程, 或加 --force 强行还原。")
        return 1

    if writers:
        print(f"⚠️ --force: 忽略 {len(writers)} 个持库进程, 强行还原")
        for w in writers:
            print(f"   - PID {w['pid']} ({w['how']})")

    try:
        info = restore(args.src, args.dest)
    except Exception as e:  # noqa: BLE001
        print(f"❌ 还原失败: {type(e).__name__}: {e}")
        return 1

    print(f"✅ 还原完成: {info['dest']}")
    print(f"   备份完整性  : {info['backup_integrity']}")
    print(f"   还原后完整性: {info['dest_integrity']}")
    print(f"   messages    : {info['dest_messages']:,} (备份里 {info['backup_messages']:,})")
    print(f"   大小        : {info['size_bytes']:,} B")
    print(f"   时间        : {info['ts']}")

    if info["dest_integrity"] != "ok":
        print("\n❌ 还原后完整性检查不通过")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
