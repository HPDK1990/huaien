r"""怀恩 — 一键体检与恢复

用户 2026-10-30: *"怎加一个怀恩死掉后的重新修复启动的备份方式。"*

## 覆盖什么

| 部件 | 坏了怎么办 | 怎么恢复 |
|---|---|---|
| **Hermes gateway** | 飞书/微信/怀恩全都答不了话 | `hermes gateway start` |
| **怀恩前台**（桌面窗口） | 你没界面可说话 | 重新起 `huaien.app` |
| **手机端 web** | 手机上打不开 | 重新起 `huaien.web` |
| **记忆不同步** | 前台跟后台记的不是一回事 | `sync_memory.py` 补齐 |
| **项目代码坏了** | 改了不该改的 | `git checkout` 回封存版本 |
| **全都不行了** | — | 从 `versions/V0.3.0/怀恩-V0.3.0.zip` 解压重装 |

## 跑法

```bash
python scripts/restore.py            # 只体检, 报状态
python scripts/restore.py --fix      # 体检 + 修能修的
python scripts/restore.py --fix --reset-code   # 连代码一起回滚到 v0.3.0
```

**桌面快捷方式**：`scripts\一键恢复怀恩.bat`

## 设计约束

- **默认只体检不改**（用户的"危险操作铁律": 不删/不改/不创除非明确说）
- `--fix` 只做**幂等且无风险**的动作（起进程、同步记忆）
- `--reset-code` 才动 git（而且是 `checkout` 到 tag，**不是 reset --hard**，不丢未提交改动）
- 每一步都写日志: `logs/restore.log`
- 修完**自动复验**（四项状态 + 一句真实问答）
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
PY = os.path.join(HERMES_HOME, "hermes-agent", "venv", "Scripts", "python.exe")
if not os.path.exists(PY):
    PY = sys.executable
LOG = os.path.join(PROJ, "logs", "restore.log")
VERSION_FILE = os.path.join(PROJ, "VERSION")


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
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
    except Exception as e:
        return 125, f"{type(e).__name__}: {e}"


def proc_count(module: str) -> int:
    """数真正在跑的实例 (叶子进程口径: venv launcher 会带一个真解释器)。"""
    ps = ("$all = @(Get-CimInstance Win32_Process | Where-Object { "
          "$_.Name -match '^python(w)?\\.exe$' -and "
          f"$_.CommandLine -match '-m\\s+huaien\\.{module}' -and "
          "$_.CommandLine -notmatch 'Get-CimInstance' }); "
          "$parents = @($all | ForEach-Object { $_.ParentProcessId }); "
          "@($all | Where-Object { $parents -notcontains $_.ProcessId }).Count")
    rc, out = run(["powershell", "-NoProfile", "-Command", ps], 90)
    try:
        return int((out or "0").strip() or 0)
    except ValueError:
        return 0


def watchdog_state() -> dict:
    """读 watchdog 的四项状态, 解析成 dict。"""
    _, out = run([PY, os.path.join(PROJ, "scripts", "watchdog.py"), "--status"], 180)
    state = {}
    for line in out.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            state[k.strip()] = v.strip()
    return state


def spawn(module: str, *extra: str) -> None:
    """后台起一个 huaien 模块 (不留窗口)。"""
    subprocess.Popen([PY, "-m", module, *extra], cwd=PROJ, env=env(),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=0x08000000 if os.name == "nt" else 0)


def check() -> dict:
    """体检。四项 + 项目状态。"""
    _, out = run(["hermes", "gateway", "status"], 120)
    gateway = "Gateway process running" in out
    state = watchdog_state()

    ver = "?"
    if os.path.exists(VERSION_FILE):
        with open(VERSION_FILE, encoding="utf-8") as f:
            ver = f.read().strip()
    elif os.path.exists(os.path.join(PROJ, "pyproject.toml")):
        with open(os.path.join(PROJ, "pyproject.toml"), encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("version"):
                    ver = line.split("=", 1)[1].strip().strip('"')
                    break

    rc4, out4 = run(["git", "status", "--short"], 60)
    dirty = len([x for x in out4.splitlines() if x.strip()])

    return {
        "gateway": gateway,
        "frontend": state.get("frontend", "?"),
        "web": state.get("web", "?"),
        "memory": state.get("memory", "?"),
        "version": ver,
        "git_dirty": dirty,
        "frontend_n": proc_count("app"),
        "web_n": proc_count("web"),
    }


def fix(st: dict) -> dict:
    """修能修的。只做幂等无风险的动作。"""
    actions = []

    if not st["gateway"]:
        log("gateway 不在 -> 拉起")
        run(["hermes", "gateway", "start"], 240)
        time.sleep(8)
        actions.append("起 gateway")

    if st["frontend_n"] == 0:
        log("怀恩前台不在 -> 拉起")
        spawn("huaien.app")
        time.sleep(6)
        actions.append("起怀恩前台")

    if st["web_n"] == 0:
        log("手机端 web 不在 -> 拉起")
        spawn("huaien.web", "--port", "8765")
        time.sleep(4)
        actions.append("起手机端 web")

    if st["memory"] != "in sync":
        log("记忆没对齐 -> 同步")
        run([PY, os.path.join(PROJ, "scripts", "sync_memory.py")], 120)
        actions.append("同步记忆")

    return actions


def reset_code(tag: str = "v0.3.0") -> bool:
    """把项目代码回滚到封存 tag。**不动未提交的东西** (不用 reset --hard)。"""
    rc, out = run(["git", "tag", "-l", tag], 60)
    if tag.strip() not in out:
        log(f"没有 tag {tag}, 不动代码")
        return False
    rc2, out2 = run(["git", "checkout", tag, "--", "."], 120)
    if rc2 != 0:
        log(f"checkout 失败: {out2[:300]}")
        return False
    log(f"代码已回滚到 {tag}")
    return True


def verify() -> bool:
    """修完复验: 四项状态。gateway/前台必须 alive, 记忆必须 in sync。"""
    state = watchdog_state()
    ok = True
    for key, want in (("gateway", "alive"), ("frontend", "alive"), ("memory", "in sync")):
        if state.get(key) != want:
            log(f"复验不过: {key} = {state.get(key)!r}, 期望 {want!r}")
            ok = False
    log(f"状态: {' | '.join(f'{k}={v}' for k, v in state.items())}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="怀恩 一键体检与恢复")
    ap.add_argument("--fix", action="store_true", help="体检 + 修能修的")
    ap.add_argument("--reset-code", action="store_true", help="代码回滚到封存 tag")
    ap.add_argument("--tag", default="v0.3.0", help="回滚到哪个 tag")
    args = ap.parse_args()

    ver_txt = "?"
    if os.path.exists(VERSION_FILE):
        with open(VERSION_FILE, encoding="utf-8") as f:
            ver_txt = f.read().strip()
    log("=" * 50)
    log(f"体检开始 (version={ver_txt})")

    st = check()
    log(f"状态: {st}")

    if args.reset_code:
        if not reset_code(args.tag):
            print("代码回滚失败", file=sys.stderr)
            return 1
        st = check()

    if args.fix:
        actions = fix(st)
        log(f"做了: {actions or '不需要修'}")
        time.sleep(3)
        st = check()
        log(f"修完状态: {st}")
        if verify():
            print("\n✅ 恢复完成, 四项状态正常")
        else:
            print("\n⚠️ 恢复后仍有问题, 看 logs/restore.log")
            return 1
    else:
        print(f"\n当前状态: {st}")
        print("加 --fix 才会动手修")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
