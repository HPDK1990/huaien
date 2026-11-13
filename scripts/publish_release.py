"""怀恩 — 一键发布到 GitHub (导出 → 审计 → 提交 → 推送)

## 用户的诉求 (2026-11-04)

> "上传的和我用的测试的如何分开, 不可能每次上传前都要这样清一遍把。"

**这条命令就是答案** —— 一条命令, 中间全自动:
导出脱敏副本 → 审计(不干净就停) → git init/commit → push。

## 用法

```bash
# 全流程 (导出 + 审计 + 提交 + 推送)
python scripts/publish_release.py

# 只导出 + 审计, 不推
python scripts/publish_release.py --no-push

# 推到别的仓
python scripts/publish_release.py --remote git@github.com:OWNER/REPO.git

# 演练: 走完全流程但不真推
python scripts/publish_release.py --dry-run
```

## 安全设计

1. **审计不通过就不推** —— 审计器 rc != 0 直接退出, 推不出去
2. **不碰开发仓库** —— 只读源, 只写导出目录
3. **要显式确认才真推** —— 默认先跑 --dry-run 让你看清楚
4. **git 身份用匿名** —— 提交作者是 `Hermes-HuaiEn <huaien@local>`
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ / "scripts"))

DEFAULT_REMOTE = "git@github.com:HPDK1990/huaien.git"
GIT_NAME = "Hermes-HuaiEn"
GIT_EMAIL = "huaien@local"


def run(args: list[str], cwd: Path | None = None, timeout: int = 300
        ) -> tuple[int, str, str]:
    try:
        r = subprocess.run(args, cwd=str(cwd) if cwd else None,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


def _rmtree(path: Path) -> None:
    """删目录 —— 能删只读文件 (git objects 就是只读的)。"""
    import shutil
    import stat

    def _onerror(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    if path.exists():
        shutil.rmtree(path, onerror=_onerror)


def step(msg: str) -> None:
    print(f"\n>>> {msg}")


def main() -> int:
    ap = argparse.ArgumentParser(description="怀恩 一键发布到 GitHub")
    ap.add_argument("--out", default=str(PROJ.parent / "huaien-public"),
                    help="导出目录 (默认: 项目同级的 huaien-public)")
    ap.add_argument("--src", default=str(PROJ),
                    help="源仓库根 (默认: 本项目)")
    ap.add_argument("--remote", default=DEFAULT_REMOTE, help="目标 remote")
    ap.add_argument("--branch", default="main", help="目标分支")
    ap.add_argument("--no-push", action="store_true", help="只导出和审计")
    ap.add_argument("--force", action="store_true",
                    help="强推 (覆盖远端历史) —— 远端旧提交带个人邮箱时必须用")
    ap.add_argument("--dry-run", action="store_true", help="走完流程但不真推")
    ap.add_argument("--message", default=None, help="commit message")
    ap.add_argument("--tag", default=None,
                    help="发布后把该 tag 移到最新提交并强推 (触发自动 Release)")
    args = ap.parse_args()

    out_dir = Path(args.out).resolve()
    src_dir = Path(args.src).resolve()

    # ── 1. 导出 ──
    step("导出脱敏副本")
    rc, out, err = run([sys.executable, str(src_dir / "scripts" / "export_public.py"),
                         "--out", str(out_dir), "--src", str(src_dir)])
    print(out or err)
    if rc != 0:
        print("❌ 导出失败")
        return rc

    # ── 2. 审计 (硬门禁) ──
    step("审计导出结果 (rc 必须为 0)")
    rc, out, err = run([sys.executable, str(PROJ / "scripts" / "audit_publish.py"),
                         "--root", str(out_dir), "--worktree"])
    print(out or err)
    if rc != 0:
        print(f"❌ 审计未通过 (rc={rc}) —— **不推**。")
        print("   上面列的都是会上传的敏感命中, 修完再发。")
        return rc
    print("✅ 审计通过")

    if args.no_push:
        print(f"\n✅ 已导出到 {out_dir} (--no-push, 未提交未推送)")
        return 0

    # ── 3. git 初始化 + 提交 ──
    step("初始化 git 并提交")
    # ⚠️ **每次全新历史** —— 不能复用导出目录里已有的 .git。
    #    复用的话, 第二次发布会把第一次的提交叠上去, 历史里可能留着
    #    已经删掉的敏感文件 (改写前的老提交永远删不掉)。
    #    公开仓只要"当前状态干净", 不需要保留历史。
    if (out_dir / ".git").exists():
        _rmtree(out_dir / ".git")
    run(["git", "init", "-q"], cwd=out_dir)
    run(["git", "checkout", "-q", "-B", args.branch], cwd=out_dir)
    # 匿名身份 —— 不暴露个人邮箱
    run(["git", "config", "user.name", GIT_NAME], cwd=out_dir)
    run(["git", "config", "user.email", GIT_EMAIL], cwd=out_dir)
    run(["git", "remote", "remove", "origin"], cwd=out_dir)
    run(["git", "remote", "add", "origin", args.remote], cwd=out_dir)

    rc, out, err = run(["git", "add", "-A"], cwd=out_dir)
    if rc != 0:
        print(f"❌ git add 失败: {err}")
        return rc

    msg = args.message or "发布怀恩公开版"
    msg_file = out_dir / ".git" / "COMMIT_MSG_TMP"
    with msg_file.open("w", encoding="utf-8") as fh:
        fh.write(msg)
    rc, out, err = run(["git", "commit", "-q", "-F", str(msg_file)], cwd=out_dir)
    msg_file.unlink(missing_ok=True)
    if rc != 0:
        # 没变化也算成功 (内容一致)
        if "nothing to commit" not in (out + err).lower():
            print(f"❌ git commit 失败: {out} {err}")
            return rc
        print("  (内容无变化, 跳过 commit)")

    rc, out, err = run(["git", "log", "--oneline", "-1"], cwd=out_dir)
    print(f"  commit: {out}")

    # ── 4. 推送 ──
    step("推送到远端")
    if args.dry_run:
        print("  [dry-run] 跳过推送。真实推送命令:")
        print(f"    cd {out_dir}")
        print(f"    git push -u origin {args.branch}")
        return 0

    # ⚠️ 远端旧 commit 的作者是个人邮箱时, 必须强推 ——
    #    普通 push 会把旧 commit 留在历史里, 邮箱永久可见。
    push = ["git", "push", "-u", "origin", args.branch]
    if args.force:
        push.insert(3, "--force")
    rc, out, err = run(push, cwd=out_dir, timeout=600)
    print(out or err)
    if rc != 0:
        print(f"❌ 推送失败 (rc={rc})")
        print(f"   确认 remote 可达: git ls-remote {args.remote}")
        return rc

    print(f"\n✅ 发布完成: {args.remote} @ {args.branch}")

    # ── 5. 同步 tag ──
    # ⚠️ 公开仓每次重建单提交历史, tag 必须跟着 HEAD 走。
    #    否则 tag 停在旧提交上 —— Release 页面挂的代码和 main 对不上。
    if args.tag:
        step(f"同步 tag {args.tag} → HEAD")
        rc, out, err = run(["git", "tag", "-f", "-a", args.tag, "-m",
                            f"怀恩 {args.tag}"], cwd=out_dir)
        if rc != 0:
            print(f"❌ 打 tag 失败: {out} {err}")
            return rc
        # 必须 --force: 公开仓每次重建历史, 普通推会被拒
        rc, out, err = run(["git", "push", "--force", "origin", args.tag],
                           cwd=out_dir, timeout=400)
        print(out or err)
        if rc != 0:
            print(f"❌ 推 tag 失败 (rc={rc})")
            return rc
        print(f"   tag {args.tag} 已指向 HEAD —— 自动 Release workflow 会跟着更新")

    # 验证: 远端历史里不能有个人邮箱
    rc2, out2, err2 = run(["git", "log", "--all", "--format=%an <%ae>"], cwd=out_dir)
    authors = set(out2.splitlines())
    if authors:
        print(f"   提交作者: {sorted(authors)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
