"""版本号一致性 —— 四处必须一样。

## 为什么要有这个测试

版本号散在 4 个地方 (VERSION / pyproject.toml / huaien/__init__.py /
CHANGELOG), 手改很容易漏。实测漂到过: VERSION=0.3.0 但 pyproject=0.1.1。

发布时 GitHub Release 的 tag 和包里的版本对不上, 用户装到的是旧版。
"""

from __future__ import annotations

import re
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent


def _version() -> str:
    return (PROJ / "VERSION").read_text(encoding="utf-8").strip()


def test_version_file_is_valid():
    v = _version()
    assert re.fullmatch(r"\d+\.\d+\.\d+", v), f"版本号格式不对: {v}"


def test_pyproject_matches_version():
    t = (PROJ / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', t, re.M)
    assert m, "pyproject.toml 里没有 version 字段"
    assert m.group(1) == _version(), f"pyproject={m.group(1)} != VERSION={_version()}"


def test_package_init_matches_version():
    t = (PROJ / "huaien" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', t)
    assert m, "huaien/__init__.py 里没有 __version__"
    assert m.group(1) == _version(), f"__init__={m.group(1)} != VERSION={_version()}"


def test_changelog_has_current_version():
    """CHANGELOG 必须有当前版本的条目 —— 漏了就是发版没记。"""
    t = (PROJ / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{_version()}]" in t, f"CHANGELOG 里没有 [{_version()}] 的条目"


def test_changelog_versions_are_unique():
    """同一版本不能有两条 —— 合并时最容易犯。"""
    t = (PROJ / "CHANGELOG.md").read_text(encoding="utf-8")
    vers = re.findall(r"^##\s*\[?([0-9.]+)\]?", t, re.M)
    dupes = {v for v in vers if vers.count(v) > 1}
    assert not dupes, f"CHANGELOG 有重复版本: {dupes}"
