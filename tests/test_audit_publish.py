"""audit_publish.py 的测试。

## 为什么这个脚本必须有测试

它**没有测试 = 没有保障**。而且它是**最后一道闸** ——
一旦它坏掉 (静默不报 / 全报 / 打印真值), 后果是敏感信息直接进公开仓。

## 三个核心契约 (必须锁死)

1. **不打印真值** —— 审计输出会被贴进 issue / 提交信息 / 发给用户看。
   打印真值 = 二次泄露。这是最容易写错的一条。
2. **不误报** —— 假警报一多就没人看了, 闸就等于没有。
   40 位 commit SHA 不是令牌; 占位符不是真值。
3. **真值必被检出** —— 前面两条都不能牺牲这一条。

## 新脚本的通用教训

只测 "happy path 能跑通" 不够 —— 这个脚本的 bug 全在边界:
自指误报、掩码泄露、40/24 位 hex 混淆、占位符误判。
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys

import _real_values as real
import pytest

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(PROJ, "scripts", "audit_publish.py")


def _load():
    spec = importlib.util.spec_from_file_location("audit_impl", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


audit = _load()


# ── 契约 1: 不打印真值 ────────────────────────────────────────────────

def test_mask_never_shows_full_value():
    """掩码后不能包含完整原值。"""
    secret = real.ACCESS_TOKEN
    masked = audit.mask(secret)
    assert secret not in masked
    assert masked.startswith("bd7a")
    assert masked.endswith("8c")


def test_mask_short_value():
    """短值也要掩码 (前后各留 1 个字符)。"""
    assert "abcdef" not in audit.mask("abcdef")
    assert audit.mask("abc") == "ab…c"


def test_scan_text_result_contains_no_raw_secret():
    """★ 最关键的一条: 命中结果里不能出现真值。

    只测 mask() 不够 —— scan_text 可能把真值塞进别的字段。
    """
    secret = real.WECHAT_CHAT
    hits = audit.scan_text(f"chat_id = {secret}", "f.md", "会上传")
    assert hits, "应该检出"
    blob = repr(hits)
    assert secret not in blob, f"命中结果泄露了真值: {blob}"


def test_report_output_has_no_raw_secret(tmp_path, capsys):
    """★ 报告文本本身不能泄露真值 (这是会被贴给用户的)。"""
    secret = real.ACCESS_TOKEN
    hits = audit.scan_text(f"token: {secret}", "docs/x.md", "会上传")
    audit.print_report(hits)
    out = capsys.readouterr().out
    assert secret not in out
    assert "bd7a" in out  # 掩码在, 说明命中了


# ── 契约 2: 不误报 ────────────────────────────────────────────────────

def test_commit_sha_not_flagged():
    """★ 40 位 commit SHA 不是令牌 —— 文档里引用 commit id 是正常的。

    误报这条规则 = 警报刷屏 = 闸失效。
    """
    sha = "8f3a2b1c9d4e5f6a" + "<TOKEN>"
    hits = audit.scan_text(f"commit {sha}", "docs/x.md", "会上传")
    assert not [h for h in hits if h["rule"] == "24 位 hex 令牌"]


def test_40hex_without_commit_context_is_flagged():
    """★ 40 位 hex 但上下文没有 commit 关键词 -> 必须报。

    长度不能作为豁免依据: 8 位前缀 + 24 位真令牌 + 8 位后缀 也是 40 位,
    靠长度豁免 = 把真令牌一起放过。
    """
    blob = "deadbeef" + real.ACCESS_TOKEN + "cafebabe"
    hits = [h for h in audit.scan_text(f"value {blob}", "f", "会上传")
            if h["rule"] == "24 位 hex 令牌"]
    assert hits, "40位拼接体里的真令牌被漏报"


def test_24_hex_token_is_flagged():
    """24 位 hex 是真的令牌形态, 必须报。"""
    token = real.ACCESS_TOKEN
    hits = audit.scan_text(f"token {token}", "docs/x.md", "会上传")
    assert any(h["rule"] == "24 位 hex 令牌" for h in hits)


def test_placeholder_not_flagged():
    """占位符不是真值。"""
    for ph in ("<TOKEN>", "oc_xxx", "cli_xxx", "xxxx", "%LOCALAPPDATA%",
               "YOUR_API_KEY", "***"):
        hits = audit.scan_text(f"value: {ph}", "docs/x.md", "会上传")
        assert not hits, f"{ph} 被误报: {hits}"


def test_public_email_not_flagged():
    """noreply / example 邮箱是公开仓正常内容。"""
    for mail in ("a@users.noreply.github.com", "b@example.com"):
        hits = audit.scan_text(f"mail {mail}", "docs/x.md", "会上传")
        assert not [h for h in hits if h["rule"] == "邮箱"], f"{mail} 被误报"


def test_auditor_does_not_flag_itself():
    """★ 审计器扫自己必须 0 命中。

    规则字符串长得像被检内容 -> 自指误报 -> 审计器永远非零 -> 没人看。
    """
    with open(SCRIPT, encoding="utf-8") as fh:
        text = fh.read()
    hits = audit.scan_text(text, "audit_publish.py", "会上传")
    assert not hits, f"自指误报: {[(h['rule'], h['line']) for h in hits]}"


def test_rules_still_match_real_values():
    """★ 反向: 改成拼接构造后, 正则必须还能匹配真值。

    防"为了不自指而把规则改坏" —— 规则不匹配 = 闸形同虚设。
    """
    bs = chr(92)
    sample = " ".join([
        real.JWT + " ",                             # JWT
        real.ANTHROPIC_KEY + " ",                   # Anthropic
        real.GITHUB_TOKEN + " ",                    # GitHub
        real.FEISHU_APP + " ",                     # 飞书 AppID (16 位)
        real.FEISHU_CHAT_FAKE + " ",               # 飞书 chat_id
        real.WECHAT_CHAT + " ",                    # 微信 chat_id
        real.ACCESS_TOKEN + " ",                   # 24 hex
        real.LAN_IP + " ",                         # 内网 IP
        "C:" + bs + "Users" + bs + "bob ",         # Windows 路径
        "E:" + bs + "Hermes-HuaiEn ",              # 机器路径
        "-" * 5 + "BEGIN RSA PRIVATE KEY" + "-" * 5,   # 私钥
    ])
    hits = audit.scan_text(sample, "probe", "会上传")
    rules = {h["rule"] for h in hits}
    for expected in ("MiniMax/JWT 密钥", "OpenAI/Anthropic key", "GitHub token",
                     "飞书 AppID", "飞书 chat_id", "微信 chat_id",
                     "24 位 hex 令牌", "内网 IP", "Windows 用户路径",
                     "私钥文件头", "本机绝对路径 E:" + chr(92)):
        assert expected in rules, f"{expected} 规则失效, 命中: {sorted(rules)}"


# ── 契约 3: 真值必被检出 ──────────────────────────────────────────────

@pytest.mark.parametrize("secret,rule", [
    ("eyJhbGciOiJIUzI1NiJ9.abcdefghijklmnop.qrstuvwxyz12", "MiniMax/JWT 密钥"),
    ("sk-proj-abcdefghijklmnopqrstuvwxyz", "OpenAI/Anthropic key"),
    ("AKIAIOSFODNN7EXAMPLE", "AWS access key"),
    ("AIzaSyA1234567890abcdefghijklmnopqrstuv", "Google API key"),
    ("xoxb-123456789012-abcdefghijkl", "Slack token"),
    (real.SANITIZE_TARGETS["test_ip_a"], "内网 IP"),
    (real.SANITIZE_TARGETS["test_ip_c"], "内网 IP"),
    ("13912345678", "中国手机号"),
])
def test_secret_detected(secret, rule):
    """每类凭据都必须被检出 —— 漏一条 = 那条规则等于没写。"""
    hits = audit.scan_text(f"value {secret}", "f.txt", "会上传")
    assert any(h["rule"] == rule for h in hits), \
        f"{rule} 没检出 {secret[:6]}..., 命中: {[h['rule'] for h in hits]}"


def test_severity_split():
    """内网 IP / 私钥 = 致命; 机器路径 / 邮箱 = 提示。"""
    hits = audit.scan_text("ip " + real.LAN_IP + " path E:" + chr(92) + "x", "f", "会上传")
    sev = {h["rule"]: h["severity"] for h in hits}
    assert sev.get("内网 IP") == "致命"
    assert sev.get("本机绝对路径 E:" + chr(92)) == "提示"


def test_scan_text_reports_line_number():
    """必须给行号 —— 否则定位不到。"""
    hits = audit.scan_text("line1\nline2\n" + real.LAN_IP + "\n", "f", "会上传")
    assert hits and hits[0]["line"] == 3


# ── 功能: scope 区分 ──────────────────────────────────────────────────

def test_scope_field_distinguishes():
    """「会上传」和「本地私有」必须分开 —— 混在一起看不出真正风险。"""
    h1 = audit.scan_text(real.SANITIZE_TARGETS["test_ip_b"], "a", "会上传")
    h2 = audit.scan_text(real.SANITIZE_TARGETS["test_ip_b"], "b", "本地私有")
    assert h1[0]["scope"] == "会上传"
    assert h2[0]["scope"] == "本地私有"


def test_summarize_counts_fatal_only_when_uploading():
    """致命计数只算「会上传」的 —— 本地私有的不算致命。"""
    hits = (audit.scan_text(real.SANITIZE_TARGETS["test_ip_b"], "a", "会上传")
            + audit.scan_text(real.SANITIZE_TARGETS["test_ip_b"], "b", "本地私有"))
    s = audit.summarize(hits)
    assert s["fatal"] == 1
    assert s["local_only"] == 1


# ── 功能: 真实仓库行为 ────────────────────────────────────────────────

def test_cli_exit_code_clean_and_dirty(tmp_path):
    """★ 退出码语义: 0 干净 / 1 会上传有致命 / 2 只有本地私有。

    pre-commit 钩子靠这个决定放不放行 —— 语义错了 = 闸失效。
    """
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / ".git").mkdir()

    def git(*args):
        return subprocess.run(("git",) + args, cwd=repo, capture_output=True,
                              text=True, timeout=60)

    git("init", "-q")
    git("config", "user.email", "t@local")
    git("config", "user.name", "t")

    # 干净
    (repo / "docs" / "ok.md").write_text("hello world\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "init")
    r = subprocess.run([sys.executable, SCRIPT, "--worktree"], cwd=repo,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"干净仓库应 rc=0, 实际 {r.returncode}: {r.stdout}"

    # 脏
    (repo / "docs" / "bad.md").write_text("ip " + real.LAN_IP + "\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "add bad")
    r2 = subprocess.run([sys.executable, SCRIPT, "--worktree"], cwd=repo,
                        capture_output=True, text=True, timeout=120)
    assert r2.returncode == 1, f"有致命命中应 rc=1, 实际 {r2.returncode}"
    assert real.LAN_IP not in r2.stdout, "CLI 输出泄露了真值!"


def test_cli_json_output_is_machine_readable(tmp_path):
    """--json 给 CI 用 —— 必须是合法 JSON。"""
    import json as _json
    repo = tmp_path / "repo2"
    repo.mkdir()
    (repo / ".git").mkdir()
    subprocess.run(("git", "init", "-q"), cwd=repo, capture_output=True, timeout=60)
    r = subprocess.run([sys.executable, SCRIPT, "--worktree", "--json"], cwd=repo,
                       capture_output=True, text=True, timeout=120)
    data = _json.loads(r.stdout)
    assert "hits" in data and "summary" in data


def test_credential_file_detected_as_local_private():
    """凭据文件必须被点名 —— 即使它被 .gitignore 挡住。"""
    hits = audit.scan_credential_files()
    # 项目里不该有 .env / auth.json; 有的话必须是「本地私有」而非「会上传」
    for h in hits:
        if h["rule"] == "凭据文件被 git 跟踪":
            pytest.fail(f"凭据文件被跟踪了: {h['where']}")


def test_git_config_remote_password_detected():
    """remote URL 里带明文密码 = 致命。"""
    hits = audit.scan_git_config()
    for h in hits:
        assert h["rule"] == "git remote 含明文密码"
        assert h["scope"] == "会上传"


def test_24hex_embedded_in_longer_hex_is_flagged():
    """★ 令牌夹在更长 hex 串里也必须检出。

    负向前后看的写法会漏掉这种 —— sha256 前缀 + 令牌 + 后缀的拼接形态。
    """
    blob = "deadbeef" + real.ACCESS_TOKEN + "cafebabe"
    hits = [h for h in audit.scan_text(f"x {blob}", "f", "会上传")
            if h["rule"] == "24 位 hex 令牌"]
    assert hits, "夹在长 hex 串里的令牌被漏报"


def test_exact_40hex_commit_sha_not_flagged():
    """commit 语境下的 40 位 SHA, 不报。"""
    sha = "8f3a2b1c9d4e5f6a" + "<TOKEN>"
    hits = [h for h in audit.scan_text(f"commit {sha}", "f", "会上传")
            if h["rule"] == "24 位 hex 令牌"]
    assert not hits, "commit 语境的 40 位 SHA 被误报"


# ── 契约 4: 测试夹具 vs 真凭据 ────────────────────────────────────────
# 这条最容易出错, 而且两个方向都出过事:
#   判据太宽 -> **真凭据被放过** (比误报严重得多)
#   判据太窄 -> 夹具误报 -> 警报刷屏 -> 闸失效
# 所以必须两个方向都锁死。

REAL_CREDENTIALS = [
    ("AIzaSyD9xK2mQ7vB4nR8tY1uI4oP6aS0dF5gH2jK9", "Google API key"),
    ("ghp_9kMn2pQ7vX4bR8tY1uI3oP6aS0dF5gH2jK9", "GitHub token"),
    ("sk-ant-api03-Zx9K2mQ7vB4nR8tY1uI4oP6aS0dF5gH2", "OpenAI/Anthropic key"),
    ("AKIAIOSFODNN7REALKEY", "AWS access key"),
    ("xoxb-9kMn2pQ7vX4bR8tY1uI3oP", "Slack token"),
    ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0"
     ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U", "MiniMax/JWT 密钥"),
]


@pytest.mark.parametrize("secret,rule", REAL_CREDENTIALS)
def test_real_credential_always_reported_in_source(secret, rule):
    """★ 真凭据在源码文件里**必须**被报。"""
    hits = [h for h in audit.scan_text(f"v {secret}", "huaien/app.py", "会上传")
            if h["rule"] == rule]
    assert hits, f"{rule} 真凭据未检出"


@pytest.mark.parametrize("secret,rule", REAL_CREDENTIALS)
def test_real_credential_always_reported_in_test_file(secret, rule):
    """★ 真凭据在**测试文件**里也必须被报。

    ⚠️ 这条最反直觉 —— "在测试里应该是假 key"。
    但真 key 混进测试文件是可能的, 而放过它的代价是别人拿你的 key 烧钱。
    """
    hits = [h for h in audit.scan_text(f"v {secret}", "tests/test_x.py", "会上传")
            if h["rule"] == rule]
    assert hits, f"{rule} 真凭据在测试文件里被放过"


@pytest.mark.parametrize("secret,rule", [
    ("AIzaSyA1234567890abcdefghijklmnopqrstuv", "Google API key"),
    ("ghp_abcdefghijklmnopqrstuvwxyz0123456789", "GitHub token"),
    ("sk-ant-api03-abcdefghijklmnopqrstuvwxyz", "OpenAI/Anthropic key"),
    ("AKIAIOSFODNN7EXAMPLE", "AWS access key"),
    ("xoxb-123456789012-abcdefghijkl", "Slack token"),
    ("eyJhbGciOiJIUzI1NiJ9.abcdefghijklmnop.qrstuvwxyz12", "MiniMax/JWT 密钥"),
])
def test_obvious_fixture_exempted_in_test_file(secret, rule):
    """明显编的夹具在测试文件里不该报 —— 公开仓必须有测试用例。"""
    hits = [h for h in audit.scan_text(f"v {secret}", "tests/test_x.py", "会上传")
            if h["rule"] == rule]
    assert not hits, f"夹具被误报: {hits}"


def test_fixture_in_source_file_still_reported():
    """夹具出现在**源码**里仍然要报 —— 只有测试文件才豁免。

    源码里出现 key 形态 = 可疑, 人工看一眼。
    """
    secret = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    hits = [h for h in audit.scan_text(f"v {secret}", "huaien/app.py", "会上传")
            if h["rule"] == "GitHub token"]
    assert hits, "源码里的 key 形态没报"


def test_looks_sequential_threshold():
    """连续段阈值必须 >= 10 —— AWS 官方示例有 8 位连续字母。"""
    assert audit._looks_sequential("abcdefghijklmnopqrstuv") is True
    assert audit._looks_sequential("IOSFODNN7REAL") is False
    assert audit._looks_sequential("9kMn2pQ7vX4bR8tY") is False
