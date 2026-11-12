"""怀恩 — 前后台记忆双向同步

## 为什么需要这个

Hermes 的每个 profile 都有**自己独立**的 `memories/MEMORY.md` 和 `USER.md`。
前台界面跑在 `huaien` profile 上, 后台 (默认 profile) 是另一份。

问题: 两份都是克隆自 default 的**副本**, 此后各写各的, 慢慢分叉。
用户在前台说的话, 后台读不到; 后台记住的事, 前台不知道。

Hermes 没有提供 "换人格但共享记忆" 的开关 ——
`hermes chat --ignore-rules` 会把 memory 一起跳过 (官方说明:
"Skip auto-injection of AGENTS.md, SOUL.md, .cursorrules, memory, and preloaded skills"),
所以那条路走不通。

## 做法

条目级**双向补齐** (只补, 不删):

- Hermes 的记忆文件用 `§` 分隔条目。
- 把两边各自按 `§` 切成条目集合。
- 对方有、自己没有的条目 → 追加到自己文件末尾。
- **绝不删除**任何一方的条目 —— 宁可变长让 Hermes 的 curator 自己裁,
  也不能在同步时把信息弄丢。

## 跑法

    python scripts/sync_memory.py            # 同步并打印结果
    python scripts/sync_memory.py --check    # 只检查差异, 不写盘
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or str(Path.home() / "AppData" / "Local" / "hermes"))
FILES = ("MEMORY.md", "USER.md")
SEP = "§"


def _profile_dir(profile: str) -> Path:
    if profile in ("default", "", None):
        return HERMES_HOME
    return HERMES_HOME / "profiles" / profile


def split_entries(text: str) -> list[str]:
    """按 § 切条目, 去掉空白项。"""
    parts = [p.strip() for p in text.split(SEP)]
    return [p for p in parts if p]


def join_entries(entries: list[str]) -> str:
    """拼回 Hermes 认得的格式 (条目之间用 § + 空行)。"""
    return ("\n\n" + SEP + "\n\n").join(entries)


def read_entries(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        return split_entries(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []


def write_entries(path: Path, entries: list[str]) -> bool:
    """写盘。返回 True 表示真的改了。"""
    if not entries:
        return False
    new = join_entries(entries)
    try:
        old = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    except OSError:
        old = ""
    if new.strip() == old.strip():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8", newline="\n")
    return True


def _limit_for(path: Path, default: int = 2200) -> int:
    """这个记忆文件对应的字符上限 (读 profile 的 config.yaml)。

    为什么要知道上限: Hermes 有硬规矩 —— MEMORY.md 超过 char limit 后,
    **后续写入全部被拒** (日志: "MEMORY.md exceeds its char limit on load:
    2780/2200 chars ... further additions are blocked")。
    同步如果不看上限, 就可能把对方撑爆, 让它彻底写不进新东西。
    """
    try:
        # memories/MEMORY.md -> profile 根目录
        root = path.parent.parent
        cfg = root / "config.yaml"
        if not cfg.exists():
            return default
        text = cfg.read_text(encoding="utf-8", errors="replace")
        import re
        m = re.search(r"memory_char_limit:\s*(\d+)", text)
        u = re.search(r"user_char_limit:\s*(\d+)", text)
        if path.name.upper() == "USER.MD":
            return int(u.group(1)) if u else default
        return int(m.group(1)) if m else default
    except (OSError, ValueError):
        return default


def sync_pair(a_path: Path, b_path: Path) -> tuple[int, int]:
    """双向补齐一对文件。返回 (补进 a 的条数, 补进 b 的条数)。

    会看各自的字符上限: 补到上限就不再补, 并把超限的条数报出来。
    宁可少同步几条, 也不能把对方的记忆撑到写不进新东西。
    """
    a = read_entries(a_path)
    b = read_entries(b_path)
    a_set, b_set = set(a), set(b)

    def _fit(base: list[str], extra: list[str], limit: int) -> tuple[list[str], int, int]:
        """在 limit 内能塞几条。返回 (要加的条目, 实际加的条数, 装不下的条数)。"""
        cur = len(join_entries(base))
        add, skipped = [], 0
        for e in extra:
            cost = len(e) + len(SEP) + 4
            if cur + cost > limit:
                skipped += 1
                continue
            add.append(e)
            cur += cost
        return add, len(add), skipped

    extra_a, n_a, skip_a = _fit(a, [e for e in b if e not in a_set], _limit_for(a_path))
    extra_b, n_b, skip_b = _fit(b, [e for e in a if e not in b_set], _limit_for(b_path))

    if extra_a:
        write_entries(a_path, a + extra_a)
    if extra_b:
        write_entries(b_path, b + extra_b)

    if skip_a or skip_b:
        print(f"  [警告] 记忆已达上限, 跳过 {skip_a + skip_b} 条 "
              f"(超出侧: {'a' if skip_a else 'b'})")
    return n_a, n_b


def sync_all(back_profile: str = "default", front_profile: str = "huaien",
             check_only: bool = False) -> dict:
    """把前台 profile 的记忆与后台 profile 双向补齐。

    返回 {"<文件名>": (补进后台数, 补进前台数), ...}
    """
    back_dir = _profile_dir(back_profile) / "memories"
    front_dir = _profile_dir(front_profile) / "memories"
    result = {}
    for name in FILES:
        a, b = back_dir / name, front_dir / name
        if check_only:
            ae, be = read_entries(a), read_entries(b)
            missing_in_back = [e for e in be if e not in set(ae)]
            missing_in_front = [e for e in ae if e not in set(be)]
            result[name] = (len(missing_in_back), len(missing_in_front))
        else:
            result[name] = sync_pair(a, b)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="怀恩 前后台记忆双向同步")
    ap.add_argument("--check", action="store_true", help="只检查差异, 不写盘")
    ap.add_argument("--back", default="default", help="后台 profile 名")
    ap.add_argument("--front", default="huaien", help="前台 profile 名")
    args = ap.parse_args()

    res = sync_all(args.back, args.front, check_only=args.check)
    verb = "待同步" if args.check else "已同步"
    for name, (nb, nf) in res.items():
        print(f"  {name:12} {verb}: 后台缺 {nb} 条 / 前台缺 {nf} 条")
    total = sum(a + b for a, b in res.values())
    print(f"  {'差异' if args.check else '变更'}: {total} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
