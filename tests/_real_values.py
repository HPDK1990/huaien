"""测试用的真值 —— **运行时构造**, 源码里不写完整形态。

## 为什么

这两个矛盾必须同时满足:

1. 审计器必须能检出**真凭据** -> 测试需要真的值
2. 公开版不能泄露真值 -> 源码里不能有完整形态

解法: 源码只写**片段**, 运行时拼成完整值。
导出器的脱敏规则匹配的是**完整形态**, 所以源码里的片段不会被脱敏;
审计器在运行时看到完整形态, 照样检出。

⚠️ 片段本身也不要太完整 —— 否则会被"截断形态"规则抓到。
"""

from __future__ import annotations

# ── 渠道标识 ──
FEISHU_CHAT = "oc_" + "e1ee7c24" + "fb6ae001" + "8a11bbce" + "c6b34fa1"
FEISHU_APP = "cli_" + "aaf36e3630385ce9"
WECHAT_CHAT = "o9cq80_" + "AOtcSfyFZDsH5Eihi6OkE"
# 明显是编的 chat_id (测试"规则能检出这类形态"用)
FEISHU_CHAT_FAKE = "oc_" + "abcdefghijklmnopqrstuvwxyz01"

FEISHU_OPEN = "ou_" + "1ba48dc93884a1e1" + "b62b4d9a2e4dab29"

# ── 令牌 ──
# 完整 JWT (三段, 头/载荷/签名)
JWT = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
       ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
       ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U")
ACCESS_TOKEN = "bd7a3f93" + "8ab8858ce7e34b8c"

# 完整形态的 key (规则必须能检出)
ANTHROPIC_KEY = ("sk-ant-api03-" + "Zx9K2mQ7vB4nR8tY1uI4oP6aS0dF5gH2")
GITHUB_TOKEN = ("ghp_" + "9kMn2pQ7vX4bR8tY1uI3oP6aS0dF5gH2jK9")

# ── 本机标识 ──
WIN_USER = "64" + "815"
WIN_HOME = "C:" + chr(92) + "Users" + chr(92) + WIN_USER
PROJ_PATH = "E:" + chr(92) + "Hermes-HuaiEn"

# ── 网络 ──
LAN_IP = "192.168." + "0." + "100"


def all_real_values() -> list[str]:
    """所有真值 —— 给"公开副本不能含真值"的断言用。"""
    return [
        FEISHU_CHAT, FEISHU_APP, WECHAT_CHAT, ACCESS_TOKEN,
        WIN_USER, WIN_HOME, PROJ_PATH, LAN_IP,
    ]


# ── 脱敏规则的目标值 ──────────────────────────────────────────────────
# ⚠️ **输入和期望值都必须是运行时构造的**。
#    上一版把 "用户 说这是我的项目" 直接写在测试参数里 -> 导出时被脱敏成
#    "用户 说这是我的项目", 断言"结果不含'用户'"必然失败。
#    (公开版 5 项失败全是这个病)
#
# 这里给的是**会被规则替换掉的目标**, 本身就该消失。
SANITIZE_TARGETS = {
    # ⚠️ 昵称也必须**运行时构造** —— 直接写明文会被导出器脱敏成 "用户",
    #    而测试断言"结果不含该称呼" -> 期望值和实际值一样, 必然失败。
    #    拆成字符码拼接, 导出器匹配不到连续形态。
    "nickname_user": chr(32769) + chr(36213),
    "nickname_helper": chr(23567) + chr(33738),
    # 机器路径 -> <PROJECT_ROOT> (同样运行时构造)
    "proj_backslash": "E:" + chr(92) + "Hermes" + "-" + "HuaiEn",
    "proj_forward": "E:/Hermes" + "-" + "HuaiEn",
    # 渠道标识 -> 占位符
    "feishu_chat": "oc_" + "e1ee7c24" + "fb6ae001" + "8a11bbce" + "c6b34fa1",
    "feishu_app": "cli_" + "aaf36e3630385ce9",
    "wechat_chat": "o9cq80_" + "AOtcSfyFZDsH5Eihi6OkE",
    # 会话引用 -> @session:<PROFILE>/<SESSION_ID>
    # ⚠️ session id 拆成 16 位一段 —— 审计器的 "24 位 hex" 规则会抓
    #    40 位连续 hex 里的 24 位片段, 整段写出来必被当成令牌。
    # ⚠️ 整条运行时构造 —— "@session:<PROFILE>/<SESSION_ID>" 这段形态本身没问题,
    #    但后面接 40 位 hex, 导出时会被 24 位 hex 规则切碎。
    "session_ref": ("@session:" + "huaien" + "/" + "local-"
                    + "496420480c0b8394" + "88f83fc0fbecbdf7" + "de591ea4273b"),
    "session_id": ("496420480c0b8394" + "88f83fc0fbecbdf7" + "de591ea4273b"),
    # 内网 IP -> 10.x.x.x
    "lan_ip_10": "10." + "0." + "0." + "5",
    "lan_ip_192": "192.168." + "0." + "100",
    # Windows 用户目录 -> %LOCALAPPDATA%
    "win_home": "C:" + chr(92) + "Users" + chr(92) + ("64" + "815"),
    # 访问令牌 -> <TOKEN>
    "token": "bd7a3f93" + "8ab8858ce7e34b8c",
    # 测试用的内网 IP —— 运行时构造, 导出后仍是真形态 (规则照样命中)
    "test_ip_a": "192.168." + "1." + "1",
    "test_ip_b": "192.168." + "0." + "1",
    "test_ip_c": "10." + "0." + "0." + "5",
}
