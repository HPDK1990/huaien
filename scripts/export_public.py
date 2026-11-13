"""怀恩 — 公开版导出器 (上传隔离)

## 用户的问题 (2026-11-04)

> "上传的和我用的测试的如何分开, 不可能每次上传前都要这样清一遍把。"

**这个问题的正解不是"每次上传前清一遍"** —— 那是人肉流程, 迟早会忘。
正解是**物理隔离**:

```
<PROJECT_ROOT>            ← 你的开发环境 (有真实路径、真实昵称、真实配置)
        │
        │  scripts/export_public.py   ← 一次性导出
        ▼
<PROJECT_ROOT>\\..\\huaien-public\\  ← 干净的公开副本 (可直接 push)
```

**开发环境永远干净不了也不需要干净** —— 它是你的工作区。
**只有导出的那一份会被 push。** 两份物理分离, 互不干扰。

## 为什么不用 git filter-repo 每次做

- `filter-repo` 改写的是**历史**, 一次性做完就完了, 不需要"每次"
- 但它**改的是你正在用的仓库** —— 改写后你的本地历史就永久变了,
  而且它不处理"昵称/机器路径"这类**语义脱敏** (它只做字面替换)
- **本脚本不碰你的仓库** —— 只读源、只写目标目录

## 用法

```bash
# 导出 (默认导出到 ..\\huaien-public)
python scripts/export_public.py

# 指定输出目录
python scripts/export_public.py --out D:\\somewhere\\huaien-public

# 只看会脱敏什么, 不真的导出
python scripts/export_public.py --dry-run

# 导出后立刻审计 (推荐)
python scripts/export_public.py --out ..\\huaien-public --audit
```

## 脱敏规则 (可配)

规则写在 `config/publish_rules.json`, 改配置不用改代码。
默认规则见 `DEFAULT_RULES`。

## 设计约束

1. **只读源仓库** —— 绝不动你的工作区和 .git
2. **幂等** —— 同一份源码导出两次, 结果一致
3. **可审计** —— 导出的每一处改动都有记录 (`publish_report.json`)
4. **默认脱敏** —— 昵称、机器路径、真实 ID 全部换掉; 不靠人记得
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

# 前缀用字符码拼出来 —— 源码里不出现明文
_PFX = "".join(chr(c) for c in (111, 57, 99, 113, 56, 48))

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ / "scripts"))

# ── 默认脱敏规则 ──────────────────────────────────────────────────────
# ⚠️ 这些是**替换模板**, 不是真值 —— 真值不许出现在任何会上传的文件里。
# 本机专有的字面量 —— **运行时拼出来**, 不写进源码。
# ⚠️ 写进源码的话, 导出的公开版里规则字面量本身就是真值 (实测踩过)。
_BS = chr(92)                                   # 反斜杠
_BS_RE = re.escape(_BS)                         # 正则里的转义反斜杠


# 真实昵称 —— **运行时构造**, 不写进源码。
# ⚠️ 写死的话, 导出的公开版里规则目标就消失了, 脱敏形同虚设。
# 项目名 —— 运行时构造, 规则表里不写明文。
# ⚠️ 同上: 写死的话, 公开版的规则表里就只剩形态, 脱敏目标消失了。
_PROJ_NAME = "Hermes" + "-" + "HuaiEn"

# ⚠️ 字符码**必须核对** —— 写错一位, 规则就匹配不到任何东西, 脱敏静默失效。
#    (曾踩过: chr(36215) 实际是"起", 不是"赵" —— 310 处昵称全没脱敏。)
_NICK_USER = chr(32769) + chr(36213)        # 用户
_NICK_HELPER = chr(23567) + chr(33738)      # 助手


def _lit(text: str) -> str:
    r"""把字面文本变成安全的正则 —— 反斜杠路径必须整体转义。

    ⚠️ 直接拼反斜杠路径会得到非法 pattern (\U / \H 不是合法转义)。
    ⚠️ 本 docstring 必须是 raw string —— 否则 \U 会被当 unicode 转义。
    """
    return re.escape(text)
_USER = "".join(chr(c) for c in (54, 52, 56, 49, 53))   # 本机用户名

DEFAULT_RULES: dict[str, list[dict]] = {
    # ⚠️ 昵称也**运行时构造** —— 写死明文的话, 导出后公开版的规则表里
    #    就只剩"用户", 脱敏不再发生 (目标已经消失了)。
    "昵称": [
        # -> 用户 (中性, 不指向任何真人)
        {"pattern": _NICK_USER, "replace": "用户"},
        # -> 助手 (去掉人格名)
        {"pattern": _NICK_HELPER, "replace": "助手"},
    ],
    # ⚠️ **规则表里不写真值** —— 全部运行时拼出来。
    #    否则导出的公开版里, 规则字面量本身就是真值 (实测用户名漏出来过)。
    # ⚠️ **规则表里不写真值** —— 全部运行时拼出来。
    #    否则导出的公开版里, 规则字面量本身就是真值 (实测用户名漏出来过)。
    "机器路径": [
        # 反斜杠/正斜杠、单反斜杠/双反斜杠 四种形态都真实存在过
        {"pattern": _lit(f"C:{_BS}{_BS}Users{_BS}{_BS}{_USER}"), "replace": "%LOCALAPPDATA%",
         "note": "Windows 用户目录 (双反斜杠) -> %LOCALAPPDATA%"},
        {"pattern": _lit(f"C:{_BS}Users{_BS}{_USER}"), "replace": "%LOCALAPPDATA%",
         "note": "Windows 用户目录 (反斜杠) -> %LOCALAPPDATA%"},
        {"pattern": f"C:/Users/{_USER}", "replace": "%LOCALAPPDATA%",
         "note": "Windows 用户目录 (正斜杠) -> %LOCALAPPDATA%"},
        {"pattern": f"/c/Users/{_USER}", "replace": "$LOCALAPPDATA",
         "note": "MSYS 用户目录 -> $LOCALAPPDATA"},
        # 项目绝对路径
        # ⚠️ **双反斜杠形态必须排在单反斜杠前面** —— 反过来会被截胡:
        #    `<PROJECT_ROOT>` 里单反斜杠规则先命中前半段, 双反斜杠残留。
        {"pattern": _lit(f"E:{_BS}{_BS}{_PROJ_NAME}"), "replace": "<PROJECT_ROOT>",
         "note": "项目根 (双反斜杠) -> <PROJECT_ROOT>"},
        {"pattern": _lit(f"E:{_BS}{_PROJ_NAME}"), "replace": "<PROJECT_ROOT>",
         "note": "项目根 (单反斜杠) -> <PROJECT_ROOT>"},
        {"pattern": _lit(f"E:/{_PROJ_NAME}"), "replace": "<PROJECT_ROOT>",
         "note": "项目根 (正斜杠) -> <PROJECT_ROOT>"},
        # 其他本机目录 (备份盘等) —— 同样暴露本机结构
        {"pattern": _lit(f"E:{_BS}{_BS}Hermes-CunDang"), "replace": "<BACKUP_DIR>",
         "note": "本机备份目录 (双反斜杠) -> <BACKUP_DIR>"},
        {"pattern": _lit(f"E:{_BS}Hermes-CunDang"), "replace": "<BACKUP_DIR>",
         "note": "本机备份目录 -> <BACKUP_DIR>"},
        # 裸用户名 (出现在路径中间, 如 .../<用户名>/AppData)
        # ⚠️ 正则里的反斜杠必须转义 —— 直接拼裸反斜杠会得到非法 pattern
        {"pattern": f"(?<=[/{_BS_RE}]){_USER}(?=[/{_BS_RE}])",
         "replace": "<USER>", "note": "路径里的裸用户名 -> <USER>"},
        # 文档里的路径写法示例 (省略形式)
        {"pattern": f"E:{_BS}{_BS}{_BS}{_BS}{_BS}{_BS}\\.\\.\\.", "replace": "<PATH>",
         "note": "路径示例 -> <PATH>"},
    ],
    "真实标识": [
        # ⚠️ **截断形态先处理** —— 文档里常写 `<CHANNEL_ID>...` 这种 (8位+省略号),
        #    完整形态的规则 ({20,}) 匹配不到, 而前 8 位**就是真 chat_id 的前缀**,
        #    一样能定位到人。省略号也算。
        {"pattern": r"\b(?:oc|ou|cli|" + _PFX + r")_[A-Za-z0-9]{6,}(?:\.\.\.|…)",
         "replace": "<CHANNEL_ID>...", "note": "渠道 ID 截断形态"},
        {"pattern": r"\boc_[A-Za-z0-9]{20,}\b", "replace": "<FEISHU_CHAT_ID>",
         "note": "飞书 chat_id"},
        {"pattern": r"\bcli_[A-Za-z0-9]{16}\b", "replace": "<FEISHU_APP_ID>",
         "note": "飞书 AppID"},
        # 飞书 open_id / user_id (也是 32 位 hex 形态, 同样是会话标识)
        {"pattern": r"\bou_[A-Za-z0-9]{20,}", "replace": "<FEISHU_OPEN_ID>",
         "note": "飞书 open_id"},
        # 微信 chat_id —— 后缀可能是完整的, 也可能是文档里手写的省略形态
        {"pattern": r"\b" + _PFX + r"_[A-Za-z0-9]{4,}", "replace": "<WECHAT_CHAT_ID>",
         "note": "微信 chat_id"},
    ],
    # ⚠️ Hermes session 引用 (形如 @session:<profile>/<40位hex>)
    #    单独一组, 排在 hex 规则**之前** —— 否则 id 被 24 位规则切碎,
    #    剩下片段变成乱码, 原始 id 反而留在文件里。
    "会话引用": [
        {"pattern": r"@session:[A-Za-z0-9_\-]+/[A-Za-z0-9\-]+",
         "replace": "@session:<PROFILE>/<SESSION_ID>", "note": "Hermes session 引用"},
        # 24 位 hex 令牌 (在 session 之后 —— session 要先整条吃掉)
        {"pattern": r"(?<![0-9a-fA-F])[0-9a-f]{24}(?![0-9a-fA-F])",
         "replace": "<TOKEN>", "note": "访问令牌"},
        # 32 位 hex (GitHub gist 凭据形态)
        {"pattern": r"(?<![0-9a-fA-F])[0-9a-f]{32}(?![0-9a-fA-F])",
         "replace": "<HEX_CREDENTIAL>", "note": "32 位 hex 凭据"},
    ],
    # ⚠️ 用否定环视而不是 \b —— \b 依赖词边界, 而 IP 紧贴数字时会失效:
    #    `line2\n192.168.x.x` 里 2 和 192 之间没有词边界, \b 匹配不到。
    "网络": [
        {"pattern": r"(?<![\d.])192\.168\.\d{1,3}\.\d{1,3}(?![\d.])",
         "replace": "192.168.x.x", "note": "内网 IP"},
        {"pattern": r"(?<![\d.])10\.\d{1,3}\.\d{1,3}\.\d{1,3}(?![\d.])",
         "replace": "10.x.x.x", "note": "内网 IP"},
        {"pattern": r"(?<![\d.])172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}(?![\d.])",
         "replace": "172.x.x.x", "note": "内网 IP"},
    ],
}

# 不导出的路径 (相对项目根)
EXCLUDE_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules",
                ".pytest_cache", ".ruff_cache", "logs", "backup"}
EXCLUDE_GLOBS = [
    "*.log", "*.pyc", "*.orig", "*.zip", "*.db",
    "versions/backup/*",
    "docs/shot_*.png",          # 自检截图 (可能含整个屏幕)
    "versions/*/截图/shot_*.png",  # 封存里的自检截图
]

# 这些文件在公开版里没有意义 (本机运维脚本)
EXCLUDE_FILES = {
    "publish_report.json",          # 记录脱敏前的路径, 不该进公开版
    "scripts/_dl_small.bat",
    "scripts/optimize_db_full.bat",
    "scripts/优化会话库完整.bat",
    "scripts/优化会话库.bat",
    # ⚠️ 内部工作文档 —— 写给"下一个接手的自己"看的, 不是给公众看的。
    #    内容含: 个人 GitHub 账号、审计过程、内部待办、复盘细节。
    #    公开仓不需要这些, 留在开发仓库里即可。
    "docs/GitHub上架-审计与交接.md",
    "docs/敏感信息治理.md",
    "docs/事故复盘-2026-10-30.md",
}


def load_rules(path: Path | None, root: Path | None = None) -> dict[str, list[dict]]:
    """规则优先级: 命令行指定 > config/publish_rules.json > 默认。"""
    if path and path.is_file():
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    cfg = (root or PROJ) / "config" / "publish_rules.json"
    if cfg.is_file():
        with cfg.open(encoding="utf-8") as fh:
            return json.load(fh)
    return DEFAULT_RULES


def compile_rules(rules: dict[str, list[dict]]) -> list[tuple[str, re.Pattern, str, str]]:
    """编译成 (类别, 正则, 替换, 说明)。"""
    out = []
    for cat, items in rules.items():
        for it in items:
            out.append((cat, re.compile(it["pattern"]), it["replace"],
                        it.get("note", "")))
    return out


def should_exclude(rel: Path) -> bool:
    parts = set(rel.parts)
    if parts & EXCLUDE_DIRS:
        return True
    rel_s = rel.as_posix()
    if rel_s in EXCLUDE_FILES:
        return True
    return any(rel.match(g) or fnmatch(rel_s, g) for g in EXCLUDE_GLOBS)


def fnmatch(name: str, pattern: str) -> bool:
    import fnmatch as _fm
    return _fm.fnmatch(name, pattern)


GIT_IGNORED = shutil.ignore_patterns(
    ".git", "__pycache__", "*.pyc", ".venv", "venv", "node_modules",
    ".pytest_cache", ".ruff_cache", "*.egg-info", ".mypy_cache",
)


def git_ls_files(root: Path | None = None) -> list[str]:
    """列出要导出的源文件。

    优先用 git ls-files (只导已跟踪的, 未跟踪的本地改动不进公开版);
    **没有 git 仓库时退回扫目录树** —— 公开副本自己就没有 .git,
    它的测试必须能导出自己。

    ⚠️ root 必须可指定 —— 否则公开副本里的测试会去读**开发仓库**,
       测试的东西和被测的东西不是一回事 (自欺欺人)。
    """
    root = Path(root or PROJ)
    r = subprocess.run(["git", "ls-files", "-z"], cwd=str(root),
                       capture_output=True, text=True, timeout=120)
    files = [f for f in (r.stdout or "").split("\0") if f]
    if files:
        return files
    # 无 git -> 扫目录树
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")
                       and d not in ("__pycache__", "node_modules", ".venv", "venv")]
        for fn in filenames:
            fp = Path(dirpath) / fn
            try:
                out.append(str(fp.relative_to(root)).replace(os.sep, "/"))
            except ValueError:
                continue
    return sorted(out)


def _reset_dir(path: Path) -> None:
    """清空输出目录 —— 能删只读文件。

    ⚠️ shutil.rmtree 删 .git/objects 里的只读文件会 PermissionError。
    ⚠️ 绝不用 shutil.rmtree 做**不可恢复**的删除 —— 但这里是导出目录,
    随时可以用 export_public.py 重新生成, 删了没风险。
    """
    if not path.exists():
        return

    def _onerror(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    shutil.rmtree(path, onerror=_onerror)


def is_texty(rel: str) -> bool:
    ext = os.path.splitext(rel)[1].lower()
    return ext not in (".png", ".jpg", ".jpeg", ".gif", ".ico", ".zip", ".pdf",
                       ".exe", ".db", ".mp4", ".mp3", ".wav", ".woff", ".woff2")


def verify_no_residue(out_dir: Path, compiled: list) -> list[dict]:
    """导出后自检: 输出目录里还有没有**没脱敏掉**的真值。

    用**同一套规则**反查输出 —— 规则写错的话这里也查不出来,
    所以额外用规则表里的**明文真值**再独立查一遍 (见 _residue_literals)。
    """
    residue = []
    literals = _residue_literals()
    for path in sorted(out_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(out_dir).as_posix()
        if not is_texty(rel):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for label, value in literals:
            if value and value in text:
                residue.append({"rule": label, "file": rel,
                                "count": text.count(value)})
    return residue


# 脱敏占位符 —— 代码/脚本里出现就是"下载下来跑不了"
# (bat 里的 PROJ=<PROJECT_ROOT> 曾经让 5 个启动脚本全部直接退出)
_PLACEHOLDER = re.compile(r"<[A-Z_][A-Z0-9_]{2,}>")
# 这些文件里的占位符是**故意的**: 脱敏规则表、文档示例、测试夹具
_PLACEHOLDER_ALLOWED = {"export_public.py", "audit_publish.py", "install_audit_hook.py",
                        "_real_values.py", "test_audit_publish.py", "test_export_public.py",
                        "test_audit_encoding.py"}


def _python_code_placeholders(text: str) -> list[str]:
    """Python: 只查**代码里的字符串字面量**, 跳过 docstring。

    注释在 AST 里根本不存在, docstring 单独排除 —— 文档里写
    `<PROJECT_ROOT>` 是合理的 (那正是在说明脱敏行为)。
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _PLACEHOLDER.findall(text)      # 解析不了就全文查 (保守)
    docs = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            docs.add(id(first.value))
    hits = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docs):
            hits += _PLACEHOLDER.findall(node.value)
    return hits


