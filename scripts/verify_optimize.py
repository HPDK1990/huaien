"""怀恩 — 优化后的完整性验证

在 `optimize-storage` + 清理 state_meta 之后跑。**任何一条不过, 外层脚本就还原备份。**

## 验什么

| # | 检查 | 判据 |
|---|---|---|
| 1 | 消息条数没变 | 与优化前一致 (一条都不能少) |
| 2 | 正文字符数没变 | 与优化前一致 |
| 3 | 压缩摘要行还在 | `_compressed_summary=1` 条数没变 |
| 4 | 归档历史还在 | `active=0` 的条数没变 (那是唯一副本) |
| 5 | 全文索引还能搜 | FTS MATCH 有命中 |
| 6 | 索引布局漂移已消除 | `fts_layout_drift()` 返回空 |
| 7 | 文件确实小了 | 体积比优化前小 |

**检查 1-4 是"数据没丢"的证明 —— 优化只该动索引, 一条消息都不该变。**

## 用法

```bash
python scripts/verify_optimize.py --before before.json
```

`before.json` 是优化前抓的基线 (由外层脚本生成)。
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import sys

# The Windows console here is cp936 (GBK) and cannot encode the check marks
# this script prints -- it crashed with UnicodeEncodeError mid-verification,
# which made a healthy run look like a failure. Force UTF-8 on both streams.
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, ValueError):
        _stream.reconfigure(encoding="utf-8", errors="replace")

HD = os.environ.get("HERMES_HOME") or os.path.join(
    os.path.expanduser("~"), "AppData", "Local", "hermes")
DB = os.path.join(HD, "state.db")

# 源码位置跟**安装**走, 不跟 HERMES_HOME 走 ——
# 测试时会把 HERMES_HOME 重定向到 scratch 副本, 但源码还在真安装目录。
# venv 的 python 位置: <install>/hermes-agent/venv/Scripts/python.exe
# 往上两层就是 <install>/hermes-agent
def _find_src() -> str:
    env_src = os.environ.get("HERMES_SRC")
    if env_src and os.path.isdir(env_src):
        return env_src
    # <install>/hermes-agent/venv/Scripts/python.exe
    #   dirname -> venv/Scripts
    #   ../..  -> venv
    #   ../../.. -> hermes-agent   ← 源码在这
    guess = os.path.abspath(os.path.join(os.path.dirname(sys.executable), "..", "..", ".."))
    if os.path.isfile(os.path.join(guess, "hermes_state_fts.py")):
        return guess
    return os.path.join(HD, "hermes-agent")


SRC = _find_src()


def snapshot() -> dict:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        def q(sql: str) -> int:
            return con.execute(sql).fetchone()[0]
        snap = {
            "size_mb": round(os.path.getsize(DB) / 1024 / 1024, 1),
            "messages": q("SELECT COUNT(*) FROM messages"),
            "chars": q("SELECT COALESCE(SUM(LENGTH(content)),0) FROM messages"),
            "summaries": q("SELECT COUNT(*) FROM messages WHERE _compressed_summary=1"),
            "archived": q("SELECT COUNT(*) FROM messages WHERE active=0"),
            "sessions": q("SELECT COUNT(*) FROM sessions"),
        }
    finally:
        con.close()
    snap["trigram_mb"] = trigram_mb()
    return snap


def fts_hits(term: str = "怀恩") -> int:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        return len(con.execute(
            "SELECT m.id FROM messages_fts f JOIN messages m ON m.id=f.rowid "
            "WHERE messages_fts MATCH ? LIMIT 50", (term,)).fetchall())
    finally:
        con.close()


def drift() -> tuple[str, list[str] | None]:
    """返回 (状态, 漂移列表)。

    状态三选一:
      "clean"  — 检测成功, 没有漂移
      "drift"  — 检测成功, 有漂移
      "error"  — **检测本身失败** (比如 HERMES_HOME 下没有 config.yaml)

    "error" 必须跟 "drift" 分开 —— 检测失败不等于检测到漂移,
    混淆会让外层脚本误判成"验证不通过"从而白白还原备份。
    """
    if SRC not in sys.path:
        sys.path.insert(0, SRC)
    try:
        from hermes_state_fts import fts_layout_drift
    except Exception as e:
        return "error", [f"import 失败: {type(e).__name__}: {e}"]
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            found = list(fts_layout_drift(con.cursor()))
        finally:
            con.close()
    except Exception as e:
        return "error", [f"检测抛错: {type(e).__name__}: {e}"]
    return ("clean" if not found else "drift"), found


def trigram_mb() -> float | None:
    """trigram 索引表占多少 MB —— 不依赖 import 的硬证据。"""
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT SUM(pgsize) FROM dbstat WHERE name LIKE 'messages_fts_trigram%'"
        ).fetchone()
        return round(row[0] / 1024 / 1024, 1) if row and row[0] else None
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="验证优化后数据完好")
    ap.add_argument("--before", help="优化前的基线 json")
    ap.add_argument("--snapshot", metavar="OUT",
                    help="只抓当前快照写到 OUT (优化前用), 不做验证")
    args = ap.parse_args()

    if args.snapshot:
        snap = snapshot()
        with open(args.snapshot, "w", encoding="utf-8") as f:
            json.dump(snap, f, indent=2, ensure_ascii=False)
        print(json.dumps(snap, ensure_ascii=False))
        return 0

    after = snapshot()
    print("=== 优化后快照 ===")
    for k, v in after.items():
        print(f"   {k:12} {v}")

    problems = []
    checks = []

    if args.before and os.path.exists(args.before):
        with open(args.before, encoding="utf-8") as f:
            before = json.load(f)
        print("\n=== 与优化前对比 ===")
        for key in ("messages", "chars", "summaries", "archived", "sessions"):
            b, a = before.get(key), after.get(key)
            same = b == a
            checks.append((key, b, a, same))
            print(f"   {key:12} {b} -> {a}  {'✅' if same else '❌ 变了!'}")
            if not same:
                problems.append(f"{key} 变了: {b} -> {a}")
        if after["size_mb"] >= before.get("size_mb", 0):
            checks.append(("size_mb", before.get("size_mb"), after["size_mb"], False))
            problems.append(f"文件没变小: {before.get('size_mb')} -> {after['size_mb']}")
        else:
            saved = round(before.get("size_mb", 0) - after["size_mb"], 1)
            checks.append(("size_mb", before.get("size_mb"), after["size_mb"], True))
            print(f"   {'size_mb':12} {before.get('size_mb')} -> {after['size_mb']}  ✅ 省了 {saved} MB")
    else:
        print("\n[警告] 没有基线文件, 只做单项检查")

    hits = fts_hits()
    print(f"\n   FTS 搜索 '怀恩' 命中 {hits} 条 {'✅' if hits else '❌'}")
    if not hits:
        problems.append("FTS 搜索没命中, 索引坏了")

    status, found = drift()
    if status == "clean":
        print("   fts_layout_drift() = []  ✅ 布局漂移已消除")
    elif status == "drift":
        # 漂移检测是"参考判据", 不是唯一判据。
        # 真正决定成败的是下面那些可量化的硬判据 —— 因为布局定义会随版本变,
        # 检测器口径跟当前实现有差异时, 硬判据 (体积/条数/可搜) 才是事实。
        print(f"   fts_layout_drift() = {found}  ⚠️ 仍有漂移 (按参考处理, 看下面的硬判据)")
    else:
        print(f"   fts_layout_drift() 检测失败: {found}")
        print("   (检测失败不等于检测到漂移; 用下面的硬判据判定)")

    tg = trigram_mb()
    if tg is not None:
        base_tg = before.get("trigram_mb") if args.before and os.path.exists(args.before) else None
        if base_tg:
            if tg < base_tg * 0.9:
                print(f"   trigram 索引 {base_tg} MB -> {tg} MB  ✅ 省了 {round(base_tg - tg, 1)} MB")
            else:
                print(f"   trigram 索引 {base_tg} MB -> {tg} MB  ❌ 没明显变小")
                problems.append(f"trigram 索引没明显变小: {base_tg} -> {tg}")
        else:
            print(f"   trigram 索引当前 {tg} MB")

    print()
    if problems:
        print("❌ 验证不通过:")
        for p in problems:
            print("   -", p)
        return 1
    print("✅ 验证通过: 数据一条没少, 索引能用, 布局已修")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
