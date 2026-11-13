"""微信消息精简层的测试。

用**真实样本** —— 从 huaien profile 的微信会话里取出来的噪音形态,
不是拍脑袋编的字符串。样本存在 tests/_real_values.py 里。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from huaien.text_brief import brief, is_internal, strip_internal, summarize  # noqa: E402

try:
    from tests._real_values import (  # noqa: E402
        REAL_ASYNC_DELEGATION_BLOCK,
        REAL_CONTEXT_COMPACTION_BLOCK,
        REAL_FILE_MARK,
        REAL_INTERNAL_NOTIFICATION_BLOCK,
        REAL_PATH_LEAK,
        REAL_STATUS_LINE,
    )
except ImportError:  # pragma: no cover - 公开副本缺样本时跳过
    pytest.skip("real samples not available", allow_module_level=True)


class TestStripInternal:
    def test_async_delegation_block_is_removed(self):
        out = strip_internal(REAL_ASYNC_DELEGATION_BLOCK)
        assert "ASYNC DELEGATION" not in out
        assert len(out) < 200, "剥完还剩一大截就是没剥干净"

    def test_context_compaction_block_is_removed(self):
        out = strip_internal(REAL_CONTEXT_COMPACTION_BLOCK)
        assert "CONTEXT COMPACTION" not in out
        assert len(out) < 200

    def test_internal_notification_block_is_removed(self):
        out = strip_internal(REAL_INTERNAL_NOTIFICATION_BLOCK)
        assert "INTERNAL NOTIFICATION" not in out

    def test_runtime_status_line_is_removed(self):
        out = strip_internal(REAL_STATUS_LINE)
        assert "正在处理" not in out

    def test_local_path_is_removed(self):
        out = strip_internal(REAL_PATH_LEAK)
        assert ":\\" not in out, "本机路径还在"

    def test_normal_text_is_untouched(self):
        msg = "连接是通的，病根找到了。"
        assert strip_internal(msg) == msg

    def test_file_marker_is_kept(self):
        """产物标记要保留 —— 那是交付物, 不是噪音。"""
        msg = "报告好了 " + REAL_FILE_MARK
        out = strip_internal(msg)
        assert REAL_FILE_MARK in out

    def test_empty_input(self):
        assert strip_internal("") == ""

    def test_explicit_end_marker_keeps_following_text(self):
        """有明确结束标记的块 -> 只剥块, 后面跟的真结论要留下。"""
        msg = ("[CONTEXT COMPACTION — REFERENCE ONLY]\n压缩摘要正文\n"
               "[/OUT-OF-BAND]\n\n真结论在这里。")
        out = strip_internal(msg)
        assert "真结论在这里" in out, "精确剥块把正文也带走了"
        assert "CONTEXT COMPACTION" not in out

    def test_important_marker_is_not_treated_as_whole_message(self):
        """[IMPORTANT:] 常跟在正文后 —— 不能当"整条丢"的判据。"""
        msg = "[IMPORTANT: 3 background processes]\n\n真结论：修好了。"
        out = strip_internal(msg)
        assert "真结论" in out, "[IMPORTANT:] 被误当成整条内部通知了"


class TestIsInternal:
    def test_pure_internal_block_is_internal(self):
        assert is_internal(REAL_ASYNC_DELEGATION_BLOCK) is True
        assert is_internal(REAL_CONTEXT_COMPACTION_BLOCK) is True

    def test_real_reply_is_not_internal(self):
        assert is_internal("连接是通的，但真正的病根找到了。") is False

    def test_empty_is_internal(self):
        assert is_internal("") is True
        assert is_internal("   ") is True

    def test_file_only_message_is_not_internal(self):
        """只有产物标记的回复是有效交付, 不能当噪音丢掉。"""
        assert is_internal(REAL_FILE_MARK) is False


class TestBrief:
    def test_short_text_unchanged(self):
        assert brief("好的") == "好的"

    def test_long_text_truncated_to_limit(self):
        out = brief("啊" * 500, limit=50)
        assert len(out) <= 50 + 20, f"截断后还 {len(out)} 字"
        assert "详细见本地" in out

    def test_limit_is_respected(self):
        out = brief("测试文本。" * 100, limit=50)
        # note 本身占字, 正文应 <= 50
        body = out.replace("…详细见本地", "")
        assert len(body) <= 50

    def test_internal_only_returns_empty(self):
        assert brief(REAL_ASYNC_DELEGATION_BLOCK) == ""

    def test_message_starting_with_internal_marker_is_dropped_whole(self):
        """首行是内部标记 -> 整条丢, 连同后面跟的内容。

        为什么这么粗暴: 实测 616 条微信消息里, 内部通知**全部独占一条**
        (首行就是标记)。Hermes 的形态不会把结论和通知塞在同一条里,
        所以"首行标记 => 整条丢"不会误伤用户要看的结论。
        宁可多丢一条, 也不能让天书混进去。
        """
        mixed = REAL_ASYNC_DELEGATION_BLOCK + "\n\n真结论在这里。"
        assert brief(mixed) == ""
        assert is_internal(mixed) is True


class TestSummarize:
    def test_takes_first_sentence(self):
        out = summarize("第一句是结论。第二句是细节。第三句更多。")
        assert out.startswith("第一句是结论")
        assert "第二句" not in out

    def test_respects_limit(self):
        out = summarize("很长的结论" * 100, limit=50)
        assert len(out) <= 70

    def test_internal_only_returns_empty(self):
        assert summarize(REAL_CONTEXT_COMPACTION_BLOCK) == ""

    def test_real_reply_works(self):
        out = summarize("连接是通的，但真正的病根找到了。")
        assert "病根" in out
