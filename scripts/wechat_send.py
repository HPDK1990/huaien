"""怀恩 — 微信发送工具 (带节流、重试、明确的失败原因)

## 为什么不用 `hermes send` 就完事

因为 iLink 的 **sendmessage session 有寿命**: 用户在微信发过一条之后, bot 大约能
连着发 3 条, 之后每条都返回:

    ret=-2 errcode=None errmsg="prepare failed — the user must send the bot
    a message first (or re-pair)"

**退避救不回来** (实测等 45 秒 / 20 秒 / 20 秒连试三次, 全是 -2)。
必须等**用户在微信里再发一条消息**, 那条消息会带新的 context_token, session 才复活。

**另一个硬约束**: 微信 adapter 的 `_send_file` 是**一个附件一条消息**
(`item_lists` 逐条发), 所以"一条推送带图+视频+文件"会变成多条消息 ->
更容易撞上这个限制。

## 用法

```bash
python scripts/wechat_send.py --to weixin:<chat_id> "文字"
python scripts/wechat_send.py --to weixin:<chat_id> "文字" --media a.png b.mp4
python scripts/wechat_send.py --to weixin:<chat_id> --dry-run "文字"
```

**间隔**默认 8 秒 (可用 `--interval` 调)。**失败时会明说该做什么**:
"请先在微信里发一条消息, session 就活了"。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import time
from datetime import datetime

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.join(
    os.path.expanduser("~"), "AppData", "Local", "hermes")
HERMES_BIN = os.path.join(HERMES_HOME, "bin")

# iLink 的 session 失效特征 —— 不同版本的 errmsg 措辞略有差别, 都按失效处理
SESSION_DEAD = re.compile(
    r"session not ready|prepare failed|the user must send the bot|re-pair", re.I)
RATE_LIMIT = re.compile(r"ret=-2|frequency limit|rate limit", re.I)


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def _env() -> dict:
    e = os.environ.copy()
    e["PATH"] = HERMES_BIN + os.pathsep + e.get("PATH", "")
    return e


def send(target: str, message: str, timeout: int = 180) -> tuple[int, str]:
    """发一条。返回 (rc, 输出)。"""
    try:
        r = subprocess.run(["hermes", "send", "-t", target, message],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout,
                           cwd=PROJ, env=_env())
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except Exception as e:
        return 125, f"{type(e).__name__}: {e}"


def diagnose(out: str) -> str:
    """把失败翻译成用户能照做的下一步。"""
    if SESSION_DEAD.search(out):
        return ("iLink session 已失效 —— **请先在微信里发一条消息**, session 就活了。\n"
                "         (bot 连发约 3 条后必然触发; 退避无效, 必须用户先发)")
    if RATE_LIMIT.search(out):
        return "iLink 频率限制 —— 拉长 --interval 再试。"
    return out.strip()[:400]


def main() -> int:
    ap = argparse.ArgumentParser(description="带节流的微信发送")
    ap.add_argument("--to", required=True, help="platform:chat_id")
    ap.add_argument("message", help="消息文本 (可含 MEDIA:/path)")
    ap.add_argument("--media", nargs="*", default=[], help="附件路径 (每个一条消息)")
    ap.add_argument("--interval", type=float, default=8.0, help="两条之间的间隔秒数")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.dry_run:
        log(f"DRY RUN -> {args.to}")
        log(f"  1. 文字: {args.message[:120]}")
        for m in args.media:
            log(f"  2. 附件: {m} ({os.path.getsize(m) if os.path.exists(m) else '缺失'} B)")
        return 0

    sent, failed = 0, 0
    plan = [args.message] + [f"MEDIA:{m}" for m in args.media]
    for i, part in enumerate(plan):
        if i:
            log(f"等待 {args.interval:.0f}s (iLink 连发约 3 条就废, 必须留间隔)")
            time.sleep(args.interval)
        rc, out = send(args.to, part)
        if rc == 0:
            sent += 1
            log(f"✅ 发出 {i+1}/{len(plan)}: {part[:80]}")
        else:
            failed += 1
            log(f"❌ 失败 {i+1}/{len(plan)}: {diagnose(out)}")
            # session 失效时后面几条也发不出去 —— 立刻停, 别继续撞
            if SESSION_DEAD.search(out):
                log("session 已死, 停止剩余发送 (继续发只会连续失败)")
                break

    log(f"完成: 成功 {sent}, 失败 {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
