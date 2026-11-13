"""怀恩 — 上架前敏感信息审计 (可复跑, 可挂 pre-commit)

## 为什么要有这个

上架 GitHub 之后, **每次 `git push` 都在发布**。一次误提交 = 一次泄露,
而且 git 历史一旦推上去, 删文件不等于删历史。

用户的要求 (2026-11-04):
- **别让人通过 GitHub 信息摸到我这里** —— 身份、内网拓扑、本机路径
- **别让我替别人交大模型的钱** —— API key / token / 订阅凭据

## 设计约束 (踩过的坑)

1. **脚本自己不打印真值** —— 只打印掩码 (前 4 + 后 2) 和位置。
   审计输出会被贴进 issue / 提交信息 / 贴给用户看, 打印真值等于二次泄露。
   真值要靠掩码去原文件里找。
2. **区分「会上传」和「本地私有」** —— `versions/backup/` 里有 Hermes 的
   config.yaml 备份 (可能含 api_key), 但它被 .gitignore 挡住。
   两类混在一起报 = 看不出真正的风险。
3. **commit SHA 不算凭据** —— 40 位 hex 有 79 个, 全是文档里引用的 commit id。
   令牌是 24 位 hex。分开两个规则, 否则满屏假警报, 警报一多就没人看了。
4. **只报「会上传」的为致命** —— 本地私有的只提示「确认被 ignore 了」。

## 用法

```bash
python scripts/audit_publish.py              # 审计工作区 + 历史
python scripts/audit_publish.py --worktree   # 只扫工作区 (快)
python scripts/audit_publish.py --json       # 机器可读 (给 pre-commit 用)
```

退出码: 0 = 干净, 1 = 有致命命中 (会上传的部分), 2 = 只有本地私有命中。

---
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys

# 前缀用字符码拼出来 —— 源码里不出现明文
_PFX = "".join(chr(c) for c in (111, 57, 99, 113, 56, 48))


# 审计目标目录: 优先用当前工作目录 (在别的仓库里跑就用那个仓库),
# 回退到脚本所在的项目根。
# ⚠️ 写死项目根 = 脚本无法在别的仓库复用, 测试也只能测到自家项目。
# 审计目标目录。
# ⚠️ 靠 cwd 猜会出错: 导出目录 (没有 .git) 跑审计时会回退到源仓库,
#    等于审计错了对象 —— 报告说"干净"其实干净的是源仓库, 不是导出结果。
# 所以: 显式 --root 优先 > cwd > 脚本所在项目根。
def _default_root() -> str:
    cwd = os.getcwd()
    if os.path.isdir(os.path.join(cwd, ".git")):
        return cwd
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ⚠️ **Windows 上 stdout 走 locale 编码 (中文系统 GBK, CI runner cp1252)** ——
#    输出中文/emoji 时直接 UnicodeEncodeError 崩溃 (rc=1)。
#    本地开发者通常设了 PYTHONIOENCODING=utf-8 所以看不出来, CI 上必炸。
#    这里自己接管 stdout, 不依赖外部环境变量。
for _stream in ("stdout", "stderr"):
    _s = getattr(sys, _stream, None)
    if _s is not None and hasattr(_s, "reconfigure"):
        with contextlib.suppress(ValueError, OSError):
            _s.reconfigure(encoding="utf-8", errors="replace")

# ⚠️ 个人昵称 —— **运行时构造**, 不写明文。
#    字符码必须核对 —— 写错一位规则就匹配不到任何东西, 静默失效。
#    (曾踩过: chr(36215)="起" 而不是"赵", 310 处昵称全没脱敏, 审计却报"干净"。)
_NICK_USER = chr(32769) + chr(36213)        # 用户
_NICK_HELPER = chr(23567) + chr(33738)      # 助手

PROJ = _default_root()


def set_root(path: str) -> None:
    """显式指定审计目标目录 (CLI --root)。"""
    global PROJ
    PROJ = os.path.abspath(path)

# ── 规则表 ──────────────────────────────────────────────────────────────
# (名字, 正则, 严重级, 说明)
#   致命 = 一旦推上去就是泄露
#   提示 = 需要人工判断 (可能是示例、可能是真值)
# 明显是占位符/示例的, 命中了不算事
PLACEHOLDER = re.compile(
    r"^(?:x{3,}|\*{3,}|<[^>]+>|\$\{[^}]+\}|%[A-Z_]+%|"
    r"YOUR[_-]?\w*|EXAMPLE|\.\.\.|placeholder|REDACTED)", re.I)

# 允许出现在公开文件里的邮箱域名 (公共 noreply / 示例)
EMAIL_OK = re.compile(r"@(?:users\.noreply\.github\.com|example\.(?:com|org)|noreply@)", re.I)

# 占位形态的"邮箱" (文档里写 XXX@im.wechat / xxx@im.bot 这种)
# ⚠️ 只认**明确的占位标记** —— 真地址不会长这样
#   <CHANNEL_ID>@im.wechat   (占位 id)
#   <CHANNEL_ID>...@im.wechat   (省略号)
#   xxx@im.bot             (纯占位)
#   <...>@...              (尖括号占位)
EMAIL_PLACEHOLDER = re.compile(
    r"(?:xxx|\.\.\.|<[^>]+>|_XXX|_xxx)(?=@)|"
    r"^(?:[a-z0-9_]*_xxx|[a-z0-9_]*_\.\.\.)@", re.I)

# Hermes iLink 机器人的官方 id 形态 (xxx@im.bot) —— 源码文档里的示例
ILINK_EXAMPLE = re.compile(r"^[a-z0-9]{8,}@im\.bot$", re.I)

# git remote 的 SSH 地址 —— 公开仓必须有, 不是隐私
GIT_SSH_URL = re.compile(r"^git@(?:github|gitlab|bitbucket|codeberg|gitee)\.")

# commit SHA 在文档里的上下文关键词 —— 旁边有这些词才是 commit 引用
SHA_CONTEXT = re.compile(r"(?i)commit|sha[0-9]?\b|git\b|HEAD\b|rev\b|引用")

# abc / 数字连续片段 (测试夹具常用)
ABC_RUN = re.compile(r"(?:abcdefghijklmnopqrstuvwxyz)+", re.I)
NUM_RUN = re.compile(r"(?:0123456789)+")

# 固定假尾巴 (一眼编的)
FAKE_CRED_TAIL = re.compile(
    r"(?:wxyz|nopq|qrstu|stuvw|uvwxy|vwxyz|ghijk|hijkl|ijklm|klmno|"
    r"456789|345678|234567|123456|MPLE|EXAMPLE|ZZZZ|aaaa|1111)\d*$", re.I)

# 文档里举例说明私钥格式时会出现这行字 —— 不是真私钥
# 文档里举例说明私钥格式时的说明词 (表格行 / 代码块内)
PRIVATE_KEY_DOC = re.compile(r"例如|示例|格式|header|私钥|PRIVATE KEY", re.I)

# 文档里举例说明路径写法时的占位词
EXAMPLE_PATH_WORDS = re.compile(
    r"[完整路目]*(完整路径|路径|目录|文件|占位|示例|xxx|Xxx)")

TEXT_EXT = {".py", ".md", ".txt", ".bat", ".sh", ".ps1", ".toml", ".yaml", ".yml",
            ".json", ".cfg", ".ini", ".html", ".js", ".css", ".gitignore"}
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".zip", ".ico", ".pdf", ".exe", ".db",
              ".mp4", ".mp3", ".wav", ".woff", ".woff2"}

RULES: list[tuple[str, str, str, str]] = [
    ("MiniMax/JWT 密钥", r"\beyJ[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}",
     "致命", "模型服务的 API key —— 泄露 = 别人拿你的 key 烧钱"),
    ("OpenAI/Anthropic key", r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{16,}",
     "致命", "同上"),
    ("GitHub token", r"\bgh[pousr]_[A-Za-z0-9]{30,}",
     "致命", "能推仓库、改设置"),
    ("AWS access key", r"\bAKIA[0-9A-Z]{16}\b", "致命", "云资源凭据"),
    # ⚠️ 长度不能写死 —— 真实 Google key 长度会变 (39 位), 写死 {35}
    #    会让**真 key 完全不匹配规则** (漏报), 而长度刚好对上 35 的
    #    测试夹具反被报出来 (误报)。用 {30,} 变长。
    ("Google API key", r"\bAIza[0-9A-Za-z_\-]{30,}", "致命", ""),
    ("Slack token", r"\bxox[baprs]-[A-Za-z0-9\-]{10,}", "致命", ""),
    ("私钥文件头", "-" * 5 + "BEGIN [A-Z ]*PRIVATE KEY" + "-" * 5,
     "致命", ""),
    ("飞书 AppID", r"\bcli_[A-Za-z0-9]{16}\b", "致命", "能定位到你的飞书应用"),
    ("飞书 chat_id", r"\boc_[A-Za-z0-9]{20,}\b", "致命", "能定位到你的会话"),
    ("微信 chat_id", r"\b" + _PFX + r"_[A-Za-z0-9]{10,}\b", "致命", "能定位到你的会话"),
    # 24 位 hex = 令牌。用**窗口法**匹配, 不加负向前后看 ——
    # 负向前后看会漏掉"夹在更长 hex 串里"的真令牌 (sha256 前缀+令牌+后缀)。
    # commit SHA 的豁免在 scan_text 里按上下文关键词判定。
    ("24 位 hex 令牌", r"[0-9a-f]{24}",
     "致命", "PWA 访问口令这类"),
    ("内网 IP", r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
                 r"192\.168\.\d{1,3}\.\d{1,3}|"
                 r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b",
     "致命", "暴露内网拓扑"),
    ("Windows 用户路径", r"[A-Za-z]:\\+Users\\+([A-Za-z0-9_\.\-]{2,})",
     "致命", "暴露本机用户名"),
    ("MSYS 用户路径", r"/c/Users/([A-Za-z0-9_\.\-]{2,})", "致命", "同上"),
    ("邮箱", r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",
     "提示", "commit 邮箱 / 联系方式"),
    ("中国手机号", r"(?<!\d)1[3-9]\d{9}(?!\d)", "致命", ""),
    # ⚠️ 个人昵称 —— 公开仓里必须中性化 (用户拍板 A 方案: 昵称也不公开)。
    #    规则值运行时构造, 源码里不出现明文。
    ("个人昵称", re.escape(_NICK_USER) + "|" + re.escape(_NICK_HELPER),
     "致命", "能定位到真人的称呼"),
    # ⚠️ 规则字符串本身会被自己扫到 (自指误报) ——
    #    所以路径部分用 chr(92) 拼出来, 不写字面量。
    ("本机绝对路径 E:" + chr(92), r"\bE:" + re.escape(chr(92)) +
     r"[A-Za-z0-9_\u4e00-\u9fff" + re.escape(chr(92)) + r"\-]+",
     "提示", "机器专属路径, 换机即失效"),
    ("本机绝对路径 C:" + chr(92), r"\bC:" + re.escape(chr(92)) + r"Users" +
     re.escape(chr(92)) + r"[A-Za-z0-9_\.\-]+",
     "致命", "同上"),
]

# 明显是占位符/示例的, 命中了不算事
PLACEHOLDER = re.compile(
    r"^(?:x{3,}|\*{3,}|<[^>]+>|\$\{[^}]+\}|%[A-Z_]+%|"
    r"YOUR[_-]?\w*|EXAMPLE|\.\.\.|placeholder|REDACTED)", re.I)

# ★ 测试夹具 —— 假 key 长得就像真 key。
#   公开仓里必须有测试用例 (不测就没保障), 但它们会被审计器当泄露。
#   判据: 明显的假值尾巴 (wxyz / nopq / MPLE / EXAMPLE / ...)。
#   ⚠️ 只认这几个尾巴 —— 放太宽 = 真 key 也能编个假尾巴混过去。
# ⚠️ 只认这几个尾巴 —— 放太宽 = 真 key 也能编个假尾巴混过去。
# 这些值都是 a-z 连续片段 (abcdefghijklmnopqrstuvwxyz) 或 1234567890 序列。
# abc 连续片段 (abcdefghijklmnopqrstuvwxyz 的子串) —— 测试夹具常用
ABC_RUN = re.compile(r"(?:abcdefghijklmnopqrstuvwxyz)+", re.I)
# 数字连续片段 (0123456789 的子串)
NUM_RUN = re.compile(r"(?:0123456789)+")

FAKE_CRED_TAIL = re.compile(
    r"(?:wxyz|nopq|qrstu|stuvw|uvwxy|vwxyz|ghijk|hijkl|ijklm|klmno|"
    r"456789|345678|234567|123456|MPLE|EXAMPLE|ZZZZ|aaaa|1111)\d*$", re.I)


def _is_fake_credential(raw: str, where: str = "") -> bool:
    """这个值是不是明显的测试夹具。

    ⚠️ **判据只能靠"上下文", 不能靠"内容长什么样"**。

    曾经的错误做法: 看结尾是不是 abc 连续序列 / 固定尾巴。
    为什么不行: 那是在**猜** —— 猜宽了会放过真 key (实测放过一个真实 key),
    猜窄了又误报夹具。内容特征和"真假"没有可靠对应关系。

    正确判据 (两条, 都靠上下文):
    1. **所在文件是测试文件** (tests/ 或 test_*.py) + 值带常见假前缀
       -> 夹具, 豁免。
       公开仓必须有测试用例 (不测就没保障), 里面的假 key 是**必需品**。
       ⚠️ 只豁免测试目录/测试文件, 不豁免整个仓库 —— 真 key 混进
       测试文件是可能的, 所以还要叠加"带假前缀"这一条。
    2. 内容是**一眼可辨的占位** (xxx / <TOKEN> / YOUR_KEY / EXAMPLE)。
    """
    # 1. 一眼可辨的占位符 —— 任何位置都豁免
    #    (xxx / <TOKEN> / YOUR_API_KEY / %LOCALAPPDATA% ...)
    if is_placeholder(raw):
        return True
    # 2. 固定假尾巴 (wxyz / EXAMPLE / MPLE) —— **只在测试文件里**豁免。
    #    ⚠️ 不能无条件豁免: 源码文件里出现 `...EXAMPLE` 形态的值仍然可疑,
    #    手机号 `13912345678` 的尾巴 `345678` 更不能放过。
    if _is_test_file(where) and FAKE_CRED_TAIL.search(raw):
        return True
    # 3. 测试文件里的夹具 —— 但**光有假前缀不够**:
    #    真 key 也可能带同样的前缀 (AKIA / AIza / ghp_ 都是真实格式)。
    #    必须**同时**满足: 在测试文件里 + 带假前缀 + 内容是明显编的连续序列。
    return (_is_test_file(where) and _has_fake_prefix(raw)
            and _looks_sequential(raw))


def _looks_sequential(raw: str) -> bool:
    """这个值的内容看起来是"编的" (连续序列), 不是随机生成的。

    判据: 剥掉前缀后, 主体里存在 >= 8 位的**字母连续段**或**数字连续段**。
    ⚠️ 随机 key 出现 8 位连续的概率极低 (26^-7) —— 真 key 不会被误豁免。
    """
    body = raw
    for prefix in ("sk-ant-api03-", "sk-proj-", "sk-", "ghp_", "ghs_", "gho_",
                   "AKIA", "AIza", "xoxb-", "xoxp-", "xoxa-"):
        if body.startswith(prefix):
            body = body[len(prefix):]
            break
    best_alpha = best_num = cur_alpha = cur_num = 0
    for ch in body:
        if ch.isalpha():
            cur_alpha += 1
            best_alpha = max(best_alpha, cur_alpha)
            cur_num = 0
        elif ch.isdigit():
            cur_num += 1
            best_num = max(best_num, cur_num)
            cur_alpha = 0
        else:
            cur_alpha = cur_num = 0
    # ⚠️ 10 位而不是 8 位 —— AWS 官方示例 key `AKIAIOSFODNN7EXAMPLE`
    #    的主体里有 `IOSFODNN` 这 8 位连续字母, 8 位判据会把它放过。
    #    夹具用 abcdefghijklmnopqrstuvwxyz (26 位连续), 10 位足够区分。
    return best_alpha >= 10 or best_num >= 10


# 测试文件的判定 (目录名或文件名)
TEST_PATH_PARTS = {"tests", "test", "fixtures"}


def _is_test_file(where: str) -> bool:
    """这个文件是不是测试文件。"""
    w = (where or "").replace("\\", "/")
    parts = w.split("/")
    if any(p in TEST_PATH_PARTS for p in parts[:-1]):
        return True
    name = parts[-1].lower() if parts else ""
    return name.startswith("test_") or name.endswith("_test.py")


# 明显的假前缀 —— 真 key 不会用这些前缀
FAKE_PREFIXES = ("sk-ant-api03-", "sk-proj-", "sk-", "ghp_", "ghs_", "gho_",
                 "AKIA", "AIza", "xoxb-", "xoxp-", "xoxa-")


def _has_fake_prefix(raw: str) -> bool:
    return raw.startswith(FAKE_PREFIXES)


def is_placeholder(hit: str) -> bool:
    """一眼可辨的占位符。"""
    return bool(PLACEHOLDER.match(hit.strip()))


# 文档里举例说明路径写法时的占位词 (中文"完整""路径""目录"之类)
EXAMPLE_PATH_WORDS = re.compile(r"[完整路目]*(完整路径|路径|目录|文件|占位|示例|xxx|Xxx)")


def _is_private_key_doc_line(text: str, m: re.Match) -> bool:
    """这一行是文档里**举例说明**私钥格式, 不是真私钥。

    判据: 行内除了私钥头本身, 还有说明性文字 (表格 |、示例词、代码块标记)。
    ⚠️ 纯私钥头 (行内只有 `-----BEGIN ... PRIVATE KEY-----`) 必须报。
    """
    line_start = text.rfind("\n", 0, m.start()) + 1
    line_end = text.find("\n", m.end())
    line = text[line_start:line_end if line_end > 0 else len(text)].strip()
    # 去掉私钥头本身, 看还剩什么
    rest = line.replace(m.group(0), "").strip(" |`\t")
    if not rest:
        return False        # 纯私钥头 -> 不豁免
    # ⚠️ 必须是**结构化的文档行**: markdown 表格行, 或代码块里的说明。
    #    仅仅"行内有某些词"不行 —— 一行挤了 30 个测试值时会被误判成文档。
    is_table_row = line.startswith("|") and line.endswith("|")
    in_code_fence = line.startswith("```") or line.startswith("    ")
    if not (is_table_row or in_code_fence):
        return False
    return bool(PRIVATE_KEY_DOC.search(line))


def _is_example_path(text: str, m: re.Match) -> bool:
    """这是文档里**举例说明**的路径写法, 不是真实路径。

    两类判据:
    1. 路径片段里含中文占位词 (完整路径 / 目录 / 文件 / 示例 ...)
    2. 盘符后**紧跟省略号** —— `E:\\...\\xxx` 这种是写法示例
       (真路径不会一上来就省略)
    """
    line_start = text.rfind("\n", 0, m.start()) + 1
    line_end = text.find("\n", m.end())
    line = text[line_start:line_end if line_end > 0 else len(text)]
    if EXAMPLE_PATH_WORDS.search(line):
        return True
    # 盘符后立刻是省略号 -> 写法示例
    tail = text[m.end():m.end() + 12]
    return bool(re.match(r"^\s*(?:\\\\|\\|/)*\s*(?:\.\.\.|…)", tail))


def mask(value: str) -> str:
    """掩码 —— 够定位, 不足以泄露。"""
    if len(value) <= 8:
        return value[:2] + "…" + value[-1:]
    return f"{value[:4]}…{value[-2:]}"


def sh(*args: str, timeout: int = 240) -> tuple[int, str]:
    """跑一个命令, 返回 (rc, stdout)。失败不抛异常 —— 审计要继续跑完。"""
    try:
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, cwd=PROJ)
        return r.returncode, (r.stdout or "")
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""


def tracked_files() -> list[str]:
    """git 跟踪的文件 (真正会上传的那批)。

    ⚠️ 目标目录没有 .git 时 (比如导出的公开副本) -> 退回扫整个目录树,
    否则一条都扫不到, 报告会说"干净" —— 那是**假的干净**。
    """
    _, out = sh("git", "ls-files", "-z")
    files = [f for f in out.split("\0") if f]
    if files:
        return files
    if not os.path.isdir(os.path.join(PROJ, ".git")):
        return _walk_all_files()
    return files


def _walk_all_files() -> list[str]:
    """没有 git 时, 列出目录下所有可审计文件。"""
    out = []
    for root, dirs, files in os.walk(PROJ):
        dirs[:] = [d for d in dirs if d not in (".git", ".venv", "venv",
                                                "__pycache__", "node_modules",
                                                ".pytest_cache", ".ruff_cache")]
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), PROJ).replace("\\", "/")
            out.append(rel)
    return out


def ignored_files() -> list[str]:
    """被 .gitignore 排除但存在于磁盘的文件 (本地私有)。"""
    _, out = sh("git", "status", "--porcelain", "--ignored", "-z")
    files = []
    for rec in out.split("\0"):
        if not rec:
            continue
        if rec.startswith("!!"):
            files.append(rec[3:] if len(rec) > 3 else rec)
    return files


def scan_text(text: str, where: str, scope: str) -> list[dict]:
    hits = []
    # 审计器自身含规则定义和示例值 (长得像被检内容) —— 结构性自指, 豁免。
    # ⚠️ 只豁免这一个文件, 不是目录 —— 白名单越宽越危险。
    # ⚠️ 只豁免这两个文件 —— 它们含规则定义和示例值 (长得像被检内容)。
    #    白名单越宽越危险: 只按**文件名**豁免, 不按目录。
    if where.replace("\\", "/").endswith(("audit_publish.py", "export_public.py")):
        return hits
    for name, pat, sev, note in RULES:
        for m in re.finditer(pat, text):
            raw = m.group(0)
            if name == "邮箱" and EMAIL_OK.search(raw):
                continue
            # 占位形态的邮箱 (XXX@im.wechat / xxx@im.bot) —— 明显不是真地址
            if name == "邮箱" and EMAIL_PLACEHOLDER.search(raw):
                continue
            # iLink 机器人示例 id (源码文档里引用的官方示例)
            if name == "邮箱" and ILINK_EXAMPLE.match(raw):
                continue
            # git remote 的 SSH 地址 —— 公开仓必须有, 不是隐私
            if name == "邮箱" and GIT_SSH_URL.match(raw):
                continue
            # commit SHA 不是凭据 —— 但只在上下文有 commit 关键词时才豁免,
            # 否则 40 位拼接体里的真令牌会被一起放过。
            if name in ("24 位 hex 令牌", "32 位 hex 凭据") and _is_commit_sha(text, m):
                continue
            # ★ commit message 里的 SHA —— **它本身就是 commit SHA**, 不必靠
            #   上下文关键词判断 (commit message 可能只有一句"发布怀恩公开版",
            #   没有任何 commit 字样, 但第一行的 40 位 hex 百分百是 SHA)。
            if name in ("24 位 hex 令牌", "32 位 hex 凭据") and _is_msg_sha_line(text, m):
                continue
            # 测试夹具假 key (公开仓必须有测试用例)
            if _is_fake_credential(raw, where):
                continue
            # 文档里举例说明私钥格式的那一行, 不是真私钥。
            # ⚠️ 判据必须严: 只豁免"行内有说明性文字"的,
            #    纯私钥头 (行内只有 -----BEGIN...PRIVATE KEY-----) 必须报。
            if name == "私钥文件头" and _is_private_key_doc_line(text, m):
                continue
            # 文档里举例说明路径写法的占位 (如 [[FILE:E:\\完整\\路径\\x.ext]])
            if name.startswith("本机绝对路径") and _is_example_path(text, m):
                continue
            if is_placeholder(raw):
                continue
            hits.append({
                "rule": name, "severity": sev, "where": where, "scope": scope,
                "masked": mask(raw), "note": note,
                "line": text.count("\n", 0, m.start()) + 1,
            })
    return hits


# 审计器自身含规则定义 (正则片段长得像被检内容) —— 结构性自指, 白名单兜底。
# ⚠️ 只豁免这一个文件, 不是目录 —— 白名单越宽越危险。
SELF_EXEMPT = {"scripts/audit_publish.py", "scripts/export_public.py"}


def scan_worktree() -> list[dict]:
    hits = []
    for rel in tracked_files():
        path = os.path.join(PROJ, rel)
        if not os.path.isfile(path):
            continue
        ext = os.path.splitext(rel)[1].lower()
        if ext in BINARY_EXT or (ext not in TEXT_EXT and ext != ""):
            continue
        if rel.replace("\\", "/") in SELF_EXEMPT:
            continue
        try:
            if os.path.getsize(path) > 4_000_000:
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        hits += scan_text(text, rel, "会上传")
    return hits


def scan_history(limit_commits: int = 0) -> list[dict]:
    """逐 commit 扫全历史 —— 工作区干净不代表历史干净。

    ⚠️ **不能**用 `git log --all -p` 一次扫完: 那把所有 commit 的 diff 拼成一大坨,
    报告里的行号是拼接后的偏移, **定位不到具体文件** —— 等于没报。
    逐 commit 扫才能给出 "哪个 commit 的哪个文件"。
    """
    hits: list[dict] = []
    _, out = sh("git", "log", "--all", "--format=%H", timeout=300)
    shas = [s for s in out.splitlines() if s.strip()]
    if limit_commits:
        shas = shas[:limit_commits]
    for sha in shas:
        _, diff = sh("git", "show", "--format=", "--no-color", sha, timeout=180)
        if not diff:
            continue
        # 拆成 per-file 段, 记下文件路径
        cur_file = "?"
        buf: list[str] = []
        for line in diff.splitlines():
            if line.startswith("diff --git "):
                if buf:
                    hits += scan_text("\n".join(buf),
                                      f"{sha[:8]}:{cur_file}", "会上传")
                    buf = []
                m = re.match(r"diff --git a/(\S+) b/(\S+)", line)
                cur_file = m.group(2) if m else "?"
                continue
            if line.startswith(("index ", "--- ", "+++ ", "@@")):
                continue
            buf.append(line)
        if buf:
            hits += scan_text("\n".join(buf), f"{sha[:8]}:{cur_file}", "会上传")
    return hits


def scan_commit_messages() -> list[dict]:
    _, out = sh("git", "log", "--all", "--format=%H%n%an <%ae>%n%s%n%b%n---")
    return scan_text(out, "<commit message>", "会上传")


def scan_local_private() -> list[dict]:
    """本地私有文件里的命中 —— 只提示, 不算致命 (它们被 ignore 挡住了)。"""
    hits = []
    for rel in ignored_files():
        path = os.path.join(PROJ, rel)
        if not os.path.isfile(path):
            continue
        ext = os.path.splitext(rel)[1].lower()
        if ext in BINARY_EXT:
            continue
        try:
            if os.path.getsize(path) > 20_000_000:
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        hits += scan_text(text, rel, "本地私有")
    return hits


def scan_git_config() -> list[dict]:
    """.git/config 里不该有明文密码 (remote URL 里带 user:pass@ 的形态)。"""
    hits = []
    cfg = os.path.join(PROJ, ".git", "config")
    if not os.path.isfile(cfg):
        return hits
    with open(cfg, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    for m in re.finditer(r"url\s*=\s*(\S+)", text):
        url = m.group(1)
        if re.search(r"://[^/\s]*:[^/\s]*@", url):
            hits.append({"rule": "git remote 含明文密码", "severity": "致命",
                         "where": ".git/config", "scope": "会上传",
                         "masked": mask(url), "note": "换成 SSH 或 credential helper",
                         "line": 0})
    return hits


def summarize(hits: list[dict]) -> dict:
    by_rule: dict[str, list[dict]] = {}
    for h in hits:
        by_rule.setdefault(h["rule"], []).append(h)
    return {
        "total": len(hits),
        "fatal": sum(1 for h in hits if h["severity"] == "致命"
                     and h["scope"] == "会上传"),
        "hint": sum(1 for h in hits if h["severity"] == "提示"
                    and h["scope"] == "会上传"),
        "local_only": sum(1 for h in hits if h["scope"] == "本地私有"),
        "by_rule": {k: len(v) for k, v in sorted(by_rule.items())},
        "files": sorted({h["where"] for h in hits if h["scope"] == "会上传"}),
    }


def print_report(hits: list[dict]) -> None:
    fatal = [h for h in hits if h["severity"] == "致命" and h["scope"] == "会上传"]
    hint = [h for h in hits if h["severity"] == "提示" and h["scope"] == "会上传"]
    local = [h for h in hits if h["scope"] == "本地私有"]

    if not hits:
        print("✅ 干净: 工作区 + git 历史 + commit message 均无敏感命中")
        return

    def block(title: str, items: list[dict]) -> None:
        if not items:
            return
        print(f"\n{title} ({len(items)})")
        groups: dict[tuple, list[dict]] = {}
        for h in items:
            groups.setdefault((h["rule"], h["masked"]), []).append(h)
        for (rule, val), lst in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            # 按文件聚合, 让"哪个文件有"一目了然
            byfile: dict[str, int] = {}
            for h in lst:
                byfile[h["where"]] = byfile.get(h["where"], 0) + 1
            top = sorted(byfile.items(), key=lambda kv: -kv[1])[:6]
            locs = ", ".join(f"{w}({n})" for w, n in top)
            more = f" 等 {len(byfile)} 个文件" if len(byfile) > 6 else ""
            print(f"  [{rule}] {val}  x{len(lst)}")
            print(f"      {locs}{more}")

    block("🔴 致命 (会随 push 发布)", fatal)
    block("🟡 提示 (需人工判断)", hint)
    block("🔵 本地私有 (被 .gitignore 挡住, 不会上传)", local)

    if fatal:
        print("\n→ 修完再 push。真值不在这里打印 —— 用上面的 位置:行号 去原文件找。")


def scan_credential_files() -> list[dict]:
    """专项: Hermes/大模型的凭据文件有没有混进仓库。

    用户最在意的一条: **别让我替别人交大模型的钱**。
    真正会烧钱的东西不在文档里, 在这些文件里:
    - `auth.json`        —— 各 provider 的 api_key
    - `.env`             —— 密钥环境变量
    - `config.yaml`      —— Hermes 的 provider 配置 (可能内嵌 key)
    - `secrets.json` / `credentials*`

    这些文件**被 .gitignore 挡住了**, 但要确认:
    ① 确实没被跟踪  ② 确实没被历史收进去  ③ 内容里的 key 是真的还是占位
    """
    hits = []
    tracked = set(tracked_files())
    for root, dirs, files in os.walk(PROJ):
        dirs[:] = [d for d in dirs if d not in (".git", ".venv", "__pycache__",
                                                "node_modules", ".pytest_cache",
                                                ".ruff_cache")]
        for f in files:
            low = f.lower()
            if not (low in ("auth.json", ".env", "secrets.json", "credentials.json")
                    or low.startswith("credentials")
                    or low.endswith((".env", ".key", ".pem", ".p12", ".pfx"))):
                continue
            rel = os.path.relpath(os.path.join(root, f), PROJ).replace("\\", "/")
            if rel in tracked:
                hits.append({"rule": "凭据文件被 git 跟踪", "severity": "致命",
                             "where": rel, "scope": "会上传",
                             "masked": "<该文件>", "note": "立刻 git rm --cached", "line": 0})
            else:
                hits.append({"rule": "凭据文件存在(已被 ignore, 未跟踪)", "severity": "提示",
                             "where": rel, "scope": "本地私有",
                             "masked": "<该文件>", "note": "确认内容不进仓库", "line": 0})
    return hits


# commit SHA 在文档里的上下文关键词
SHA_CONTEXT = re.compile(r"(?i)commit|sha[0-9]?\b|git\b|HEAD\b|rev\b|引用")

def _is_msg_sha_line(text: str, m: re.Match) -> bool:
    """这个 hex 命中是不是 commit message 里的 **SHA 行**。

    commit message 的扫描格式是 `%H\n%an <%ae>\n%s\n%b\n---`,
    第一行就是 40 位 SHA。24 位规则会切它一刀 -> 误报"令牌"。

    判据: 命中所在行**去掉这个 hex 后什么都不剩** (SHA 独占一行)。
    ⚠️ 只豁免**独占一行**的 —— 夹在文字里的 hex 不豁免。
    """
    line_start = text.rfind("\n", 0, m.start()) + 1
    line_end = text.find("\n", m.end())
    line = text[line_start:line_end if line_end > 0 else len(text)].strip()
    # ⚠️ 命中可能是 40 位 SHA 的**中间子串** (24 位规则切了一刀),
    #    所以不能比 line == m.group(0) —— 要看**整行是不是纯 hex**。
    return bool(re.fullmatch(r"[0-9a-fA-F]{40,}", line))


def _is_commit_sha(text: str, m: re.Match) -> bool:
    """这个 hex 命中是不是 commit SHA (不是凭据)。

    ⚠️ **不能靠长度判**。40 位 hex 有两种形态, 内容都是随机, 分不出来:
      - 真正的 commit SHA
      - 拼接体 (8 位前缀 + 24 位真令牌 + 8 位后缀)
    靠长度豁免 = 把真令牌一起放过。

    所以改成: 40 位**且上下文有 commit 关键词**才豁免。
    """
    i, j = m.start(), m.end()
    while i > 0 and text[i - 1] in "0123456789abcdefABCDEF":
        i -= 1
    while j < len(text) and text[j] in "0123456789abcdefABCDEF":
        j += 1
    if (j - i) != 40:
        return False
    lo, hi = max(0, i - 60), min(len(text), j + 60)
    return bool(SHA_CONTEXT.search(text[lo:hi]))


def main() -> int:
    ap = argparse.ArgumentParser(description="怀恩 上架前敏感信息审计")
    ap.add_argument("--worktree", action="store_true", help="只扫工作区")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--local-private", action="store_true",
                    help="也扫被 ignore 的本地私有文件")
    ap.add_argument("--history-limit", type=int, default=0,
                    help="只扫最近 N 个 commit (调试用, 0 = 全扫)")
    ap.add_argument("--root", default=None,
                    help="显式指定审计目标目录 (默认: 当前仓库)")
    args = ap.parse_args()
    if args.root:
        set_root(args.root)

    hits: list[dict] = []
    hits += scan_worktree()
    hits += scan_git_config()
    hits += scan_credential_files()
    if not args.worktree:
        hits += scan_history(args.history_limit)
        hits += scan_commit_messages()
    if args.local_private:
        hits += scan_local_private()

    if args.json:
        print(json.dumps({"hits": hits, "summary": summarize(hits)},
                         ensure_ascii=False, indent=2))
    else:
        print_report(hits)

    s = summarize(hits)
    if not args.json:
        print(f"\n统计: 致命 {s['fatal']} · 提示 {s['hint']} · 本地私有 {s['local_only']}")
    return 1 if s["fatal"] else (2 if s["local_only"] else 0)


if __name__ == "__main__":
    sys.exit(main())
