"""管家面板 —— 前台/网页/手机端共用的那一份。

## 为什么要这个文件

管家逻辑的真身在**项目根**的 `scripts/butler.py` (CLI + 规则解析 + 检查器)。
`web.py` 和 `app.py` 都要用同一份, 所以这里把它**加载进来**而不是复制一遍 ——
复制就会出现两份规则、两套判定, 迟早不一致。

`scripts/butler.py` 里的核心常量 (POLICY_FILE / CHECKERS / SEVERITY) 一并带过来。

## 用法

```python
from huaien import butler
panel = butler.panel()      # -> {"待你处理": [...], "安静": bool, ...}
```
"""
from __future__ import annotations

import contextlib
import importlib.util
import os
import time

_SCRIPTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
_IMPL_PATH = os.path.join(_SCRIPTS, "butler.py")

if not os.path.exists(_IMPL_PATH):
    raise ImportError(f"找不到管家实现: {_IMPL_PATH}")

_spec = importlib.util.spec_from_file_location("huaien_butler_impl", _IMPL_PATH)
_impl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_impl)

load_policy = _impl.load_policy
assess = _impl.assess
build_panel = _impl.build_panel
render_text = _impl.render_text
CHECKERS = _impl.CHECKERS
SEVERITY = _impl.SEVERITY

# 规则命中状态在几十秒内不会变, 但重算要拉起 `hermes kanban list` 子进程 (3-10 秒)。
# 用户打开手机端要秒回, 所以缓存一小会儿。
_TTL = 30.0
_cache: dict = {"at": 0.0, "data": None}


def panel(force: bool = False) -> dict:
    """跑一遍全部规则, 返回统一面板。这是网页/手机端唯一的入口。

    force=True 时跳过缓存 (cron 主动汇报时用)。
    """
    now = time.time()
    if not force and _cache["data"] is not None and now - _cache["at"] < _TTL:
        return _cache["data"]
    rules = load_policy()
    hits = assess(rules)
    data = build_panel(hits, rules)
    _cache.update(at=now, data=data)
    return data


def warmup() -> None:
    """后台预热 —— 服务起来时先把面板算好, 第一次打开手机端就是秒回。"""
    with contextlib.suppress(Exception):   # 预热失败不该拖垮服务
        panel(force=True)
