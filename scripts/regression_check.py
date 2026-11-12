"""怀恩 — 回归看门狗 (纯脚本, 不调 LLM)

## 为什么不用 agent

之前这个看门狗是 agent 形式的 cron job, 2026-10-30 那次运行报
`RuntimeError: Request timed out` —— agent 版要跑 selftest + 单测 + 看门狗状态,
再加上模型推理, 很容易撞上超时。

**纯脚本更快、更稳、不会超时**, 而且没有额度消耗。
看门狗这种"只关心有没有坏"的活, 本来就不需要推理。

## 行为

- 全部通过 → **什么都不输出** (cron 的 deliver=local + 空输出 = 不打扰)
- 有问题 → 输出问题摘要, 写进日志

## 跑法

    python scripts/regression_check.py          # 人跑也一样
    python scripts/regression_check.py --quiet  # 只返回码, 不打日志
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.join(
    os.path.expanduser("~"), "AppData", "Local", "hermes")


def _pick_python() -> str:
    """按"能不能干活"选解释器 —— 不是按路径存在。

    ⚠️ 2026-11-04 教训: 这个看门狗连续失败 17 次没人发现, 根因就是它一直用
    `hermes-agent\\venv\\Scripts\\python.exe` —— 那个 venv 里**没装 ruff/pytest**,
    于是每轮都报 "No module named ruff" + "单测超时" + "找不到 selftest.py"。
    所以这里必须先验"能不能 import pytest 和 ruff", 过了才用。
    """
    candidates = [
        os.path.join(PROJ, ".venv", "Scripts", "python.exe"),
        os.path.join(HERMES_HOME, "hermes-agent", "venv", "Scripts", "python.exe"),
        sys.executable,
    ]
    for py in candidates:
        if not os.path.exists(py):
            continue
        try:
            r = subprocess.run([py, "-c", "import pytest, ruff"],
                               capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0:
            return py
    return sys.executable


PY = _pick_python()

LOG = os.path.join(PROJ, "logs", "regression.log")


def log(msg: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def run(cmd: list[str], timeout: int = 300) -> tuple[int, str]:
    env = os.environ.copy()
    env["PATH"] = os.path.join(HERMES_HOME, "bin") + os.pathsep + env.get("PATH", "")
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, cwd=PROJ, env=env)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"超时 ({timeout}s)"
    except Exception as e:
        return 125, f"{type(e).__name__}: {e}"


def main() -> int:
    ap = argparse.ArgumentParser(description="怀恩回归看门狗")
    ap.add_argument("--quiet", action="store_true", help="不打日志, 只返回码")
    args = ap.parse_args()

    problems: list[str] = []

    # 1. 静态检查
    rc, out = run([PY, "-m", "ruff", "check", "."], 120)
    if rc != 0:
        problems.append(f"ruff 有 {rc} 个问题:\n" + out[-600:])

    # 2. 单元测试
    rc, out = run([PY, "-m", "pytest", "-q"], 300)
    if rc != 0:
        tail = [ln for ln in out.splitlines() if ln.strip()][-1:] or ["(无输出)"]
        problems.append(f"单测失败: {tail[0]}\n" + out[-800:])

    # 3. 三层自检 (起窗口 -> 发一句 -> 收回复 -> 截图)
    rc, out = run([PY, os.path.join(PROJ, "scripts", "selftest.py")], 300)
    if rc != 0 or "3/3 通过" not in out:
        problems.append("三层自检失败:\n" + out[-800:])

    # 4. 看门狗四项状态
    rc, out = run([PY, os.path.join(PROJ, "scripts", "watchdog.py"), "--status"], 180)
    if rc != 0:
        problems.append(f"看门狗状态查询失败:\n{out[-400:]}")
    else:
        state = {}
        for line in out.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                state[k.strip()] = v.strip()
        if state.get("gateway") != "alive":
            problems.append("gateway 不在了")
        if state.get("frontend") != "alive":
            problems.append("前台窗口不在了")
        if state.get("memory") != "in sync":
            problems.append(f"记忆没对齐: {state.get('memory')}")

    # 5. 会话库体检 (只读)。超阈值只是提醒, 不是故障 —— 不该让看门狗报警。
    rc, out = run([PY, os.path.join(PROJ, "scripts", "db_health.py")], 180)
    if rc == 0:
        log(f"会话库: {out.strip()[:200]}")
    else:
        log(f"会话库提醒: {out.strip()[:300]}")

    # 5.5 敏感信息审计 (上架前必须干净)。
    #     ⚠️ 只扫工作区 —— 扫全历史慢(逐 commit), 留给上架那一次。
    #     有命中就报出来, 但**不算故障** —— 上架前的脱敏是独立一步。
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "audit_impl", os.path.join(PROJ, "scripts", "audit_publish.py"))
        aimpl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(aimpl)
        ahits = aimpl.scan_worktree() + aimpl.scan_credential_files()
        afatal = [h for h in ahits if h["severity"] == "致命"
                  and h["scope"] == "会上传"]
        if afatal:
            rules = sorted({h["rule"] for h in afatal})
            log(f"⚠️ 敏感信息审计: {len(afatal)} 处致命命中 (规则: {', '.join(rules)})")
            log("   上架前必须脱敏: python scripts/audit_publish.py")
        else:
            log("敏感信息审计: 工作区干净 ✅")
    except Exception as exc:  # noqa: BLE001
        log(f"敏感信息审计失败(不影响回归): {exc}")

    # 6. 释放"只等上游、上游已完成"的 blocked 卡。
    #    Super-agent 的规则: "A new human message releases a blocked or failed
    #    Ticket to its Executor" —— 上游齐了就该放出去, 不该一直卡着。
    #    ⚠️ 释放失败不算故障 (它是维护动作, 不是健康检查), 记日志但不进 problems。
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "butler_impl", os.path.join(PROJ, "scripts", "butler.py"))
        impl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(impl)
        released = impl.release_stuck_cards()
        if released:
            log(f"释放了 {released} 张等上游的 blocked 卡")
    except Exception as exc:  # noqa: BLE001
        log(f"释放 blocked 卡失败(不影响体检): {exc}")

    if not problems:
        if not args.quiet:
            log("回归检查全过 ✅ (ruff / pytest / 三层自检 / 看门狗四项 / 会话库体检 / 释放卡)")
        return 0

    msg = "怀恩回归检查发现问题:\n" + "\n\n".join(f"- {p}" for p in problems)
    log(msg)
    print(msg)
    return 1


if __name__ == "__main__":
    sys.exit(main())
