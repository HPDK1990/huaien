"""怀恩 · 一句话管家 (Butler Run)

## 这是干什么的

用户 2026-11-04: *"我要的管家的主要功能和能力是否弄完了？主要精力还是要放到核心功能上。"*

怀恩的每一环**单独都做过、都验过**:
- 拆解  → `hermes kanban swarm` / `decompose`
- 分派  → `--worker PROFILE:TITLE` + `--verifier` + `--synthesizer`
- 执行  → dispatcher 随 gateway tick 自动派单
- 汇总  → swarm 内建 synthesizer
- 推送  → `scripts/butler.py push`

**缺的是把它们串起来的那个入口。** 这个脚本就是那个入口:

    用户一句话 → 拆解 → 建图 → 并行跑 → 等验收汇总 → 推回微信

## 为什么拆解要过一次 LLM

`kanban swarm` 要人指定 `--worker PROFILE:TITLE`, 自己不会想该派谁。
所以这里先问模型一句"这个目标该拆成哪几个 worker、各派给谁",
拿到结构化结果再喂给 swarm。**模型只负责"想", 干活的是 Hermes 原生那套。**

## 用法

```bash
python scripts/butler_run.py --goal "把 X 做完" --plan-only
    只拆解, 打印会怎么派, 不建卡

python scripts/butler_run.py --goal "把 X 做完"
    拆解 + 建图 + 等完成

python scripts/butler_run.py --goal "把 X 做完" --push
    完成 + 把汇总报告推回微信

python scripts/butler_run.py --goal "..." --json
    机器可读输出
```

## 三条硬约束 (踩过的坑)

1. **worker 标题里不能有冒号** —— `hermes kanban swarm` 用 `PROFILE:TITLE` 分隔,
   标题里写 `E:\\xxx` 会被当 skill 名解析。所以标题只写"做什么", 不写路径。
2. **中文正文走文件不走上层参数** —— MSYS 传中文参数会乱码,
   所以最终报告让 synthesizer 写文件, 推送用 `hermes send -f <文件>`。
3. **不能靠模型输出"已经做完了"** —— 汇总只认 kanban 里的真实状态。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.join(
    os.path.expanduser("~"), "AppData", "Local", "hermes")
HERMES_BIN = os.path.join(HERMES_HOME, "bin")
OUTBOX_FILE = os.path.join(PROJ, "logs", "outbox_pending.json")
REPORT_DIR = os.path.join(PROJ, "logs", "runs")

# 可以当 worker 的 profile —— 全是怀恩自己的, 不碰 default 的开发会话
WORKER_PROFILES = ("researcher", "coder", "auditor")
DEFAULT_VERIFIER = "auditor"
DEFAULT_SYNTHESIZER = "default"


def _env():
    env = os.environ.copy()
    env["PATH"] = HERMES_BIN + os.pathsep + env.get("PATH", "")
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run(args, timeout=300, cwd=PROJ):
    try:
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=_env(), cwd=cwd)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"超时 ({timeout}s)"
    except OSError as exc:
        return 125, "", str(exc)


def hermes(*args, timeout=300):
    """跑 hermes 命令。**stderr 绝不混进 stdout** —— 踩过坑: 把"不是命令"的
    错误信息当成了模型输出, 于是从错误里抠出了 prompt 里的 JSON 示例,
    看起来"模型照抄了示例", 其实是根本没跑模型。"""
    rc, out, err = _run(["hermes", *args], timeout)
    return rc, out, err


# ---------------------------------------------------------------- 拆解

PLAN_PROMPT = """你是一个任务编排器。把下面的目标拆成 2-4 个可以并行的子任务。

目标: {goal}

可用 worker profile:
{profiles}

要求:
1. 每个子任务要能独立做完, 不要互相依赖
2. title 只写"要做什么", **绝对不要出现冒号(:)** —— 冒号会被当成参数分隔符
3. title 里不要写文件路径, 路径由执行的人自己决定
4. 最后一个 worker 负责产出最终交付物
5. 必须指定一个 verifier (负责验收) 和一个 synthesizer (负责汇总成最终报告)

