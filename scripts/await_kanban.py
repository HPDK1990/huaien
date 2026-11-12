"""等一批看板卡跑完 —— 派活之后不用守着。

用法:
    python scripts/await_kanban.py t_aaa t_bbb
    python scripts/await_kanban.py t_aaa t_bbb --timeout 600 --json out.json

退出码:
    0  全部 done
    1  超时(还有卡没完)
    2  参数错 / 查不到看板

为什么需要它: 用户要的是"派完活就走, 不用盯"。
以前只能靠人肉盯 `hermes kanban list`, 这个脚本把它变成一条命令。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.join(
    os.path.expanduser("~"), "AppData", "Local", "hermes")
HERMES_BIN = os.path.join(HERMES_HOME, "bin")

def _env() -> dict:
    env = os.environ.copy()
    env["PATH"] = HERMES_BIN + os.pathsep + env.get("PATH", "")
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    return env


def board_status(task_ids: list[str]) -> dict[str, str]:
    """id -> status。查不到就返回空 dict。"""
    r = subprocess.run(["hermes", "kanban", "list", "--json"], capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       timeout=150, env=_env())
    try:
        rows = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return {}
    return {t["id"]: t.get("status", "?") for t in rows if t.get("id") in task_ids}


def await_tasks(task_ids: list[str], timeout: float = 1800.0,
                interval: float = 20.0, quiet: bool = False) -> tuple[bool, dict]:
    """轮询到全部 done(或有卡失败/超时)。返回 (是否全部完成, id->status)。"""
    deadline = time.monotonic() + timeout
    last: dict[str, str] = {}
    while True:
        last = board_status(task_ids)
        states = [last.get(tid, "?") for tid in task_ids]
        if not quiet:
            # 直接显示卡号 —— 别搞 P1/P2 标签, 传几张卡就显示几张, 不会串
            line = " ".join(f"{tid}:{s}" for tid, s in zip(task_ids, states, strict=True))
            print(f"{time.strftime('%H:%M:%S')} {line}", flush=True)
        if all(s == "done" for s in states):
            return True, last
        if any(s in ("failed", "error", "cancelled") for s in states):
            return False, last
        if time.monotonic() >= deadline:
            return False, last
        time.sleep(interval)


def main() -> int:
    ap = argparse.ArgumentParser(description="等一批看板卡跑完")
    ap.add_argument("task_ids", nargs="+", help="卡号, 如 t_abc123")
    ap.add_argument("--timeout", type=float, default=1800.0, help="最多等多少秒")
    ap.add_argument("--interval", type=float, default=20.0, help="轮询间隔秒")
    ap.add_argument("--json", help="把结果写到这个文件")
    ap.add_argument("--quiet", action="store_true", help="不打印进度")
    args = ap.parse_args()

    for tid in args.task_ids:
        if not tid.startswith("t_"):
            print(f"卡号格式不对: {tid} (应以 t_ 开头)")
            return 2

    ok, states = await_tasks(args.task_ids, args.timeout, args.interval, args.quiet)
    result = {"ok": ok, "statuses": states}
    if args.json:
        try:
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=1)
        except OSError as e:
            print(f"写 {args.json} 失败: {e}")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
