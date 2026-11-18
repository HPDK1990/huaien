"""怀恩 — 微信消息精简层

## 为什么需要这层

用户在手机上看微信, 字多了直接跳过。他 2026-11-12 上午连续抱怨过五次:

    10:32 "另一件事。你给我的回答要精简下。不要发这么一大堆推导过程。"
    10:59 "太多了, 精简下, 主要查下今天上午还在运行的会话"
    11:01 "除非我问细节, 否则你就需要在 50 个字内给我回复。字数太多了看不过来"
    11:04 "你的回答里还有 text 推理内容。那块不要放到给我的回答里。"
    11:15 "读取下刚刚你微信发我的信息。太多东西了。不是说只给我总结的东西么。"

**光在 SOUL 里写"要精简"不够** —— 模型会忘, 也会在长任务里逐渐跑偏。
所以在**发送之前**再过一道: 这层不依赖模型听话, 是机械的、可测的。

实测噪音 (微信会话, 2026-11-13 统计):

| 噪音源                        | 条数 | 字符量    |
|------------------------------|------|-----------|
| `[ASYNC DELEGATION COMPLETE]` | 28   | 312325    |
| `[INTERNAL NOTIFICATION]`     | 23   | 205911    |
| `[CONTEXT COMPACTION]`        | 4    | 154895    |
| `[[FILE:` 产物标记            | 10   | 138534    |
| `[PRIOR CONTEXT]`             | 1    | 25379     |

这些标记块**都不是用户的话** —— 是 Hermes 运行时推进会话的系统消息。
模型有时会把它们当提问回显出去, 用户就在手机上收到一堆天书。

## 关键事实: 内部通知独占一条消息

实测 616 条微信消息里, 内部通知**从来不是混在正文中间的**, 都是**独立一条**
(首行就是 `[ASYNC DELEGATION COMPLETE ...]` 之类)。所以判定用**首行标记**,
不用"剥完还剩多少字" —— 后者在块内有空行时会误判。

## 取舍: 首行是内部标记 -> 整条丢

粗一点, 但符合实际: 内部通知在实测里**永远独占一条消息**, 结论不会和它同条。
所以"首行命中内部标记 => 整条丢掉"不会误伤用户要看的结论, 却能保证
天书一个字都不进他的手机。

宁可多丢一条 (用户会再说"这条没收到"), 也不能让 3000 字符的系统标记流过去。

## 四件事

1. `is_internal(text)` —— 整条是不是纯内部通知 (该整条丢掉)
2. `strip_internal(text)` —— 剥掉内部块、运行时状态行、本机路径
3. `brief(text, limit)` —— 压到 N 字以内
4. `summarize(text, limit)` —— 取第一句结论再压到 N 字

零第三方依赖, 只用标准库。
"""
from __future__ import annotations

import re

# --- 内部标记 -------------------------------------------------------------
# 首行命中其中之一 = 整条消息是系统通知, 不是用户的话。
# 注意: `[IMPORTANT:` **不在这个列表里** —— 它常跟在正文后面而不是独占一条,
# 拿它当"整条丢"的判据会误伤用户真正要看的结论 (实测踩过)。
_BLOCK_START = re.compile(
    r"\[(?:"
    r"CONTEXT COMPACTION\b"
    r"|PRIOR CONTEXT\b"
    r"|ASYNC DELEGATION COMPLETE\b"
    r"|INTERNAL NOTIFICATION\b"
    r"|OUT-OF-BAND USER MESSAGE\b"
    r")",
    re.I,
)

# 有**明确结束标记**的块 —— 可以精确切掉, 不伤正文
_EXPLICIT_BLOCKS = [
    re.compile(r"\[CONTEXT COMPACTION\b.*?\[/OUT-OF-BAND\]", re.S | re.I),
    re.compile(r"\[CONTEXT COMPACTION\b.*?\[END OF PRIOR CONTEXT[^\]]*\]", re.S | re.I),
    re.compile(r"\[PRIOR CONTEXT\b.*?(?=\n\s*\n\[|\Z)", re.S | re.I),
]

# 单行的运行时状态通知 (用户在微信里见过: "正在处理——3 分钟 — 第 1/500 轮迭代")
_STATUS_LINE = re.compile(
    r"^\s*[\"'\u201c]?(正在处理|Model fallback|context compression|compaction)[^\n]{0,200}$",
    re.M | re.I,
)

# 产物标记 [[FILE:...]] [[IMG:...]] [[PATH:...]] —— **必须保护**, 不能被路径清理误伤
_FILE_MARK = re.compile(r"\[\[(?:FILE|IMG|PATH):[^\]]*\]\]", re.I)

# 本机绝对路径 —— 用户在手机上用不上, 而且是信息泄漏
_LOCAL_PATH = re.compile(r"[A-Za-z]:\\[^\s\"'<>\]\|]+")


def _protect_file_marks(text: str) -> tuple[str, list[str]]:
    """把产物标记换成占位符, 清理完再放回去。"""
    marks: list[str] = []

    def _grab(m: re.Match) -> str:
        marks.append(m.group(0))
        return f"\x00F{len(marks) - 1}\x00"

    return _FILE_MARK.sub(_grab, text), marks


def _restore_file_marks(text: str, marks: list[str]) -> str:
    for i, m in enumerate(marks):
        text = text.replace(f"\x00F{i}\x00", m)
    return text


def is_internal(text: str) -> bool:
    """整条消息是不是纯内部通知 —— 该整条丢掉, 不该回显。

    判据: **首行**命中内部标记前缀。内部通知在实测里从来独占一条消息,
    所以看首行比"剥完剩多少字"可靠 (块内有空行时后者会误判)。
    """
    if not text or not text.strip():
        return True
    first_line = text.lstrip().split("\n", 1)[0]
    return bool(_BLOCK_START.match(first_line))


def strip_internal(text: str) -> str:
    """剥掉内部块、运行时状态行、本机路径。保留正常内容和产物标记。"""
    if not text:
        return ""

    guarded, marks = _protect_file_marks(text)

    # 先精确剥掉**有明确结束标记**的块 —— 剥完正文还在就留着正文
    out = guarded
    for pat in _EXPLICIT_BLOCKS:
        out = pat.sub("", out)
    out = _STATUS_LINE.sub("", out)
    out = _LOCAL_PATH.sub("", out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    out = _restore_file_marks(out, marks)

    # 剥完之后首行还是内部标记 -> 没有明确结束标记的通知 (ASYNC / INTERNAL
    # NOTIFICATION 之类), 它们在实测里总是独占一条 -> 整条丢
    if is_internal(out):
        return ""
    return out


def brief(text: str, limit: int = 50, note: str = "…详细见本地") -> str:
    """压到 limit 字以内。

    - 先剥内部标记
    - 剥完为空 -> 返回空串 (调用方应当整条不发)
    - 超长 -> 截断到 limit, 末尾加 note
    """
    out = strip_internal(text)
    if not out:
        return ""
    if len(out) <= limit:
        return out
    keep = max(0, limit - len(note))
    return out[:keep].rstrip() + note


def summarize(text: str, limit: int = 50) -> str:
    """给"一句话结论"用: 取第一句, 再压到 limit。

    第一句通常是结论 —— 用户要的就是这个。
    句子边界认中文句号/问号/叹号, 以及换行。
    """
    out = strip_internal(text)
    if not out:
        return ""
    m = re.search(r"^[\s\S]{0,400}?([。！？!?\n])", out)
    if m:
        out = out[: m.end(1)]
    return brief(out, limit)
