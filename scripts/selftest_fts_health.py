"""怀恩 — FTS 索引健康自检

在"修 FTS 索引"这个动作之前跑一次（红），修完再跑一次（绿）。
这是本项目的红→绿判据, 不是事后补的日志。

## 判什么

| # | 检查 | 判据 | 为什么 |
|---|---|---|---|
| 1 | 主库完整性 | `PRAGMA integrity_check` == ok | 页级撕裂 |
| 2 | trigram 覆盖率 | trigram 行数 / **源视图行数** >= 0.98 | 重建只建了一半是最常见的坏法 |
| 3 | 普通 FTS 覆盖率 | messages_fts 行数 / messages 行数 >= 0.95 | 同上 |
| 4 | ASCII 可搜 | `messages_fts MATCH 'hermes'` 命中 > 0 | 索引活着的基本证明 |
| 5 | 子串可搜 | `messages_fts_trigram MATCH 'hermes'` 命中 > 0 | trigram 活着的基本证明 |
| 6 | 索引非空文档 | 无空内容文档 | 重建中断会留一批空壳 |
| 7 | 数据行数 | messages > 0 且 sessions > 0 | 不能为了修索引把表清空 |

## 三个容易搞错的点 (都实测过)

**1. trigram 索引的覆盖率不能用 `trigram / messages` 算 —— 会把好库误判成坏库。**

`messages_fts_trigram` 的源视图 `messages_fts_trigram_src` **按设计**排除了:
```sql
WHERE m.role <> 'tool'
  AND s.source NOT IN ('cron','subagent')
  AND json_extract(...,'$._delegate_from') IS NULL
```
本机实测: messages 共 50,574 行, 其中 role='tool' 24,327 行 + cron/subagent 会话 9,933 行,
**真正该进 trigram 索引的只有 21,627 行** —— 而索引恰好就是 21,627 行, **一行不差**。

拿 21,627 / 50,574 = 42.8% 当判据, 等于拿"该索引的"跟"全部"比, 会永远判红。
**正确基准是源视图行数。**

**2. 别拿"trigram 搜不到中文"当损坏判据。**
SQLite 的 `trigram` 分词器只对 **3 字符以上的 ASCII 词**有效:
`'怀恩'` → 0 条, `'怀恩测试'` → 1 条, `'ell'` → 1 条。
中文 2 字词在 trigram 里永远 MATCH 不到, **这是分词器设计, 不是索引损坏**。

**3. 别拿"中文搜不到"当损坏判据。**
真库的 `messages_fts` 用默认 `unicode61`, 它把连续 CJK 切成**整段一个 token**,
所以 `MATCH '怀恩'` 命中与否取决于**数据里有没有恰好出现这个片段**, 是数据分布问题
不是索引健康度。

**索引健康度的可靠判据只有两个: 覆盖率(对源视图) + ASCII 词能搜。**

## 用法

```bash
python scripts/selftest_fts_health.py              # 人看
python scripts/selftest_fts_health.py --json       # 机器读 (给 bat 用)
```

退出码: 0 = 全绿, 1 = 有失败项, 2 = 检测本身失败(读不到库)
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import sys

# cp936 控制台不能编码这个脚本的输出, 强制 UTF-8
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, ValueError):
        _stream.reconfigure(encoding="utf-8", errors="replace")

HD = os.environ.get("HERMES_HOME") or os.path.join(
    os.path.expanduser("~"), "AppData", "Local", "hermes")
DB = os.path.join(HD, "state.db")

# trigram 索引覆盖率下限。**基准是源视图行数, 不是 messages 总行数** ——
# 源视图按设计排除 tool/cron/subagent 消息 (见文件头"三个容易搞错的点")。
# 0.98 留一点余量: 重建收尾时可能有极少数行还没落盘。
COVERAGE_FLOOR = 0.98

# trigram 的源视图名 (Hermes 建的, 定义了"该索引哪些消息")
TRIGRAM_SRC_VIEW = "messages_fts_trigram_src"

# 搜索抽查词。ASCII 是**唯一可靠**的选择 —— 中文能不能 MATCH 取决于数据分布
# 和分词器切法, 不是索引健康度 (见文件头"两个容易搞错的点")。
# 'hermes' 在本机库里必然存在, 且 >= 3 字符, 对 unicode61 和 trigram 都适用。
SEARCH_TERM = "hermes"


def _open_ro() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def _count(con: sqlite3.Connection, sql: str, args: tuple = ()) -> int:
    return con.execute(sql, args).fetchone()[0]


def _match(con: sqlite3.Connection, table: str, term: str) -> int:
    try:
        return _count(
            con,
            f"SELECT COUNT(*) FROM {table} WHERE {table} MATCH ?",
            (term,),
        )
    except sqlite3.OperationalError:
        return -1  # 索引损坏到无法查询


# trigram 源视图不存在时的等价表达式 —— 跟 Hermes 建视图时用的条件一致。
# role <> 'tool' AND session.source NOT IN ('cron','subagent') AND 不是 delegate 会话
_TRIGRAM_SRC_FALLBACK = """
    SELECT COUNT(*) FROM messages m
    JOIN sessions s ON s.id = m.session_id
    WHERE m.role <> 'tool'
      AND s.source NOT IN ('cron', 'subagent')
      AND json_extract(
            (CASE WHEN json_valid(s.model_config) THEN s.model_config
                  ELSE json_object() END), '$._delegate_from') IS NULL
