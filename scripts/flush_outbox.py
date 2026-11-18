"""怀恩 — 补发 outbox 里积压的微信 (独立入口)

## 这是干什么的

`scripts/butler.py` 推微信失败时会把消息存进 `logs/outbox_pending.json`,
但**光存着没用** —— 得有人在他 session 恢复之后把它们发出去。这个脚本就是那个人。

微信 iLink 的 `sendmessage` session 有寿命: bot 连发约 3 条就废, 报

    ret=-2 prepare failed - the user must send the bot a message first

**退背无效**, 必须用户自己在微信里发一条消息, 那条消息带新的 context_token,
session 才复活。所以补发只在"他刚说过话"之后才有意义 —— cron 里挂这个脚本,
失败就失败着, 消息一直留在 outbox 里等下一次机会。

## 用法

```bash
python scripts/flush_outbox.py --dry        # 只看会补发什么, 不真发
python scripts/flush_outbox.py              # 真补发
python scripts/flush_outbox.py --to weixin:<chat_id>   # 指定目标
```

cron 里挂这个就行 (隔几分钟一次也无所谓, session 没活过来就一条都发不出去):

```
*/10 * * * * cd /e/Hermes-HuaiEn && .venv/Scripts/python.exe scripts/flush_outbox.py
```
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(SCRIPTS)
_BUTLER = os.path.join(SCRIPTS, "butler.py")

if not os.path.exists(_BUTLER):
    raise ImportError(f"找不到管家实现: {_BUTLER}")

# 直接按文件加载 scripts/butler.py —— 逻辑真身只有一份, 不复制第二套
_spec = importlib.util.spec_from_file_location("huaien_butler_impl", _BUTLER)
butler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(butler)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="怀恩 补发 outbox 积压的微信")
    ap.add_argument("--dry", action="store_true", help="只看会补发什么, 不真发")
    ap.add_argument("--to", help="目标, 如 weixin:<chat_id>")
    args = ap.parse_args(argv)

    pending = butler.outbox_pending_count()
    if not pending:
        print("outbox 是空的, 没东西可补发。")
        return 0

    if args.dry:
        butler.flush_outbox_preview(args.to)
        print(f"dry 模式 —— 没真发。outbox 里还压着 {pending} 条。")
        return 0

    sent = butler.flush_outbox(args.to)
    left = butler.outbox_pending_count()
    print(f"补发成功 {sent} 条, 还压着 {left} 条。")
    if left:
        print("  (session 还没活 —— 等用户在微信发一条消息, 下次再来补)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
