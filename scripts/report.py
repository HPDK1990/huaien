"""怀恩 — 主动汇报

## 这是干什么的

用户要"怎么看任务和汇报"。被动问已经能用了 (前台直接问怀恩"任务到哪了")。
这个脚本解决另一半: **主动推**。

怀恩在后台干完一件长活, 主动把结果推到飞书 —— 不用守着屏幕。

## 底层靠什么

`hermes send` —— Hermes 自带的出站命令, 复用 gateway 的平台凭据:

```bash
hermes send "消息文字" -t feishu:<chat_id>
hermes send -f 报告.md -t feishu:<chat_id>     # 发文档
hermes send -f 图.png -t feishu:<chat_id>     # 发图片
```

实测 (2026-10-30): 文字 / 图片 / 文档 / 表格 **四种都发得出去**。

## 用法

```bash
python scripts/report.py --kanban            # 把看板里跑完的任务汇总推飞书
python scripts/report.py --say "干完了"        # 推一句自定义的话
python scripts/report.py --say "看这个" --file 报告.md   # 带文件
python scripts/report.py --kanban --dry       # 只看要发什么, 不真发
```

## chat_id 怎么来的

从 Hermes 自己的 `state.db` 的 `gateway_routing` 表里读
(用户在飞书跟怀恩说过话之后就有了)。找不到就让用户先发一条。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.join(
    os.path.expanduser("~"), "AppData", "Local", "hermes")
HERMES_BIN = os.path.join(HERMES_HOME, "bin")
STATE_DB = os.path.join(HERMES_HOME, "state.db")


def _env():
    env = os.environ.copy()
    env["PATH"] = HERMES_BIN + os.pathsep + env.get("PATH", "")
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    return env


def _home_channel(platform: str) -> str | None:
    """配置文件里写死的 home channel (FEISHU_HOME_CHANNEL / WEIXIN_HOME_CHANNEL)。"""
    cfg = os.path.join(HERMES_HOME, "config.yaml")
    if not os.path.exists(cfg):
        return None
    try:
        with open(cfg, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    # 字符类覆盖两种 id: 飞书 oc_xxx / 微信 o9cq80_xxx@im.wechat, 同时排除引号
    m = re.search(rf"{platform.upper()}_HOME_CHANNEL[\"\s:=]+([A-Za-z0-9_@.\-]+)", text)
    return m.group(1) if m else None


def _routed_chat(platform: str) -> str | None:
    """routing 表里用户跟怀恩说过话留下的 chat_id。优先 DM, 群聊容易发错地方。"""
    if not os.path.exists(STATE_DB):
        return None
    try:
        con = sqlite3.connect(STATE_DB)
        rows = con.execute("SELECT entry_json FROM gateway_routing").fetchall()
        con.close()
    except sqlite3.Error:
        return None

    fallback = None
    for (blob,) in rows:
        try:
            d = json.loads(blob)
        except (json.JSONDecodeError, TypeError):
            continue
        o = d.get("origin") or {}
        if (o.get("platform") or d.get("platform")) != platform:
            continue
        cid = o.get("chat_id") or d.get("chat_id")
        if not cid:
            continue
        if (o.get("chat_type") or d.get("chat_type")) == "dm":
            return cid
        fallback = fallback or cid
    return fallback


def find_chat(platform: str) -> str | None:
    """找某个平台的 chat_id。

    顺序:
      1. 配置里的 home channel (FEISHU_HOME_CHANNEL / WEIXIN_HOME_CHANNEL)
      2. gateway_routing 里用户跟怀恩说过话留下的 DM chat_id

    ⚠️ 实测坑: 设了 home channel 之后, `hermes send` **仍然要求显式 --to**
       (报 "--to PLATFORM[:channel[:thread]] is required") —— 那个配置对 send 不生效。
       所以本脚本自己解析目标, 不指望 hermes 帮我们省事。

    ⚠️ 微信的坑: iLink 的 sendmessage session 有寿命, 连发约 3 条就废
       (ret=-2 "prepare failed — the user must send the bot a message first")。
       退避无效, 必须用户先在微信发一条消息。所以主动推送微信要挑他刚说过话的时候。
    """
    return _home_channel(platform) or _routed_chat(platform)


def kanban_summary() -> tuple[str, bool]:
    """看板里最近跑完的活。返回 (文字, 有没有东西)。"""
    r = subprocess.run(["hermes", "kanban", "list"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=90, env=_env())
    lines = [ln.rstrip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    done = [ln for ln in lines if " done " in f" {ln} "]
    running = [ln for ln in lines if any(w in ln for w in (" running ", " ready ", " blocked "))]
    if not done and not running:
        return ("看板里没有活。", False)
    out = [f"看板汇报 ({datetime.now().strftime('%m-%d %H:%M')})"]
    if running:
        out.append(f"进行中 {len(running)} 件")
        for ln in running[:5]:
            out.append("  " + ln.strip()[:90])
    if done:
        out.append(f"已完成 {len(done)} 件")
        for ln in done[-5:]:
            out.append("  " + ln.strip()[:90])
    return ("\n".join(out), True)


def send(text: str, target: str, file_path: str | None = None, dry: bool = False) -> int:
    """走 hermes send 出站。"""
    cmd = ["hermes", "send"]
    if file_path:
        cmd += ["-f", file_path]
    else:
        cmd.append(text)
    cmd += ["-t", target]
    if dry:
        print("  [dry] 会执行:", " ".join(cmd))
        return 0
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=180, env=_env())
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    print(f"  hermes send -> rc={r.returncode} {out[:160] or err[:160]}")
    if r.returncode != 0 and "weixin" in target:
        blob = f"{out}\n{err}"
        if "session not ready" in blob or "prepare failed" in blob:
            print("  ⚠️ 微信的 iLink session 失效了 —— 退避没用, 必须用户先在微信里发一条消息。")
            print("     (bot 连发约 3 条后必然触发; 一问一答的回合里没问题)")
    return r.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description="怀恩 主动汇报")
    ap.add_argument("--kanban", action="store_true", help="把看板任务汇总推过去")
    ap.add_argument("--say", help="推一句自定义的话")
    ap.add_argument("--file", help="附带一个文件 (图片/文档)")
    ap.add_argument("--to", help="目标, 如 weixin:<chat_id> / feishu:oc_xxx")
    ap.add_argument("--platform", default="weixin",
                    help="目标平台 (默认 weixin; 用户的日常通道)")
    ap.add_argument("--dry", action="store_true", help="只看不发")
    args = ap.parse_args()

    if not (args.kanban or args.say or args.file):
        ap.print_help()
        return 0

    target = args.to
    if not target:
        cid = find_chat(args.platform)
        if not cid:
            print(f"没找到 {args.platform} 的 chat_id。")
            print(f"  办法: 先在 {args.platform} 里给机器人发一句话, 再重试。")
            print(f"  或者用 --to {args.platform}:<chat_id> 手动指定。")
            return 2
        target = f"{args.platform}:{cid}"
        print(f"  目标: {target}")

    text = ""
    if args.kanban:
        text, has = kanban_summary()
        print(f"  内容:\n{text}")
        if not has and not args.say:
            pass
    if args.say:
        text = (text + "\n" if text else "") + args.say

    if not text and not args.file:
        print("没内容可发。")
        return 2

    rc = send(text, target, args.file, args.dry)
    return rc


if __name__ == "__main__":
    sys.exit(main())
