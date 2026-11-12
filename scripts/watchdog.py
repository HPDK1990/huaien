"""怀恩 — 看门狗

盯两件事:
  1. Hermes gateway 活着吗? (后台的根 — 它死了, 前台就答不了话)
  2. 前台窗口活着吗?

用法:
  python scripts/watchdog.py            # 常驻, 每 30 秒查一次
  python scripts/watchdog.py --once     # 查一次就退出 (给 cron 用)
  python scripts/watchdog.py --status   # 只报告, 不修

设计约束:
  - 只做"拉起", 不做"杀进程"—— 除非那个进程已经僵死。
  - 每次动作都写日志, 便于事后回看。
  - 失败不抛异常, 只记录; 看门狗自己挂掉不能影响被看的服务。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.join(
    os.path.expanduser("~"), "AppData", "Local", "hermes")
HERMES_BIN = os.path.join(HERMES_HOME, "bin")
LOG_PATH = os.path.join(PROJ, "logs", "watchdog.log")
INTERVAL = 30

# 前台存活探针 (PowerShell)。
#
# 坑 (2026-10-30 回归看门狗实测): 早先这里是
#     Where-Object { $_.CommandLine -match 'huaien.app' } | Measure-Object
# 探测命令**自己的** PowerShell 进程命令行里就含 'huaien.app',
# 于是自己匹配自己 —— 计数恒 >= 1, 前台真死了也报 alive (静默失效)。
#
# 三重过滤, 缺一不可:
#   1. Name 必须是 python/pythonw  —— bash 包装器、powershell 都排除
#   2. CommandLine 必须真的以 '-m huaien.app' 启动 —— 精确到模块调用
#   3. CommandLine 不含 'Get-CimInstance' —— 排除探针自身 (防自匹配)
#
# 拆成两块: 过滤条件单独一个常量, 单测可以直接把它当 scriptblock 喂假进程,
# 用真正的 PowerShell 语义验证, 不用在 Python 里复刻一遍正则。
FRONTEND_FILTER_PS = (
    "$_.Name -match '^python(w)?\\.exe$' -and "
    "$_.CommandLine -match '-m\\s+huaien\\.app' -and "
    "$_.CommandLine -notmatch 'Get-CimInstance'"
)

FRONTEND_PROBE_PS = (
    "$p = @(Get-CimInstance Win32_Process | Where-Object { "
    + FRONTEND_FILTER_PS + " }); $p.Count"
)


def _env():
    env = os.environ.copy()
    env["PATH"] = HERMES_BIN + os.pathsep + env.get("PATH", "")
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    return env


def log(msg: str) -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


class _Failed:
    """子进程起不来时的替身 (看门狗自己不能因为探测失败而崩)。"""
    stdout = ""
    stderr = ""
    returncode = -1

    def __init__(self, err: str):
        self.stderr = err


def _run(cmd, timeout=90):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout, env=_env(), shell=isinstance(cmd, str))
    except Exception as e:
        return _Failed(str(e))


def gateway_alive() -> bool:
    r = _run(["hermes", "gateway", "status"], timeout=60)
    out = (r.stdout or "") + (r.stderr or "")
    return "Gateway process running" in out


def frontend_alive() -> bool:
    """前台窗口是否活着。

    坑 (2026-10-30 回归看门狗实测): 早先这里用
        Where-Object { $_.CommandLine -match 'huaien.app' }
    统计进程数, 但探测命令本身的 PowerShell 进程命令行里就含 'huaien.app',
    于是**自己匹配自己**, 计数恒 >= 1 —— 前台真死了也报 alive。
    实测: 旧探针计数 5 (其中 4 个是探测链自身), 前台真实进程 0。

    现在只认"以 -m huaien.app 方式启动的 python 进程", 并排除探测命令自身。
    """
    r = _run(["powershell", "-NoProfile", "-Command", FRONTEND_PROBE_PS],
             timeout=60)
    try:
        return int((r.stdout or "0").strip() or 0) > 0
    except ValueError:
        return False


def start_gateway() -> bool:
    log("gateway 不在, 尝试拉起 ...")
    _run(["hermes", "gateway", "start"], timeout=180)
    ok = gateway_alive()
    log(f"gateway 拉起 {'成功' if ok else '失败'}")
    return ok


def start_frontend() -> bool:
    log("前台不在, 尝试拉起 ...")
    python = os.path.join(HERMES_HOME, "hermes-agent", "venv", "Scripts", "python.exe")
    if not os.path.exists(python):
        log("找不到 python, 前台拉起失败")
        return False
    flags = 0x08000000 if os.name == "nt" else 0
    try:
        subprocess.Popen([python, "-m", "huaien.app"], cwd=PROJ, env=_env(),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags)
    except Exception as e:
        log(f"前台拉起异常: {e}")
        return False
    time.sleep(6)
    ok = frontend_alive()
    log(f"前台拉起 {'成功' if ok else '失败'}")
    return ok


SYNC_SCRIPT = os.path.join(PROJ, "scripts", "sync_memory.py")


def _run_sync(check_only: bool) -> tuple[bool, str]:
    """跑记忆同步脚本。返回 (是否成功, 最后一行输出)。"""
    if not os.path.exists(SYNC_SCRIPT):
        return False, "同步脚本不存在"
    cmd = [sys.executable, SYNC_SCRIPT]
    if check_only:
        cmd.append("--check")
    r = _run(cmd, timeout=60)
    out = (r.stdout or "").strip()
    tail = out.splitlines()[-1] if out else ""
    return r.returncode == 0, tail


def memory_in_sync() -> bool:
    """前后台记忆是否已对齐 (只查不改)。"""
    ok, tail = _run_sync(check_only=True)
    # tail 形如 "  差异: 0 条"
    return ok and "差异: 0 条" in tail


def sync_memory() -> bool:
    """前后台记忆双向补齐 (前台 huaien profile ↔ 后台 default profile)。

    为什么不共享同一个 profile: 前台要的是"话不多的管家", 后台的人格契约
    (结论先行 / H2 / 格式自检) 会污染前台输出。而 `--ignore-rules` 会连 memory
    一起跳过, 没有"换人格但共享记忆"的开关 —— 所以只能在文件层做条目级补齐。
    """
    ok, tail = _run_sync(check_only=False)
    if ok and tail:
        log(f"记忆同步: {tail}")
    return ok


def web_alive() -> bool:
    """Web 前台 (手机端) 是否在跑。

    探针只认"以 -m huaien.web 启动的 python 进程", 与前台探针同样的坑:
    探测命令自己会匹配自己, 所以必须排除含 'huaien.web' 字样的 powershell 自身。
    """
    ps = ("$p = @(Get-CimInstance Win32_Process | Where-Object { "
          "$_.Name -match '^python(w)?\\.exe$' -and "
          "$_.CommandLine -match '-m\\s+huaien\\.web' -and "
          "$_.CommandLine -notmatch 'Get-CimInstance' }); $p.Count")
    r = _run(["powershell", "-NoProfile", "-Command", ps], timeout=60)
    try:
        return int((r.stdout or "0").strip() or 0) > 0
    except ValueError:
        return False


def check_once(fix: bool = True) -> dict:
    """检查三件事: gateway / 前台 / 记忆。

    fix=True  → 缺什么拉什么, 记忆做一次同步;
    fix=False → 只报告, 记忆只查不改 (返回是否已对齐)。
    三个值恒为 bool。
    """
    state = {"gateway": gateway_alive(), "frontend": frontend_alive(),
             "web": web_alive()}
    if fix:
        if not state["gateway"]:
            state["gateway"] = start_gateway()
        if not state["frontend"]:
            state["frontend"] = start_frontend()
        state["memory"] = sync_memory()
    else:
        state["memory"] = memory_in_sync()
    return state


def main() -> int:
    ap = argparse.ArgumentParser(description="怀恩看门狗")
    ap.add_argument("--once", action="store_true", help="查一次就退出")
    ap.add_argument("--status", action="store_true", help="只报告, 不修")
    args = ap.parse_args()

    if args.status:
        st = check_once(fix=False)
        print(f"gateway: {'alive' if st['gateway'] else 'DOWN'}")
        print(f"frontend: {'alive' if st['frontend'] else 'DOWN'}")
        print(f"memory : {'in sync' if st['memory'] else 'OUT OF SYNC'}")
        print(f"web    : {'alive' if st['web'] else 'down (手机端没开)'}")
        return 0

    if args.once:
        st = check_once(fix=True)
        log(f"单次检查: gateway={st['gateway']} frontend={st['frontend']}")
        return 0

    log(f"看门狗启动, 每 {INTERVAL}s 检查一次 (Ctrl+C 退出)")
    try:
        while True:
            try:
                check_once(fix=True)
            except Exception as e:
                log(f"检查异常: {e}")
            time.sleep(INTERVAL)
    except KeyboardInterrupt:
        log("看门狗退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