**下面的例子只是说明格式, 你必须写出针对上面目标的真实标题, 不许照抄。**
严格只输出 JSON, 不要任何解释文字、不要 markdown 代码块。格式示例:
{{"workers":[{{"profile":"researcher","title":"搜罗所有涉及认证鉴权的代码位置"}},{{"profile":"coder","title":"实现统一的令牌刷新逻辑"}}],"verifier":"auditor","synthesizer":"default"}}
"""


def plan(goal: str, timeout=300) -> dict:
    """问模型该怎么拆。失败就退回一个保守的默认拆法。"""
    profs = "\n".join(f"- {p}" for p in WORKER_PROFILES)
    prompt = PLAN_PROMPT.format(goal=goal, profiles=profs)
    # -z (--oneshot) 只输出答案, 干净好解析。-q 会带上"查询:"和推理过程, 不能用。
    rc, out, err = hermes("-p", "huaien", "-z", prompt, timeout=timeout)
    if rc != 0:
        print(f"  拆解调用失败 rc={rc}: {(err or out)[:200]}")
    else:
        data = _parse_plan(out)
        if data:
            return data
    # 兜底: 一个 worker 干到底, verifier + synthesizer 照常
    return {
        "workers": [{"profile": "coder", "title": f"完成: {goal[:60]}"}],
        "verifier": DEFAULT_VERIFIER,
        "synthesizer": DEFAULT_SYNTHESIZER,
        "fallback": True,
    }


def _parse_plan(text: str) -> dict | None:
    """从模型输出里抠出 JSON。模型很爱加解释和代码块, 所以要多试几种。"""
    if not text:
        return None
    candidates = []
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        candidates.append(m.group(0))
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        candidates.append(fence.group(1))
    candidates.append(text)
    for c in candidates:
        try:
            d = json.loads(c)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(d, dict):
            continue
        workers = d.get("workers")
        if not isinstance(workers, list) or not workers:
            continue
        clean = []
        for w in workers:
            if not isinstance(w, dict):
                continue
            title = str(w.get("title") or "").strip()
            prof = str(w.get("profile") or "").strip()
            if not title or ":" in title:      # 冒号 = 参数分隔符, 必炸
                continue
            if prof not in WORKER_PROFILES:
                prof = "coder"
            clean.append({"profile": prof, "title": title[:90]})
        if not clean:
            continue
        verifier = str(d.get("verifier") or DEFAULT_VERIFIER)
        synth = str(d.get("synthesizer") or DEFAULT_SYNTHESIZER)
        return {"workers": clean[:6],
                "verifier": verifier if verifier else DEFAULT_VERIFIER,
                "synthesizer": synth if synth else DEFAULT_SYNTHESIZER}
    return None


# ---------------------------------------------------------------- 建图

def build_swarm(goal: str, plan_data: dict) -> tuple[bool, str]:
    """调 hermes kanban swarm 建图。"""
    args = ["kanban", "swarm", goal]
    for w in plan_data["workers"]:
        args += ["--worker", f"{w['profile']}:{w['title']}"]
    args += ["--verifier", plan_data.get("verifier", DEFAULT_VERIFIER)]
    args += ["--synthesizer", plan_data.get("synthesizer", DEFAULT_SYNTHESIZER)]
    args += ["--created-by", "huaien"]
    rc, out, err = hermes(*args, timeout=180)
    if rc != 0:
        return False, (err or out)[:600]
    m = re.search(r"(t_[0-9a-f]{6,})", out)
    return True, (m.group(1) if m else out[:400])


# ---------------------------------------------------------------- 等

def card_status(card_id: str) -> dict | None:
    """取某张卡的状态。

    ⚠️ `hermes kanban show <id> --json` 的输出是 `{"task": {...}}` ——
    **status 在 task 里, 不在顶层**。踩过: 直接 `.get("status")` 拿到 None,
    于是 wait_done() 一直等到超时, 以为 agent 没跑。
    """
    rc, out, _err = hermes("kanban", "show", card_id, "--json", timeout=90)
    if rc != 0:
        return None
    try:
        d = json.loads(out or "")
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict):
        return None
    return d.get("task") or d


def wait_done(card_id: str, timeout=1800, poll=20) -> tuple[bool, str]:
    """轮询直到 root 卡 done / blocked / failed, 或超时。"""
    t0 = time.time()
    last = ""
    while time.time() - t0 < timeout:
        st = card_status(card_id)
        if st:
            s = str(st.get("status", "")).lower()
            last = s
            if s in ("done", "archived"):
                return True, s
            if s in ("blocked", "failed"):
                return False, s
        time.sleep(poll)
    return False, f"超时 ({int(time.time() - t0)}s), 最后状态={last or '未知'}"


# ---------------------------------------------------------------- 汇总+推

def collect_report(card_id: str) -> tuple[str, str]:
    """从 root 卡里找 synthesizer 产出的报告文件。返回 (路径, 内容)。

    `kanban list --json` 的每张卡带 `result` 字段, synthesizer 写的文件路径就在里面;
    找不到再从 result 文本里正则抠路径。
    """
    rc, out, _err = hermes("kanban", "list", "--json", timeout=120)
    result_text = ""
    if rc == 0:
        try:
            tasks = json.loads(out or "[]")
        except json.JSONDecodeError:
            tasks = []
        if isinstance(tasks, dict):
            tasks = tasks.get("tasks") or []
        for t in tasks:
            if t.get("id") == card_id:
                result_text = str(t.get("result") or "")
                break
    if not result_text:
        st = card_status(card_id)
        result_text = str((st or {}).get("result") or "")

    paths = re.findall(r"[A-Za-z]:\\\\?[^\"'\n,]*?\.(?:md|txt|json)", result_text)
    for p in dict.fromkeys(x.replace("\\\\", "\\") for x in paths):
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    return p, f.read()
            except OSError:
                continue
    return "", result_text


def push_file(path: str, target: str | None = None) -> int:
    """把报告推微信。session 失效的话留给 outbox。"""
    if not path:
        return 2
    if target is None:
        sys.path.insert(0, os.path.join(PROJ, "scripts"))
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "butler_impl", os.path.join(PROJ, "scripts", "butler.py"))
            impl = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(impl)
            cid = impl._find_wechat()
        except Exception:  # noqa: BLE001
            cid = ""
        if not cid:
            return 2
        target = f"weixin:{cid}"
    rc, out, err = hermes("send", "-f", path, "-t", target, timeout=180)
    print(f"  推送 -> {target}  rc={rc}  {(out or err)[:160]}")
    if rc != 0 and ("session not ready" in out or "prepare failed" in out):
        print("  ⚠️ iLink session 失效 —— 已存 outbox, 等用户发过消息再补发。")
        _save_outbox(path)
    return rc


def _save_outbox(path: str):
    items = []
    if os.path.exists(OUTBOX_FILE):
        try:
            with open(OUTBOX_FILE, encoding="utf-8") as f:
                items = json.load(f)
        except (OSError, json.JSONDecodeError):
            items = []
    items.append({"text": f"[管家报告] {path}", "at": datetime.now().isoformat(),
                  "err": "iLink session 失效, 等用户发消息后补发"})
    try:
        os.makedirs(os.path.dirname(OUTBOX_FILE), exist_ok=True)
        with open(OUTBOX_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="怀恩 一句话管家")
    ap.add_argument("--goal", required=True, help="一句话目标")
    ap.add_argument("--plan-only", action="store_true", help="只拆解, 不建卡")
    ap.add_argument("--push", action="store_true", help="完成后推微信")
    ap.add_argument("--wait", type=int, default=1800, help="等完成的最长秒数")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--to", help="推送目标, 如 weixin:<chat_id>")
    args = ap.parse_args()

    print(f"[1/5] 拆解目标: {args.goal}")
    plan_data = plan(args.goal)
    if plan_data.get("fallback"):
        print("  模型没给出可用结构, 用保守默认拆法")
    for w in plan_data["workers"]:
        print(f"  worker: {w['profile']:11} {w['title']}")
    print(f"  verifier={plan_data.get('verifier')}  synthesizer={plan_data.get('synthesizer')}")

    if args.plan_only:
        if args.json:
            print(json.dumps(plan_data, ensure_ascii=False, indent=2))
        return 0

    print("\n[2/5] 建图 (kanban swarm)")
    ok, info = build_swarm(args.goal, plan_data)
    if not ok:
        print(f"  ❌ 建图失败: {info}")
        return 1
    root = info if info.startswith("t_") else ""
    print(f"  root 卡: {root or info}")

    print(f"\n[3/5] 等执行完成 (最多 {args.wait}s)")
    done, status = wait_done(root, timeout=args.wait) if root else (False, "没有 root 卡号")
    print(f"  结果: {status}")

    print("\n[4/5] 找汇总报告")
    path, content = collect_report(root) if root else ("", "")
    if path:
        print(f"  报告: {path} ({len(content)} 字符)")
    else:
        print("  没找到报告文件 (synthesizer 可能还没写完)")

    pushed = 0
    if args.push and path:
        print("\n[5/5] 推微信")
        pushed = push_file(path, args.to)

    if args.json:
        print(json.dumps({"goal": args.goal, "plan": plan_data, "root": root,
                          "status": status, "report": path, "pushed": pushed},
                         ensure_ascii=False, indent=2))
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main())
