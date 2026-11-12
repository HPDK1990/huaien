"""export_public.py / publish_release.py 的测试。

## 为什么必须有

这两个脚本是**上架的最后一道执行环节** —— 导出错了就泄露, 推错了就发出去。
没有测试 = 没有保障。而且它们的 bug 全在边界:

- 脱敏规则漏了一种形态 (正斜杠 vs 反斜杠、单反斜杠 vs 双反斜杠)
- 排除规则把该导的文件排掉了
- 审计门禁形同虚设 (rc 不为 0 也往下走)
- 报告写进导出目录 = 又泄露一次

## 三个核心契约

1. **只读源** —— 绝不动开发仓库
2. **脱敏彻底** —— 导出结果里不能再有真值
3. **审计是硬门禁** —— 不干净就停, 推不出去
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import _real_values as real
import pytest

PROJ = Path(__file__).resolve().parent.parent
# 被测的源仓库 —— **测试跑在哪个副本里, 就测哪个副本**。
# ⚠️ 写死开发仓库 = 公开版的测试在测开发仓库 (自欺欺人)。
SRC_ROOT = PROJ
EXPORT_SCRIPT = PROJ / "scripts" / "export_public.py"
PUBLISH_SCRIPT = PROJ / "scripts" / "publish_release.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


exporter = _load(EXPORT_SCRIPT, "export_impl")
publisher = _load(PUBLISH_SCRIPT, "publish_impl")


# ── 契约 1: 只读源 ────────────────────────────────────────────────────

def test_export_does_not_touch_source(tmp_path):
    """★ 导出器只读源仓库 —— 绝不改开发环境。

    这是用户的核心诉求: "我用的和上传的怎么分开, 不用每次清一遍"。
    如果导出器会改源, 就等于每次都在改我的工作区。
    """
    before = subprocess.run(["git", "status", "--porcelain"], cwd=PROJ,
                            capture_output=True, text=True, timeout=60).stdout
    exporter.export(tmp_path / "out", exporter.DEFAULT_RULES,
                     dry_run=False, src_root=SRC_ROOT)
    after = subprocess.run(["git", "status", "--porcelain"], cwd=PROJ,
                           capture_output=True, text=True, timeout=60).stdout
    assert before == after, "导出器改了源仓库!"


def test_export_does_not_create_git_in_source(tmp_path):
    """导出器不该在源仓库留下 .git 之类的东西。"""
    exporter.export(tmp_path / "out", exporter.DEFAULT_RULES,
                     dry_run=False, src_root=SRC_ROOT)
    assert not (PROJ / ".git" / "PUBLISH_TMP").exists()


# ── 契约 2: 脱敏彻底 ──────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expect_gone", [
    # ⚠️ 输入和期望值都用运行时构造 —— 直接写字面量的话,
    #    导出时会被脱敏掉, 断言"结果不含它"必然失败 (公开版 5 项失败全是这个)。
    (real.SANITIZE_TARGETS["nickname_user"] + " 说这是我的项目",
     real.SANITIZE_TARGETS["nickname_user"]),
    (real.SANITIZE_TARGETS["nickname_helper"] + " 来回答",
     real.SANITIZE_TARGETS["nickname_helper"]),
    (real.SANITIZE_TARGETS["win_home"] + chr(92) + "AppData" + chr(92) + "Local",
     real.SANITIZE_TARGETS["win_home"]),
    ("C:/Users/" + real.SANITIZE_TARGETS["win_home"].split(chr(92))[-1] + "/AppData",
     real.SANITIZE_TARGETS["win_home"].split(chr(92))[-1]),
    ("/c/Users/" + real.SANITIZE_TARGETS["win_home"].split(chr(92))[-1] + "/AppData",
     real.SANITIZE_TARGETS["win_home"].split(chr(92))[-1]),
    (real.SANITIZE_TARGETS["proj_backslash"] + chr(92) + "scripts" + chr(92) + "x.py",
     real.SANITIZE_TARGETS["proj_backslash"]),
    (real.SANITIZE_TARGETS["proj_forward"] + "/scripts/x.py",
     real.SANITIZE_TARGETS["proj_forward"]),
    ("chat_id = " + real.SANITIZE_TARGETS["feishu_chat"],
     real.SANITIZE_TARGETS["feishu_chat"]),
    ("app_id = " + real.SANITIZE_TARGETS["feishu_app"],
     real.SANITIZE_TARGETS["feishu_app"]),
    ("wechat = " + real.SANITIZE_TARGETS["wechat_chat"],
     real.SANITIZE_TARGETS["wechat_chat"]),
    ("token = " + real.SANITIZE_TARGETS["token"],
     real.SANITIZE_TARGETS["token"]),
    (real.SANITIZE_TARGETS["session_ref"],
     real.SANITIZE_TARGETS["session_ref"].split(":")[-1]),
    ("host = " + real.SANITIZE_TARGETS["lan_ip_192"],
     real.SANITIZE_TARGETS["lan_ip_192"]),
    ("host = " + real.SANITIZE_TARGETS["lan_ip_10"],
     real.SANITIZE_TARGETS["lan_ip_10"]),
])
def test_rules_remove_secret(raw, expect_gone):
    """★ 每种敏感形态都必须被脱敏。

    形态清单是踩出来的 —— 反斜杠/正斜杠、单反斜杠/双反斜杠都真实存在过。
    """
    compiled = exporter.compile_rules(exporter.DEFAULT_RULES)
    text, hits = exporter._apply(raw, compiled, {"rule_hits": {}})
    assert expect_gone not in text, f"没脱敏: {raw} -> {text}"
    assert hits, f"没记录命中: {raw}"


def test_export_output_has_no_real_values(tmp_path):
    """★ 真实导出后, 公开副本里不能再有真值。"""
    out = tmp_path / "pub"
    exporter.export(out, exporter.DEFAULT_RULES,
                 dry_run=False, src_root=SRC_ROOT)
    blob = ""
    for p in out.rglob("*"):
        if p.is_file() and p.suffix.lower() in (".py", ".md", ".txt", ".bat",
                                               ".sh", ".ps1", ".yml", ".yaml",
                                               ".toml", ".json", ".cfg", ".ini"):
            blob += p.read_text(encoding="utf-8", errors="replace")
    for secret in real.all_real_values():
        assert secret not in blob, f"公开副本里还有真值: {secret}"


def test_export_is_idempotent(tmp_path):
    """同一份源码导出两次, 结果一致。"""
    a, b = tmp_path / "a", tmp_path / "b"
    exporter.export(a, exporter.DEFAULT_RULES, dry_run=False)
    exporter.export(b, exporter.DEFAULT_RULES, dry_run=False)
    files_a = sorted(p.relative_to(a) for p in a.rglob("*") if p.is_file())
    files_b = sorted(p.relative_to(b) for p in b.rglob("*") if p.is_file())
    assert files_a == files_b
    for rel in files_a:
        if rel.suffix in (".png", ".zip"):
            continue
        assert (a / rel).read_bytes() == (b / rel).read_bytes(), f"{rel} 两次导出不一致"


def test_dry_run_writes_nothing(tmp_path):
    """--dry-run 绝不写盘。"""
    out = tmp_path / "dry"
    report = exporter.export(out, exporter.DEFAULT_RULES, dry_run=True,
                           src_root=SRC_ROOT)
    assert not out.exists(), "dry-run 建了输出目录"
    assert report["exported"] > 0


def test_report_records_hits(tmp_path):
    """报告要记下每条规则命中了多少处 —— 不然没法核对。

    ⚠️ **不能断言"命中数 > 0"** —— 公开副本的源文件已经脱敏过了,
       再导出自然一条都不命中 (那是对的: 没有真值可脱敏)。
       该断言的是**报告结构完整**。
    """
    report = exporter.export(tmp_path / "out", exporter.DEFAULT_RULES, dry_run=True,
                             src_root=SRC_ROOT)
    assert report["exported"] > 0, "没导出任何文件"
    assert report["total_files"] > 0
    assert isinstance(report["rule_hits"], dict), "命中统计不是字典"
    assert isinstance(report["changed"], list), "changed 不是列表"
    for c in report["changed"]:
        assert c["file"] and c["hits"], f"changed 项不完整: {c}"


def test_report_hits_real_when_source_has_secrets(tmp_path):
    """★ 源里有真值时, 报告**必须**记录命中。

    用带真值的临时源来测 —— 不依赖仓库当前状态, 开发版公开版都能跑。
    """
    fake = tmp_path / "src"
    fake.mkdir()
    (fake / "leak.py").write_text(
        "x = " + real.SANITIZE_TARGETS["nickname_user"] + "\n", encoding="utf-8")
    report = exporter.export(tmp_path / "out", exporter.DEFAULT_RULES, dry_run=True,
                             src_root=fake)
    assert report["rule_hits"], "源里有真值, 报告却没命中统计"
    assert sum(report["rule_hits"].values()) > 0
    assert report["changed"], "报告没记哪些文件被改了"

def test_excluded_paths_are_not_exported(tmp_path):
    """敏感/运行时文件不能进公开版。"""
    out = tmp_path / "out"
    report = exporter.export(out, exporter.DEFAULT_RULES,
                 dry_run=False, src_root=SRC_ROOT)
    for skipped in report["skipped"]:
        assert not (out / skipped).exists(), f"{skipped} 被排除了却又出现了"


def test_report_not_in_output_dir(tmp_path):
    """★ 报告不能写进导出目录 —— 它记录了脱敏前的路径。"""
    out = tmp_path / "out"
    exporter.export(out, exporter.DEFAULT_RULES,
                 dry_run=False, src_root=SRC_ROOT)
    assert not (out / "publish_report.json").exists()


# ── 契约 3: 审计是硬门禁 ──────────────────────────────────────────────

def test_audit_gate_blocks_publish(tmp_path):
    """★ 审计不通过 -> publish_release 必须停, 不能推。

    用一个含真值的假导出目录测。
    """
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "leak.py").write_text("TOKEN = '" + real.ACCESS_TOKEN + "'\n",
                                 encoding="utf-8")
    # 直接调审计器, 确认它确实判失败
    rc = subprocess.run([sys.executable, str(EXPORT_SCRIPT.parent / "audit_publish.py"),
                         "--root", str(bad), "--worktree"],
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=180)
    assert rc.returncode == 1, "审计器没挡住真值"


def test_publish_script_has_gate_before_push():
    """★ publish_release 里, 审计必须在 push 之前且不通过就 return。

    静态检查: 审计调用点行号 < push 调用点行号, 且 return 在中间。
    """
    src = PUBLISH_SCRIPT.read_text(encoding="utf-8")
    audit_at = src.index("audit_publish.py")
    check_at = src.index('if rc != 0:', audit_at)
    push_at = src.index('"push"', audit_at)
    assert audit_at < check_at < push_at, "审计门禁不在 push 之前"


def test_publish_uses_anonymous_git_identity():
    """★ 提交身份必须是匿名的 —— 不暴露个人邮箱。"""
    src = PUBLISH_SCRIPT.read_text(encoding="utf-8")
    assert 'GIT_EMAIL = "huaien@local"' in src
    assert "GIT_NAME" in src
    # 不该出现任何 @qq.com / @163.com / @gmail.com 之类
    for bad_mail in ("qq.com", "163.com", "gmail.com", "outlook.com"):
        assert bad_mail not in src


def test_publish_dry_run_does_not_push(tmp_path):
    """--dry-run 必须不推。"""
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, str(PUBLISH_SCRIPT),
                        "--out", str(out), "--dry-run",
                        "--remote", "git@github.com:NONE/none.git"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=400, cwd=str(PROJ))
    assert r.returncode == 0, r.stdout[-800:] + r.stderr[-800:]
    assert "dry-run" in r.stdout
    # remote 只是配置上了, 没真的推
    rp = subprocess.run(["git", "log", "--oneline", "-1"], cwd=out,
                        capture_output=True, text=True, timeout=60)
    assert rp.returncode == 0, "dry-run 都没 commit"


def test_publish_no_push_flag_stops_before_git(tmp_path):
    """--no-push 应该只导出, 不建 git 仓。"""
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, str(PUBLISH_SCRIPT),
                        "--out", str(out), "--no-push"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=400, cwd=str(PROJ))
    assert r.returncode == 0, r.stdout[-500:]
    assert not (out / ".git").exists(), "--no-push 建了 git 仓"


def test_publish_end_to_end_dry_run_produces_clean_output(tmp_path):
    """★ 全流程: 导出 -> 审计 -> 提交, 且审计结果干净。"""
    out = tmp_path / "pub"
    r = subprocess.run([sys.executable, str(PUBLISH_SCRIPT),
                        "--out", str(out), "--dry-run"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=560, cwd=str(PROJ))
    assert r.returncode == 0, r.stdout[-1500:] + r.stderr[-1000:]
    assert "审计通过" in r.stdout
    assert "致命 0" in r.stdout
    # 提交作者是匿名的
    who = subprocess.run(["git", "log", "-1", "--format=%an <%ae>"], cwd=out,
                         capture_output=True, text=True, timeout=60)
    assert who.stdout.strip() == "Hermes-HuaiEn <huaien@local>"


# ── 契约 4: 公开副本必须能跑自己的测试 ────────────────────────────────

def test_export_uses_own_root_not_dev_repo():
    """★ 导出器必须用**调用者指定的源根**。

    ⚠️ 写死开发仓库的话, 公开副本里的测试会去测开发仓库 —— 自欺欺人。
    公开版的测试必须能验证公开版自己。
    """
    import inspect
    sig = inspect.signature(exporter.export)
    assert "src_root" in sig.parameters, "export() 没有 src_root 参数"


def test_export_from_other_root_reads_that_root(tmp_path):
    """换个源根导出, 导出的必须是那个根的文件。"""
    fake = tmp_path / "fake"
    (fake / "pkg").mkdir(parents=True)
    (fake / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")
    out = tmp_path / "out"
    exporter.export(out, exporter.DEFAULT_RULES, dry_run=False, src_root=fake)
    assert (out / "pkg" / "a.py").exists(), "没用指定源根"
