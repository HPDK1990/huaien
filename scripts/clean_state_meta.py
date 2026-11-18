"""怀恩 — 清理 state_meta 里的网关回执垃圾

## 是什么

`state_meta` 表里有一批 `gateway.admission.result.v1.<hash>` 键, 每行存着
**网关对某一轮对话的准入判定结果快照** (完整回复 + 工具调用记录)。

本机实测: 186 行 / 1477 万字符 / 16 MB, **全部是这一类键**。

## 能不能删

**能。** 它们是**可再生的缓存** —— 网关下一次做同样的准入判定时会重新生成。
删掉不影响:
- 任何对话内容 (`messages` 表完全不动)
- 搜索 (FTS 索引不依赖 state_meta)
- 会话列表

源码路径: `hermes_state_dbfile.py` / gateway admission 相关模块。

## 但有个时序要求

**必须先停 gateway**, 否则网关正在往里写, 删了它会立刻又写回去;
更糟的是正在进行的 turn 的回执被删掉 → 那一轮无法重放。

所以这个脚本**只做删除**, 停 gateway 的事交给外层的 bat。

## 用法

```bash
python scripts/clean_state_meta.py            # dry-run, 只报不删 (默认)
python scripts/clean_state_meta.py --apply    # 真删
```

**默认 dry-run** —— 用户的"危险操作铁律": 不删不改除非明确说。
"""
from __future__ import annotations

import argparse
import contextlib
import os
import sqlite3
import sys

# cp936 console cannot encode this script's Chinese output.
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, ValueError):
        _stream.reconfigure(encoding="utf-8", errors="replace")

HD = os.environ.get("HERMES_HOME") or os.path.join(
    os.path.expanduser("~"), "AppData", "Local", "hermes")
DB = os.path.join(HD, "state.db")

# 只删这一类键。别的键 (fts 重建进度标记等) 一个都不碰。
PATTERN = "gateway.admission.result.v1.%"


def main() -> int:
    ap = argparse.ArgumentParser(description="清理 state_meta 网关回执")
    ap.add_argument("--apply", action="store_true", help="真删 (默认只 dry-run)")
    args = ap.parse_args()

    if not os.path.exists(DB):
        print(f"找不到 {DB}")
        return 1

    con = sqlite3.connect(DB)
    try:
        n = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(COALESCE(value,''))),0) "
            "FROM state_meta WHERE key LIKE ?", (PATTERN,)).fetchone()
        total_rows = con.execute("SELECT COUNT(*) FROM state_meta").fetchone()[0]
        other = total_rows - n[0]
        print(f"state_meta 总行数      : {total_rows}")
        print(f"网关回执行 (可再生)     : {n[0]} 行, {n[1]} 字符")
        print(f"其它键 (不动)           : {other} 行")

        if not args.apply:
            print("\n[dry-run] 加上 --apply 才真删")
            return 0

        if n[0] == 0:
            print("\n没有可删的, 跳过")
            return 0

        con.execute("DELETE FROM state_meta WHERE key LIKE ?", (PATTERN,))
        con.commit()
        left = con.execute(
            "SELECT COUNT(*) FROM state_meta WHERE key LIKE ?", (PATTERN,)).fetchone()[0]
        print(f"\n已删。剩余回执行: {left}")
        # 不做 VACUUM —— 空间回收交给 optimize-storage 的最后一步,
        # 免得在同一个进程里做两件重活。
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
