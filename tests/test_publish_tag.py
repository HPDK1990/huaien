"""发布时 tag 必须跟着 HEAD 走。

## 为什么要这个

公开仓每次发布都**重建单提交历史** —— tag 如果不手动挪, 会一直
停在旧提交上。于是 Release 页面挂的是旧代码, `main` 是新代码, 两者对不上。

而且 tag 留在旧提交上, 之后推新版本号时很容易**忘了挪**,
或者挪错了 —— 这类问题在网页上看不出来, 只会让人点下载拿到旧版本。

所以: 发布脚本必须带 `--tag` 把 tag 移到 HEAD 并强推。
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "pr_tag", PROJ / "scripts" / "publish_release.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_publish_has_tag_option():
    """--tag 参数必须存在, 且默认关闭 (不带 tag 不该乱动远端 tag)。"""
    mod = _load()
    src = inspect.getsource(mod.main)
    assert '"--tag"' in src, "publish_release.py 没有 --tag 参数"
    assert 'ap.add_argument("--tag", default=None' in src, (
        "--tag 必须默认 None —— 不传就不该动远端 tag")


def test_tag_sync_moves_tag_to_head_and_force_pushes():
    """tag 必须: -f 移到 HEAD + --force 推 (公开仓历史被重建过, 普通推会被拒)。"""
    mod = _load()
    src = inspect.getsource(mod.main)
    assert '"tag", "-f", "-a", args.tag' in src, "tag 没用 -f 移到新提交"
    assert '"push", "--force", "origin", args.tag' in src, (
        "tag 没用 --force 推 —— 公开仓每次重建历史, 普通推会被拒")


def test_tag_sync_runs_after_push():
    """顺序: 先推 main, 再挪 tag —— 反了 tag 会指向还没推上去的提交。"""
    mod = _load()
    src = inspect.getsource(mod.main)
    i_push = src.find('run(push, cwd=out_dir')
    i_tag = src.find('"--force", "origin", args.tag')
    assert 0 < i_push < i_tag, "tag 同步必须在 main 推送之后"


def test_release_workflow_updates_existing_release():
    """Release 已存在时要**更新**而不是跳过 —— tag 挪了正文也得跟着变。"""
    path = PROJ / ".github" / "workflows" / "release.yml"
    raw = path.read_text(encoding="utf-8")
    assert "gh release edit" in raw, "release.yml 不会更新已存在的 Release"
    assert "gh release view" in raw, "release.yml 没做存在性判断"