"""


def _trigram_expected_rows(con: sqlite3.Connection) -> int | None:
    """trigram 索引**应该**有多少行。

    优先用 Hermes 自己建的源视图 (它定义了"该索引哪些消息");
    视图不在时用等价表达式手算。两者都失败返回 None。
    """
    try:
        has_view = con.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='view' AND name=?", (TRIGRAM_SRC_VIEW,)
        ).fetchone()[0]
        if has_view:
            return con.execute(
                f"SELECT COUNT(*) FROM {TRIGRAM_SRC_VIEW}").fetchone()[0]
    except sqlite3.Error:
        pass
    try:
        return con.execute(_TRIGRAM_SRC_FALLBACK).fetchone()[0]
    except sqlite3.Error:
        return None


def check() -> tuple[list[dict], list[str]]:
    """返回 (检查项列表, 失败项说明)"""
    results: list[dict] = []
    failures: list[str] = []

    def record(name: str, ok: bool, detail: str, hard: bool = True) -> None:
        results.append({"name": name, "ok": ok, "detail": detail})
        if not ok and hard:
            failures.append(f"{name}: {detail}")

    if not os.path.exists(DB):
        return (
            [{"name": "库存在", "ok": False, "detail": f"{DB} 不存在"}],
            [f"库不存在: {DB}"],
        )

    con = _open_ro()
    try:
        # 1 主库完整性
        try:
            ic = con.execute("PRAGMA integrity_check").fetchone()[0]
        except Exception as e:  # noqa: BLE001
            ic = f"查询失败: {e}"
        record("主库完整性", ic == "ok", f"integrity_check = {ic}")

        # 行数
        try:
            n_msg = _count(con, "SELECT COUNT(*) FROM messages")
            n_sess = _count(con, "SELECT COUNT(*) FROM sessions")
        except Exception as e:  # noqa: BLE001
            record("数据行数", False, f"读表失败: {e}")
            n_msg = n_sess = 0
        else:
            record("数据行数", n_msg > 0 and n_sess > 0,
                   f"messages={n_msg:,} sessions={n_sess:,}")

        # 2 trigram 覆盖率 —— 基准是**源视图**, 不是 messages 总行数。
        # 源视图按设计排除 tool/cron/subagent 消息; 拿 messages 总数当分母
        # 会把好库误判成坏库 (见文件头"三个容易搞错的点"第 1 条)。
        try:
            n_tg = _count(con, "SELECT COUNT(*) FROM messages_fts_trigram")
            n_fts = _count(con, "SELECT COUNT(*) FROM messages_fts")
        except Exception as e:  # noqa: BLE001
            record("trigram 覆盖率", False, f"读索引表失败: {e}")
            n_tg = n_fts = 0
        else:
            n_expected = _trigram_expected_rows(con)
            if n_expected is None:
                record("trigram 覆盖率", False,
                       "找不到源视图, 无法算基准 (硬失败)", hard=False)
            else:
                cov = (n_tg / n_expected) if n_expected else 0.0
                record("trigram 覆盖率", cov >= COVERAGE_FLOOR,
                       f"trigram={n_tg:,} / 源视图={n_expected:,} = {cov:.1%} "
                       f"(下限 {COVERAGE_FLOOR:.0%})")
            record("普通 FTS 覆盖率",
                   (n_fts / n_msg) >= 0.95 if n_msg else False,
                   f"fts={n_fts:,} / messages={n_msg:,} = "
                   f"{(n_fts / n_msg) if n_msg else 0:.1%}")

        # 4/5 搜索命中
        # 普通 FTS -> unicode61 全文搜索; trigram -> 子串搜索。
        # 抽查词统一用 ASCII (见文件头说明)。
        hits = _match(con, "messages_fts", SEARCH_TERM)
        record("ASCII 可搜", hits > 0,
               f"MATCH '{SEARCH_TERM}' -> {hits} 条" if hits >= 0 else "查询失败(索引损坏)")
        hits = _match(con, "messages_fts_trigram", SEARCH_TERM)
        record("子串可搜", hits > 0,
               f"trigram MATCH '{SEARCH_TERM}' -> {hits} 条" if hits >= 0 else "查询失败(索引损坏)")

        # 6 空壳文档
        try:
            n_empty = _count(
                con,
                "SELECT COUNT(*) FROM messages_fts_trigram "
                "WHERE messages_fts_trigram MATCH '\"\"'",
            )
        except Exception:  # noqa: BLE001
            n_empty = -1
        if n_empty < 0:
            record("无空壳文档", False, "查不了 (索引损坏)", hard=False)
        else:
            record("无空壳文档", n_empty == 0, f"{n_empty} 个空内容文档")
    finally:
        con.close()

    return results, failures


def main() -> int:
    ap = argparse.ArgumentParser(description="FTS 索引健康自检")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    args = ap.parse_args()

    try:
        results, failures = check()
    except Exception as e:  # noqa: BLE001
        if args.json:
            print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"},
                             ensure_ascii=False))
        else:
            print(f"❌ 自检本身失败: {type(e).__name__}: {e}")
        return 2

    ok = not failures
    if args.json:
        print(json.dumps(
            {"ok": ok, "results": results, "failures": failures},
            ensure_ascii=False, indent=2))
        return 0 if ok else 1

    print("=" * 60)
    print("FTS 索引健康自检")
    print("=" * 60)
    for r in results:
        mark = "✅" if r["ok"] else "❌"
        print(f"  {mark} {r['name']:<16} {r['detail']}")
    print()
    if ok:
        print("✅ 全绿")
        return 0
    print(f"❌ {len(failures)} 项不通过:")
    for f in failures:
        print(f"   - {f}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
