"""运行时环境清理 —— 让怀恩在脏 PYTHONPATH 下也能正常工作。

## 实测坑 (2026-11-12 克隆验收时暴露)

从 Hermes 终端启动怀恩时, Hermes 运行时会把 `PYTHONPATH` 指向它自己的
site-packages。而 `PYTHONPATH` 的路径**排在 venv 之前** (sys.path 顺序),
于是 `import PIL` 拿到的是**别的环境**的 Pillow —— 版本对不上就报:

    ImportError: cannot import name '_imaging' from 'PIL'

表现是"自检界面层挂了", 但根因不在界面, 在环境。

## 为什么不能靠"用户别在 Hermes 终端里跑"

双击 bat 启动确实没有这个问题, 但从终端跑是**正常用法**。
怀恩自己不该因为调用方的环境变量而崩。

## 为什么只剔 PYTHONPATH 里的

不动 venv / 标准库 / 当前目录 —— 只剔那些"调用方强塞进来的"路径。
别人的 PYTHONPATH 可能是故意的, 但对怀恩来说那只会造成版本冲突。
"""

from __future__ import annotations

import os
import sys


def clean_sys_path() -> int:
    """把 PYTHONPATH 注入的路径从 sys.path 里剔掉, 返回剔了几条。

    必须在**任何第三方 import 之前**调用 —— sys.path 一旦被用来
    解析过模块, 再改就不生效了 (importlib 的缓存)。
    """
    pp = os.environ.get("PYTHONPATH", "")
    if not pp:
        return 0
    dirty = {os.path.normcase(os.path.abspath(p)) for p in pp.split(os.pathsep) if p}
    before = len(sys.path)
    sys.path[:] = [p for p in sys.path
                   if os.path.normcase(os.path.abspath(p or ".")) not in dirty]
    return before - len(sys.path)


def clean_env() -> dict[str, str]:
    """给子进程用的干净环境 —— 清掉 PYTHONPATH。

    子进程 (比如截图) 必须**从干净环境启动**, 否则它继承的 PYTHONPATH
    又会把别的环境的包顶上来。
    """
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return env
