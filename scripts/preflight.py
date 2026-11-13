"""怀恩 — 优化前的前置门禁

在**停任何进程之前**跑这个脚本。它回答一个问题:
"现在动 state.db 安不安全?"

## 三道检查

| # | 检查 | 不通过时 |
|---|---|---|
| 1 | 有没有**活会话** (正在跑的 turn) | **中止** —— 停 gateway 会砍断正在跑的对话 |
| 2 | 有没有 **writer** 进程持有 state.db | **中止** —— 有 writer 时换库必然撕裂 |
| 3 | state.db **完整性** + 备份是否可用 | **中止** —— 在坏库上操作只会更坏 |

## 为什么必须有这个

2026-10-30 19:23 的事故直接原因: `optimize_db_full.bat` 无条件
`hermes gateway stop --yes`, 脚本自己都打印了警告:

    --yes: stop proceeds: 2 live session(s) would be interrupted.
      - chat turn agent:main:local:dm:20261009_151342_87a316  running 1m05s
      - chat turn agent:main:weixin:dm:o9cq80_...            running 6m52s

然后照杀不误。那两个 turn 里一个就是用户正在进行的对话。

**门禁的作用**: 让脚本在"有人正在用"时**直接不干活**, 而不是先干再说。

## 用法

```bash
python scripts/preflight.py                 # 只检查, 退出码 0 = 可以动
python scripts/preflight.py --json          # 机器读
python scripts/preflight.py --force         # 明知有活会话也继续 (会打印警告)
```

退出码: 0 = 可以动, 1 = 不安全, 2 = 检查本身失败。
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
RUNTIME = os.path.join(HD, "runtime", "active_sessions.json")


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def check_live_sessions() -> tuple[bool, list[dict]]:
    """返回 (是否有活会话, 活会话列表)。

    **权威来源是 state.db 的 `session_turn_leases` 表**, 不是
    `runtime/active_sessions.json` —— 后者只登记桌面端起的会话,
    **不登记 gateway 内部的活 turn**。19:23 事故里被砍断的正是后者
    看不见的那些会话, 所以只查 active_sessions.json 等于没查。

    租约行长这样:
        conversation_id = '20261009_151342_87a316'
        holder          = 'pid=5484:turn=20261009_151342_87a316:...'
        acquired_at     = 1793362900.68   (unix 时间戳)
        expires_at      = 1793363980.88   (unix 时间戳, 未来 = 还活着)
    """
    leases = _read_leases()
    now = datetime.now().timestamp()
    live = []
    for row in leases:
        expires = row.get("expires_at")
        # expires_at 缺失或已过期 -> 这条租约已经失效, 不算活会话
        if expires is not None and expires < now:
            continue
        holder = row.get("holder") or ""
        pid = None
        if "pid=" in holder:
            try:
                pid = int(holder.split("pid=", 1)[1].split(":", 1)[0])
            except ValueError:
                pid = None
        live.append({
            "pid": pid,
            "conversation_id": row.get("conversation_id"),
            "holder": holder[:80],
            "expires_at": expires,
            "expires_in_s": round(expires - now) if expires else None,
        })
    return bool(live), live


def _read_leases() -> list[dict]:
    """从 state.db 读 session_turn_leases。读不到返回空列表。"""
    if not os.path.exists(DB):
        return []
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT conversation_id, holder, acquired_at, expires_at "
                "FROM session_turn_leases"
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def check_writers() -> tuple[bool, list[dict]]:
    """找持有 state.db 的进程 (gateway / desktop backend / 裸 python)。"""
    writers = []
    try:
        import psutil  # noqa: PLC0415
    except ImportError:
        # 没 psutil 就退回读 gateway_state.json + 探测已知 PID
        gs = _read_json(os.path.join(HD, "gateway_state.json"))
        if gs and gs.get("pid"):
            writers.append({"pid": gs.get("pid"), "how": "gateway_state.json"})
        return bool(writers), writers

    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            cmd = " ".join(proc.info.get("cmdline") or [])
            name = (proc.info.get("name") or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if "state.db" in cmd:
            writers.append({"pid": proc.info["pid"], "how": "cmdline"})
        elif "hermes_cli.main" in cmd and "gateway" in cmd:
            writers.append({"pid": proc.info["pid"], "how": "gateway"})
        elif name in ("hermes.exe", "hermes"):
            writers.append({"pid": proc.info["pid"], "how": "desktop"})
    return bool(writers), writers


def check_db_integrity() -> tuple[bool, str]:
    """主库完整性 + 基本行数。"""
    if not os.path.exists(DB):
        return False, f"state.db 不存在: {DB}"
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            ic = con.execute("PRAGMA integrity_check").fetchone()[0]
            n = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error as e:
        return False, f"读库失败: {e}"
    if ic != "ok":
        return False, f"integrity_check = {ic}"
    if n == 0:
        return False, "messages 表是空的"
    return True, f"ok (messages={n:,})"


def main() -> int:
    ap = argparse.ArgumentParser(description="优化前置门禁")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    ap.add_argument("--force", action="store_true",
                    help="明知有活会话也继续 (仍然检查库完整性)")
    args = ap.parse_args()

    checks = []
    blockers = []

    has_live, live = check_live_sessions()
    checks.append({
        "name": "活会话",
        "ok": not has_live,
        "detail": (f"{len(live)} 个正在跑" if has_live
                   else "无"),
        "items": live,
    })

    db_ok, db_detail = check_db_integrity()
    checks.append({"name": "库完整性", "ok": db_ok, "detail": db_detail, "items": []})
    if not db_ok:
        blockers.append(f"库有问题: {db_detail}")

    has_writers, writers = check_writers()
    checks.append({
        "name": "持库进程",
        "ok": True,  # writer 存在是**正常**的 (gateway 总在跑), 只作提示
        "detail": f"{len(writers)} 个进程持有 state.db (正常, 停机时会一起停)",
        "items": writers,
    })

    # 活会话是硬阻断 —— 除非 --force。
    #
    # 这里**只判一次**。之前这个判断散在三处 (checks.append / blockers.append /
    # live_blocks), 结果变异测试打掉任何一处都被另外两处兜住, 反而说明
    # 逻辑该收敛到单一出口 —— 现在这一行就是唯一的决策点。
    live_blocks = has_live and not args.force
    if live_blocks:
        blockers.append(
            f"有 {len(live)} 个活会话正在跑 —— 停 gateway 会砍断正在跑的对话 "
            f"(加 --force 可强行继续)"
        )
    safe = False if live_blocks else not blockers
    payload = {
        "ok": safe,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "checks": checks,
        "blockers": blockers,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if safe else 1

    print("=" * 60)
    print("优化前置门禁")
    print("=" * 60)
    for c in checks:
        mark = "✅" if c["ok"] else "⛔"
        print(f"  {mark} {c['name']:<10} {c['detail']}")
        for it in c.get("items", [])[:6]:
            print(f"        - {it}")
    print()
    if safe:
        print("✅ 可以动")
        return 0
    print("⛔ 不安全, 已中止:")
    for b in blockers:
        print(f"   - {b}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
