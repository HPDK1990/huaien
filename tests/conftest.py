"""pytest 全局配置。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

# 微信通道守护者的测试**不属于本仓库**:
# 它测的是 Hermes home 里的 weixin_guard.py, 仓库里没有那个文件。
# 靠 fallback 到本机绝对路径才跑得通 —— 换个机器就是 error, 不是 skip。
collect_ignore = ["test_weixin_guard.py"]


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
