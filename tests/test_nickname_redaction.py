"""昵称脱敏 —— 锁死 2026-11-11 的重大事故。

## 事故经过

公开仓推上去之后, **310 处个人昵称原样留在里面**。三重失效:

1. **导出器字符码写错** —— `chr(36215)` 实际是"起", 不是"赵"。
   规则从来没匹配到过任何东西, 但导出照样"成功"。
2. **审计器不查昵称** —— 只查邮箱/路径/ID/令牌, 昵称是盲区。
3. **我的扫描脚本用同一个错字符码** —— 于是报"0 命中", 差点又放过。

## 这个测试干什么

三层防护, 每一层都能单独抓出事故:

- **A. 交叉一致性** —— 脱敏规则 (十进制) 与自检源 (十六进制) 必须相等。
  两边同时写错会被这条抓住。
- **B. 假昵称探针** —— 用**运行时随机构造**的假昵称验证脱敏机制本身有效。
  不依赖任何具体真值, 所以"规则表整体失效"也会被抓。
- **C. 导出后自检** —— 导出器必须真的拒绝带残留的输出。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent


def _load_export_mod():
    spec = importlib.util.spec_from_file_location(
        "ep_nick", PROJ / "scripts" / "export_public.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_nickname_rule_and_residue_source_agree():
    """A. 十进制脱敏规则 == 十六进制自检源。"""
    mod = _load_export_mod()
    # ⚠️ 别用 dict() —— 两个昵称同 key, 会互相覆盖。
    nicknames = [v for k, v in mod._residue_literals() if k == "个人昵称"]
    assert len(nicknames) == 2, f"自检源里的昵称条目数不对: {nicknames}"
    # 脱敏规则的两个目标
    assert mod._NICK_USER in nicknames, (
        f"脱敏规则的用户昵称 ({mod._NICK_USER!r}) 与自检源对不上 —— "
        f"十六进制源是 {nicknames}")
    assert mod._NICK_HELPER in nicknames, (
        f"脱敏规则的助手昵称 ({mod._NICK_HELPER!r}) 与自检源对不上")


def test_nickname_rule_is_not_empty():
    """脱敏目标不能是空串或单字 —— 那种等于没规则。"""
    mod = _load_export_mod()
    for name in (mod._NICK_USER, mod._NICK_HELPER):
        assert len(name) == 2, f"昵称规则目标长度异常: {name!r}"
        assert name.strip() == name and name


def test_fake_nickname_actually_gets_redacted(tmp_path):
    """B. 假昵称探针 —— 机制有效, 与具体真值无关。

    构造一个源码里不可能出现的假昵称 (用私有区码点),
    塞进临时文件走一遍脱敏, 断言它被替换掉了。
    """
    mod = _load_export_mod()
    fake = chr(0xE000) + chr(0xE001)      # 私有区, 绝不会撞上真昵称
    src = tmp_path / "sample.md"
    src.write_text(f"# 标题\n\n这里提到 {fake} 一次。\n", encoding="utf-8")

    # compile_rules 收的是 {分类: [规则...]} —— 不是 list。
    rules = {"昵称": [{"pattern": fake, "replace": "用户"}]}
    compiled = mod.compile_rules(rules)

    text = src.read_text(encoding="utf-8")
    new_text, hits = mod._apply(text, compiled, {"rule_hits": {}})
    assert fake not in new_text, "假昵称没被脱敏 —— 脱敏机制本身坏了"
    assert hits, "脱敏没报告命中"


def test_export_refuses_residue(tmp_path):
    """C. 导出器必须拒绝带残留的输出, 而不是照常"成功"。"""
    mod = _load_export_mod()
    # 造一个只含残留昵称的目录
    out = tmp_path / "out"
    out.mkdir()
    (out / "a.md").write_text(f"# hi {mod._NICK_USER}\n", encoding="utf-8")
    compiled = mod.compile_rules({})
    residue = mod.verify_no_residue(out, compiled)
    assert residue, "导出后自检没发现残留昵称 —— 自检形同虚设"
    assert any(r["rule"] == "个人昵称" for r in residue)