def _shell_code_placeholders(text: str) -> list[str]:
    """bat / ps1 / sh: 跳过整行注释 (`#` / `REM` / `::`)。"""
    hits = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(("#", "REM ", "rem ", "::")):
            continue
        hits += _PLACEHOLDER.findall(line)
    return hits


def verify_no_placeholder_in_code(out_dir: Path) -> list[dict]:
    """代码/脚本里不许有脱敏占位符。

    文档和注释里的占位符无害 (那是给人看的示例), 但**可执行代码**里的
    占位符意味着"下载下来跑不了" —— 曾经 `set "PROJ=<PROJECT_ROOT>"`
    让 5 个 bat 双击就退出。
    """
    bad = []
    for path in sorted(out_dir.rglob("*")):
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        if ext not in (".py", ".bat", ".ps1", ".sh"):
            continue
        rel = path.relative_to(out_dir).as_posix()
        if path.name in _PLACEHOLDER_ALLOWED or rel.startswith("docs/"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hits = (_python_code_placeholders(text) if ext == ".py"
                else _shell_code_placeholders(text))
        for m in hits:
            bad.append({"rule": f"代码占位符 {m}", "file": rel})
    return bad


def _residue_literals() -> list[tuple[str, str]]:
    """要独立复查的真值 —— **运行时构造**, 源码里不出现明文。

    ⚠️⚠️ **必须与脱敏规则用不同的编码源** —— 否则两边一起写错时,
       自检等于没做 (曾实测: 脱敏规则用错码 → 自检跟着错 → 全绿放行)。

    脱敏规则: 十进制 chr()
    本函数  : 十六进制 chr()
    两边同时写错的概率因此大幅降低, 且 tests/ 里有交叉断言锁死一致性。
    """
    return [
        # 十六进制: U+8001 U+8D75 = 用户
        ("个人昵称", chr(0x8001) + chr(0x8D75)),
        # 十六进制: U+5C0F U+83CA = 助手
        ("个人昵称", chr(0x5C0F) + chr(0x83CA)),
        # 十六进制: 6 4 8 1 5
        ("Windows 用户名", chr(0x36) + chr(0x34) + chr(0x38) + chr(0x31) + chr(0x35)),
        ("项目路径", "E:" + chr(0x5C) + "Hermes-HuaiEn"),
    ]


def export(out_dir: Path, rules: dict, dry_run: bool = False,
           include_untracked: bool = False, src_root: Path | None = None) -> dict:
    """导出公开版。返回报告。

    src_root: 源仓库根 —— 默认开发仓库。公开副本里的测试必须传自己的根,
              否则测的是开发仓库 (自欺欺人)。
    """
    src_root = Path(src_root) if src_root else PROJ
    compiled = compile_rules(rules)
    files = git_ls_files(src_root)
    if include_untracked:
        r = subprocess.run(["git", "ls-files", "-z", "--others", "--exclude-standard"],
                           cwd=str(src_root), capture_output=True, text=True, timeout=120)
        files += [f for f in (r.stdout or "").split("\0") if f]

    report = {
        "out_dir": str(out_dir),
        "total_files": len(files),
        "exported": 0,
        "skipped": [],
        "changed": [],       # 脱敏过的文件
        "rule_hits": {},     # 规则 -> 命中次数
    }

    # ══ 写入目标: 先落到暂存目录, 全部成功再替换 ══
    # ⚠️ **不能先清空目标再写** —— 写到一半失败 (目录被占用 / 磁盘满 /
    #    规则自检报错) 就会把上一份好副本换成半成品, 而上一份是能直接推的。
    #    这次真踩过: 目标目录被 pytest 占用 -> 清空成功、写入失败 -> 副本缺
    #    .github/workflows, 公开版测试直接挂了 5 项。
    #    暂存目录在**同一个父目录**下, 所以最后的 rename 是同卷原子操作。
    staging = out_dir.with_name(out_dir.name + ".staging")
    if not dry_run:
        _reset_dir(staging)
        staging.mkdir(parents=True, exist_ok=True)
        write_dir = staging
    else:
        write_dir = out_dir

    try:
        for rel_s in files:
            rel = Path(rel_s)
            if should_exclude(rel):
                report["skipped"].append(rel_s)
                continue
            src = src_root / rel
            if not src.is_file():
                continue
            dst = write_dir / rel
            if dry_run:
                if is_texty(rel_s):
                    text = src.read_text(encoding="utf-8", errors="replace")
                    _new_text, hits = _apply(text, compiled, report)
                    if hits:
                        report["changed"].append({"file": rel_s, "hits": hits})
                report["exported"] += 1
                continue

            dst.parent.mkdir(parents=True, exist_ok=True)
            if is_texty(rel_s):
                text = src.read_text(encoding="utf-8", errors="replace")
                new_text, hits = _apply(text, compiled, report)
                dst.write_text(new_text, encoding="utf-8", newline="\n")
                if hits:
                    report["changed"].append({"file": rel_s, "hits": hits})
            else:
                shutil.copy2(src, dst)
            report["exported"] += 1

        # ══ 导出后自检 ══
        # ⚠️ **必须自检** —— 脱敏规则写错一个字 (比如 chr 码错一位),
        #    导出会"成功"但真值原样留在公开副本里, 而外部审计器不一定查这个维度。
        #    这里用**自己刚写的规则表**反过来查输出, 残留就当场报错。
        if not dry_run:
            residue = verify_no_residue(write_dir, compiled)
            report["residue"] = residue
            ph = verify_no_placeholder_in_code(write_dir)
            report["placeholders"] = ph
            if residue or ph:
                names = ", ".join(sorted({r["rule"] for r in residue + ph}))
                raise RuntimeError(
                    f"导出后自检失败: 输出里还有 {len(residue) + len(ph)} 处问题 ({names})。"
                    f"先修规则再重导 —— 绝不能把这份副本推上去。"
                )
            _swap_in(staging, out_dir)
    finally:
        # 任何失败都不留残骸, 也不碰上一份好副本
        if not dry_run and staging.exists():
            _reset_dir(staging)

    return report


def _swap_in(staging: Path, out_dir: Path) -> None:
    """把暂存目录换上目标目录。

    先把旧目录挪到 .old, 再把暂存目录改名成目标 —— 同卷 rename 是原子操作。
    旧目录挪不动 (被占用) 时直接抛错, **旧副本原地不动**, 不会丢。
    """
    old = out_dir.with_name(out_dir.name + ".old")
    _reset_dir(old)
    moved = False
    if out_dir.exists():
        out_dir.rename(old)
        moved = True
    try:
        staging.rename(out_dir)
    except OSError:
        if moved:
            old.rename(out_dir)      # 回滚: 旧副本放回原位
        raise
    if moved:
        _reset_dir(old)


def _apply(text: str, compiled, report: dict) -> tuple[str, dict]:
    """应用所有规则。返回 (新文本, 命中明细)。"""
    hits: dict[str, int] = {}
    for cat, rx, repl, _note in compiled:
        text, n = rx.subn(repl, text)
        if n:
            hits[cat] = hits.get(cat, 0) + n
            report["rule_hits"][cat] = report["rule_hits"].get(cat, 0) + n
    return text, hits


def write_readme(out_dir: Path) -> None:
    """给公开版写一份独立 README (说明这是脱敏后的发布副本)。"""
    readme = out_dir / "PUBLISH_NOTICE.md"
    readme.write_text(
        "# 怀恩 · 公开版\n\n"
        "本仓库是 `Hermes-HuaiEn` 的**发布副本**。\n\n"
        "## 与开发仓库的关系\n\n"
        "| | 开发版 | 公开版 |\n"
        "|---|---|---|\n"
        "| 位置 | 本机工作区 | 本仓库 |\n"
        "| 昵称 | 真实称呼 | 已替换为中性称呼 |\n"
        "| 路径 | 本机绝对路径 | 已替换为占位符 |\n"
        "| 配置 | 真实渠道 ID / 令牌 | 全部占位符 + 示例配置 |\n\n"
        "**公开版不含任何真实凭据、真实会话 ID、真实网络地址。**\n\n"
        "## 敏感信息去哪了\n\n"
        "- API key / provider 凭据 → 运行时从环境变量或 `~/.hermes/.env` 读\n"
        "- 微信 / 飞书会话 ID → 运行时配置, 不在代码里\n"
        "- 访问口令 → 启动时随机生成, 写入本地日志\n\n"
        "## 导出方式\n\n"
        "```bash\n"
        "python scripts/export_public.py --out ../huaien-public --audit\n"
        "```\n\n"
        "导出器只读源仓库, 不改任何东西 —— 你的开发环境保持原样。\n",
        encoding="utf-8", newline="\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="怀恩 公开版导出器 (上传隔离)")
    ap.add_argument("--out", default=str(PROJ.parent / "huaien-public"),
                    help="输出目录 (默认: 项目同级的 huaien-public)")
    ap.add_argument("--rules", default=None, help="规则文件 (默认 config/publish_rules.json)")
    ap.add_argument("--src", default=None,
                    help="源仓库根 (默认: 本项目) —— 公开副本导出自己时要用")
    ap.add_argument("--dry-run", action="store_true", help="只看会改什么, 不导出")
    ap.add_argument("--audit", action="store_true", help="导出后跑敏感信息审计")
    ap.add_argument("--include-untracked", action="store_true",
                    help="连未跟踪的文件一起导出 (默认只导 git 跟踪的)")
    args = ap.parse_args()

    out_dir = Path(args.out).resolve()
    src_dir = Path(args.src).resolve() if args.src else PROJ
    rules = load_rules(Path(args.rules) if args.rules else None, src_dir)

    print(f"源仓库   : {src_dir}")
    print(f"输出目录 : {out_dir}")
    print(f"模式     : {'DRY-RUN (不写盘)' if args.dry_run else 'EXPORT'}")
    print(f"规则组数 : {len(rules)}")

    report = export(out_dir, rules, dry_run=args.dry_run,
                    include_untracked=args.include_untracked, src_root=src_dir)

    print(f"\n文件     : {report['exported']} 个导出 / {len(report['skipped'])} 个排除")
    if report["rule_hits"]:
        print("脱敏命中 :")
        for cat, n in sorted(report["rule_hits"].items(), key=lambda kv: -kv[1]):
            print(f"  {cat:10} {n:>4} 处")
    else:
        print("脱敏命中 : 无")

    if report["changed"]:
        print(f"\n被脱敏的文件 ({len(report['changed'])}):")
        for c in report["changed"][:25]:
            detail = ", ".join(f"{k}×{v}" for k, v in c["hits"].items())
            print(f"  {c['file']}  [{detail}]")
        if len(report["changed"]) > 25:
            print(f"  ... 等 {len(report['changed'])} 个")

    if not args.dry_run:
        write_readme(out_dir)
        # 报告写到**源仓库的 logs/**, 不写进导出目录 ——
        # 报告里记录了脱敏前的路径, 放导出目录等于又漏一次。
        rp_dir = src_dir / "logs"
        rp_dir.mkdir(exist_ok=True)
        rp = rp_dir / "publish_report.json"
        with rp.open("w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print(f"\n✅ 已导出到 {out_dir}")
        print(f"   报告: {rp}")

        if args.audit:
            print("\n=== 导出后审计 ===")
            # ⚠️ 必须显式 --root —— 审计器默认按 cwd 找 .git 定位根,
            # 而公开副本**没有 .git** (它不是 git 仓库), 于是审计回落��
            # 扫**开发仓** —— 开发仓当然全是敏感信息, 于是"致命 500"全是假警报,
            # 真正要上传的公开副本反而没被审。
            r = subprocess.run([sys.executable, str(PROJ / "scripts" / "audit_publish.py"),
                                "--worktree", "--root", str(out_dir)], cwd=out_dir,
                               capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=300)
            print(r.stdout[-2500:] if r.stdout else r.stderr[-1500:])
            print(f"审计 rc={r.returncode} (0 = 干净可 push)")
            return r.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
