"""怀恩 · WSL 弹窗抓现行探针

## 这是干什么的

用户 2026-11-04: *"老是这个错误。是不是技术路线出问题了？"*

弹窗内容是 Windows 自带 `C:\\Windows\\System32\\wsl.exe` 第一次运行时的提示:

    未安装适用于 Linux 的 Windows 子系统。可通过运行 "wsl.exe --install" 进行安装。
    按任意键安装适用于 Linux 的 Windows 子系统。按 ESC 或 CTRL-C 取消。
    此提示将在 60 秒后超时。

**已排除的范围** (都查过了):
- Hermes 源码不调 wsl.exe —— `is_wsl()` 是读 `/proc/version` 的纯文件检查,
  `_build_wsl_interop_paths()` 在非 WSL 上直接 `return []`
- 全项目 + `HERMES_HOME` 的脚本/配置里没有一处提到 wsl
- Windows 计划任务里没有调 wsl 的
- 备份用的 `hermes-weekly-backup.sh` 走的是 Git Bash
  (`shutil.which('bash')` 命中 `hermes\\tools\\git-...\\usr\\bin\\bash.EXE`),
  而 `C:\\Windows\\System32\\bash.exe` **在这台机器上根本不存在**
- Hermes 的 gateway.log / errors.log 里 0 条 wsl 记录

**结论: 是某个我们还没定位到的进程在调 `wsl.exe`。** 这个探针就是去找凶手的。

## 原理

`wsl.exe` 弹窗后会**挂着等按键 60 秒** —— 所以它是个"活的"进程, 能被 psutil 看到。
轮询进程表, 一旦发现 `wsl.exe`, 立刻记下:

- 自己的 PID + 完整命令行
- **父进程的 PID + 名字 + 完整命令行** ← 真凶在这行

## 用法

```bash
python scripts/wsl_catcher.py --once      # 扫一次, 看现在有没有 wsl 在跑
python scripts/wsl_catcher.py --watch     # 常驻盯 (默认)
python scripts/wsl_catcher.py --watch --interval 1
```

日志: `logs/wsl_catcher.log`
"""
from __future__ import annotations

import argparse
import contextlib
import os
import sys
import time
from datetime import datetime

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(PROJ, "logs", "wsl_catcher.log")

# 命中这些名字就算 —— wsl.exe / wsl / WSLService / wslservice / WSL.exe
TARGET_NAMES = {"wsl.exe", "wsl", "wslservice.exe", "wslhost.exe"}
# 这些是 wsl.exe 自己的子进程/服务, 单独出现不代表有人主动调它
BENIGN = {"wslservice.exe", "wslhost.exe"}


def _log(msg: str, to_file: bool = True) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    if not to_file:
        return
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _describe(proc) -> str:
    """尽量把一个进程的来龙去脉说清楚 (psutil 可能因为权限拿不到命令行)。"""
    bits = []
    try:
        bits.append(f"pid={proc.pid}")
    except Exception:
        return "?"
    with contextlib.suppress(Exception):
        bits.append(f"name={proc.name()}")
    with contextlib.suppress(Exception):
        cmd = proc.cmdline()
        if cmd:
            bits.append("cmd=" + " ".join(cmd)[:300])
    ppid = 0
    with contextlib.suppress(Exception):
        ppid = proc.ppid()
    if ppid:
        bits.append(f"ppid={ppid}")
        # 先走 parent(); 它可能因为父进程刚退出而拿不到 -> 再用 pid 直查一次
        parent = None
        with contextlib.suppress(Exception):
            parent = proc.parent()
        if parent is None:
            with contextlib.suppress(Exception):
                import psutil
                parent = psutil.Process(ppid)
        if parent is not None:
            with contextlib.suppress(Exception):
                bits.append(f"parent={parent.name()}")
            with contextlib.suppress(Exception):
                pcmd = parent.cmdline()
                if pcmd:
                    bits.append("parent_cmd=" + " ".join(pcmd)[:400])
        else:
            bits.append("parent=<已退出, 抓不到命令行>")
    return " | ".join(bits)


def scan_once() -> list:
    """扫一遍进程表, 返回所有 wsl 相关进程。"""
    try:
        import psutil
    except ImportError:
        _log("❌ 没装 psutil, 抓不了")
        return []
    found = []
    for proc in psutil.process_iter(["name"]):
        try:
            name = (proc.info.get("name") or "").lower()
        except Exception:
            continue
        if name in TARGET_NAMES:
            found.append(proc)
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description="WSL 弹窗抓现行")
    ap.add_argument("--once", action="store_true", help="只扫一次")
    ap.add_argument("--watch", action="store_true", help="常驻盯")
    ap.add_argument("--interval", type=float, default=0.1,
                    help="轮询间隔秒 (wsl.exe 只活 0.4 秒, 别调太大)")
    args = ap.parse_args()

    if args.once:
        procs = scan_once()
        if not procs:
            _log("当前没有 wsl 相关进程在跑。")
            return 0
        for p in procs:
            _log(f"发现: {_describe(p)}")
        return 0

    _log(f"👁 开始盯 wsl.exe (每 {args.interval}s 一次)。抓到就记下它和它爹。Ctrl-C 停。")
    seen: set[int] = set()
    try:
        while True:
            for p in scan_once():
                pid = p.pid
                key = pid
                if key in seen:
                    continue
                seen.add(key)
                try:
                    name = p.name().lower()
                except Exception:
                    name = "?"
                tag = "(仅服务进程, 大概不是主动调用)" if name in BENIGN else "★ 可能是主动调用"
                _log(f"{tag} {_describe(p)}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        _log("停了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
