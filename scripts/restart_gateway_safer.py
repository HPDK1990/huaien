"""怀恩 — 安全重启 gateway (让配置改动生效)

按 `hermes-session-store-safety` skill 的规则来:

1. **只改 config, 不动 state.db** -> 不需要 preflight 的 writer 门禁
   (那个门禁是给"写库"用的, 这次只是重启)
2. **停完必须确认真停了**。`Hermes_Gateway` 计划任务的
   `RestartOnFailure` 是 Count=999 / PT1M, gateway 一死一分钟内就被拉回来。
   在旧进程还活着的时候 start 新进程 = 两个 gateway 写同一个库。
3. **重启后必须扫围堵**。刚被中断的那几轮会留下 `status='unknown'`
   的准入行, **永久堵死该会话的 FIFO** —— 后面的消息永远不跑,
   而 `integrity_check` 还是 ok。这种情况下微信表现就是
   "对方正在输入中"一直转。

## 用法

```bash
python scripts/restart_gateway_safer.py          # 走一遍
python scripts/restart_gateway_safer.py --dry-run # 只打印要做什么
```
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.join(
    os.path.expanduser("~"), "AppData", "Local", "hermes")
HERMES_BIN = os.path.join(HERMES_HOME, "bin")
LOG = os.path.join(PROJ, "logs", "gateway_restart.log")
WEDGE_SCANNER = os.path.join(HERMES_HOME, "skills", "hermes-internals-bypass",
                              "scripts", "list_wedged_sessions.py")


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def env() -> dict:
    e = os.environ.copy()
    e["PATH"] = HERMES_BIN + os.pathsep + e.get("PATH", "")
    e.pop("PYTHONPATH", None)
    e.pop("PYTHONHOME", None)
    return e


def run(cmd: list[str], timeout: int = 200) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, cwd=PROJ, env=env())
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"")
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return 124, out + "\n(timed out)"
    except Exception as e:
        return 125, f"{type(e).__name__}: {e}"


def gateway_pids() -> list[int]:
    """精确找出跑 gateway 的进程 PID。不靠 taskkill /IM python (那会杀掉一切)。"""
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "
          "'hermes_cli\\.main gateway' -and $_.CommandLine -notmatch 'Get-CimInstance' } | "
          "Select-Object -ExpandProperty ProcessId")
    rc, out = run(["powershell", "-NoProfile", "-Command", ps], 90)
    pids = []
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def wait_stopped(timeout: int = 90) -> tuple[bool, list[int]]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        pids = gateway_pids()
        if not pids:
            return True, []
        time.sleep(3)
    return False, gateway_pids()


def scan_wedges() -> tuple[int, list[str]]:
    """扫围堵会话。返回 (数量, 会话 id 列表)。

    用扫描器的 --json 输出, 不解析人类可读文本 —— 那上面每行都长得像
    标识符 ("integrity_check : ok" 就是一个 8+ 字符的词), 用正则捞必然
    捞出假 id 去执行 discard。
    """
    if not os.path.exists(WEDGE_SCANNER):
        log("wedge scanner 不存在, 跳过扫描")
        return 0, []
    _, out = run([sys.executable, WEDGE_SCANNER, "--json"], 200)
    try:
        report = json.loads(out)
    except (json.JSONDecodeError, TypeError):
        log(f"扫描器输出不是 JSON, 跳过: {out.strip()[:200]}")
        return 0, []
    entries = report.get("wedged_sessions") or []
    ids = [e["session_id"] for e in entries if e.get("session_id")]
    return report.get("wedged_session_count", len(ids)), ids


def discard(ids: list[str]) -> None:
    """逐个丢弃被中断的那一轮 (承认丢失, 让排队的消息继续跑)。"""
    for sid in ids:
        rc, out = run(["hermes", "sessions", "discard", sid, "--yes"], 200)
        last = out.strip().splitlines()[-1][:90] if out.strip() else ""
        log(f"  discard {sid} -> rc={rc} {last}")


def main() -> int:
    ap = argparse.ArgumentParser(description="安全重启 gateway")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--timeout", type=int, default=90)
    args = ap.parse_args()

    log("=" * 60)
    if args.dry_run:
        log("DRY RUN — 不做任何改动")
        log(f"  当前 gateway PID: {gateway_pids()}")
        log("  将要: stop --yes -> 等真停 -> start -> 扫围堵 -> discard")
        return 0

    # ---------- 1. stop ----------
    before = gateway_pids()
    log(f"stopping gateway (PID {before})")
    run(["hermes", "gateway", "stop", "--yes"], 240)

    # ---------- 2. 确认真停了 ----------
    stopped, lingering = wait_stopped(args.timeout)
    if not stopped:
        # 精确强杀那几个 PID —— 绝不用 taskkill /IM python.exe
        log(f"!! {args.timeout}s 后仍在跑 (PID {lingering}), 精确强杀")
        for pid in lingering:
            run(["taskkill", "/PID", str(pid), "/F"], 60)
        stopped, lingering = wait_stopped(30)
    if not stopped:
        log("!! gateway 停不掉, 放弃重启 (不启动第二个 gateway)")
        return 1
    log("gateway 已确认停止")

    # ---------- 3. start ----------
    log("starting gateway")
    run(["hermes", "gateway", "start"], 240)
    time.sleep(25)

    pids = gateway_pids()
    log(f"gateway 运行中: PID {pids}")
    if not pids:
        log("!! 启动失败")
        return 1

    # ---------- 4. 扫围堵 ----------
    log("scanning for wedged sessions (被中断的 turn 会永久堵死会话 FIFO)")
    n, ids = scan_wedges()
    log(f"wedged sessions: {n}")
    if ids:
        log(f"clearing: {ids}")
        discard(ids)

    log("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
