"""pytest 全局配置。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

# 微信通道守护者的测试**现在属于本仓库** (2026-11-12 起):
# 守护脚本真身已进 scripts/weixin_guard.py, 测试优先用 repo 内的那份,
# 找不到才回退到本机安装位置, 再找不到就 skip。
#
# ⚠️ 之前这里是 collect_ignore = ["test_weixin_guard.py"] —— 那 13 项
#    "绝不用 --replace 替换活网关" 的契约测试**从没被收集过**,
#    等于没有测试。现在必须保持为空数组。
collect_ignore = []


@pytest.fixture(scope="session")
def load_module():
    """按项目相对路径加载模块 —— 9 个测试文件共用这一份样板。

    **每次都新建模块实例** (和原先各文件里的 `_load()` 语义一致) ——
    不做缓存: 模块的全局状态不能跨测试共享, 否则测试会互相影响。
    """
    def _load(relpath: str):
        path = PROJ / relpath
        spec = importlib.util.spec_from_file_location(f"_mod_{relpath}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    return _load
