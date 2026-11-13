#!/usr/bin/env python3
"""怀恩对外说话的统一入口 —— 一条命令把消息推到用户的微信。

## 为什么需要它

怀恩要告诉用户事情的路有两条:

| 路 | 能不能用 | 原因 |
|---|---|---|
| cron 的 `deliver=weixin` | **不能** | huaien profile 的 scoped secret 里没有微信凭据 → "Weixin token missing" |
| `hermes send` (默认 profile 作用域) | **能** | 凭据可用 —— 盯梢任务每天就是这么发的 |

所以**所有要推微信的事都走这个脚本**, 不要给 cron 配 `deliver=weixin`。

## 用法

    python scripts/notify.py "孩子视频那批成片好了, 3 个 mp4"
    python scripts/notify.py --file report.md          # 从文件读
    python scripts/notify.py --max-chars 120 "很长的正文"
    python scripts/notify.py --no-dedup "每次都要发"    # 关掉去重

## 它保证什么

1. **噪音不过去** —— 内部通知块 (`[ASYNC DELEGATION COMPLETE]` /
   `[CONTEXT COMPACTION]` 之类) 一个字都不进用户手机; 剥完没内容就整条不发。
2. **不刷屏** —— 同一条内容默认 6 小时内只推一次 (`--no-dedup` 可关)。
3. **不丢** —— 微信 session 没就绪 (用户刚还没说过话) 时自动存进 outbox,
   由 cron「怀恩·微信补发」每 10 分钟重试, 用户下次说话后自动收到。
4. **不越界** —— 退出码: 0 已送达或已入队, 1 失败。

## 它不做什么

- 不做判断 (该不该报) —— 那是调用方的事
- 不读用户的凭据 —— 走 `hermes send`, 凭据由 Hermes 自己管
"""
from __future__ import annotations

import argparse
import os
import sys

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)

from scripts.butler import push  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="怀恩微信推送出口")
    ap.add_argument("text", nargs="?", help="要发的内容")
    ap.add_argument("--file", help="从文件读内容 (UTF-8)")
    ap.add_argument("--target", help="推送目标, 默认自动找用户的微信")
    ap.add_argument("--max-chars", type=int, default=0,
                    help="长度上限, 0 = 不截断 (只过滤噪音)")
    ap.add_argument("--dedup-hours", type=float, default=6.0,
                    help="同一条内容的去重窗口小时数, 0 = 不去重")
    ap.add_argument("--no-dedup", action="store_true", help="不去重")
    args = ap.parse_args()

    if args.file:
        with open(args.file, encoding="utf-8") as f:
            body = f.read()
    elif args.text:
        body = args.text
    else:
        ap.error("要么给内容, 要么给 --file")

    rc = push(
        body,
        target=args.target,
        max_chars=args.max_chars,
        dedup_hours=0.0 if args.no_dedup else args.dedup_hours,
    )
    if rc == 0:
        # 0 有两种含义: 送达, 或者"剥完是噪音, 什么都不该发"。
        # 两种都不是失败。
        print("✅ 已处理 (送达或已入队)")
    sys.exit(rc)
