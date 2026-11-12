"""GitHub Actions workflow 的本地检查。

## 为什么需要这个

CI 连挂 6 次才找到根因 (Windows stdout 编码), 每次都是推到 GitHub
才发现 —— 本地跑一遍就能拦住的错误, 不该花 6 个版本去试。

这里把踩过的坑固化成断言:

- **YAML 能解析** —— 语法错根本进不了 CI
- **`on:` 触发器存在** —— PyYAML 按 YAML 1.1 把 `on` 解析成布尔 True,
  所以要用 `d.get("on") or d.get(True)` 两种都查
- **audit 必须 `fetch-depth: 0`** —— 浅克隆拿不到完整历史, 审计会漏
- **audit 必须设 UTF-8** —— Windows runner 的 locale 编码会打爆中文输出
- **release 必须 `contents: write`** —— 少这个权限建不出 Release
- **引用的本地脚本要存在** —— 路径写错要到 CI 才发现
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

PROJ = Path(__file__).resolve().parent.parent
WF_DIR = PROJ / ".github" / "workflows"


def _workflows() -> list[Path]:
    return sorted(WF_DIR.glob("*.yml")) + sorted(WF_DIR.glob("*.yaml"))


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _triggers(doc: dict) -> dict:
    """PyYAML(YAML 1.1) 把裸 `on` 解析成布尔 True —— 两种都认。"""
    return doc.get("on") or doc.get(True) or {}


def test_workflows_exist():
    """仓库里必须至少有一个 workflow —— 审计闸不能只是文档。"""
    assert _workflows(), ".github/workflows/ 是空的"


def test_every_workflow_parses_and_has_triggers():
    for path in _workflows():
        doc = _load(path)
        assert isinstance(doc, dict), f"{path.name} 顶层不是字典"
        trig = _triggers(doc)
        assert trig, f"{path.name} 没有 on: 触发器"
        assert "jobs" in doc, f"{path.name} 没有 jobs"
        for job_name, job in doc["jobs"].items():
            steps = job.get("steps")
            assert steps, f"{path.name}:{job_name} 没有 steps"
            for st in steps:
                assert "uses" in st or "run" in st, (
                    f"{path.name}:{job_name} 有 step 既没 uses 也没 run")


def test_audit_workflow_covers_full_history():
    """审计必须拿完整历史 —— 浅克隆等于没审。"""
    path = WF_DIR / "audit.yml"
    assert path.exists(), "audit.yml 不存在"
    doc = _load(path)
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            if step.get("uses", "").startswith("actions/checkout"):
                assert step.get("with", {}).get("fetch-depth") == 0, (
                    "audit.yml 的 checkout 缺 fetch-depth: 0 —— "
                    "浅克隆拿不到完整历史, 审计会漏")
                return
    raise AssertionError("audit.yml 里找不到 checkout 步骤")


def test_audit_workflow_forces_utf8():
    """Windows runner 的 locale 编码会打爆中文/emoji 输出 (踩过 UnicodeEncodeError)。"""
    raw = (WF_DIR / "audit.yml").read_text(encoding="utf-8")
    assert "PYTHONUTF8" in raw, "audit.yml 没设 PYTHONUTF8=1"
    assert "PYTHONIOENCODING" in raw, "audit.yml 没设 PYTHONIOENCODING=utf-8"


def test_release_workflow_has_write_permission():
    """建 Release 需要 contents: write —— 少这个权限 workflow 必然失败。"""
    path = WF_DIR / "release.yml"
    assert path.exists(), "release.yml 不存在"
    doc = _load(path)
    perms = doc.get("permissions")
    assert perms, "release.yml 没声明 permissions"
    assert perms.get("contents") == "write", (
        f"release.yml 的 permissions.contents = {perms.get('contents')!r}, "
        f"需要 'write' 否则建不出 Release")


def test_release_workflow_is_tag_triggered():
    """Release 靠 tag 推送触发 —— 没有 tag 触发器就永远不发布。"""
    doc = _load(WF_DIR / "release.yml")
    trig = _triggers(doc)
    push = trig.get("push") or {}
    tags = push.get("tags") or []
    assert tags, "release.yml 的 on.push.tags 没配 —— tag 推上来不会触发"


def test_workflow_local_script_references_exist():
    """workflow 里引用的本地脚本必须真的存在 —— 路径写错要到 CI 才发现。"""
    for path in _workflows():
        raw = path.read_text(encoding="utf-8")
        for ref in re.findall(r"scripts/[A-Za-z0-9_\-./]+\.(?:py|bat)", raw):
            assert (PROJ / ref).exists(), f"{path.name} 引用了不存在的 {ref}"
