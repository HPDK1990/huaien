"""怀恩 — 版本封存打包

给某个版本生成一个独立可运行的快照:

    versions/V0.3.0/
      版本文档.md          (从 docs/ 拷)
      能力测试报告.md      (从 docs/ 拷)
      方案.md              (从 docs/ 拷)
      CHANGELOG.md        (项目根的)
      截图/                (docs/ 下的 png)
      怀恩-V0.3.0.zip      (完整源码, 解压就能跑)

用法:
    python scripts/package_version.py 0.3.0
    python scripts/package_version.py 0.3.0 --no-zip
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import zipfile
from datetime import datetime

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 进 zip 的东西 (运行时必需)
ZIP_INCLUDE = [
    "huaien",
    "scripts",
    "tests",
    "docs",
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "VERSION",
    "pyproject.toml",
    ".gitignore",
]
ZIP_EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".git", "logs", "versions"}
ZIP_EXCLUDE_EXT = {".pyc", ".pyo", ".zip"}


def build(version: str, make_zip: bool = True) -> str:
    vdir = os.path.join(PROJ, "versions", f"V{version}")
    os.makedirs(vdir, exist_ok=True)

    # ---- 文档 ----
    docs_map = {
        "V0.3-能力方案.md": "方案.md",
        "能力测试报告.md": "能力测试报告.md",
        "架构-前后台边界.md": "架构-前后台边界.md",
        "批判性审核记录.md": "批判性审核记录.md",
    }
    for src_name, dst_name in docs_map.items():
        src = os.path.join(PROJ, "docs", src_name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(vdir, dst_name))

    for name in ("CHANGELOG.md", "README.md", "LICENSE", "VERSION"):
        src = os.path.join(PROJ, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(vdir, name))

    # ---- 截图 ----
    # ⚠️ **白名单, 不全量复制**。
    #    历史教训: 原来是把 docs/*.png 全量拷进来, 而 docs/shot_reply.png
    #    是**自检时的整屏截图** —— 里面可能抓到别的窗口 (实测抓到过一张
    #    太空卡车插画), 494 KB 直接进了 Git 历史。
    #    现在只收 drill/ 下的 (UI 专项截图, 逐张视觉审过的)。
    shots = os.path.join(vdir, "截图")
    os.makedirs(shots, exist_ok=True)
    drill = os.path.join(PROJ, "docs", "drill")
    if os.path.isdir(drill):
        for f in sorted(os.listdir(drill)):
            if f.lower().endswith(".png"):
                shutil.copy2(os.path.join(drill, f), os.path.join(shots, "drill_" + f))
    # docs/ 根下的截图 —— 只收明确在白名单里的
    d = os.path.join(PROJ, "docs")
    for f in SHOT_ALLOWLIST:
        src_f = os.path.join(d, f)
        if os.path.isfile(src_f):
            shutil.copy2(src_f, os.path.join(shots, f))

    # ---- 源码 zip ----
    zpath = os.path.join(vdir, f"怀恩-V{version}.zip")
    if make_zip:
        if os.path.exists(zpath):
            os.remove(zpath)
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for item in ZIP_INCLUDE:
                src = os.path.join(PROJ, item)
                if not os.path.exists(src):
                    continue
                if os.path.isfile(src):
                    z.write(src, f"怀恩-V{version}/{item}")
                    continue
                for root, dirs, files in os.walk(src):
                    dirs[:] = [x for x in dirs if x not in ZIP_EXCLUDE_DIRS]
                    for f in files:
                        if os.path.splitext(f)[1].lower() in ZIP_EXCLUDE_EXT:
                            continue
                        fp = os.path.join(root, f)
                        rel = os.path.relpath(fp, PROJ)
                        z.write(fp, f"怀恩-V{version}/{rel}")

    # ---- 版本说明 ----
    ver_file = os.path.join(PROJ, "VERSION")
    if os.path.exists(ver_file):
        with open(ver_file, encoding="utf-8") as f:
            cur = f.read().strip()
    else:
        cur = "?"
    readme = os.path.join(vdir, "README-封存说明.md")
    with open(readme, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"""# 怀恩 V{version} 封存快照

- 生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M")}
- 当前项目版本: {cur}
- 源码包: `怀恩-V{version}.zip`（{os.path.getsize(zpath) // 1024 if make_zip else 0} KB，解压后 `python -m huaien.app` 即可跑）

## 里面有什么

| 文件 | 内容 |
|---|---|
| `版本文档.md` | 本版本的功能范围、实测数字、已知限制 |
| `能力测试报告.md` | 前台真实操作测试的完整记录 |
| `方案.md` | 设计 → 批判性审核 → 执行的全过程 |
| `架构-前后台边界.md` | 模型/工具/记忆/会话逐项对照表 |
| `批判性审核记录.md` | 历次审计发现的问题与修法 |
| `截图/` | 界面与测试过程的真实截图 |
| `怀恩-V{version}.zip` | 完整源码（含测试和脚本） |

## 回滚

```bash
git checkout v{version}          # 回到这一版代码
# 或者直接解压 zip 到任意目录
```
""")
    return vdir


# docs/ 下允许进封存的截图 (白名单)
# ⚠️ 空的 = 不收。docs/ 根下的 png 大多是**运行时产物**
#    (自检整屏截图), 不是给用户看的界面图。
SHOT_ALLOWLIST: tuple[str, ...] = ()


def main() -> int:
    ap = argparse.ArgumentParser(description="怀恩版本封存打包")
    ap.add_argument("version", help="版本号, 如 0.3.0")
    ap.add_argument("--no-zip", action="store_true", help="不打源码包")
    args = ap.parse_args()
    vdir = build(args.version, make_zip=not args.no_zip)
    total = 0
    for root, _, files in os.walk(vdir):
        for f in files:
            total += os.path.getsize(os.path.join(root, f))
    print(f"封存完成: {vdir}")
    print(f"  文件数 {sum(len(fs) for _, _, fs in os.walk(vdir))}, 合计 {total // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
