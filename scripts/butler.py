"""怀恩 · 管家大脑 (Butler)

## 这是干什么的

用户 2026-11-03: *"最终目标是要做个管家/秘书身份的agent……我以后不想打字
和盯着一大堆会话看了。"*

怀恩原来只会**被动响应** —— 用户问一句它答一句。用户不盯着就不知道发生了什么。

这个脚本是**管家的主动神经**:
读规则 → 扫状态 → 判断该不该开口 → 有事才推微信, 没事绝不打扰。

对应 DeepSeek Super-agent 的四个概念:

| Super-agent | 怀恩这里 | 在本文件的哪里 |
|---|---|---|
| `Policy` 团队写的规则 | `profiles/huaien/POLICY.md` | `load_policy()` |
| `MasterAssessor` 评估该不该做 | 规则命中判定 | `assess()` |
| `Panel` 什么需要人处理 | `--json` 统一视图 | `build_panel()` |
| `Outbox` 可靠投递 | 微信推送 + 失败留待重投 + 补发 | `push()` / `flush_outbox()` |

## 铁律

**只在有人必须行动、或能用上结果时才开口。** 没命中任何规则 = 一句话都不说。

## 用法

```bash
python scripts/butler.py --dry            # 只看会说什么, 不真推
python scripts/butler.py --json           # 机器可读的"待你处理"清单
python scripts/butler.py --push           # 真推微信
python scripts/butler.py --push --voice   # 同时推一条 .silk 语音
python scripts/butler.py --rules          # 看当前生效的规则
python scripts/butler.py --flush          # 补发 outbox 里积压的微信 (等用户发过话之后)
```

补发的独立入口: `python scripts/flush_outbox.py --dry` (cron 和手动都能调)。

## 为什么是脚本不是 agent

**cron 里的 agent 型任务会 401 / 超时** (实测: "HTTP 401: Missing Authentication header")。
主动汇报是**必须可靠**的通路, 所以走纯脚本 —— 判定逻辑是确定性的规则, 不需要 LLM。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.join(
    os.path.expanduser("~"), "AppData", "Local", "hermes")
HERMES_BIN = os.path.join(HERMES_HOME, "bin")
STATE_DB = os.path.join(HERMES_HOME, "state.db")
CRON_DIR = os.path.join(HERMES_HOME, "cron")
# 机器可读的规则放项目里 (git 跟踪); profile 里那份是给人/agent 读的完整版
POLICY_FILE = os.path.join(PROJ, "config", "POLICY.md")
OUTBOX_FILE = os.path.join(PROJ, "logs", "outbox_pending.json")

# 规则 -> 检查器。检查器返回 (命中?, 说明文字, 严重度)
SEVERITY = {"blocked": "高", "cron-fail": "中", "db-size": "中",
             "backup-fail": "高", "gateway-down": "低"}

# iLink sendmessage session 失效的特征串。
# 沿用 push() 里一直用的那两句 —— 不另造花样, 换个措辞就漏判比漏判更糟。
# 命中它 = "等用户先发一条消息", 退避/重试都救不回来。
SESSION_DEAD_MARKERS = ("session not ready", "prepare failed")


def _env():
    env = os.environ.copy()
    env["PATH"] = HERMES_BIN + os.pathsep + env.get("PATH", "")
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    return env


def _hermes(*args, timeout=120) -> tuple[int, str, str]:
    """跑 hermes 命令, 返回 (rc, stdout, stderr)。

    ⚠️ **stderr 绝不混进 stdout** —— 踩过两次的坑: 把"不是命令"的错误信息当成了
    模型输出, 于是从错误里抠出了 prompt 里的 JSON 示例, 看起来像"模型照抄示例",
    其实是根本没跑模型(只花 3 秒)。

    ⚠️ 返回值必须是三元组 —— 之前这里返回单个字符串, 却被按 (rc, out) 或
    (rc, out, err) 解包, 连炸两次。**改这个函数时, 所有调用点一起改。**
    """
    try:
        r = subprocess.run(["hermes", *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=timeout, env=_env())
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 125, "", f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------- 规则

def load_policy(path: str = POLICY_FILE) -> dict[str, str]:
    """读 POLICY.md, 返回 {规则ID: 说明}。

    格式: markdown 列表, 每行 `- [rule-id] 说明文字`。`#` 开头是注释。
    读不到文件 = 没有规则 = 保持安静 (不猜、不硬编码行为)。
    """
    rules: dict[str, str] = {}
    if not os.path.exists(path):
        return rules
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return rules
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("-") or line.startswith("---"):
            continue
        m = re.match(r"^-\s*\[([a-z0-9\-]+)\]\s*(.*)$", line, re.I)
        if m:
            rules[m.group(1).lower()] = m.group(2).strip()
    return rules


# ---------------------------------------------------------------- 状态源

def check_blocked_cards() -> tuple[bool, str]:
    """看板上有没有卡卡在 blocked / failed。"""
    rc, out, _err = _hermes("kanban", "list", "--json", timeout=90)
    if rc != 0 or not out:
        return False, ""
    try:
        tasks = json.loads(out)
    except json.JSONDecodeError:
        return False, ""
    bad = [t for t in tasks if str(t.get("status", "")).lower() in ("blocked", "failed")]
    if not bad:
        return False, ""
    lines = [f"  {t.get('title', '?')[:50]} [{t.get('status')}] {t.get('id', '')}"
             for t in bad[:5]]
    return True, f"{len(bad)} 张卡卡住了:\n" + "\n".join(lines)


def check_cron_failures() -> tuple[bool, str]:
    """定时任务最近一次是不是失败了。"""
    jobs_file = os.path.join(CRON_DIR, "jobs.json")
    if not os.path.exists(jobs_file):
        return False, ""
    try:
        with open(jobs_file, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False, ""
    items = data.get("jobs", data) if isinstance(data, dict) else data
    if isinstance(items, dict):
        items = list(items.values())
    bad = []
    for j in items if isinstance(items, list) else []:
        status = str(j.get("last_status", j.get("status", ""))).lower()
        if status in ("error", "failed", "failure"):
            bad.append(j)
    if not bad:
        return False, ""
    lines = [f"  {j.get('name', j.get('id', '?'))[:40]}" for j in bad[:5]]
    return True, f"{len(bad)} 个定时任务失败:\n" + "\n".join(lines)


def check_db_size(limit_mb: int = 500) -> tuple[bool, str]:
    """会话库是不是胖到该提醒了。"""
    if not os.path.exists(STATE_DB):
        return False, ""
    mb = os.path.getsize(STATE_DB) / 1024 / 1024
    if mb <= limit_mb:
        return False, ""
    return True, f"会话库 {mb:.0f} MB, 超过 {limit_mb} MB 阈值 (该清理索引了)"


def check_backup_failure() -> tuple[bool, str]:
    """最近一次备份有没有失败。"""
    jobs_file = os.path.join(CRON_DIR, "jobs.json")
    if not os.path.exists(jobs_file):
        return False, ""
    try:
        with open(jobs_file, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False, ""
    items = data.get("jobs", data) if isinstance(data, dict) else data
    if isinstance(items, dict):
        items = list(items.values())
    for j in items if isinstance(items, list) else []:
        name = str(j.get("name", ""))
        if "备份" not in name:
            continue
        status = str(j.get("last_status", j.get("status", ""))).lower()
        if status in ("error", "failed", "failure"):
            return True, f"备份任务失败: {name[:50]}"
        last = j.get("last_run_at") or j.get("last_run")
        if last:
            return False, ""
    return False, ""


def check_gateway_alive(stale_sec: int = 180) -> tuple[bool, str]:
    """gateway 心跳还在不在 —— 它死了前台就答不了话。

    判据用 `state/gateway.heartbeat` 的 mtime (Hermes 每 tick 刷新一次),
    不用旧 .pid, 也不用 `hermes kanban list` (那是 CLI, 跟 gateway 死活无关)。
    """
    hb = os.path.join(HERMES_HOME, "state", "gateway.heartbeat")
    if not os.path.exists(hb):
        return True, "gateway 心跳文件都没了 —— gateway 可能根本没起来"
    age = time.time() - os.path.getmtime(hb)
    if age <= stale_sec:
        return False, ""
    return True, f"gateway 心跳 {age / 60:.0f} 分钟没动了 (阈值 {stale_sec}s) —— gateway 可能卡死"


CHECKERS = {
    "blocked": check_blocked_cards,
    "cron-fail": check_cron_failures,
    "db-size": check_db_size,
    "backup-fail": check_backup_failure,
    "gateway-down": check_gateway_alive,
}


# ---------------------------------------------------------------- 评估

def assess(rules: dict[str, str]) -> list[dict]:
    """跑每条规则的检查器, 返回命中的项。

    规则里没写的检查器 = 不启用 (用户说了算, 不是代码说了算)。
    """
    hits = []
    for rid in rules:
        fn = CHECKERS.get(rid)
        if fn is None:
            continue
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001 - 体检脚本不该因为一个源崩掉
            hits.append({"rule": rid, "hit": False, "detail": f"检查器异常: {exc}",
                         "severity": "低"})
            continue
        if ok:
            hits.append({"rule": rid, "hit": True, "detail": detail,
                         "severity": SEVERITY.get(rid, "中")})
    return hits


def build_panel(hits: list[dict], rules: dict[str, str]) -> dict:
    """统一视图 —— 这就是 Super-agent 的 Panel 等价物。"""
    need_me = [h for h in hits if h["hit"]]
    return {
        "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "启用规则": len(rules),
        "命中规则": len(need_me),
        "待你处理": need_me,
        "安静": not need_me,
    }


def render_text(panel: dict) -> str:
    """给人看的简报。"""
    if panel["安静"]:
        return "怀恩简报: 一切正常, 无需打扰。"
    lines = [f"怀恩简报 ({panel['生成时间']}) — {panel['命中规则']} 件事需要你看:"]
    for i, h in enumerate(panel["待你处理"], 1):
        lines.append(f"{i}. [{h['severity']}] {h['rule']}")
        for dl in (h["detail"] or "").splitlines():
            lines.append(f"   {dl}")
    return "\n".join(lines)


def _board_tasks() -> list[dict]:
    """看板上的所有卡 (含已归档的, 因为 archived 的卡也可能卡着)。"""
    rc, out, _err = _hermes("kanban", "list", "--json", timeout=120)
    if rc != 0 or not out:
        return []
    try:
        tasks = json.loads(out)
    except json.JSONDecodeError:
        return []
    return tasks if isinstance(tasks, list) else []


def _parents_of(card_id: str) -> list[str]:
    """取某张卡的父卡 id。

    ⚠️ `hermes kanban show <id> --json` 的输出是 `{"task": {...}}`, **里面没有 parents
    字段** —— parents 只出现在人类可读输出里那一行 `parents:   t_xxx, t_yyy`。
    所以这里读文本行, 不去抠 JSON。
    """
    rc, out, _err = _hermes("kanban", "show", card_id, timeout=90)
    if rc != 0:
        return []
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("parents:"):
            return re.findall(r"t_[0-9a-f]{6,}", s)
    return []


def find_releasable_cards() -> list[dict]:
    """找出"只等上游、上游已经完成"的 blocked 卡。

    Super-agent 的规则: *"A new human message releases a `blocked` or `failed`
    Ticket to its Executor"* —— 上游齐了就该把卡放出去, 不该一直卡着。

    **判据 (故意的窄, 宁可漏解不可误解)**:
    - 必须有 parents (等的是上游产出)
    - 所有 parents 都必须是 done/archived
    - **没有 parents 的 blocked 卡一律不动** —— 那种是用户自己要拍板的决策卡,
      自动解开等于替他做决定。
    """
    tasks = _board_tasks()
    blocked = [t for t in tasks if str(t.get("status", "")).lower() == "blocked"]
    releasable = []
    for t in blocked:
        cid = t.get("id")
        parents = _parents_of(cid)
        if not parents:
            continue                      # 没人可等的 = 等人拍板 = 不动
        states = {x.get("id"): str(x.get("status", "")).lower() for x in tasks}
        if all(states.get(p) in ("done", "archived") for p in parents):
            releasable.append({"id": cid, "title": t.get("title"),
                               "assignee": t.get("assignee"), "parents": parents})
    return releasable


def release_stuck_cards(reason: str = "上游产出已就位, 自动释放") -> int:
    """把等上游的 blocked 卡放出去。返回释放了几张。"""
    releasable = find_releasable_cards()
    if not releasable:
        return 0
    ids = [c["id"] for c in releasable]
    print(f"  释放 {len(ids)} 张等上游的卡: {ids}")
    rc, out, _err = _hermes("kanban", "unblock", *ids, "--reason", reason, timeout=180)
    print(f"  unblock rc={rc} {out[:200]}")
    if rc != 0:
        return 0
    return len(ids)


# ---------------------------------------------------------------- 推送

def _outbox_load() -> list:
    if not os.path.exists(OUTBOX_FILE):
        return []
    try:
        with open(OUTBOX_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return []


def _outbox_save(items: list) -> None:
    os.makedirs(os.path.dirname(OUTBOX_FILE), exist_ok=True)
    with open(OUTBOX_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def outbox_list() -> list[dict]:
    """outbox 里积压的条目 (dry 预览用)。文件坏了就当空的。"""
    items = _outbox_load()
    return items if isinstance(items, list) else []


def outbox_pending_count() -> int:
    """outbox 里现在压着几条 (等用户发过消息之后补发)。"""
    return len(outbox_list())


def _is_session_dead(blob: str) -> bool:
    """这段输出是不是"iLink session 失效"。

    只认 SESSION_DEAD_MARKERS 那两句 —— 换别的措辞去猜, 漏判比误判更坑。
    """
    return any(m in blob for m in SESSION_DEAD_MARKERS)


def _send_text(text: str, target: str, timeout: int = 180) -> tuple[int, str]:
    """发一条文字, 返回 (rc, 输出)。**不动 outbox** —— 补发的去留由调用方决定。"""
    try:
        r = subprocess.run(["hermes", "send", text, "-t", target],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=_env())
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 1, str(exc)
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def resolve_target(target: str | None = None) -> str:
    """把目标算出来: 显式给了就用, 没给就找微信私聊。"""
    if target:
        return target
    return f"weixin:{_find_wechat()}"


def push(text: str, target: str | None = None, voice: bool = False) -> int:
    """推微信。失败了留进 outbox 等下次 (Super-agent 的 Outbox 思路)。

    微信 iLink 的 sendmessage session 有寿命, 连发约 3 条就废, 退避无效。
    所以推送失败不是重试的问题, 是"等用户先发一条消息"的问题 —— 那就存着。
    """
    if target is None:
        target = f"weixin:{_find_wechat()}"
    cmd = ["hermes", "send", text, "-t", target]
    if voice:
        silk = _make_voice(text)
        if silk:
            cmd = ["hermes", "send", "-f", silk, "-t", target]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180, env=_env())
    except (subprocess.TimeoutExpired, OSError) as exc:
        _outbox_save(_outbox_load() + [{"text": text, "at": datetime.now().isoformat(),
                                        "err": str(exc)}])
        print(f"  推送失败, 已存 outbox: {exc}")
        return 1
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    print(f"  hermes send -> rc={r.returncode} {out[:150] or err[:150]}")
    if r.returncode != 0:
        blob = out + err
        if _is_session_dead(blob):
            _outbox_save(_outbox_load() + [{"text": text, "at": datetime.now().isoformat(),
                                            "err": "iLink session 失效, 等用户先发一条消息"}])
            print("  ⚠️ iLink session 失效 —— 退避没用, 必须用户先在微信发一条消息。")
            print("     这条已存进 outbox, 下次他发过话之后再补发。")
    return r.returncode


def flush_outbox_preview(target: str | None = None) -> int:
    """dry 模式: 只报"会补发什么", **一个都不真发**。返回会补发的条数。

    跟 flush_outbox 分开是因为"看"和"发"是两件事 —— dry 绝不能有副作用。
    """
    items = outbox_list()
    if not items:
        print("  outbox 是空的, 没东西可补发")
        return 0
    tgt = resolve_target(target)
    print(f"  [dry] 会补发 {len(items)} 条 -> {tgt}")
    for item in items:
        if not isinstance(item, dict):
            continue
        print(f"    - {str(item.get('text') or '')[:60]}")
    return len(items)


def flush_outbox(target: str | None = None) -> int:
    """把 outbox 里积压的消息补发出去, 返回**成功条数**。

    **只在用户刚发过微信消息之后调用** —— iLink 的 sendmessage session 有寿命,
    session 没活过来之前补发也是白撞 (退避/重试都无效, 那是 push() 里写的坑)。

    发成功的从队列里去掉; 发失败的**留在队列里**, 并把失败原因写进 err。
    session 失效了就直说 —— 认特征串, 说人话, 别让用户以为补发坏了。
    """
    items = outbox_list()
    if not items:
        print("  outbox 是空的, 没东西可补发")
        return 0
    tgt = resolve_target(target)
    if not tgt or tgt == "weixin:":
        print("  找不到微信 chat_id —— 用户先在微信发一条消息, 才有地方补发")
        return 0
    remaining: list[dict] = []
    sent = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            print("  (跳过一条空消息)")
            continue
        rc, out = _send_text(text, tgt)
        if rc == 0:
            sent += 1
            print(f"  ✅ 已补发: {text[:60]}")
            continue
        if _is_session_dead(out):
            item["err"] = "iLink session 失效, 等用户先发一条消息"
            print(f"  ⚠️ 补发失败 (session 失效): {text[:40]}")
        else:
            item["err"] = out[:200] or f"hermes send rc={rc}"
            print(f"  ❌ 补发失败: {text[:40]} —— {item['err'][:80]}")
        remaining.append(item)
    _outbox_save(remaining)
    print(f"  补发完成: 成功 {sent} 条, 还剩 {len(remaining)} 条")
    return sent


def _find_wechat() -> str:
    """找微信 chat_id (优先 DM)。"""
    if not os.path.exists(STATE_DB):
        return ""
    try:
        con = sqlite3.connect(STATE_DB)
        rows = con.execute("SELECT entry_json FROM gateway_routing").fetchall()
        con.close()
    except sqlite3.Error:
        return ""
    fallback = None
    for (blob,) in rows:
        try:
            d = json.loads(blob)
        except (json.JSONDecodeError, TypeError):
            continue
        o = d.get("origin") or {}
        if (o.get("platform") or d.get("platform")) != "weixin":
            continue
        cid = o.get("chat_id") or d.get("chat_id")
        if not cid:
            continue
        if (o.get("chat_type") or d.get("chat_type")) == "dm":
            return cid
        fallback = fallback or cid
    return fallback or ""


def _make_voice(text: str) -> str | None:
    """生成 .silk 语音 (微信只认 .silk)。

    走 Hermes 的 TTS, 再用 pilk 编码。pilk 不在就老实返回 None —— 不装野包。
    """
    try:
        import pilk  # noqa: F401
    except ImportError:
        print("  (没装 pilk, 跳过语音)")
        return None
    out_dir = os.path.join(PROJ, "logs", "voice")
    os.makedirs(out_dir, exist_ok=True)
    mp3 = os.path.join(out_dir, f"brief_{int(datetime.now().timestamp())}.mp3")
    r = subprocess.run(["hermes", "tts", "--output", mp3, text],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=180, env=_env())
    if not os.path.exists(mp3):
        print(f"  (TTS 没出文件: {(r.stderr or r.stdout or '')[:120]})")
        return None
    silk = mp3[:-4] + ".silk"
    try:
        import pilk
        pilk.encode(mp3, silk, rate=16000)
    except Exception as exc:  # noqa: BLE001
        print(f"  (silk 编码失败: {exc})")
        return None
    return silk


def main() -> int:
    ap = argparse.ArgumentParser(description="怀恩 管家大脑")
    ap.add_argument("--dry", action="store_true", help="只看会说什么, 不真推")
    ap.add_argument("--json", action="store_true", help="输出机器可读的待处理清单")
    ap.add_argument("--push", action="store_true", help="真推微信")
    ap.add_argument("--voice", action="store_true", help="同时推一条 .silk 语音")
    ap.add_argument("--rules", action="store_true", help="只看当前生效的规则")
    ap.add_argument("--release", action="store_true",
                    help="释放'只等上游、上游已完成'的 blocked 卡")
    ap.add_argument("--flush", action="store_true", help="补发 outbox 里积压的微信")
    ap.add_argument("--to", help="目标, 如 weixin:<chat_id>")
    args = ap.parse_args()

    if args.flush:
        # 补发是独立动作: 用户发过消息之后才跑, 或者直接调 scripts/flush_outbox.py
        if args.dry:
            flush_outbox_preview(args.to)
            print(f"dry 模式 —— 没真发, 还压着 {outbox_pending_count()} 条")
            return 0
        sent = flush_outbox(args.to)
        print(f"补发成功 {sent} 条, 还压着 {outbox_pending_count()} 条")
        return 0

    if args.release:
        n = release_stuck_cards()
        print(f"释放了 {n} 张卡")
        return 0

    rules = load_policy()
    if args.rules:
        print(f"规则文件: {POLICY_FILE}")
        if not rules:
            print("  (没有规则 —— 管家会一直保持安静)")
        for rid, desc in rules.items():
            known = "✓" if rid in CHECKERS else "✗ 无对应检查器"
            print(f"  [{rid}] {known}  {desc}")
        return 0

    hits = assess(rules)
    panel = build_panel(hits, rules)

    if args.json:
        print(json.dumps(panel, ensure_ascii=False, indent=2))
        return 0

    text = render_text(panel)
    print(text)

    if args.push and not args.dry:
        pending = _outbox_load()
        if pending:
            print(f"\noutbox 里还压着 {len(pending)} 条 (用户发过消息后可以补发)")
        rc = push(text, args.to, args.voice)
        return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
