"""怀恩 — 等 gateway 真正停下来 (防 RestartOnFailure 自动复活)

## 为什么需要它

`Hermes_Gateway` 计划任务的设置里有:

```xml
<RestartOnFailure>
  <Count>999</Count>
  <Interval>PT1M</Interval>
</RestartOnFailure>
```

gateway 进程一死, 任务会在 **1 分钟内**把它拉起来 —— 然后它又开始往
state.db 写。19:23 的事故日志里能直接看到:

    STEP3 stop gateway
    Killed 1 gateway process(es)
    ...
    STEP4 optimize-storage
    Refusing: another process is using state.db.
      PID 32292 (python.exe -m hermes_cli.main gateway run): state.db

PID 32292 就是被自动复活的那个。原脚本的注释里写着"graceful stop 会成功
退出任务, 所以 RestartOnFailure 不会触发" —— **这个推理被现场证伪了**。

## 怎么用

    python scripts/wait_gateway_stopped.py --timeout 90

退出码:
    0  = gateway 确实停了, 可以动 state.db
    1  = 超时, gateway 还在跑 (有 PID 列表)
    2  = 检查本身失败

## 为什么不用 --kill

自动杀进程是有副作用的 —— 正在跑的 turn 会被砍断。这个脚本只**报告**,
不杀。要杀就明确地 `hermes gateway stop --yes`。
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time

# cp936 控制台不能编码输出, 强制 UTF-8
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, ValueError):
        _stream.reconfigure(encoding="utf-8", errors="replace")

HD = os.environ.get("HERMES_HOME") or os.path.join(
    os.path.expanduser("~"), "AppData", "Local", "hermes")


def gateway_pids() -> list[int]:
    """找还在跑的 gateway 进程 PID。"""
    pids: list[int] = []
    try:
        import psutil  # noqa: PLC0415
    except ImportError:
        return pids
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmd = " ".join(proc.info.get("cmdline") or [])
        except Exception:  # noqa: BLE001, S110
            continue
        if "hermes_cli.main" in cmd and "gateway" in cmd:
            pids.append(proc.info["pid"])
    return pids


def gateway_pid_from_state() -> int | None:
    """从 gateway_state.json 拿 PID (psutil 不可用时的退路)。"""
    p = os.path.join(HD, "gateway_state.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f).get("pid")
    except (OSError, ValueError):
        return None


def _process_alive(pid: int) -> bool:
    """PID 对应的进程还活着吗。Windows 专用。"""
    import ctypes
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not h:
        return False
    ctypes.windll.kernel32.CloseHandle(h)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="等 gateway 真正停下来")
    ap.add_argument("--timeout", type=int, default=90,
                    help="最长等多少秒 (默认 90, 要 > RestartOnFailure 的 60s)")
    ap.add_argument("--interval", type=int, default=5, help="轮询间隔秒 (默认 5)")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    args = ap.parse_args()

    deadline = time.time() + args.timeout
    seen: list[int] = []
    checks = 0

    while time.time() < deadline:
        checks += 1
        alive = list(gateway_pids())
        if not alive:
            # psutil 缺失时只能靠 gateway_state.json 兜底。
            # 旧写法在"进程活着"时直接落到循环后的无条件 break, 只查一轮就
            # 谎报"已停" —— 2026-10-30 撕裂事故正是这么来的。活着就得接着等。
            pid = gateway_pid_from_state()
            if pid is not None and _process_alive(pid):
                alive = [pid]
        seen = alive
        if not alive:
            break
        time.sleep(args.interval)

    still = gateway_pids() or seen
    stopped = not still

    payload = {
        "ok": stopped,
        "checks": checks,
        "timeout_s": args.timeout,
        "still_running": still,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        if stopped:
            print(f"✅ gateway 已停 ({checks} 次检查, {args.timeout}s 内)")
        else:
            print(f"⛔ gateway 还在跑: PID {still}")
            print(f"   (等了 {args.timeout}s, {checks} 次检查)")
            print("   别动 state.db —— 先查清它为什么还活着:")
            print("     · 之前那步停机失败? (看日志)")
            print("     · Hermes_Gateway 任务的 RestartOnFailure (Count=999/PT1M) 把它拉回来了?")
            print("     · 另一个 gateway 实例 (多 profile)?")
    return 0 if stopped else 1


if __name__ == "__main__":
    raise SystemExit(main())
